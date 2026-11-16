"""Conversation-facing facade over the shared, once-only model transport."""
from . import transport

MAX_RESPONSE_BYTES = transport.MAX_RESPONSE_BYTES
_NoRedirect = transport._NoRedirect


def _send(req, timeout):
    # Keep the established facade seam while all clients share one transport.
    return transport._send(req, timeout)


def request(config, system, user, schema=None, max_output_tokens=3200):
    """One bounded real request; no provider fallback or automatic retries."""
    return transport.request(config, system, user, schema, max_output_tokens,
                             purpose='dialogue', send=_send)


def test_connection(config):
    """An actual minimal generation is required; configuration is not success."""
    effective = dict(config or {}) if isinstance(config, dict) or config is None else config
    if isinstance(effective, dict):
        effective['reasoning_effort'] = 'none'
    result = transport.request(effective,
        'This is a connection test. Reply exactly OK, with no additional text.',
        'Reply only OK.', max_output_tokens=128, purpose='connection', send=_send)
    if result['status'] == 'ok' and result['content'].strip() != 'OK':
        result.update(status='failed', content='', error_code='unexpected_test_response',
                      message=transport._MESSAGES['unexpected_test_response'])
    return result
