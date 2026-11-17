"""Hosted HTTP security and deletion checks using in-memory connections only."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nhis_agent.engine import Agent
from nhis_agent.conversation import ConversationService
from server import handler_for
from test_http import Connection


class HostedHttpTests(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        environment=patch.dict(os.environ, {'OPENAI_API_KEY':'','NHIS_MODEL_NAME':'','MODEL_NAME':''})
        environment.start()
        self.addCleanup(environment.stop)
        self.agent=Agent(Path(self.folder.name))
        # /readyz observes readiness asynchronously in production. HTTP unit
        # fixtures supply an observation and must not launch an R child that
        # can retain the process-wide worker slot after the test returns.
        observation=patch.object(self.agent._statistics_health,'snapshot',return_value={
            'ready':False,'status':'blocked','observed_at':None,'stale':False,'refreshing':False})
        self.statistics_snapshot=observation.start()
        self.addCleanup(observation.stop)
        self.secret='UNIT_ACCESS_PASSWORD_NOT_A_REAL_SECRET_12345'
        self.handler=handler_for(self.agent, allowed_hosts=['health-agent.example'],
                                 allowed_origins=['https://health-agent.example'], auth_secret=self.secret)

    def request(self, method, path, *, authenticated=False, host='health-agent.example', origin=None, value=None):
        body=json.dumps(value).encode() if value is not None else b''
        headers=[f'{method} {path} HTTP/1.0',f'Host: {host}']
        if authenticated:
            headers.append('Authorization: Basic '+base64.b64encode(('owner:'+self.secret).encode()).decode())
        if origin:
            headers.append('Origin: '+origin)
        if value is not None:
            headers += ['Content-Type: application/json','Content-Length: '+str(len(body))]
        connection=Connection(('\r\n'.join(headers)+'\r\n\r\n').encode()+body)
        with contextlib.redirect_stderr(io.StringIO()):
            self.handler(connection,('127.0.0.1',1234),SimpleNamespace(server_port=8765))
        head,data=bytes(connection.output).split(b'\r\n\r\n',1)
        return int(head.split(b' ')[1]),head,data

    def test_hosted_workspace_requires_auth_before_reading_or_writing(self):
        for method,path,value in [('GET','/api/conversations',None),('POST','/api/conversations',{}),
                                  ('DELETE','/api/conversations',None),('GET','/',None)]:
            with self.subTest(method=method):
                status,head,data=self.request(method,path,value=value)
                self.assertEqual(status,401)
                self.assertIn(b'WWW-Authenticate: Basic',head)
                self.assertNotIn(self.secret.encode(),data)
        self.assertEqual(ConversationService(self.agent).list(),[])

    def test_liveness_does_not_require_login_or_probe_the_statistics_engine(self):
        with patch.object(self.agent,'status') as diagnostic:
            status,_,body=self.request('GET','/healthz',host='internal-health-probe')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body),{'status':'ok'})
        diagnostic.assert_not_called()

    def test_http_fixture_diagnostics_never_start_an_async_r_probe(self):
        with patch('nhis_agent.r_worker.run_probe') as probe:
            for path in ('/readyz','/api/status'):
                status,_,body=self.request('GET',path,authenticated=True)
                self.assertIn(status,(200,503))
                self.assertIn('statistics',json.loads(body))
            self.assertIsNone(self.agent._statistics_health._worker)
            probe.assert_not_called()
        self.assertEqual(self.statistics_snapshot.call_count,2)

    def test_authenticated_host_and_origin_are_both_enforced(self):
        status,_,body=self.request('POST','/api/conversations',authenticated=True,
                                   origin='https://health-agent.example',value={})
        self.assertEqual(status,201)
        cid=json.loads(body)['id']
        status,_,_=self.request('DELETE','/api/conversations/'+cid,authenticated=True,
                               origin='https://untrusted.example')
        self.assertEqual(status,403)
        self.assertEqual(len(ConversationService(self.agent).list()),1)
        status,_,_=self.request('GET','/api/conversations',authenticated=True,host='untrusted.example')
        self.assertEqual(status,403)

    def test_deletion_api_removes_messages_and_preserves_research_files(self):
        status,_,body=self.request('POST','/api/conversations',authenticated=True,value={})
        self.assertEqual(status,201)
        cid=json.loads(body)['id']
        run=Path(self.folder.name)/'runs'/'kept-record'
        run.mkdir()
        status,_,body=self.request('DELETE','/api/conversations/'+cid,authenticated=True)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body),{'deleted':[cid],'deleted_count':1})
        status,_,_=self.request('GET','/api/conversations/'+cid,authenticated=True)
        self.assertEqual(status,404)
        self.assertTrue(run.is_dir())

    def test_clear_api_is_idempotent_and_rejects_path_traversal(self):
        for _ in range(2):
            self.request('POST','/api/conversations',authenticated=True,value={})
        status,_,body=self.request('DELETE','/api/conversations',authenticated=True)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['deleted_count'],2)
        status,_,body=self.request('DELETE','/api/conversations',authenticated=True)
        self.assertEqual(json.loads(body)['deleted_count'],0)
        status,_,_=self.request('DELETE','/api/conversations/../../.env',authenticated=True)
        self.assertEqual(status,404)

    def test_readiness_reports_configuration_without_claiming_model_verification(self):
        status,_,body=self.request('GET','/readyz',authenticated=True)
        self.assertEqual(status,503)
        payload=json.loads(body)
        self.assertEqual(payload['status'],'not_configured')
        self.assertFalse(payload['chat']['configured'])
        self.assertEqual(payload['chat']['last_observed_status'],'not_configured')
        self.assertIn('observed_at',payload['statistics'])
        with patch.object(self.agent,'dialogue_status',return_value={'configured':True}):
            status,_,body=self.request('GET','/readyz',authenticated=True)
        self.assertEqual(status,200)
        payload=json.loads(body)
        self.assertEqual(payload['status'],'configured')
        self.assertEqual(payload['chat']['last_observed_status'],'unverified')
        self.assertIn('sends no model request',payload['probe'])


if __name__=='__main__':
    unittest.main()
