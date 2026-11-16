import threading
import time
import unittest

from nhis_agent.jobs import ChatJobs, ConversationBusy


class Service:
    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.fail = False
        self.calls = 0

    def get(self, cid):
        if cid != 'a' * 32:
            raise FileNotFoundError('Unknown conversation')
        return {'id':cid,'messages':[]}

    def send(self, cid, text, on_event=None, **options):
        self.calls += 1
        on_event({'stage':'statistics','label':'Run survey statistics','status':'running','detail':'Waiting for the isolated test to finish','facts':{'n':1000}})
        self.started.set()
        self.release.wait(2)
        if self.fail:
            raise ValueError('PRIVATE_DETAIL_NOT_FOR_API')
        return {'conversation_id':cid,'message':{'content':'Complete','status':'ok'},'result':None}


class JobsTests(unittest.TestCase):
    def finish(self, jobs, jid):
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            value=jobs.get(jid)
            if value['status'] != 'running':return value
            threading.Event().wait(.005)
        self.fail('job did not terminate')

    def test_progress_can_be_resumed_without_duplicate_execution(self):
        service=Service();jobs=ChatJobs(service)
        submitted=jobs.submit('a'*32,'那女性呢')
        try:
            self.assertTrue(service.started.wait(1))
            self.assertEqual(jobs.active_job('a'*32),submitted['job_id'])
            live=jobs.get(submitted['job_id'])
            self.assertEqual(live['status'],'running')
            self.assertEqual(live['events'][0]['stage'],'statistics')
            live['events'][0]['facts']['n']=9
            self.assertEqual(jobs.get(submitted['job_id'])['events'][0]['facts']['n'],1000)
            with self.assertRaises(ConversationBusy):jobs.submit('a'*32,'再次发送')
            self.assertEqual(service.calls,1)
        finally:service.release.set()
        complete=self.finish(jobs,submitted['job_id'])
        self.assertEqual(complete['status'],'complete')
        self.assertEqual(complete['response']['message']['status'],'ok')
        self.assertIsNone(jobs.active_job('a'*32))

    def test_worker_failure_is_explicit_and_does_not_expose_internal_details(self):
        service=Service();service.fail=True;service.release.set();jobs=ChatJobs(service)
        submitted=jobs.submit('a'*32,'问一个问题')
        failed=self.finish(jobs,submitted['job_id'])
        self.assertEqual(failed['status'],'failed')
        self.assertNotIn('PRIVATE_DETAIL',str(failed))
        self.assertIsNone(jobs.active_job('a'*32))

    def test_invalid_messages_and_unknown_jobs_rejected(self):
        jobs=ChatJobs(Service())
        for message in ['', 'x'*2001,None]:
            with self.assertRaises(ValueError):jobs.submit('a'*32,message)
        with self.assertRaises(FileNotFoundError):jobs.submit('b'*32,'hello')
        with self.assertRaises(FileNotFoundError):jobs.get('b'*32)


if __name__ == '__main__':unittest.main()
