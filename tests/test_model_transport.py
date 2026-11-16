"""Isolated synthetic provider responses; these tests do not call a live API."""
import json
import concurrent.futures
import socket
import time
import tempfile
import threading
import unittest
from email.message import Message
from unittest.mock import patch

from nhis_agent import models, transport
from nhis_agent.budget import ModelBudget
from nhis_agent.telemetry import Telemetry
from nhis_agent.execution_context import ExecutionContext, bind, ExternalOutcomeUnknown
from nhis_agent.job_store import JobStore, fingerprint


CONFIG = {'provider': 'openai', 'base_url': 'https://compatible.invalid/v1',
          'model': 'isolated-model', 'api_key': 'isolated-credential'}


class Response:
    def __init__(self, value):
        self.body = json.dumps(value).encode()
        self.headers = Message()
    def getcode(self): return 200
    def read(self, limit): return self.body[:limit]
    def __enter__(self): return self
    def __exit__(self, *args): return False


class ModelBoundaryRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        self.budget, self.telemetry = ModelBudget(temporary.name), Telemetry(temporary.name)
        for target, value in [('_budget_for', self.budget), ('_telemetry_for', self.telemetry)]:
            patcher = patch.object(transport, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
    def test_llm_01_truncated_valid_json_is_not_a_valid_measurement(self):
        payload = {'choices': [{'finish_reason': 'length',
                              'message': {'content': '{"percent":42}'}}]}
        with patch.object(transport, '_send', return_value=Response(payload)) as send:
            result = models._call('registered question', models.resolve_config(CONFIG), time.monotonic()+10)
        self.assertEqual(result['status'], 'incomplete_response')
        self.assertNotIn('estimate', result)
        send.assert_called_once()

    def test_llm_02_non_object_provider_response_is_retained_per_call(self):
        with patch.object(transport, '_send', return_value=Response([])) as send:
            result = models.measure({'filters': {}}, [{'group_id': 'overall'}], CONFIG, 2, 1)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['calls']), 2)
        self.assertTrue(all(call['status'] == 'invalid_response' for call in result['calls']))
        self.assertEqual(result['valid_calls'], 0)
        self.assertEqual(send.call_count, 2)

    def context(self):
        store = JobStore(self.directory)
        row, _ = store.create('workspace', 'a'*32, 'message', fingerprint({'text': 'question'}), {'text': 'question'})
        self.assertTrue(store.claim('workspace', row['job_id'], 'owner'))
        return ExecutionContext(store=store, workspace_id='workspace', turn_id=row['turn_id'],
                                job_id=row['job_id'], trace_id=row['trace_id'], owner='owner',
                                state_root=self.directory)

    def test_completed_attempt_reuses_durable_result_without_second_charge_or_send(self):
        context = self.context()
        payload = {'id': 'observed_response', 'model': 'observed_model',
                   'choices': [{'finish_reason': 'stop', 'message': {'content': 'answer'}}],
                   'usage': {'prompt_tokens': 12, 'completion_tokens': 7}}
        with bind(context), patch.object(transport, '_send', return_value=Response(payload)) as send:
            first = transport.request(CONFIG, 'system', 'question')
            second = transport.request(CONFIG, 'system', 'question')
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(second['content'], 'answer')
        self.assertTrue(second['replayed'])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.budget.status()['day']['calls'], 1)
        attempt = context.store.external('workspace', context.job_id, 'dialogue:'+fingerprint({}))
        self.assertIsNone(attempt)  # no arbitrary semantic key was inserted
        saved = context.store.get('workspace', context.job_id)['external_attempts']
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['state'], 'completed')

    def test_unknown_delivery_is_durable_and_cannot_fall_back_or_resend(self):
        context = self.context()
        with bind(context), patch.object(transport, '_send', side_effect=socket.timeout('PRIVATE_DETAIL')) as send:
            with self.assertRaises(ExternalOutcomeUnknown):
                transport.request(CONFIG, 'system', 'question')
            with self.assertRaises(ExternalOutcomeUnknown):
                transport.request(CONFIG, 'system', 'question')
        self.assertEqual(send.call_count, 1)
        saved = context.store.get('workspace', context.job_id)['external_attempts']
        self.assertEqual(saved[0]['state'], 'unknown')
        self.assertEqual(self.budget.status()['held_reservations'], 1)
        self.assertEqual(self.budget.status()['day']['calls'], 1)
        self.assertNotIn('PRIVATE_DETAIL', str(self.telemetry.recent()))

    def test_blind_repeat_workers_receive_context_and_are_distinct_attempts(self):
        context = self.context()
        payload = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"percent":20}'}}],
                   'usage': {'prompt_tokens': 12, 'completion_tokens': 7}}
        with bind(context), patch.object(transport, '_send', return_value=Response(payload)) as send:
            result = models.measure({'filters': {}}, [{'group_id': 'overall'}], CONFIG, 2, 1)
        self.assertEqual(result['valid_calls'], 2)
        self.assertEqual(send.call_count, 2)
        attempts = context.store.get('workspace', context.job_id)['external_attempts']
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len({row['attempt_key'] for row in attempts}), 2)
        self.assertTrue(all(row['state'] == 'completed' for row in attempts))
        self.assertEqual(self.budget.status(context.turn_id)['turn']['calls'], 2)

    def test_all_consumers_reject_non_objects_incomplete_tools_and_missing_completion(self):
        cases = [[], None, 'text', {'choices': []},
                 {'choices': [{'finish_reason': 'length', 'message': {'content': '{"percent":42}'}}]},
                 {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [{}]}}]},
                 {'choices': [{'message': {'content': '{"percent":42}'}}]}]
        for payload in cases:
            with self.subTest(payload=payload), patch.object(transport, '_send', return_value=Response(payload)):
                answer = transport.request(CONFIG, 'system', 'question')
                blind = models._call('question', models.resolve_config(CONFIG), time.monotonic()+10)
                interpreted = models.interpret_question('Compare education groups', CONFIG)
            self.assertEqual(answer['status'], 'failed')
            self.assertNotEqual(blind['status'], 'ok')
            self.assertEqual(interpreted['status'], 'clarify')

    def test_strict_json_rejects_duplicate_and_nonfinite_structured_fields(self):
        for text in ('{"percent":1,"percent":2}', '{"percent":Infinity}', '{"percent":NaN}',
                     '{"refusal":"no","percent":42}', '{"refusal":false}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                models.parse_percent(text)

    def test_expired_deadline_and_kill_switch_do_not_send(self):
        with patch.object(transport, '_send') as send:
            expired = transport.request(CONFIG, 'system', 'question', deadline=time.monotonic()-1)
            self.budget.set_enabled(False)
            killed = transport.request(CONFIG, 'system', 'question')
        self.assertEqual(expired['error_code'], 'deadline_exceeded')
        self.assertEqual(killed['error_code'], 'kill_switch')
        self.assertEqual(killed['delivery_state'], 'not_sent')
        self.assertEqual(self.budget.status()['day']['calls'], 0)
        send.assert_not_called()

    def test_actual_usage_reconciles_even_when_completion_is_rejected(self):
        payload = {'choices': [{'finish_reason': 'length', 'message': {'content': '{"percent":42}'}}],
                   'usage': {'prompt_tokens': 12, 'completion_tokens': 7}}
        with patch.object(transport, '_send', return_value=Response(payload)):
            result = models._call('question', models.resolve_config(CONFIG), time.monotonic()+10)
        self.assertEqual(result['status'], 'incomplete_response')
        self.assertEqual(self.budget.status()['held_reservations'], 0)
        settled = self.budget.reservations('reconciled')
        self.assertEqual(settled[0]['input_tokens'], 12)
        self.assertEqual(settled[0]['output_tokens'], 7)

    def test_shared_process_capacity_bounds_all_clients_and_releases_slots(self):
        lock, release = threading.Lock(), threading.Event()
        active = maximum = 0
        payload = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'answer'}}],
                   'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
        def send(request, timeout):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            release.wait(1)
            with lock:
                active -= 1
            return Response(payload)
        with patch.object(transport, '_send', side_effect=send), concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            jobs = [pool.submit(transport.request, CONFIG, 'system', 'question '+str(i)) for i in range(12)]
            deadline = time.monotonic()+2
            while time.monotonic() < deadline:
                with lock:
                    if active == 4: break
                time.sleep(.005)
            with lock: self.assertEqual(active, 4)
            release.set()
            results = [job.result() for job in jobs]
        self.assertEqual(maximum, 4)
        self.assertTrue(all(result['status'] == 'ok' for result in results))

    def test_capacity_timeout_records_no_external_attempt_or_budget_charge(self):
        context = self.context()
        for _ in range(4): self.assertTrue(transport._CAPACITY.acquire(blocking=False))
        try:
            with bind(context), patch.object(transport, '_send') as send:
                result = transport.request(CONFIG, 'system', 'question', deadline=time.monotonic()+.02)
        finally:
            for _ in range(4): transport._CAPACITY.release()
        self.assertEqual(result['error_code'], 'capacity_busy')
        self.assertEqual(result['delivery_state'], 'not_sent')
        self.assertEqual(self.budget.status()['day']['calls'], 0)
        self.assertEqual(context.store.get('workspace', context.job_id)['external_attempts'], [])
        send.assert_not_called()
