"""Metadata safety and bounded durable retention; no live network or credentials."""
import tempfile
import os
import unittest

from nhis_agent.telemetry import Telemetry, safe_metadata


class TelemetryTests(unittest.TestCase):
    def test_allowlist_omits_raw_input_bodies_headers_and_exception_text(self):
        actual = safe_metadata({'trace_id': 'trace', 'workspace_id': '/private/workspace',
                                'request_id': 'req_observed', 'response_id': 'resp_observed',
                                'prompt': 'PRIVATE_PROMPT', 'content': 'PRIVATE_BODY',
                                'api_key': 'PRIVATE_CREDENTIAL', 'headers': {'Authorization': 'PRIVATE'},
                                'exception': 'PRIVATE_EXCEPTION', 'error_code': 'invalid_response',
                                'usage': {'input_tokens': 10, 'output_tokens': 2, 'private': 123}})
        self.assertEqual(actual['input_tokens'], 10)
        self.assertEqual(actual['request_id'], 'req_observed')
        self.assertEqual(actual['error_code'], 'invalid_response')
        self.assertEqual(len(actual['workspace_hash']), 24)
        self.assertNotIn('PRIVATE', str(actual))
        self.assertNotIn('/private/workspace', str(actual))

    def test_secret_shapes_controls_and_invalid_numbers_are_sanitized(self):
        actual = safe_metadata({'model': 'sk-isolatedNotRealCredential',
                                'request_id': 'bad\nline', 'output_tokens': True,
                                'total_tokens': float('inf'), 'input_tokens': -1})
        self.assertEqual(actual, {'model': '[REDACTED]'})

    def test_events_survive_restart_and_have_bounded_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Telemetry(directory, max_events=10)
            for index in range(15):
                log.emit('model.finished', {'elapsed_ms': index, 'prompt': 'PRIVATE'})
            restored = Telemetry(directory, max_events=10).recent()
            self.assertEqual(len(restored), 10)
            self.assertEqual(restored[0]['elapsed_ms'], 14)
            self.assertEqual(restored[-1]['elapsed_ms'], 5)
            self.assertNotIn('PRIVATE', str(restored))

    def test_error_records_only_stack_locations_not_message_source_or_locals(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Telemetry(directory)
            try:
                private_local = 'PRIVATE_LOCAL_VALUE'
                raise ValueError('PRIVATE_EXCEPTION_MESSAGE ' + private_local)
            except ValueError as error:
                log.emit_error('worker.failed', error, {'trace_id': 'trace_observed', 'stage': 'validation'})
            saved = log.recent()[0]
            self.assertEqual(saved['exception_type'], 'ValueError')
            self.assertEqual(saved['trace_id'], 'trace_observed')
            self.assertTrue(saved['stack'])
            self.assertNotIn('PRIVATE_', str(saved))
            self.assertEqual(saved['stack'][-1]['file'], 'test_telemetry.py')
            self.assertEqual(os.stat(log.path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(log.path.parent).st_mode & 0o777, 0o700)
