"""Real orchestration recovery against temporary state, without R or live APIs.

The aggregate point/n fixture comes from the independently checked synthetic
survey fixture. Its structural interval is only an orchestration test input;
numerical survey validation belongs to the separate R fixture gate.
"""
import copy
from email.message import Message
import json
import math
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from nhis_agent import engine, transport
from nhis_agent.budget import ModelBudget
from nhis_agent.conversation import ConversationService
from nhis_agent.execution_context import ExecutionContext, ExecutionDeadline, bind
from nhis_agent.job_store import JobStore, fingerprint
from nhis_agent.jobs import ChatJobs
from nhis_agent.storage_policy import StorageLimit
from nhis_agent.telemetry import Telemetry


ROOT = Path(__file__).resolve().parents[1]
QUESTION = 'Compare fair or poor self-rated health overall'
SPEC = {'year': 2024, 'outcome': 'fair_poor', 'analysis': 'proportion',
        'group_by': 'overall', 'filters': {}, 'min_n': 100}
RUNTIME = {'ready': True, 'r_version': 'fixture-R', 'survey_version': 'fixture-survey',
           'jsonlite_version': 'fixture-jsonlite', 'mitools_version': 'fixture-mitools'}
SOURCES = ('config/registry.json', 'scripts/analyze.R', 'nhis_agent/stats.py',
           'nhis_agent/spec.py', 'nhis_agent/intent.py', 'nhis_agent/outcome_intent.py', 'nhis_agent/engine.py',
           'nhis_agent/r_worker.py', 'nhis_agent/models.py', 'nhis_agent/transport.py')


class SimulatedCrash(BaseException):
    """Bypass ordinary local error fallbacks at a deliberate crash boundary."""


def survey_fixture():
    meta = json.loads((ROOT/'tests/fixtures/expected_metadata.json').read_text())
    item = meta['outcomes']['fair_poor']
    p = item['weighted_proportion']
    return {'status': 'ok', 'rows': [
        {'group_id': 'overall', 'label': 'Overall', 'n': item['valid_outcome_n'],
         'estimate': p, 'se': .01, 'ci_low': p-.02, 'ci_high': p+.02,
         'status': 'ok', 'design_df': meta['full_design_df']}],
        'denominators': {'full_sample_n': meta['full_sample_n'],
                         'age_domain_n': meta['age_domain_n'],
                         'filtered_domain_n': meta['age_domain_n'],
                         'valid_outcome_n': item['valid_outcome_n']},
        'design': {'constructed_before_domain_subset': True},
        'imputation': {'required': False, 'm': 1}}


class CheckpointRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='nhis-checkpoint-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Freeze a real source snapshot so unrelated concurrent development
        # cannot alter the input identity halfway through a crash test.
        code = self.root/'code'
        for name in SOURCES:
            target = code/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT/name, target)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self._patch('nhis_agent.engine.ROOT', code)
        self.agent = engine.Agent(self.root/'workspace')
        self.service = ConversationService(self.agent)
        self.cid = self.service.create()['id']
        self.store = JobStore(self.root/'global')
        self.jobs = ChatJobs(self.service, store=self.store)
        self.addCleanup(self.jobs.shutdown, 2)
        self.workspace = self.jobs.workspace_id
        self.agent.decide_reply = Mock(return_value={
            'status': 'ok', 'mode': 'chat', 'reply': 'An isolated conversational fixture.'})
        self.agent.compose_reply = Mock(side_effect=lambda question, draft:
                                       {'status': 'ok', 'content': draft})
        self.health = self._patch_object(self.agent._statistics_health, 'snapshot',
                                         return_value=copy.deepcopy(RUNTIME))
        self.dataset = {'ready': True, 'sha256': 'a'*64, 'row_count': 8000,
                        'income_ready': True, 'datasets': {'income': {'sha256': 'b'*64}}}
        self.data_status = self._patch('nhis_agent.engine.data.status',
                                       return_value=self.dataset)
        self.statistics = self._patch('nhis_agent.engine.stats.analyze',
                                      side_effect=lambda *args: survey_fixture())
        self.readiness = self._patch('nhis_agent.engine.stats.readiness',
                                     side_effect=AssertionError('No real R probe is permitted.'))
        self._patch('nhis_agent.engine.SurveyCache.identity', return_value=None)
        self.network = self._patch('nhis_agent.transport._send',
                                  side_effect=AssertionError('No live API call is permitted.'))

    def _patch(self, name, *args, **kwargs):
        value = patch(name, *args, **kwargs)
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def _patch_object(self, obj, name, **kwargs):
        value = patch.object(obj, name, **kwargs)
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def tearDown(self):
        self.network.assert_not_called()
        self.readiness.assert_not_called()

    def seed(self, text='Tell me a short story', *, measure=False):
        request = ChatJobs._request(text, measure, 1, 1)
        row, created = self.store.create(self.workspace, self.cid, 'checkpoint-message-0001',
                                         fingerprint(request), request)
        self.assertTrue(created)
        self.assertTrue(self.store.claim(self.workspace, row['job_id'], 'crashed-worker'))
        context = ExecutionContext(store=self.store, workspace_id=self.workspace,
            job_id=row['job_id'], turn_id=row['turn_id'], trace_id=row['trace_id'],
            owner='crashed-worker', state_root=self.store.state_root,
            client_message_id='checkpoint-message-0001')
        return row, context, request

    def send(self, context, request):
        with bind(context):
            return self.service.send(self.cid, request['message'],
                measure_model=request['measure_model'], repeats=1, prompt_versions=1)

    def recover(self, row):
        # Fault injection only affects this test's private SQLite file.
        with self.store.transaction() as connection:
            connection.execute('UPDATE jobs SET owner_pid=0,lease_expires_at=0 WHERE job_id=?',
                               (row['job_id'],))
        replacement = ChatJobs(self.service, store=JobStore(self.store.state_root))
        self.addCleanup(replacement.shutdown, 2)
        pending = replacement.get(row['job_id'])
        self.assertTrue(pending['recovery_required'])
        self.assertTrue(pending['can_resume'])
        return replacement

    def finish(self, jobs, identifier):
        deadline = time.monotonic()+4
        while time.monotonic() < deadline:
            value = jobs.get(identifier)
            if value['execution_state'] not in ('queued', 'running'):
                return value
            threading.Event().wait(.005)
        self.fail('The isolated recovery did not finish promptly.')

    def test_saved_user_is_reused_after_crash_before_assistant(self):
        row, context, request = self.seed()
        self.agent.decide_reply.side_effect = SimulatedCrash
        with self.assertRaises(SimulatedCrash):
            self.send(context, request)
        messages = self.service.get(self.cid)['messages']
        self.assertEqual([item['role'] for item in messages], ['user'])
        user_id = messages[0]['id']
        self.assertEqual(self.store.get(self.workspace, row['job_id'])['user_message_id'], user_id)
        self.agent.decide_reply.side_effect = None
        resumed = self.recover(row)
        resumed.resume(row['job_id'])
        complete = self.finish(resumed, row['job_id'])
        self.assertEqual(complete['execution_state'], 'succeeded')
        messages = self.service.get(self.cid)['messages']
        self.assertEqual([item['role'] for item in messages], ['user', 'assistant'])
        self.assertEqual(messages[0]['id'], user_id)
        self.assertEqual({item['turn_id'] for item in messages}, {row['turn_id']})
        self.assertEqual({item['client_message_id'] for item in messages}, {'checkpoint-message-0001'})
        self.assertEqual(self.agent.decide_reply.call_count, 2)  # interrupted + resumed
        self.statistics.assert_not_called()
        self.agent.compose_reply.assert_not_called()

    def test_saved_assistant_replays_if_worker_finish_was_never_committed(self):
        row, context, request = self.seed()
        original = self.send(context, request)
        before = self.service.get(self.cid)['messages']
        self.assertEqual(len(before), 2)
        self.assertEqual(self.store.get(self.workspace, row['job_id'])['state'], 'running')
        self.agent.decide_reply.reset_mock()
        self.agent.decide_reply.side_effect = AssertionError('Replay must not call a planner.')
        resumed = self.recover(row)
        resumed.resume(row['job_id'])
        complete = self.finish(resumed, row['job_id'])
        self.assertEqual(complete['execution_state'], 'succeeded')
        self.assertEqual(complete['response']['message'], original['message'])
        self.assertEqual(self.service.get(self.cid)['messages'], before)
        self.agent.decide_reply.assert_not_called()
        self.statistics.assert_not_called()
        self.agent.compose_reply.assert_not_called()
        duplicate = resumed.submit(self.cid, request['message'], repeats=1, prompt_versions=1,
                                   client_message_id='checkpoint-message-0001')
        self.assertTrue(duplicate['idempotent'])
        self.assertEqual(duplicate['id'], row['job_id'])

    def test_committed_assistant_replays_after_the_original_deadline_expires(self):
        row, context, request = self.seed()
        original = self.send(context, request)
        messages = self.service.get(self.cid)['messages']
        self.agent.decide_reply.reset_mock()
        self.agent.decide_reply.side_effect = AssertionError('A committed reply needs no new generation.')
        # Both direct execution and the durable worker must return committed
        # evidence before applying limits to any new work.
        context.deadline = time.monotonic()-1
        self.assertEqual(self.send(context, request)['message'], original['message'])
        began = self.store.clock()-self.jobs.wall_seconds-1
        with self.store.transaction() as connection:
            connection.execute('UPDATE jobs SET started_at=? WHERE job_id=?',
                               (began, row['job_id']))
        resumed = self.recover(row)
        with patch('nhis_agent.engine.models.measure') as measure:
            resumed.resume(row['job_id'])
            complete = self.finish(resumed, row['job_id'])
        self.assertEqual(complete['execution_state'], 'succeeded')
        self.assertEqual(complete['response']['message'], original['message'])
        self.assertEqual(self.service.get(self.cid)['messages'], messages)
        self.assertEqual(self.store.get(self.workspace, row['job_id'])['started_at'], began)
        self.agent.decide_reply.assert_not_called()
        self.agent.compose_reply.assert_not_called()
        self.statistics.assert_not_called()
        measure.assert_not_called()

    def test_successful_result_is_not_visible_until_the_publication_size_gate_passes(self):
        observed = []
        def inspect_before_publication(directory):
            directory = Path(directory)
            self.assertFalse((directory/'result.json').exists())
            self.assertIsNone(self.service._run(directory.name))
            pending = list(directory.glob('.publication-*.json'))
            self.assertEqual(len(pending), 1)
            unpublished = json.loads(pending[0].read_text())
            self.assertEqual(unpublished['status'], 'ok')
            self.assertEqual(unpublished['survey']['rows'], survey_fixture()['rows'])
            self.assertTrue((directory/'report.md').is_file())
            self.assertTrue((directory/'estimates.csv').is_file())
            observed.append(directory)
        with patch('nhis_agent.engine.StoragePolicy.check_run_size',
                   side_effect=inspect_before_publication) as size_check:
            result = self.agent.analyze_spec(SPEC, QUESTION)
        size_check.assert_called_once()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.service._run(result['run_id'])['status'], 'ok')
        self.assertEqual(json.loads((observed[0]/'result.json').read_text()), result)
        self.assertEqual(list(observed[0].glob('.publication-*.json')), [])

    def test_failed_publication_size_gate_removes_exports_but_retains_private_audit(self):
        def statistics_with_private_audit(csv_path, spec, directory):
            value = survey_fixture()
            engine.write_json(Path(directory)/'analysis_result.json',
                              dict(value, private_test_audit='PRIVATE_STATISTICS_TEST_AUDIT'))
            return value
        self.statistics.side_effect = statistics_with_private_audit
        def reject_before_publication(directory):
            directory = Path(directory)
            self.assertFalse((directory/'result.json').exists())
            self.assertIsNone(self.service._run(directory.name))
            self.assertEqual(len(list(directory.glob('.publication-*.json'))), 1)
            self.assertTrue((directory/'analysis_result.json').is_file())
            raise StorageLimit('Isolated artifact-cap fixture.', 'run_storage_limit')
        with patch('nhis_agent.engine.StoragePolicy.check_run_size',
                   side_effect=reject_before_publication) as size_check:
            result = self.agent.analyze_spec(SPEC, QUESTION)
        size_check.assert_called_once()
        directory = self.agent.root/'runs'/result['run_id']
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['code'], 'run_storage_limit')
        self.assertEqual(result['survey'], {'status': 'blocked', 'rows': []})
        self.assertEqual(set(result['artifacts']), {'json'})
        self.assertFalse((directory/'report.md').exists())
        self.assertFalse((directory/'estimates.csv').exists())
        self.assertEqual(list(directory.glob('.publication-*.json')), [])
        self.assertEqual(self.service._run(result['run_id'])['status'], 'blocked')
        self.assertEqual(json.loads((directory/'result.json').read_text()), result)
        self.assertEqual(json.loads((directory/'analysis_result.json').read_text())['private_test_audit'],
                         'PRIVATE_STATISTICS_TEST_AUDIT')
        self.assertNotIn('PRIVATE_STATISTICS_TEST_AUDIT', json.dumps(result))
        reports = [event for event in result['process'] if event.get('stage') == 'report']
        self.assertEqual(reports[-1]['status'], 'blocked')
        self.assertFalse(reports[-1]['facts']['report_released'])

    def crash_after_statistics(self):
        row, context, request = self.seed(QUESTION, measure=True)
        measure = self._patch('nhis_agent.engine.models.measure', side_effect=SimulatedCrash)
        with self.assertRaises(SimulatedCrash):
            self.send(context, request)
        self.statistics.assert_called_once()
        self.agent.decide_reply.assert_not_called()
        paths = list((self.agent.root/'runs').iterdir())
        self.assertEqual(len(paths), 1)
        with self.store.transaction() as connection:
            saved = connection.execute('SELECT kind,result_json FROM step_results WHERE job_id=?',
                                       (row['job_id'],)).fetchall()
        self.assertIn('statistics', [item[0] for item in saved])
        self.assertEqual(len(self.service.get(self.cid)['messages']), 1)
        return row, request, measure, paths[0]

    def test_completed_statistics_checkpoint_is_reused_before_measurement(self):
        row, request, measure, directory = self.crash_after_statistics()
        frozen = (directory/'execution_plan.json').read_bytes()
        measure.side_effect = None
        measure.return_value = {'status': 'ok', 'valid_calls': 1, 'rows': [
            {'group_id': 'overall', 'mean': .4, 'sd': 0., 'valid_count': 1, 'total_count': 1}]}
        resumed = self.recover(row)
        resumed.resume(row['job_id'])
        complete = self.finish(resumed, row['job_id'])
        self.assertEqual(complete['execution_state'], 'succeeded', complete.get('error'))
        result = complete['response']['result']
        self.assertEqual(result['run_id'], directory.name)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual((directory/'execution_plan.json').read_bytes(), frozen)
        self.assertEqual(len(list((self.agent.root/'runs').iterdir())), 1)
        self.statistics.assert_called_once()
        self.assertEqual(measure.call_count, 2)  # no provider request in interrupted mock
        self.assertTrue(any(event.get('facts', {}).get('checkpoint_restored')
                            for event in result['process']))
        self.assertEqual(len(self.service.get(self.cid)['messages']), 2)
        self.assertEqual(complete['response']['message']['run_id'], directory.name)

    def test_changed_frozen_inputs_block_resume_without_recomputing_or_measuring(self):
        row, request, measure, directory = self.crash_after_statistics()
        original = (directory/'execution_plan.json').read_bytes()
        self.data_status.return_value = dict(self.dataset, sha256='c'*64)
        measure.side_effect = AssertionError('Changed frozen inputs must not reach a paid boundary.')
        resumed = self.recover(row)
        resumed.resume(row['job_id'])
        complete = self.finish(resumed, row['job_id'])
        self.assertEqual(complete['execution_state'], 'blocked')
        self.assertEqual(complete['response']['message']['status'], 'blocked')
        self.assertIsNone(complete['response']['result'])
        self.assertNotIn('spec', self.service.get(self.cid)['context'])
        self.assertEqual((directory/'execution_plan.json').read_bytes(), original)
        self.assertFalse((directory/'result.json').exists())
        self.statistics.assert_called_once()
        self.assertEqual(measure.call_count, 1)
        self.agent.compose_reply.assert_not_called()

    def test_regression_never_enters_proportion_measurement_even_if_rows_are_present(self):
        survey = survey_fixture()
        term = {'term': 'educationless_than_high_school', 'log_odds': math.log(2),
                'se': .1, 'odds_ratio': 2., 'ci_low': 1.5, 'ci_high': 2.5}
        survey['regression'] = {'status': 'ok', 'n': 4000,
            'formula': 'outcome_value ~ education + age + sex + race + income',
            'income_adjusted': True, 'imputation_count': 10,
            'coefficients': [term], 'unadjusted': {'n': 4000, 'coefficients': [dict(term)]}}
        self.statistics.side_effect = None
        self.statistics.return_value = survey
        with patch('nhis_agent.engine.models.measure') as measure:
            result = self.agent.analyze_spec(dict(SPEC, analysis='regression', adjustment='full',
                group_by='education'), 'Regress self-rated health on education adjusting for income',
                measure_model=True, repeats=1, prompt_versions=1)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['measurement']['status'], 'not_applicable')
        self.assertEqual(result['measurement']['rows'], [])
        self.assertEqual(result['comparison'], [])
        self.assertEqual(len(result['survey']['rows']), 1)
        measure.assert_not_called()
        self.assertFalse((self.agent.root/'runs'/result['run_id']/'measurement_plan.json').exists())

    def test_expired_deadline_is_a_workflow_control_not_an_ordinary_fallback(self):
        row, context, request = self.seed()
        context.deadline = time.monotonic()-1
        with self.assertRaises(ExecutionDeadline):
            self.send(context, request)
        self.assertFalse(issubclass(ExecutionDeadline, Exception))
        self.assertEqual(self.service.get(self.cid)['messages'], [])
        self.agent.decide_reply.assert_not_called()

    def test_resumed_job_keeps_initial_wall_clock_budget(self):
        row, context, request = self.seed()
        began = self.store.clock()-self.jobs.wall_seconds-1
        with self.store.transaction() as connection:
            connection.execute('UPDATE jobs SET started_at=? WHERE job_id=?', (began, row['job_id']))
        resumed = self.recover(row)
        resumed.resume(row['job_id'])
        complete = self.finish(resumed, row['job_id'])
        self.assertEqual(complete['execution_state'], 'blocked')
        self.assertEqual(complete['error_code'], 'job_deadline_exceeded')
        self.assertEqual(self.store.get(self.workspace, row['job_id'])['started_at'], began)
        self.assertEqual(self.service.get(self.cid)['messages'], [])
        self.agent.decide_reply.assert_not_called()
        self.statistics.assert_not_called()

    def test_checkpoint_checks_use_only_read_only_lease_metadata(self):
        row, context, request = self.seed()
        with self.store.transaction() as connection:
            connection.execute('UPDATE jobs SET response_json=? WHERE job_id=?',
                               ('{"private":"MUST_NOT_BE_LOADED_BY_LEASE_CHECK"}', row['job_id']))
        with patch.object(self.store, 'get', side_effect=AssertionError('No history decoding.')), \
             patch.object(self.store, 'transaction', side_effect=AssertionError('No admission writer.')):
            context.check_cancelled()
            metadata = self.store.lease_status(self.workspace, row['job_id'])
        self.assertTrue({'state', 'cancel_requested', 'lease_owner', 'lease_expires_at'} <= set(metadata))
        self.assertFalse({'response_json', 'request_json', 'events', 'response'} & set(metadata))
        self.assertNotIn('MUST_NOT_BE_LOADED_BY_LEASE_CHECK', json.dumps(metadata))
        with self.assertRaises(FileNotFoundError):
            self.store.lease_status('a-different-workspace', row['job_id'])

    def test_event_pages_are_bounded_ordered_complete_and_workspace_scoped(self):
        row, context, request = self.seed()
        for index in range(425):
            self.store.event(self.workspace, row['job_id'], {'stage': 'fixture', 'index': index})
        pages, before = [], None
        while True:
            page = self.jobs.get(row['job_id'], event_limit=200, before_event=before)
            values = [event['index'] for event in page['events']]
            self.assertLessEqual(len(values), 200)
            self.assertEqual(values, sorted(values))
            pages.append(values)
            if not page['event_pagination']['has_more']:
                break
            before = page['event_pagination']['before']
        self.assertEqual([value for page in reversed(pages) for value in page], list(range(425)))
        self.assertEqual([len(page) for page in pages], [200, 200, 25])
        with self.assertRaises(FileNotFoundError):
            self.store.events_page('another-workspace', row['job_id'])
        for options in ({'limit': 201}, {'limit': True}, {'before': True}, {'before': 0}, {'before': '1'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.store.events_page(self.workspace, row['job_id'], **options)

    def test_model_io_timeout_is_clipped_to_the_durable_job_deadline(self):
        row, context, request = self.seed()
        context.deadline = time.monotonic()+5
        observed = []
        class Response:
            headers = Message()
            def getcode(self): return 200
            def read(self, limit):
                return json.dumps({'choices': [{'finish_reason': 'stop',
                    'message': {'content': 'Isolated provider fixture.'}}]}).encode()[:limit]
            def __enter__(self): return self
            def __exit__(self, *args): return False
        def send(req, timeout):
            observed.append(timeout)
            return Response()
        budget = ModelBudget(self.store.state_root)
        telemetry = Telemetry(self.store.state_root)
        config = {'provider': 'openai', 'base_url': 'https://fixture.invalid/v1',
                  'api_key': 'UNIT_TEST_ONLY', 'model': 'fixture-model', 'timeout': 60}
        with bind(context), patch.object(transport, '_budget_for', return_value=budget), \
             patch.object(transport, '_telemetry_for', return_value=telemetry):
            result = transport.request(config, 'Isolated test', 'No real network', send=send,
                                       deadline=time.monotonic()+60, max_output_tokens=32)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        self.assertLessEqual(observed[0], 5)
        self.assertEqual(self.store.get(self.workspace, row['job_id'])['external_attempts'][0]['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
