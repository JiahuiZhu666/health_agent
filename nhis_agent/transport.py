"""One real, bounded chat API request; no retries or provider fallbacks.

The transport never persists credentials, HTTP headers, or raw response bodies.
Responses API parameters follow the official API reference and structured-output
guide: https://developers.openai.com/api/reference/resources/responses/methods/create
https://developers.openai.com/api/docs/guides/structured-outputs
"""
import http.client
import hashlib
import json
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .budget import ModelBudget, BudgetExceeded
from .telemetry import Telemetry

MAX_RESPONSE_BYTES = 2_000_000
_CAPACITY = threading.BoundedSemaphore(4)
_SECRET = re.compile(r'\bsk-[A-Za-z0-9_-]{12,}')
_USAGE_FIELDS = {
    'input_tokens', 'output_tokens', 'total_tokens', 'prompt_tokens',
    'completion_tokens', 'prompt_eval_count', 'eval_count',
    'total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration',
}
_USAGE_DETAILS = {
    'input_tokens_details': {'cached_tokens'},
    'output_tokens_details': {'reasoning_tokens'},
    'prompt_tokens_details': {'cached_tokens', 'audio_tokens'},
    'completion_tokens_details': {
        'reasoning_tokens', 'audio_tokens', 'accepted_prediction_tokens',
        'rejected_prediction_tokens',
    },
}
_MESSAGES = {
    'invalid_config': 'The model connection configuration is invalid. Check the server endpoint, model name, and timeout.',
    'not_configured': 'The service is not ready. Check the server configuration.',
    'invalid_request': 'The model service did not accept the request. Check model and structured-output support on the server.',
    'authentication_error': 'The model service could not authenticate. Check the server credentials.',
    'permission_denied': 'The model service denied access. Check server permissions.',
    'model_or_endpoint_not_found': 'The model or endpoint path was not found. Check the server configuration.',
    'rate_limit_or_quota': 'The model service reached a rate or quota limit. Check the server account status.',
    'server_error': 'The model service is temporarily unable to complete the request.',
    'http_error': 'The model service returned an HTTP error.',
    'redirect_refused': 'The model endpoint returned a redirect. Configure the final server endpoint directly.',
    'timeout': 'The model request timed out. It was not automatically retried.',
    'network_error': 'The model service could not be reached. Check the network and server endpoint.',
    'tls_error': 'The model service security certificate could not be verified.',
    'response_too_large': 'The model response exceeded the 2 MB limit.',
    'invalid_response': 'The model service did not return a valid, complete text response.',
    'incomplete_response': 'The model response was incomplete and cannot be used as a complete answer.',
    'refusal': 'The model declined this request.',
    'unsupported_tool_call': 'The model returned a tool call. This application did not execute it.',
    'remote_error': 'The model service reported that generation failed.',
    'unexpected_test_response': 'The endpoint responded but did not return the requested OK. The connection test did not pass.',
    'deadline_exceeded': 'The operation reached its deadline before a complete model response was available.',
    'budget_exceeded': 'The model request was not sent because its reserved budget would exceed a configured limit.',
    'kill_switch': 'New model requests are temporarily disabled by the operator.',
    'cancelled': 'The operation was cancelled before sending a new model request.',
    'unknown_attempt': 'A previous model request has an unknown delivery outcome and will not be sent again.',
    'audit_unavailable': 'The model request was not sent because durable accounting is unavailable.',
    'capacity_busy': 'The model service is busy. No new request was sent before this operation’s deadline.',
}


def resolve_config(config=None):
    """Resolve the common provider contract; never include this object in logs."""
    config = dict(config or {})
    provider = config.get('provider', os.environ.get('NHIS_MODEL_PROVIDER', 'openai'))
    if provider not in ('openai', 'ollama'):
        raise ValueError('The model provider must be openai or ollama.')
    default = 'http://127.0.0.1:11434' if provider == 'ollama' else 'https://api.openai.com/v1'
    base = str(config.get('base_url') or os.environ.get('NHIS_MODEL_BASE_URL') or default).rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('The model endpoint must be an HTTP(S) URL without user credentials or query parameters.')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Remote model endpoints must use HTTPS. HTTP is allowed only for local models.')
    model = str(config.get('model') or os.environ.get('NHIS_MODEL_NAME') or '').strip()
    key = str(config.get('api_key') or os.environ.get('OPENAI_API_KEY') or '') if provider == 'openai' else ''
    if len(model) > 160 or any(ord(c) < 32 for c in model):
        raise ValueError('The model name format is invalid.')
    timeout, seconds = float(config.get('timeout', 25)), float(config.get('max_seconds', 300))
    if not math.isfinite(timeout) or not math.isfinite(seconds):
        raise ValueError('Model time limits must be finite.')
    return {'provider': provider, 'base_url': base, 'model': model, 'api_key': key,
            'timeout': max(1.0, min(timeout, 60.0)), 'max_seconds': max(1.0, min(seconds, 600.0))}


def strict_json(value):
    """Reject duplicate keys and non-JSON numbers rather than silently correcting."""
    def pairs(items):
        result = {}
        for name, item in items:
            if name in result:
                raise ValueError('Duplicate JSON member')
            result[name] = item
        return result
    def constant(_):
        raise ValueError('Non-finite JSON number')
    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not send an Authorization header to an unconfigured destination.
        return None


def _send(req, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)


def _context():
    try:
        from .execution_context import current
    except ImportError:
        return None
    return current()


def _state_dir(context):
    value = getattr(context, 'state_root', None) if context else None
    if not value and context:
        value = getattr(getattr(context, 'store', None), 'state_root', None)
    return Path(value or os.environ.get('NHIS_STATE_DIR') or Path(__file__).resolve().parents[1])


def _budget_for(context):
    return getattr(context, 'budget', None) or ModelBudget(_state_dir(context))


def _telemetry_for(context):
    return Telemetry(_state_dir(context))


def _read_response(response, deadline):
    """Bound size and wall time even when an upstream trickles its body."""
    read = getattr(response, 'read1', None)
    if read is None:
        # Test adapters and some compatibility clients expose only bounded read.
        return response.read(MAX_RESPONSE_BYTES + 1)
    chunks, size = [], 0
    while size <= MAX_RESPONSE_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        raw_socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if raw_socket is not None:
            raw_socket.settimeout(remaining)
        part = read(min(65536, MAX_RESPONSE_BYTES + 1 - size))
        if not part:
            break
        chunks.append(part)
        size += len(part)
    return b''.join(chunks)


def _safe_text(value, key='', limit=None):
    if not isinstance(value, str):
        return None
    if key:
        value = value.replace(key, '[REDACTED]')
    value = _SECRET.sub('[REDACTED]', value)
    return value[:limit] if limit is not None else value


def _identifier(value, key=''):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        return None
    return _safe_text(value, key)


def _usage(source):
    if not isinstance(source, dict):
        return {}
    def numeric(value):
        try:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
        except OverflowError:
            return False
    result = {name: source[name] for name in _USAGE_FIELDS if numeric(source.get(name))}
    for name, fields in _USAGE_DETAILS.items():
        detail = source.get(name)
        if isinstance(detail, dict):
            values = {field: detail[field] for field in fields if numeric(detail.get(field))}
            if values:
                result[name] = values
    return result


def _endpoint(base, provider, official):
    suffix = '/api/chat' if provider == 'ollama' else '/responses' if official else '/chat/completions'
    for old in ('/chat/completions', '/responses', '/api/chat'):
        if base.endswith(old):
            return base[:-len(old)] + suffix
    if provider == 'ollama' and base.endswith('/api'):
        return base + '/chat'
    return base + suffix


def _http_code(status):
    if 300 <= status < 400:
        return 'redirect_refused'
    return {400:'invalid_request', 401:'authentication_error', 403:'permission_denied',
            404:'model_or_endpoint_not_found', 408:'timeout', 422:'invalid_request',
            429:'rate_limit_or_quota'}.get(status, 'server_error' if status >= 500 else 'http_error')


def _content(parsed, provider, responses):
    if parsed.get('error') is not None:
        return None, 'remote_error'
    if provider == 'ollama':
        if parsed.get('done') is not True:
            return None, 'incomplete_response'
        message = parsed.get('message')
        if not isinstance(message, dict):
            return None, 'invalid_response'
        if message.get('tool_calls'):
            return None, 'unsupported_tool_call'
        if parsed.get('done_reason') not in (None, 'stop'):
            return None, 'incomplete_response'
        if message.get('role') not in (None, 'assistant'):
            return None, 'invalid_response'
        return message.get('content'), None
    if responses:
        if parsed.get('status') != 'completed':
            return None, 'incomplete_response'
        if not isinstance(parsed.get('output'), list):
            return None, 'invalid_response'
        texts = []
        for item in parsed.get('output', []):
            if not isinstance(item, dict):
                return None, 'invalid_response'
            if item.get('type') not in ('message', 'reasoning'):
                return None, 'unsupported_tool_call'
            if item.get('type') != 'message' or item.get('role') != 'assistant':
                continue
            if item.get('status') not in (None, 'completed'):
                return None, 'incomplete_response'
            if not isinstance(item.get('content'), list):
                return None, 'invalid_response'
            for block in item.get('content', []):
                if not isinstance(block, dict):
                    return None, 'invalid_response'
                if block.get('type') == 'refusal':
                    return None, 'refusal'
                if block.get('type') == 'output_text' and isinstance(block.get('text'), str):
                    texts.append(block['text'])
                else:
                    return None, 'invalid_response'
        return '\n'.join(texts), None
    choices = parsed.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None, 'invalid_response'
    choice = choices[0]
    message = choice.get('message')
    if not isinstance(message, dict):
        return None, 'invalid_response'
    if message.get('refusal') or choice.get('finish_reason') == 'content_filter':
        return None, 'refusal'
    if message.get('tool_calls') or message.get('function_call') or choice.get('finish_reason') in ('tool_calls', 'function_call'):
        return None, 'unsupported_tool_call'
    if choice.get('finish_reason') == 'length':
        return None, 'incomplete_response'
    if choice.get('finish_reason') != 'stop':
        return None, 'invalid_response'
    if message.get('role') not in (None, 'assistant'):
        return None, 'invalid_response'
    content = message.get('content')
    if isinstance(content, list):
        if any(not isinstance(block, dict) or block.get('type') != 'text' or not isinstance(block.get('text'), str) for block in content):
            return None, 'invalid_response'
        content = '\n'.join(block['text'] for block in content)
    return content, None


def request(config, system, user, schema=None, max_output_tokens=3200, *,
            purpose='dialogue', response_mode='text', temperature=None,
            deadline=None, attempt_key=None, send=None):
    """Make one API request and return safe text plus observed response metadata."""
    started = time.monotonic()
    result = {'status':'failed', 'content':'', 'provider':None, 'model':None,
              'response_id':None, 'request_id':None, 'usage':{}, 'elapsed_ms':0,
              'http_status':None, 'error_code':None, 'message':'', 'delivery_state':'not_sent'}
    context = _context()
    context_deadline=getattr(context,'deadline',None)
    if context_deadline is not None:
        deadline=min(deadline,context_deadline) if deadline is not None else context_deadline
    durable_context = context if getattr(context, 'store', None) is not None else None
    budget = telemetry = reservation = external_id = None
    trace = {name: getattr(context, name, None) for name in ('trace_id', 'workspace_id', 'turn_id', 'job_id', 'run_id')}
    finalized = False
    capacity_acquired = False

    def finish(code=None, status='failed'):
        nonlocal finalized, capacity_acquired
        if finalized:
            return result
        result.update(status=status, error_code=code, message=_MESSAGES.get(code, ''),
                      elapsed_ms=round((time.monotonic() - started) * 1000))
        finalized = True
        if reservation is not None:
            result['reservation_id'] = reservation['id']
            try:
                budget.reconcile(reservation['id'], result['usage'],
                                 unknown=result['delivery_state'] == 'unknown',
                                 not_sent=result['delivery_state'] == 'not_sent')
            except Exception:
                result['accounting_status'] = 'held'
        if external_id is not None:
            result['attempt_id'] = external_id
            try:
                if result['delivery_state'] == 'unknown':
                    durable_context.external_unknown(external_id, error_code=code or 'unknown_attempt')
                else:
                    durable_context.external_finish(external_id, dict(result))
            except Exception:
                # Never resend if persistence fails after a request might run.
                result.update(status='failed', content='', error_code='audit_unavailable',
                              message=_MESSAGES['audit_unavailable'], delivery_state='unknown')
        if telemetry is not None:
            try:
                telemetry.emit('model.finished', {**trace, **result, 'purpose': purpose})
            except Exception:
                result['telemetry_status'] = 'unavailable'
        if capacity_acquired:
            _CAPACITY.release()
            capacity_acquired = False
        if durable_context is not None and result['delivery_state'] == 'unknown':
            from .execution_context import ExternalOutcomeUnknown
            raise ExternalOutcomeUnknown('A model request may have executed, but no durably saved complete result is available. It was not repeated.')
        return result

    try:
        effective_config = dict(config or {})
        effective_config.setdefault('timeout', 60)
        c = resolve_config(effective_config)
        result['provider'] = c['provider']
        if not c['model'] or (c['provider'] == 'openai' and not c['api_key']):
            return finish('not_configured', 'not_configured')
        if not math.isfinite(c['timeout']):
            return finish('invalid_config')
    except (ValueError, TypeError, OverflowError):
        return finish('invalid_config')
    if (not isinstance(system, str) or not isinstance(user, str) or not user.strip()
            or not isinstance(max_output_tokens, int) or isinstance(max_output_tokens, bool)
            or not 1 <= max_output_tokens <= 32768 or (schema is not None and not isinstance(schema, dict))):
        return finish('invalid_request')
    if purpose not in ('dialogue', 'blind', 'interpreter', 'connection') or response_mode not in ('text', 'json_object'):
        return finish('invalid_request')
    if deadline is not None and (not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
        return finish('invalid_request')
    if deadline is not None and time.monotonic() >= deadline:
        return finish('deadline_exceeded')
    official = c['provider'] == 'openai' and urllib.parse.urlsplit(c['base_url']).hostname == 'api.openai.com'
    responses = official and purpose in ('dialogue', 'connection')
    messages = [{'role':'system', 'content':system}, {'role':'user', 'content':user}]
    if responses:
        body = {'model':c['model'], 'instructions':system, 'input':user,
                'store':False, 'stream':False, 'max_output_tokens':max_output_tokens}
        if c['model'] in ('gpt-5.6-sol', 'gpt-5.6'):
            # These documented aliases default to medium; preserve the text
            # budget for evidence editing and use none for a minimal probe.
            effort = effective_config.get('reasoning_effort', 'low')
            if effort not in ('none', 'low', 'medium', 'high', 'xhigh', 'max'):
                return finish('invalid_config')
            body['reasoning'] = {'effort':effort}
        if schema is not None:
            body['text'] = {'format':{'type':'json_schema', 'name':'nhis_agent_response', 'strict':True, 'schema':schema}}
        elif response_mode == 'json_object':
            body['text'] = {'format': {'type': 'json_object'}}
    elif c['provider'] == 'ollama':
        body = {'model':c['model'], 'messages':messages, 'stream':False,
                'options':{'num_predict':max_output_tokens}}
        if schema is not None:
            body['format'] = schema
        elif response_mode == 'json_object':
            body['format'] = 'json'
        if temperature is not None:
            body['options']['temperature'] = temperature
    else:
        token_field = 'max_completion_tokens' if official else 'max_tokens'
        body = {'model':c['model'], 'messages':messages, 'stream':False, token_field:max_output_tokens}
        if temperature is not None:
            body['temperature'] = temperature
        if official and c['model'] in ('gpt-5.6-sol', 'gpt-5.6'):
            body['reasoning_effort'] = 'none'
        if schema is not None:
            body['response_format'] = {'type':'json_schema', 'json_schema':{
                'name':'nhis_agent_response', 'strict':True, 'schema':schema}}
        elif response_mode == 'json_object':
            body['response_format'] = {'type': 'json_object'}
    try:
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        headers = {'Content-Type':'application/json', 'Accept':'application/json', 'User-Agent':'NHIS-Health-Agent/1.0'}
        if c['api_key']:
            headers['Authorization'] = 'Bearer ' + c['api_key']
        req = urllib.request.Request(_endpoint(c['base_url'], c['provider'], responses), encoded,
                                     headers=headers, method='POST')
        remaining = min(60.0, c['timeout'], deadline-time.monotonic() if deadline is not None else 60.0)
        if remaining <= 0:
            return finish('deadline_exceeded')
        digest = hashlib.sha256(encoded).hexdigest()
        key = attempt_key or purpose + ':' + digest
        meta = {'purpose': purpose, 'provider': c['provider'], 'model': _identifier(c['model'], c['api_key']),
                'request_hash': digest, 'input_tokens': len(encoded)+256, 'output_tokens': max_output_tokens}
        if context is not None:
            try:
                context.check_cancelled()
                cached = durable_context.external_cached(key, request_meta=meta) if durable_context else None
                if cached is not None:
                    return dict(cached, replayed=True)
            except Exception as error:
                name = type(error).__name__
                return finish('unknown_attempt' if name == 'UnknownExternalAttempt' else 'cancelled' if 'Cancel' in name else 'audit_unavailable')
        gate_deadline = min(started + min(c['timeout'], 60.0), deadline) if deadline is not None else started + min(c['timeout'], 60.0)
        while not capacity_acquired:
            if context is not None:
                context.check_cancelled()
            wait_seconds = gate_deadline - time.monotonic()
            if wait_seconds <= 0:
                return finish('capacity_busy')
            capacity_acquired = _CAPACITY.acquire(timeout=min(0.1, wait_seconds))
        try:
            budget, telemetry = _budget_for(context), _telemetry_for(context)
            reservation = budget.reserve(
                turn_id=str(trace.get('turn_id') or effective_config.get('_budget_turn_id') or uuid.uuid4().hex),
                workspace_id=str(trace.get('workspace_id') or 'local'), input_tokens=len(encoded)+256,
                output_tokens=max_output_tokens, paid=c['provider'] != 'ollama')
            telemetry.emit('model.reserved', {**trace, 'purpose': purpose, 'provider': c['provider'],
                           'reservation_id': reservation['id'], 'reserved_input_tokens': len(encoded)+256,
                           'reserved_output_tokens': max_output_tokens, 'reserved_cost_micros': reservation['cost_micros']})
            if context is not None:
                context.check_cancelled()
                if durable_context is not None:
                    external_id = durable_context.external_start('model', key, dict(meta, reservation_id=reservation['id']))
        except BudgetExceeded as error:
            return finish('kill_switch' if error.code == 'kill_switch' else 'budget_exceeded')
        except Exception as error:
            if type(error).__name__ == 'ReplayAvailable':
                finish('unknown_attempt')  # releases this unused admission
                return dict(error.result, replayed=True)
            return finish('cancelled' if 'Cancel' in type(error).__name__ else 'audit_unavailable')
        # Cancellation/deadlines are checked again after durable admission.
        io_deadline = min(started + min(60.0, c['timeout']), deadline) if deadline is not None else started + min(60.0, c['timeout'])
        remaining = io_deadline - time.monotonic()
        if remaining <= 0:
            return finish('deadline_exceeded')
        result['delivery_state'] = 'unknown'
        with (send or _send)(req, remaining) as response:
            result['http_status'] = response.getcode()
            result['request_id'] = _identifier(response.headers.get('x-request-id'), c['api_key'])
            if not isinstance(result['http_status'], int) or not 200 <= result['http_status'] < 300:
                result['delivery_state'] = 'received'
                return finish(_http_code(result['http_status']) if isinstance(result['http_status'], int) else 'invalid_response')
            length = response.headers.get('Content-Length', '')
            if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                result['delivery_state'] = 'received'
                return finish('response_too_large')
            raw = _read_response(response, io_deadline)
        result['delivery_state'] = 'received'
        if len(raw) > MAX_RESPONSE_BYTES:
            return finish('response_too_large')
        if time.monotonic() >= io_deadline:
            return finish('deadline_exceeded')
        parsed = strict_json(raw.decode('utf-8'))
        if not isinstance(parsed, dict):
            return finish('invalid_response')
        result.update(model=_identifier(parsed.get('model'), c['api_key']),
                      response_id=_identifier(parsed.get('id'), c['api_key']),
                      usage=_usage(parsed if c['provider'] == 'ollama' else parsed.get('usage')))
        content, error = _content(parsed, c['provider'], responses)
        if error:
            return finish(error)
        if not isinstance(content, str) or not content.strip():
            return finish('invalid_response')
        result['content'] = _safe_text(content, c['api_key'])
        return finish(status='ok')
    except urllib.error.HTTPError as error:
        result['http_status'] = error.code
        result['delivery_state'] = 'received'
        result['request_id'] = _identifier(error.headers.get('x-request-id') if error.headers else None, c['api_key'])
        error.close()
        return finish(_http_code(error.code))
    except (TimeoutError, socket.timeout):
        return finish('timeout')
    except ssl.SSLError:
        return finish('tls_error')
    except urllib.error.URLError as error:
        code = 'timeout' if isinstance(error.reason, (TimeoutError, socket.timeout)) else 'tls_error' if isinstance(error.reason, ssl.SSLError) else 'network_error'
        return finish(code)
    except OSError:
        return finish('network_error')
    except http.client.HTTPException:
        return finish('invalid_response')
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError, RecursionError):
        return finish('invalid_response')
    except BaseException as error:
        # Preserve cancellation/worker-control propagation while reconciling a
        # reservation and marking a possibly sent external attempt unknown.
        finish('unknown_attempt' if type(error).__name__ in ('UnknownExternalAttempt', 'ExternalOutcomeUnknown') else 'cancelled')
        raise


def test_connection(config):
    """Generate one minimal real reply; configuration alone never means success."""
    test_config = dict(config or {}) if isinstance(config, dict) or config is None else config
    if isinstance(test_config, dict):
        test_config['reasoning_effort'] = 'none'
    result = request(test_config, 'This is a connection test. Reply exactly OK, with no additional text.',
                     'Reply only OK.', max_output_tokens=128)
    if result['status'] == 'ok' and result['content'].strip() != 'OK':
        result.update(status='failed', content='', error_code='unexpected_test_response',
                      message=_MESSAGES['unexpected_test_response'])
    return result
