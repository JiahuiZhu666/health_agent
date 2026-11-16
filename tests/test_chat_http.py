import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nhis_agent.engine import Agent
from server import handler_for
from test_http import Connection


class ChatHttpTests(unittest.TestCase):
    def setUp(self):
        environment=patch.dict(os.environ, {'OPENAI_API_KEY':'','NHIS_MODEL_NAME':'','MODEL_NAME':''})
        environment.start()
        self.addCleanup(environment.stop)
        self.directory=tempfile.TemporaryDirectory()
        self.agent=Agent(Path(self.directory.name))
        self.handler=handler_for(self.agent)

    def tearDown(self):self.directory.cleanup()

    def request(self,method,path,value=None):
        body=json.dumps(value).encode() if value is not None else b''
        headers=[method+' '+path+' HTTP/1.0','Host: 127.0.0.1:8765']
        if value is not None:headers += ['Content-Type: application/json','Content-Length: '+str(len(body))]
        connection=Connection(('\r\n'.join(headers)+'\r\n\r\n').encode()+body)
        with contextlib.redirect_stderr(io.StringIO()):
            self.handler(connection,('127.0.0.1',1234),SimpleNamespace(server_port=8765))
        head,body=bytes(connection.output).split(b'\r\n\r\n',1)
        return int(head.split(b' ')[1]),json.loads(body)

    def wait_response(self,jid):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            status,body=self.request('GET','/api/jobs/'+jid)
            self.assertEqual(status,200)
            if body['status']!='running':return body
            threading.Event().wait(.005)
        self.fail('conversation job did not finish')

    def test_greeting_and_followup_messages_persist_through_async_api(self):
        status,conversation=self.request('POST','/api/conversations',{})
        self.assertEqual(status,201)
        cid=conversation['id']
        status,submitted=self.request('POST','/api/conversations/'+cid+'/messages',{'message':'你好'})
        self.assertEqual(status,202)
        done=self.wait_response(submitted['job_id'])
        self.assertEqual(done['status'],'complete')
        self.assertEqual(done['response']['message']['status'],'blocked')
        self.assertEqual(done['response']['message']['generation']['status'],'not_configured')
        self.assertIsNone(done['response']['result'])
        status,restored=self.request('GET','/api/conversations/'+cid)
        self.assertEqual(status,200)
        self.assertEqual(len(restored['messages']),2)
        self.assertIsNone(restored['active_job_id'])
        status,history=self.request('GET','/api/conversations')
        self.assertEqual(history['conversations'][0]['id'],cid)
        self.assertEqual(history['conversations'][0]['message_count'],2)
        _,submitted=self.request('POST','/api/conversations/'+cid+'/messages',{'message':'置信区间是什么意思'})
        response=self.wait_response(submitted['job_id'])['response']
        self.assertIn('95%',response['message']['content'])
        self.assertIsNone(response['result'])
        self.assertEqual(len(response['conversation']['messages']),4)

    def test_unknown_job_and_malformed_message_do_not_create_statistics(self):
        status,_=self.request('GET','/api/jobs/'+'f'*32)
        self.assertEqual(status,404)
        _,conversation=self.request('POST','/api/conversations',{})
        status,_=self.request('POST','/api/conversations/'+conversation['id']+'/messages',{'message':''})
        self.assertEqual(status,400)
        self.assertFalse(list((Path(self.directory.name)/'runs').iterdir()))

    def test_chat_artifact_uses_the_same_agent_workspace(self):
        _,conversation=self.request('POST','/api/conversations',{})
        _,submitted=self.request('POST','/api/conversations/'+conversation['id']+'/messages',
                                 {'message':'按教育比较自评健康'})
        response=self.wait_response(submitted['job_id'])['response']
        self.assertEqual(response['message']['status'],'blocked')
        run=response['result']['run_id']
        status,saved=self.request('GET','/api/runs/'+run)
        self.assertEqual(status,200)
        self.assertEqual(saved['run_id'],run)
        self.assertEqual(saved['status'],'blocked')


if __name__ == '__main__':unittest.main()
