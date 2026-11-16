"""One real, bounded chat API request; no retries or provider fallbacks.

The transport never persists credentials, HTTP headers, or raw response bodies.
Responses API parameters follow the official API reference and structured-output
guide: https://developers.openai.com/api/reference/resources/responses/methods/create
https://developers.openai.com/api/docs/guides/structured-outputs
"""
import http.client
import json
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import resolve_config

MAX_RESPONSE_BYTES = 2_000_000
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
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not send an Authorization header to an unconfigured destination.
        return None


def _send(req, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)


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


def _content(parsed, provider, official):
    if parsed.get('error'):
        return None, 'remote_error'
    if provider == 'ollama':
        if parsed.get('done') is not True:
            return None, 'incomplete_response'
        message = parsed.get('message')
        if not isinstance(message, dict):
            return None, 'invalid_response'
        if message.get('tool_calls'):
            return None, 'unsupported_tool_call'
        return message.get('content'), None
    if official:
        if parsed.get('status') != 'completed':
            return None, 'incomplete_response'
        texts = []
        for item in parsed.get('output', []):
            if not isinstance(item, dict):
                continue
            if item.get('type') != 'message' or item.get('role') != 'assistant':
                continue
            for block in item.get('content', []):
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'refusal':
                    return None, 'refusal'
                if block.get('type') == 'output_text' and isinstance(block.get('text'), str):
                    texts.append(block['text'])
        return '\n'.join(texts), None
    choices = parsed.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, 'invalid_response'
    choice = choices[0]
    message = choice.get('message')
    if not isinstance(message, dict):
        return None, 'invalid_response'
    if message.get('refusal') or choice.get('finish_reason') == 'content_filter':
        return None, 'refusal'
    if message.get('tool_calls') or choice.get('finish_reason') in ('tool_calls', 'function_call'):
        return None, 'unsupported_tool_call'
    if choice.get('finish_reason') == 'length':
        return None, 'incomplete_response'
    content = message.get('content')
    if isinstance(content, list):
        content = '\n'.join(block['text'] for block in content if isinstance(block, dict)
                            and block.get('type') == 'text' and isinstance(block.get('text'), str))
    return content, None


def request(config, system, user, schema=None, max_output_tokens=3200):
    """Make one API request and return safe text plus observed response metadata."""
    started = time.monotonic()
    result = {'status':'failed', 'content':'', 'provider':None, 'model':None,
              'response_id':None, 'request_id':None, 'usage':{}, 'elapsed_ms':0,
              'http_status':None, 'error_code':None, 'message':''}

    def finish(code=None, status='failed'):
        result.update(status=status, error_code=code, message=_MESSAGES.get(code, ''),
                      elapsed_ms=round((time.monotonic() - started) * 1000))
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
    official = c['provider'] == 'openai' and urllib.parse.urlsplit(c['base_url']).hostname == 'api.openai.com'
    messages = [{'role':'system', 'content':system}, {'role':'user', 'content':user}]
    if official:
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
    elif c['provider'] == 'ollama':
        body = {'model':c['model'], 'messages':messages, 'stream':False,
                'options':{'num_predict':max_output_tokens}}
        if schema is not None:
            body['format'] = schema
    else:
        body = {'model':c['model'], 'messages':messages, 'stream':False, 'max_tokens':max_output_tokens}
        if schema is not None:
            body['response_format'] = {'type':'json_schema', 'json_schema':{
                'name':'nhis_agent_response', 'strict':True, 'schema':schema}}
    try:
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        headers = {'Content-Type':'application/json', 'Accept':'application/json', 'User-Agent':'NHIS-Health-Agent/1.0'}
        if c['api_key']:
            headers['Authorization'] = 'Bearer ' + c['api_key']
        req = urllib.request.Request(_endpoint(c['base_url'], c['provider'], official), encoded,
                                     headers=headers, method='POST')
        with _send(req, min(60.0, c['timeout'])) as response:
            result['http_status'] = response.getcode()
            result['request_id'] = _identifier(response.headers.get('x-request-id'), c['api_key'])
            if not isinstance(result['http_status'], int) or not 200 <= result['http_status'] < 300:
                return finish(_http_code(result['http_status']) if isinstance(result['http_status'], int) else 'invalid_response')
            length = response.headers.get('Content-Length', '')
            if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                return finish('response_too_large')
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return finish('response_too_large')
        parsed = json.loads(raw.decode('utf-8'))
        if not isinstance(parsed, dict):
            return finish('invalid_response')
        result.update(model=_identifier(parsed.get('model'), c['api_key']),
                      response_id=_identifier(parsed.get('id'), c['api_key']),
                      usage=_usage(parsed if c['provider'] == 'ollama' else parsed.get('usage')))
        content, error = _content(parsed, c['provider'], official)
        if error:
            return finish(error)
        if not isinstance(content, str) or not content.strip():
            return finish('invalid_response')
        result['content'] = _safe_text(content, c['api_key'])
        return finish(status='ok')
    except urllib.error.HTTPError as error:
        result['http_status'] = error.code
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
