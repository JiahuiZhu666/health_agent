"""Deletion fixtures use temporary conversations only, with no API or R calls."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from nhis_agent.conversation import ConversationService
from nhis_agent.jobs import ChatJobs, ConversationBusy


class LocalChatAgent:
    def __init__(self, root):
        self.root = Path(root)
        self.block = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def decide_reply(self, question, history, research_context=None):
        self.calls += 1
        self.started.set()
        if self.block and not self.release.wait(3):
            raise AssertionError('Test did not release the fixture')
        return {'status':'ok','mode':'chat','reply':'A local test reply.'}


class ConversationDeletionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.agent = LocalChatAgent(self.folder.name)
        self.service = ConversationService(self.agent)

    def tearDown(self):
        self.agent.release.set()
        self.folder.cleanup()

    def create(self):
        return self.service.create()['id']

    def finished(self, jobs, job_id):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            value=jobs.get(job_id)
            if value['status']!='running' and jobs.active_job(value['conversation_id']) is None:
                return value
            threading.Event().wait(.005)
        self.fail('The isolated chat job did not finish')

    def test_delete_removes_only_selected_chat_and_is_idempotent(self):
        first,second=self.create(),self.create()
        self.service.send(first,'Hello')
        lock_path=self.service.directory/(first+'.lock')
        inode=lock_path.stat().st_ino
        self.assertEqual(self.service.delete(first),{'deleted':[first],'deleted_count':1})
        self.assertEqual(lock_path.stat().st_ino,inode)
        self.assertEqual(self.service.get(second)['id'],second)
        with self.assertRaises(FileNotFoundError):
            self.service.get(first)
        self.assertEqual(self.service.delete(first),{'deleted':[],'deleted_count':0})

    def test_clear_deletes_only_strict_regular_chat_files_and_preserves_research(self):
        identifiers=[self.create(),self.create()]
        root=Path(self.folder.name)
        preserved=[root/'runs'/'saved-run'/'result.json',root/'data'/'adult24.csv',root/'.env',
                   self.service.directory/'invalid.json',self.service.directory/('A'*32+'.json'),
                   self.service.directory/'notes.txt']
        for path in preserved:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('PRESERVED_LOCAL_FIXTURE')
        outside=root/'outside.json';outside.write_text('OUTSIDE_FIXTURE')
        link=self.service.directory/('f'*32+'.json');link.symlink_to(outside)
        directory=self.service.directory/('e'*32+'.json');directory.mkdir()
        corrupt=self.service.directory/('d'*32+'.json');corrupt.write_text('invalid JSON fixture')
        result=self.service.clear()
        self.assertEqual(result,{'deleted':sorted(identifiers+['d'*32]),'deleted_count':3})
        self.assertEqual(self.service.list(),[])
        for path in preserved:
            self.assertEqual(path.read_text(),'PRESERVED_LOCAL_FIXTURE')
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(),'OUTSIDE_FIXTURE')
        self.assertTrue(directory.is_dir())
        self.assertEqual(self.service.clear(),{'deleted':[],'deleted_count':0})
        self.assertFalse(any(path.name.endswith('.json') and path.stem in identifiers for path in self.service.directory.iterdir()))

    def test_invalid_ids_and_symlink_targets_are_not_deleted(self):
        cid=self.create()
        for value in ('../'+cid, cid+'.json', 'A'*32, '', None):
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.service.delete(value)
        outside=Path(self.folder.name)/'outside.json';outside.write_text('keep')
        linked='f'*32
        (self.service.directory/(linked+'.json')).symlink_to(outside)
        self.assertEqual(self.service.delete(linked),{'deleted':[],'deleted_count':0})
        self.assertEqual(outside.read_text(),'keep')
        self.assertEqual(self.service.get(cid)['id'],cid)

    def test_symlinked_conversation_directory_cannot_delete_outside_files(self):
        outside=Path(self.folder.name)/'outside';outside.mkdir()
        path=outside/('a'*32+'.json');path.write_text('keep')
        self.service.directory.rmdir()
        self.service.directory.symlink_to(outside,target_is_directory=True)
        for operation in (lambda:self.service.delete('a'*32),self.service.clear):
            with self.assertRaises(OSError):
                operation()
        self.assertEqual(path.read_text(),'keep')

    def test_busy_clear_is_all_or_nothing_and_releases_earlier_locks(self):
        identifiers=sorted([self.create(),self.create(),self.create()])
        lock=self.service._lock(identifiers[-1]);lock.acquire()
        try:
            with self.assertRaises(ConversationBusy):self.service.clear()
            self.assertEqual(sorted(item['id'] for item in self.service.list()),identifiers)
            earlier=self.service._lock(identifiers[0])
            self.assertTrue(earlier.acquire(blocking=False));earlier.release()
        finally:
            lock.release()
        self.assertEqual(self.service.clear()['deleted'],identifiers)

    def test_posix_file_lock_blocks_delete_and_clear_without_partial_removal(self):
        try:
            import fcntl
        except ImportError:
            self.skipTest('POSIX flock is unavailable')
        identifiers=sorted([self.create(),self.create()])
        descriptor=self.service._file_lock(identifiers[-1])
        try:
            with self.assertRaises(ConversationBusy):self.service.delete(identifiers[-1])
            with self.assertRaises(ConversationBusy):self.service.clear()
            self.assertEqual(len(self.service.list()),2)
        finally:
            import os
            os.close(descriptor)
        self.assertEqual(self.service.clear()['deleted_count'],2)

    def test_active_send_cannot_be_deleted_or_resurrect_a_deleted_chat(self):
        cid,other=self.create(),self.create()
        self.agent.block=True
        outcomes=[]
        thread=threading.Thread(target=lambda:outcomes.append(self.service.send(cid,'Hello')))
        thread.start()
        try:
            self.assertTrue(self.agent.started.wait(1))
            second_service=ConversationService(self.agent)
            with self.assertRaises(ConversationBusy):second_service.delete(cid)
            with self.assertRaises(ConversationBusy):second_service.clear()
            self.assertEqual(self.service.get(other)['id'],other)
        finally:
            self.agent.release.set();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes),1)
        self.service.delete(cid)
        with self.assertRaises(FileNotFoundError):self.service.send(cid,'Again')
        self.assertFalse((self.service.directory/(cid+'.json')).exists())

    def test_create_and_clear_share_a_directory_guard(self):
        cid=self.create()
        with self.service._directory_guard():
            with self.assertRaises(ConversationBusy):self.service.create()
            with self.assertRaises(ConversationBusy):self.service.clear()
            with self.assertRaises(ConversationBusy):self.service.delete(cid)
        self.assertEqual(self.service.clear()['deleted'],[cid])
        self.assertEqual(self.service.list(),[])

    def test_job_delete_rejects_active_work_then_discards_completed_response(self):
        cid=self.create();jobs=ChatJobs(self.service);self.agent.block=True
        submitted=jobs.submit(cid,'Hello')
        try:
            self.assertTrue(self.agent.started.wait(1))
            with self.assertRaises(ConversationBusy):jobs.delete_conversation(cid)
            with self.assertRaises(ConversationBusy):jobs.clear_conversations()
        finally:
            self.agent.release.set()
        self.finished(jobs,submitted['job_id'])
        self.assertEqual(jobs.delete_conversation(cid),{'deleted':[cid],'deleted_count':1})
        with self.assertRaises(FileNotFoundError):jobs.get(submitted['job_id'])
        with self.assertRaises(FileNotFoundError):jobs.store.get(jobs.workspace_id,submitted['job_id'])

    def test_clear_discards_all_completed_job_memory_and_creates_no_new_chat(self):
        jobs=ChatJobs(self.service)
        identifiers=[self.create(),self.create()]
        job_ids=[]
        for cid in identifiers:
            submitted=jobs.submit(cid,'Hello');job_ids.append(submitted['job_id'])
            self.finished(jobs,submitted['job_id'])
        result=jobs.clear_conversations()
        self.assertEqual(result['deleted'],sorted(identifiers))
        self.assertEqual(self.service.list(),[])
        for job_id in job_ids:
            with self.assertRaises(FileNotFoundError):jobs.get(job_id)
        with jobs.store.transaction() as connection:
            count=connection.execute('SELECT COUNT(*) FROM jobs JOIN turns USING(turn_id) WHERE jobs.workspace_id=? AND turns.deleted=0',
                                     (jobs.workspace_id,)).fetchone()[0]
        self.assertEqual(count,0)

    def test_externally_deleted_conversation_cannot_be_read_through_old_job(self):
        cid=self.create();jobs=ChatJobs(self.service)
        submitted=jobs.submit(cid,'Hello');self.finished(jobs,submitted['job_id'])
        ConversationService(self.agent).delete(cid)
        with self.assertRaises(FileNotFoundError):jobs.get(submitted['job_id'])
        with self.assertRaises(FileNotFoundError):jobs.store.get(jobs.workspace_id,submitted['job_id'])

    def test_concurrent_submission_waits_for_delete_then_fails_without_execution(self):
        cid=self.create();jobs=ChatJobs(self.service)
        entered=threading.Event();release=threading.Event();deleted=[];errors=[]
        original=self.service.delete
        def held_delete(value):
            entered.set()
            if not release.wait(2):raise AssertionError('Deletion fixture was not released')
            return original(value)
        def submit():
            try:jobs.submit(cid,'Hello')
            except Exception as error:errors.append(error)
        with patch.object(self.service,'delete',side_effect=held_delete):
            deleting=threading.Thread(target=lambda:deleted.append(jobs.delete_conversation(cid)))
            deleting.start();self.assertTrue(entered.wait(1))
            sending=threading.Thread(target=submit);sending.start()
            release.set();deleting.join(3);sending.join(3)
        self.assertFalse(deleting.is_alive());self.assertFalse(sending.is_alive())
        self.assertEqual(deleted,[{'deleted':[cid],'deleted_count':1}])
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],FileNotFoundError)
        self.assertEqual(self.agent.calls,0)
        with jobs.store.transaction() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM jobs WHERE workspace_id=?',
                                               (jobs.workspace_id,)).fetchone()[0],0)


if __name__=='__main__':
    unittest.main()
