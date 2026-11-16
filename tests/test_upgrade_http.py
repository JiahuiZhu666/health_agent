"""Upgrade route integration using in-memory HTTP connections and temporary state.

No listener, real survey, external API, R process or .env is used. Numerical
artifact contents below are explicit test sentinels, not research estimates.
"""
import json
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote
import uuid

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
import test_public_http as _fixture
from nhis_agent.conversation import ConversationService
from nhis_agent.engine import Agent
from nhis_agent.execution_context import ExecutionContext
from nhis_agent.job_store import JobStore, fingerprint, workspace_identity
from nhis_agent.jobs import ChatJobs
from nhis_agent.readiness import ObservedReadiness
from nhis_agent.storage_policy import StorageLimit, StoragePolicy
from nhis_agent.workspaces import VisitorWorkspaces


class UpgradeHttpTests(unittest.TestCase):
    factory = _fixture.PublicHttpTests.factory
    request = _fixture.PublicHttpTests.request
    visitor = _fixture.PublicHttpTests.visitor
    create = _fixture.PublicHttpTests.create
    completed = _fixture.PublicHttpTests.completed

    def setUp(self):
        _fixture.PublicHttpTests.setUp(self)
        for target in ('urllib.request.urlopen', 'subprocess.run'):
            guard = patch(target, side_effect=AssertionError('No live network or process is allowed in HTTP fixtures.'))
            guard.start(); self.addCleanup(guard.stop)

    def workspace(self, cid):
        paths = list((self.root / 'visitors').glob('*/conversations/' + cid + '.json'))
        self.assertEqual(len(paths), 1)
        root = paths[0].parent.parent
        return root, ConversationService(Agent(root))

    def submit(self, cookie, cid, message='Hello from an isolated fixture.', client_id=None):
        payload = {'message': message, 'client_message_id': client_id or str(uuid.uuid4()), 'measure_model': False, 'repeats': 5, 'prompt_versions': 3}
        status, headers, raw = self.request('POST', '/api/conversations/' + cid + '/messages', cookie, payload)
        return status, headers, json.loads(raw), payload

    def terminal(self, cookie, jid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, _, raw = self.request('GET', '/api/jobs/' + jid, cookie)
            self.assertEqual(status, 200)
            value = json.loads(raw)
            if value['execution_state'] not in ('queued', 'running') or value.get('recovery_required'):
                return value
            threading.Event().wait(.005)
        self.fail('The isolated fixture job did not reach an observable outcome.')

    def seed_messages(self, service, cid, count=120):
        value = service._load(cid)
        value['messages'] = [{'id': format(index, '032x'), 'role': 'user' if index % 2 else 'assistant',
                              'content': 'Synthetic historical message ' + str(index), 'created_at': '2026-11-16T12:00:00Z'}
                             for index in range(1, count + 1)]
        service._save(value)
        return value['messages']

    def test_idempotent_retry_after_exhausted_quota_returns_one_job_and_one_turn(self):
        self.handler = self.factory(messages_per_hour=1, messages_per_day=1)
        cookie = self.visitor(); cid = self.create(cookie); client = str(uuid.uuid4())
        decision = {'status': 'ok', 'mode': 'chat', 'reply': 'Synthetic saved reply.'}
        with patch.object(Agent, 'decide_reply', return_value=decision) as model:
            status, _, original, payload = self.submit(cookie, cid, client_id=client)
            self.assertEqual(status, 202); jid = original.get('job_id', original.get('id')); self.completed(cookie, jid)
            status, _, raw = self.request('POST', '/api/conversations/' + cid + '/messages', cookie, payload)
            repeated = json.loads(raw)
            self.assertEqual(status, 202); self.assertEqual(repeated.get('job_id', repeated.get('id')), jid); self.assertTrue(repeated['idempotent'])
            self.assertEqual(self.submit(cookie, cid, message='A different new fixture turn')[0], 429)
            self.assertEqual(model.call_count, 1)
        _, service = self.workspace(cid)
        messages = service.get(cid)['messages']
        self.assertEqual([item['role'] for item in messages], ['user', 'assistant'])
        with sqlite3.connect(str(self.root / '.cache/public-access.sqlite3')) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accepted_events WHERE kind='message'").fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM turns').fetchone()[0], 1)

    def test_reusing_an_id_for_a_different_payload_is409_even_after_quota_is_used(self):
        self.handler = self.factory(messages_per_hour=1, messages_per_day=1)
        cookie = self.visitor(); cid = self.create(cookie); client = str(uuid.uuid4())
        with patch.object(Agent, 'decide_reply', return_value={'status': 'ok', 'mode': 'chat', 'reply': 'Synthetic reply.'}) as model:
            code, _, original, _ = self.submit(cookie, cid, client_id=client); self.assertEqual(code, 202)
            self.completed(cookie, original.get('job_id', original.get('id')))
            code, _, _, _ = self.submit(cookie, cid, message='Different text with the same key', client_id=client)
            self.assertEqual(code, 409); self.assertEqual(model.call_count, 1)

    def assert_admission_and_deletion_finish(self, *, clear):
        """Force DB -> map-lock and deletion -> DB to meet, without a 5s hang.

        Only the test's thread-map lock has a deadline. SQLite uses its real
        transaction/locking behavior; a reversed lock order fails the POST
        promptly instead of making this regression wait for busy_timeout.
        """
        workspaces = VisitorWorkspaces(self.agent)
        with patch('nhis_agent.workspaces.VisitorWorkspaces', return_value=workspaces):
            self.handler = self.factory()
        cookie = self.visitor(); cid = self.create(cookie)
        coordinator = next(iter(workspaces._items.values())).jobs
        writer_held, deletion_entered, release_reply = (threading.Event() for _ in range(3))
        self.addCleanup(release_reply.set)
        original_lock = coordinator._lock

        class BoundedMapLock:
            def __enter__(self):
                if not original_lock.acquire(timeout=.5):
                    raise TimeoutError('Admission waited on a map lock held by a SQLite writer waiter.')
                return self

            def __exit__(self, *exception):
                original_lock.release()

        original_transaction = coordinator.store.transaction
        original_lookup = coordinator.lookup_submission

        @contextmanager
        def observe_transaction(connection=None):
            if threading.current_thread().name.startswith('upgrade-delete'):
                # In the former implementation deletion already owned _lock
                # here. Admission is guaranteed to still own SQLite's writer.
                deletion_entered.set()
            with original_transaction(connection) as current:
                yield current

        def hold_admission(*args, **kwargs):
            self.assertTrue(kwargs['connection'].in_transaction)
            writer_held.set()
            if not deletion_entered.wait(1):
                raise AssertionError('Deletion did not reach its real transaction boundary.')
            return original_lookup(*args, **kwargs)

        def reply(*args, **kwargs):
            if not release_reply.wait(2):
                raise AssertionError('The concurrency fixture did not release its reply.')
            return {'status': 'ok', 'mode': 'chat', 'reply': 'Synthetic concurrency reply.'}

        path = '/api/conversations' + ('' if clear else '/' + cid)
        with patch.object(coordinator, '_lock', BoundedMapLock()), \
                patch.object(coordinator.store, 'transaction', side_effect=observe_transaction), \
                patch.object(coordinator, 'lookup_submission', side_effect=hold_admission), \
                patch.object(Agent, 'decide_reply', side_effect=reply):
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='upgrade-admit') as admission_pool, \
                    ThreadPoolExecutor(max_workers=1, thread_name_prefix='upgrade-delete') as deletion_pool:
                try:
                    admission = admission_pool.submit(self.submit, cookie, cid)
                    self.assertTrue(writer_held.wait(1), 'Public admission never acquired SQLite writer.')
                    deletion = deletion_pool.submit(self.request, 'DELETE', path, cookie)
                    self.assertTrue(deletion_entered.wait(1), 'Deletion never reached SQLite.')
                    accepted = admission.result(timeout=1.5)
                    deleted = deletion.result(timeout=1.5)
                    self.assertEqual(accepted[0], 202, accepted[2])
                    # Admission committed first; queued/running work prevents
                    # both single deletion and clear-all, without any partial delete.
                    self.assertEqual(deleted[0], 409, deleted[2])
                finally:
                    release_reply.set()
            self.completed(cookie, accepted[2].get('job_id', accepted[2].get('id')))
        _, service = self.workspace(cid)
        self.assertEqual([message['role'] for message in service.get(cid)['messages']], ['user', 'assistant'])
        with sqlite3.connect(str(self.root / '.cache/public-access.sqlite3')) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accepted_events WHERE kind='message'").fetchone()[0], 1)

    def test_public_admission_and_single_delete_do_not_reverse_database_and_map_locks(self):
        self.assert_admission_and_deletion_finish(clear=False)

    def test_public_admission_and_clear_do_not_reverse_database_and_map_locks(self):
        self.assert_admission_and_deletion_finish(clear=True)

    def test_workspace_lru_eviction_reads_active_jobs_while_global_writer_is_held(self):
        workspaces = VisitorWorkspaces(self.agent, maximum=1)
        first, second = 'a' * 32, 'b' * 32
        previous = workspaces.acquire(first, create=True); workspaces.release(first)
        rendezvous = threading.Barrier(2)

        def evict():
            rendezvous.wait(timeout=1)
            current = workspaces.acquire(second, create=True)
            workspaces.release(second)
            return current

        # A real BEGIN IMMEDIATE owns the same global SQLite writer used by
        # public admission. LRU must finish before that writer is released:
        # reconciliation or BEGIN IMMEDIATE in has_active would block it.
        blocked = False
        with ThreadPoolExecutor(max_workers=1) as pool:
            with workspaces._store.transaction() as connection:
                self.assertTrue(connection.in_transaction)
                future = pool.submit(evict)
                rendezvous.wait(timeout=1)
                try:
                    current = future.result(timeout=.75)
                except FutureTimeoutError:
                    blocked = True
            # Release the writer even on regression, so no SQLite 5s timeout
            # or stranded worker can delay cleanup.
            current = future.result(timeout=1)
        self.assertFalse(blocked, 'LRU eviction waited for the global SQLite writer instead of using a read-only job check.')
        self.assertIsNot(current, previous)
        self.assertEqual(list(workspaces._items), [second])
        self.assertEqual(current.leases, 0)

    def test_other_visitors_cannot_get_cancel_or_resume_a_known_job(self):
        alice, bob = self.visitor(), self.visitor(); cid = self.create(alice); self.create(bob)
        started, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        def decide(*args, **kwargs): started.set(); release.wait(2); return {'status': 'ok', 'mode': 'chat', 'reply': 'Synthetic reply.'}
        with patch.object(Agent, 'decide_reply', side_effect=decide):
            code, _, accepted, _ = self.submit(alice, cid); self.assertEqual(code, 202); jid = accepted.get('job_id', accepted.get('id'))
            self.assertTrue(started.wait(1))
            for method, suffix, body in [('GET', '', None), ('POST', '/cancel', {}), ('POST', '/resume', {})]:
                self.assertEqual(self.request(method, '/api/jobs/' + jid + suffix, bob, body)[0], 404)
            code, _, raw = self.request('POST', '/api/jobs/' + jid + '/cancel', alice, {})
            self.assertEqual(code, 202); value = json.loads(raw)
            self.assertTrue(value['cancel_requested']); self.assertEqual(value['execution_state'], 'running')
            self.assertEqual(value['cancellation_effect'], 'requested')
            release.set(); result = self.terminal(alice, jid)
            self.assertIn(result['execution_state'], ('cancelled', 'succeeded', 'blocked'))

    def test_completed_jobs_survive_handler_restart_without_model_or_statistical_execution(self):
        cookie = self.visitor(); cid = self.create(cookie); client = str(uuid.uuid4())
        with patch.object(Agent, 'decide_reply', return_value={'status': 'ok', 'mode': 'chat', 'reply': 'Persisted synthetic reply.'}) as model:
            code, _, original, payload = self.submit(cookie, cid, client_id=client); self.assertEqual(code, 202)
            jid = original.get('job_id', original.get('id')); completed = self.completed(cookie, jid)
            self.handler = self.factory()
            code, _, raw = self.request('GET', '/api/jobs/' + jid, cookie); restored = json.loads(raw)
            self.assertEqual(code, 200); self.assertEqual(restored['response']['message'], completed['response']['message'])
            code, _, raw = self.request('POST', '/api/conversations/' + cid + '/messages', cookie, payload)
            self.assertEqual(code, 202); self.assertTrue(json.loads(raw)['idempotent']); self.assertEqual(model.call_count, 1)

    def seed_interrupted(self, cookie, cid, external=False):
        root, _ = self.workspace(cid); store = JobStore(self.root); workspace = workspace_identity(root)
        request = {'message': 'Interrupted synthetic fixture', 'measure_model': False, 'repeats': 5, 'prompt_versions': 3}
        row, _ = store.create(workspace, cid, str(uuid.uuid4()), fingerprint(request), request)
        self.assertTrue(store.claim(workspace, row['job_id'], 'synthetic-old-worker'))
        context = ExecutionContext(store=store, workspace_id=workspace, job_id=row['job_id'], turn_id=row['turn_id'],
                                   trace_id=row['trace_id'], owner='synthetic-old-worker', state_root=self.root)
        if external: context.external_start('model', 'synthetic-planned-attempt', {'request_hash': 'synthetic'})
        else: context.save_step('synthetic_saved_step', {'status': 'saved'}, 'synthetic-fixed-inputs')
        with store.transaction() as connection:
            connection.execute('UPDATE jobs SET owner_pid=0,lease_expires_at=0 WHERE job_id=?', (row['job_id'],))
        return row['job_id']

    def test_unknown_external_work_remains_unknown_after_restart_and_cannot_resume(self):
        cookie = self.visitor(); cid = self.create(cookie); jid = self.seed_interrupted(cookie, cid, external=True)
        with patch.object(Agent, 'decide_reply') as model, patch.object(ChatJobs, '_dispatch') as dispatch:
            self.handler = self.factory()
            code, _, raw = self.request('GET', '/api/jobs/' + jid, cookie); value = json.loads(raw)
            self.assertEqual(code, 200); self.assertEqual(value['execution_state'], 'unknown'); self.assertFalse(value['can_resume'])
            self.assertEqual(self.request('POST', '/api/jobs/' + jid + '/resume', cookie, {})[0], 409)
            model.assert_not_called(); dispatch.assert_not_called()

    def test_safe_interrupted_work_requires_explicit_resume(self):
        cookie = self.visitor(); cid = self.create(cookie); jid = self.seed_interrupted(cookie, cid)
        with patch.object(Agent, 'decide_reply', return_value={'status': 'ok', 'mode': 'chat', 'reply': 'Resumed synthetic fixture.'}) as model:
            self.handler = self.factory()
            code, _, raw = self.request('GET', '/api/jobs/' + jid, cookie); value = json.loads(raw)
            self.assertEqual(code, 200); self.assertEqual(value['execution_state'], 'queued'); self.assertTrue(value['recovery_required']); self.assertTrue(value['can_resume']); model.assert_not_called()
            self.assertEqual(self.request('POST', '/api/jobs/' + jid + '/resume', cookie, {})[0], 202)
            self.completed(cookie, jid); self.assertEqual(model.call_count, 1)

    def test_conversation_history_pages_are_bounded_and_cursors_are_validated(self):
        cookie = self.visitor(); cid = self.create(cookie); _, service = self.workspace(cid)
        for _ in range(34): service.create()
        code, _, raw = self.request('GET', '/api/conversations', cookie); first = json.loads(raw)
        self.assertEqual(code, 200); self.assertEqual(len(first['conversations']), 30); self.assertTrue(first['next_cursor'])
        code, _, raw = self.request('GET', '/api/conversations?cursor=' + quote(first['next_cursor'], safe=''), cookie); second = json.loads(raw)
        self.assertEqual(code, 200); self.assertEqual(len(second['conversations']), 5); self.assertIsNone(second['next_cursor'])
        ids = [item['id'] for item in first['conversations'] + second['conversations']]
        self.assertEqual(len(set(ids)), 35)
        self.assertEqual(self.request('GET', '/api/conversations?cursor=invalid-cursor', cookie)[0], 400)

    def test_message_pages_and_completed_job_response_keep_only_last50(self):
        cookie = self.visitor(); cid = self.create(cookie); _, service = self.workspace(cid); messages = self.seed_messages(service, cid)
        code, _, raw = self.request('GET', '/api/conversations/' + cid, cookie); latest = json.loads(raw)
        self.assertEqual(code, 200); self.assertEqual(len(latest['messages']), 50); self.assertEqual(latest['pagination']['total'], 120)
        self.assertEqual(latest['messages'][0]['id'], messages[70]['id'])
        collected = list(latest['messages']); before = latest['pagination']['before']
        while before:
            code, _, raw = self.request('GET', '/api/conversations/' + cid + '?before=' + before + '&limit=50', cookie); page = json.loads(raw)
            self.assertEqual(code, 200); self.assertLessEqual(len(page['messages']), 50)
            collected = page['messages'] + collected
            before = page['pagination']['before'] if page['pagination']['has_more'] else None
        self.assertEqual([item['id'] for item in collected], [item['id'] for item in messages])
        for cursor in ('invalid', 'f' * 32):
            self.assertEqual(self.request('GET', '/api/conversations/' + cid + '?before=' + cursor, cookie)[0], 400)
        with patch.object(Agent, 'decide_reply', return_value={'status': 'ok', 'mode': 'chat', 'reply': 'Paged fixture reply.'}):
            code, _, accepted, _ = self.submit(cookie, cid); self.assertEqual(code, 202)
            completed = self.completed(cookie, accepted.get('job_id', accepted.get('id')))
        window = completed['response']['conversation']
        self.assertEqual(len(window['messages']), 50); self.assertEqual(window['pagination']['total'], 122)

    def test_run_list_pagination_and_release_status_gate_every_export(self):
        cookie = self.visitor(); cid = self.create(cookie); root, _ = self.workspace(cid)
        for number in range(35):
            rid = '20261116T120000Z-{:010x}'.format(number)
            directory = root / 'runs' / rid; directory.mkdir()
            (directory / 'registration.json').write_text(json.dumps({'run_id': rid, 'question': 'Synthetic export fixture',
                'registered_at': '2026-11-16T12:00:00Z', 'spec': {'outcome': 'fair_poor', 'group_by': 'overall', 'analysis': 'proportion'}}))
            (directory / 'result.json').write_text(json.dumps({'run_id': rid, 'status': 'ok' if number else 'blocked', 'spec': {}, 'survey': {'status': 'ok'}, 'question': 'Synthetic export fixture'}))
            (directory / 'report.md').write_text('SYNTHETIC_AGGREGATE_REPORT')
            (directory / 'estimates.csv').write_text('SYNTHETIC_AGGREGATE_CSV')
        code, _, raw = self.request('GET', '/api/runs', cookie); first = json.loads(raw)
        self.assertEqual(code, 200); self.assertEqual(len(first['runs']), 30); self.assertTrue(first['next_cursor'])
        code, _, raw = self.request('GET', '/api/runs?before=' + first['next_cursor'], cookie); second = json.loads(raw)
        self.assertEqual(code, 200); self.assertEqual(len(second['runs']), 5); self.assertIsNone(second['next_cursor'])
        self.assertEqual(self.request('GET', '/api/runs?before=invalid', cookie)[0], 400)
        blocked = '20261116T120000Z-0000000000'
        for suffix in ('/report', '/csv'):
            code, _, raw = self.request('GET', '/api/runs/' + blocked + suffix, cookie)
            self.assertEqual(code, 404); self.assertNotIn(b'SYNTHETIC_AGGREGATE', raw)
            self.assertEqual(self.request('GET', '/api/runs/20261116T120000Z-0000000001' + suffix, cookie)[0], 200)
        self.assertEqual(self.request('GET', '/api/runs/' + blocked, cookie)[0], 200)

    def test_storage_limit_has507_and_does_not_charge_or_execute(self):
        cookie = self.visitor()
        with patch.object(StoragePolicy, 'check_conversation_write', side_effect=StorageLimit('Synthetic storage ceiling.', code='workspace_storage_limit')), patch.object(Agent, 'decide_reply') as model:
            code, _, raw = self.request('POST', '/api/conversations', cookie, {})
            self.assertEqual(code, 507)
            self.assertEqual(json.loads(raw)['code'], 'workspace_storage_limit')
            model.assert_not_called()
        with sqlite3.connect(str(self.root / '.cache/public-access.sqlite3')) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM accepted_events').fetchone()[0], 0)

    def test_ready_endpoint_is_nonblocking_and_distinguishes_configuration_from_observation(self):
        cookie = self.visitor(); started, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        calls = []
        def probe():
            calls.append(1); started.set(); release.wait(2)
            return {'ready': True, 'status': 'observed', 'rscript': 'SYNTHETIC_PRIVATE_RUNTIME_PATH'}
        self.agent._statistics_health = ObservedReadiness(probe)
        with patch.object(self.agent, 'dialogue_status', return_value={'configured': True, 'connection': {'status': 'unverified'}}), \
                patch('server.survey_data.status', return_value={'ready': True}), patch.object(Agent, 'decide_reply') as model:
            self.assertEqual(self.request('GET', '/healthz', cookie)[0], 200); self.assertEqual(calls, [])
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    code, _, raw = pool.submit(self.request, 'GET', '/readyz', cookie).result(timeout=.5)
                    self.assertEqual(code, 200); self.assertTrue(started.wait(.5)); value = json.loads(raw)
                    self.assertTrue(value['chat']['configured']); self.assertEqual(value['chat']['last_observed_status'], 'unverified')
                    self.assertFalse(value['statistics']['ready']); self.assertTrue(value['statistics']['refreshing']); self.assertTrue(value['statistics']['stale'])
                    self.assertFalse(value['research']['ready']); self.assertNotIn(b'SYNTHETIC_PRIVATE', raw)
                finally: release.set()
            self.agent._statistics_health.snapshot(wait=True)
            code, _, raw = self.request('GET', '/readyz', cookie); value = json.loads(raw)
            self.assertEqual(code, 200); self.assertTrue(value['statistics']['ready']); self.assertFalse(value['statistics']['stale'])
            self.assertTrue(value['research']['ready']); self.assertEqual(len(calls), 1); model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
