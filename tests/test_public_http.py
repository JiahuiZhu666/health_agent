"""Public visitor isolation and admission checks; no network or paid model calls."""
import contextlib
from http.cookies import SimpleCookie
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

from nhis_agent.conversation import ConversationService
from nhis_agent.access import PublicAccess
from nhis_agent.engine import Agent
from nhis_agent.workspaces import VisitorWorkspaces
from server import handler_for
from test_http import Connection


SECRET = 'UNIT_PUBLIC_SIGNING_MATERIAL_NOT_A_REAL_SECRET_12345'


class PublicHttpTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        environment = patch.dict(os.environ, {'OPENAI_API_KEY':'', 'NHIS_MODEL_NAME':'', 'MODEL_NAME':''})
        environment.start()
        self.addCleanup(environment.stop)
        self.agent = Agent(self.root)
        self.handler = self.factory()

    def factory(self, **limits):
        return handler_for(self.agent, allowed_hosts=['health.example'],
                           allowed_origins=['https://health.example'], auth_secret=SECRET,
                           public_access=True, public_limits=limits)

    def request(self, method, path, cookie=None, value=None, origin=None, handler=None):
        body = json.dumps(value).encode() if value is not None else b''
        headers = [f'{method} {path} HTTP/1.0', 'Host: health.example']
        if cookie:
            headers.append('Cookie: '+cookie)
        if origin:
            headers.append('Origin: '+origin)
        if value is not None:
            headers += ['Content-Type: application/json', 'Content-Length: '+str(len(body))]
        connection = Connection(('\r\n'.join(headers)+'\r\n\r\n').encode()+body)
        with contextlib.redirect_stderr(io.StringIO()):
            (handler or self.handler)(connection, ('127.0.0.1', 1234), SimpleNamespace(server_port=8765))
        head, data = bytes(connection.output).split(b'\r\n\r\n', 1)
        lines = head.decode().split('\r\n')
        response_headers = dict(line.split(': ', 1) for line in lines[1:])
        return int(lines[0].split(' ')[1]), response_headers, data

    def visitor(self):
        status, headers, _ = self.request('GET', '/')
        self.assertEqual(status, 200)
        cookie = SimpleCookie(headers['Set-Cookie'])
        name = '__Host-nhis_visitor'
        self.assertTrue(cookie[name]['secure'])
        self.assertTrue(cookie[name]['httponly'])
        self.assertEqual(cookie[name]['samesite'], 'Lax')
        return name+'='+cookie[name].value

    def create(self, cookie):
        status, _, body = self.request('POST', '/api/conversations', cookie, {})
        self.assertEqual(status, 201)
        return json.loads(body)['id']

    def completed(self, cookie, job_id):
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            status, _, body = self.request('GET', '/api/jobs/'+job_id, cookie)
            self.assertEqual(status, 200)
            job = json.loads(body)
            if job['status'] != 'running':
                self.assertEqual(job['status'], 'complete')
                return job
            threading.Event().wait(.005)
        self.fail('The isolated fixture job did not finish.')

    def test_landing_is_public_and_cookie_does_not_allocate_a_workspace(self):
        self.visitor()
        self.assertEqual(list((self.root/'visitors').iterdir()), [])
        status, headers, body = self.request('GET', '/healthz')
        self.assertEqual(status, 200)
        self.assertNotIn('WWW-Authenticate', headers)
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(json.loads(body), {'status':'ok'})

    def test_unsigned_tampered_or_missing_cookie_cannot_write(self):
        alice = self.visitor()
        cid = self.create(alice)
        for cookie in (None, '__Host-nhis_visitor='+'a'*32, alice[:-1]+('a' if alice[-1]!='a' else 'b')):
            for method, path, value in [('POST','/api/conversations',{}),
                                       ('DELETE','/api/conversations/'+cid,None)]:
                status, headers, body = self.request(method, path, cookie, value)
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body)['code'], 'cookies_required')
                self.assertNotIn('WWW-Authenticate', headers)
        self.assertEqual(self.request('GET','/api/conversations/'+cid,alice)[0], 200)

    def test_chat_read_write_delete_and_clear_are_scoped_even_with_known_ids(self):
        owner = ConversationService(self.agent).create()['id']
        alice, bob = self.visitor(), self.visitor()
        a, b = self.create(alice), self.create(bob)
        for cid in (b, owner):
            for method, path, value in [('GET','/api/conversations/'+cid,None),
                                       ('DELETE','/api/conversations/'+cid,None),
                                       ('POST','/api/conversations/'+cid+'/messages',{'message':'hello'})]:
                self.assertEqual(self.request(method,path,alice,value)[0], 404)
        _, _, body = self.request('GET','/api/conversations',alice)
        self.assertEqual([item['id'] for item in json.loads(body)['conversations']], [a])
        _, _, body = self.request('DELETE','/api/conversations',alice)
        self.assertEqual(json.loads(body)['deleted'], [a])
        self.assertEqual(self.request('GET','/api/conversations/'+b,bob)[0], 200)
        self.assertEqual(ConversationService(self.agent).get(owner)['id'], owner)

    def test_jobs_and_all_artifact_routes_are_scoped_to_the_visitor(self):
        alice, bob = self.visitor(), self.visitor()
        a = self.create(alice)
        self.create(bob)
        status, _, body = self.request('POST','/api/conversations/'+a+'/messages',alice,
                                      {'message':'Compare self-rated health by education'})
        self.assertEqual(status, 202)
        job_id = json.loads(body)['job_id']
        self.assertEqual(self.request('GET','/api/jobs/'+job_id,bob)[0], 404)
        job = self.completed(alice, job_id)
        run_id = job['response']['result']['run_id']
        root = next(path for path in (self.root/'visitors').iterdir() if (path/'runs'/run_id).exists())
        # Fixture exports exercise the actual authorization path; they are not
        # presented as released real survey output.
        (root/'runs'/run_id/'report.md').write_text('OWNED_FIXTURE_REPORT')
        (root/'runs'/run_id/'estimates.csv').write_text('OWNED_FIXTURE_CSV')
        result_path=root/'runs'/run_id/'result.json'
        fixture=json.loads(result_path.read_text())
        fixture['status']='ok'  # Explicitly simulate a released fixture.
        result_path.write_text(json.dumps(fixture))
        for suffix in ('','/report','/csv'):
            self.assertEqual(self.request('GET','/api/runs/'+run_id+suffix,alice)[0], 200)
            self.assertEqual(self.request('GET','/api/runs/'+run_id+suffix,bob)[0], 404)
        _, _, body = self.request('GET','/api/runs',bob)
        self.assertEqual(json.loads(body)['runs'], [])
        self.assertEqual(list((self.root/'runs').iterdir()), [])

    def test_rate_limits_apply_before_job_admission_and_survive_factory_restart(self):
        self.handler = self.factory(messages_per_hour=1, messages_per_day=2)
        alice, bob = self.visitor(), self.visitor()
        a, b = self.create(alice), self.create(bob)
        decision = {'status':'ok','mode':'chat','reply':'Hello from a fixture.'}
        with patch.object(Agent,'decide_reply',return_value=decision) as model:
            status, _, body = self.request('POST','/api/conversations/'+a+'/messages',alice,{'message':'Hi'})
            self.assertEqual(status, 202)
            self.completed(alice,json.loads(body)['job_id'])
            status, headers, body = self.request('POST','/api/conversations/'+a+'/messages',alice,{'message':'Hi again'})
            self.assertEqual(status, 429)
            self.assertGreater(int(headers['Retry-After']), 0)
            self.assertEqual(json.loads(body)['code'], 'rate_limited')
            self.assertEqual(model.call_count, 1)
            status, _, body = self.request('POST','/api/conversations/'+b+'/messages',bob,{'message':'Hi'})
            self.assertEqual(status, 202)
            self.completed(bob,json.loads(body)['job_id'])
            self.handler = self.factory(messages_per_hour=1,messages_per_day=2)
            charlie = self.visitor()
            c = self.create(charlie)
            status, _, _ = self.request('POST','/api/conversations/'+c+'/messages',charlie,{'message':'Hi'})
            self.assertEqual(status, 429)
            self.assertEqual(model.call_count, 2)

    def test_public_creation_limits_prevent_cookie_churn_and_do_not_charge_bad_messages(self):
        self.handler = self.factory(chats_per_hour=1,chats_per_day=2,messages_per_hour=1)
        alice = self.visitor()
        a = self.create(alice)
        self.assertEqual(self.request('POST','/api/conversations',alice,{})[0], 429)
        self.create(self.visitor())
        self.assertEqual(self.request('POST','/api/conversations',self.visitor(),{})[0], 429)
        self.assertEqual(len(list((self.root/'visitors').iterdir())), 2)
        for _ in range(2):
            self.assertEqual(self.request('POST','/api/conversations/'+a+'/messages',alice,{'message':''})[0], 400)
        status, _, body = self.request('POST','/api/conversations/'+a+'/messages',alice,{'message':'Hi'})
        self.assertEqual(status, 202)
        self.completed(alice,json.loads(body)['job_id'])

    def test_failed_quota_commit_never_starts_a_model_request(self):
        alice = self.visitor()
        a = self.create(alice)
        connection = sqlite3.connect(str(self.root/'.cache'/'public-access.sqlite3'), isolation_level=None)
        connection.row_factory=sqlite3.Row
        broken = MagicMock(wraps=connection)
        broken.commit.side_effect = sqlite3.OperationalError('Simulated quota commit failure')
        with patch.object(PublicAccess,'_connect',return_value=broken), \
             patch.object(Agent,'decide_reply') as model:
            status, _, _ = self.request('POST','/api/conversations/'+a+'/messages',alice,{'message':'Hello'})
            self.assertEqual(status, 500)
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                _, _, body = self.request('GET','/api/conversations/'+a,alice)
                if json.loads(body)['active_job_id'] is None:
                    break
                threading.Event().wait(.005)
            else:
                self.fail('A rejected job retained its admission capacity.')
            model.assert_not_called()
            self.assertEqual(json.loads(body)['messages'], [])
            broken.rollback.assert_called_once()
        # Failed admission leaves neither a paid request nor a charged event.
        with sqlite3.connect(str(self.root/'.cache'/'public-access.sqlite3')) as verify:
            self.assertEqual(verify.execute("SELECT COUNT(*) FROM accepted_events WHERE kind='message'").fetchone()[0], 0)

    def test_owner_only_routes_and_cross_origin_calls_cannot_bypass_limits(self):
        alice = self.visitor()
        a = self.create(alice)
        with patch.object(self.agent,'ask') as legacy, patch.object(self.agent,'setup_data') as setup:
            for path,value in [('/api/ask',{'question':'Hi'}),('/api/setup-data',{}),
                               ('/api/conversations/'+a+'/messages',{'message':'Hi','measure_model':True})]:
                self.assertEqual(self.request('POST',path,alice,value)[0], 403)
            legacy.assert_not_called()
            setup.assert_not_called()
        self.assertEqual(self.request('DELETE','/api/conversations',alice,origin='https://evil.example')[0], 403)
        self.assertEqual(self.request('GET','/api/conversations/'+a,alice)[0], 200)

    def test_public_status_hides_owner_details_and_bounds_statistical_probes(self):
        alice, bob = self.visitor(), self.visitor()
        private = {'data':{'ready':False,'file':'/PRIVATE/FILE','message':'/PRIVATE/ERROR'},
                   'statistics':{'ready':False,'rscript':'/PRIVATE/R','reason':'/PRIVATE/R_ERROR'},
                   'dialogue':{'configured':True,'model':'PRIVATE_MODEL','api_key':'PRIVATE_KEY'},
                   'project':'/PRIVATE/PROJECT'}
        with patch.object(self.agent,'status',return_value=private) as probe:
            _, _, first = self.request('GET','/api/status',alice)
            _, _, second = self.request('GET','/api/status',bob)
        self.assertEqual(probe.call_count, 1)
        self.assertNotIn(b'PRIVATE', first)
        self.assertNotIn(SECRET.encode(), first)
        self.assertTrue(json.loads(first)['access']['public'])
        self.assertNotEqual(json.loads(first)['access']['scope'],json.loads(second)['access']['scope'])


class VisitorWorkspaceTests(unittest.TestCase):
    def test_survey_uses_shared_verified_input_and_writes_only_the_visitor_run(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = Agent(Path(folder))
            visitor_root = agent.root/'visitors'/('a'*32)
            scoped = agent.workspace(visitor_root)
            with patch('nhis_agent.engine.data.status',return_value={'ready':True,'row_count':32629}) as data_status, \
                 patch('nhis_agent.engine.SurveyCache.identity',return_value=None), \
                 patch('nhis_agent.engine.stats.analyze',return_value={'status':'blocked','reason':'Isolated statistical fixture','rows':[]}) as analyze:
                result = scoped.ask('Compare self-rated health by education')
            data_status.assert_called_once_with(agent.root)
            self.assertEqual(analyze.call_args.args[0],str(agent.root/'data'/'adult24.csv'))
            self.assertEqual(Path(analyze.call_args.args[2]),visitor_root/'runs'/result['run_id'])
            self.assertTrue((visitor_root/'runs'/result['run_id']/'result.json').is_file())
            self.assertEqual(list((agent.root/'runs').iterdir()), [])

    def test_shared_official_inputs_do_not_share_conversations_runs_or_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = Agent(Path(folder))
            pool = VisitorWorkspaces(agent, maximum=1)
            a, b = 'a'*32, 'b'*32
            first = pool.acquire(a,create=True)
            self.assertEqual(first.agent.data_root, agent.root)
            self.assertEqual(first.agent.root, agent.root/'visitors'/a)
            cid = first.conversations.create()['id']
            with self.assertRaises(ValueError):
                pool.acquire('../../runs',create=True)
            from nhis_agent.jobs import ConversationBusy
            with self.assertRaises(ConversationBusy):
                pool.acquire(b,create=True)
            self.assertFalse((agent.root/'visitors'/b).exists())
            pool.release(a)
            second = pool.acquire(b,create=True)
            self.assertEqual(second.conversations.list(), [])
            pool.release(b)
            restored = pool.acquire(a)
            self.assertEqual(restored.conversations.get(cid)['id'],cid)
            pool.release(a)

    def test_global_job_capacity_is_shared_between_visitor_workspaces(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = VisitorWorkspaces(Agent(Path(folder)), concurrent=1)
            first = pool.acquire('a'*32,create=True)
            second = pool.acquire('b'*32,create=True)
            a, b = first.conversations.create()['id'], second.conversations.create()['id']
            entered, finish = threading.Event(), threading.Event()
            def blocked(*args, **kwargs):
                entered.set()
                finish.wait(3)
                return {'fixture':True}
            with patch.object(first.conversations,'send',side_effect=blocked):
                job = first.jobs.submit(a,'Hello')
                self.assertTrue(entered.wait(2))
                from nhis_agent.jobs import ConversationBusy
                try:
                    with self.assertRaises(ConversationBusy):
                        second.jobs.submit(b,'Hello')
                finally:
                    finish.set()
                deadline = time.monotonic()+3
                while first.jobs.has_active() and time.monotonic()<deadline:
                    threading.Event().wait(.005)
                self.assertEqual(first.jobs.get(job['job_id'])['status'],'complete')
            self.assertFalse(first.jobs.has_active())
            pool.release('a'*32)
            pool.release('b'*32)


if __name__ == '__main__':
    unittest.main()
