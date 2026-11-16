"""Deterministic coordinator lifecycle regressions; no API, R, or user state."""
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from nhis_agent.job_store import JobStore
from nhis_agent.jobs import ChatJobs


CID = 'a'*32


class GatedService:
    def __init__(self, root):
        self.root = Path(root)
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def get(self, conversation_id):
        if conversation_id != CID:
            raise FileNotFoundError('Unknown isolated conversation.')
        return {'id': CID, 'messages': []}

    def send(self, conversation_id, text, on_event=None, **options):
        self.calls += 1
        self.started.set()
        if not self.release.wait(3):
            raise AssertionError('The isolated service gate was never released.')
        return {'conversation_id': CID, 'message': {
            'id': 'b'*32, 'content': 'Isolated lifecycle fixture.', 'status': 'ok'},
            'result': None}


class JobShutdownTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix='nhis-shutdown-test-')
        self.addCleanup(folder.cleanup)
        self.service = GatedService(Path(folder.name)/'workspace')
        self.store = JobStore(Path(folder.name)/'state')
        self.jobs = ChatJobs(self.service, store=self.store, heartbeat_interval=.01)
        self.release_gates = [self.service.release]
        self.helpers = []
        self.addCleanup(self.stop)

    def stop(self):
        for event in self.release_gates:
            event.set()
        for thread in self.helpers:
            if thread.ident is not None:
                thread.join(3)
        outcome = self.jobs.shutdown(3)
        self.assertTrue(outcome['drained'], outcome)
        self.assertTrue(all(not thread.is_alive() for thread in self.helpers))

    def test_shutdown_cannot_join_a_registered_thread_before_start(self):
        entered = threading.Event()
        allow_start = threading.Event()
        probed = threading.Event()
        shutdown_done = threading.Event()
        self.release_gates.append(allow_start)
        errors, results, available = [], [], []
        original_start = threading.Thread.start
        map_lock = self.jobs._lock

        class ObservedMapLock:
            def __enter__(inner):
                if threading.current_thread().name == 'isolated-shutdown':
                    acquired = map_lock.acquire(blocking=False)
                    available.append(acquired)
                    probed.set()
                    if not acquired:
                        map_lock.acquire()
                else:
                    map_lock.acquire()
                return inner
            def __exit__(inner, *args):
                map_lock.release()

        def gated_start(thread):
            if thread.name.startswith('nhis-chat-'):
                entered.set()
                if not allow_start.wait(3):
                    raise AssertionError('The isolated worker-start gate timed out.')
            return original_start(thread)

        def submit():
            try:
                self.jobs.submit(CID, 'Isolated message', client_message_id='lifecycle-start-0001')
            except BaseException as error:
                errors.append(error)

        def shutdown():
            try:
                results.append(self.jobs.shutdown(1))
            except BaseException as error:
                errors.append(error)
            finally:
                shutdown_done.set()

        self.jobs._lock = ObservedMapLock()
        producer = threading.Thread(target=submit, name='isolated-submitter')
        closer = threading.Thread(target=shutdown, name='isolated-shutdown')
        self.helpers.extend((producer, closer))
        with patch.object(threading.Thread, 'start', new=gated_start):
            try:
                producer.start()
                self.assertTrue(entered.wait(2))
                closer.start()
                self.assertTrue(self.jobs._stopping.wait(2))
                self.assertTrue(probed.wait(2))
                # This probe is synchronous, rather than a scheduling sleep:
                # shutdown must wait until the registered thread is startable.
                self.assertEqual(available, [False])
                self.assertFalse(shutdown_done.is_set())
                self.assertEqual(self.service.calls, 0)
            finally:
                allow_start.set()
                producer.join(3)
                if closer.ident is not None:
                    closer.join(3)
        self.assertFalse(producer.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [{'drained': True, 'pending_jobs': []}])
        self.assertEqual(self.service.calls, 0)

    def test_terminal_job_is_not_drained_while_its_owned_heartbeat_is_still_running(self):
        heartbeat_entered = threading.Event()
        heartbeat_release = threading.Event()
        terminal_committed = threading.Event()
        self.release_gates.append(heartbeat_release)
        heartbeat_threads = []
        original_heartbeat = self.store.heartbeat
        original_finish = self.store.finish

        def gated_heartbeat(*args, **kwargs):
            heartbeat_threads.append(threading.current_thread())
            heartbeat_entered.set()
            # Gate outside the transaction so neither result publication nor
            # cancellation waits for a synthetic SQLite write lock.
            if not heartbeat_release.wait(3):
                raise AssertionError('The isolated heartbeat gate was never released.')
            return original_heartbeat(*args, **kwargs)

        def finish(*args, **kwargs):
            saved = original_finish(*args, **kwargs)
            terminal_committed.set()
            return saved

        with patch.object(self.store, 'heartbeat', side_effect=gated_heartbeat), \
             patch.object(self.store, 'finish', side_effect=finish):
            try:
                submitted = self.jobs.submit(CID, 'Isolated message',
                                             client_message_id='lifecycle-heartbeat-0001')
                self.assertTrue(self.service.started.wait(2))
                self.assertTrue(heartbeat_entered.wait(2))
                self.service.release.set()
                self.assertTrue(terminal_committed.wait(2))
                persisted = self.store.get(self.jobs.workspace_id, submitted['id'])
                self.assertEqual(persisted['state'], 'succeeded')
                self.assertTrue(heartbeat_threads[0].is_alive())
                pending = self.jobs.shutdown(.05)
                self.assertFalse(pending['drained'])
                self.assertEqual(pending['pending_jobs'], [submitted['id']])
                self.assertTrue(heartbeat_threads[0].is_alive())
            finally:
                heartbeat_release.set()
                self.service.release.set()
                drained = self.jobs.shutdown(3)
        self.assertTrue(drained['drained'], drained)
        self.assertEqual(drained['pending_jobs'], [])
        self.assertTrue(all(not thread.is_alive() for thread in heartbeat_threads))
        self.assertEqual(self.service.calls, 1)
        self.assertEqual(self.store.get(self.jobs.workspace_id, submitted['id'])['state'], 'succeeded')

    def test_heartbeat_start_failure_releases_the_worker_without_joining_an_unstarted_thread(self):
        original_start=threading.Thread.start
        failure_entered=threading.Event()
        def start(thread):
            if thread.name.startswith('nhis-heartbeat-'):
                failure_entered.set()
                raise RuntimeError('Injected heartbeat startup failure')
            return original_start(thread)
        with patch.object(threading.Thread,'start',new=start):
            submitted=self.jobs.submit(CID,'Isolated message',client_message_id='lifecycle-pulse-fail-0001')
            self.assertTrue(failure_entered.wait(2))
            outcome=self.jobs.shutdown(3)
        self.assertTrue(outcome['drained'],outcome)
        self.assertEqual(outcome['pending_jobs'],[])
        self.assertEqual(self.service.calls,0)
        self.assertEqual(self.store.get(self.jobs.workspace_id,submitted['id'])['state'],'failed')


if __name__ == '__main__':
    unittest.main()
