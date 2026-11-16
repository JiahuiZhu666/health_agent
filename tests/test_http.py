import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nhis_agent.engine import Agent
from server import handler_for, browser_payload


class Connection:
    def __init__(self, request):
        self.request = io.BytesIO(request)
        self.output = bytearray()
    def makefile(self, *args, **kwargs):
        return self.request
    def sendall(self, data):
        self.output.extend(data)


class HttpTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'OPENAI_API_KEY':'', 'NHIS_MODEL_NAME':'', 'MODEL_NAME':''})
        environment.start()
        self.addCleanup(environment.stop)

    def request(self, agent, method, path, value=None, host='127.0.0.1:8765', origin=None):
        payload = json.dumps(value).encode() if value is not None else b''
        headers = [f'{method} {path} HTTP/1.0', f'Host: {host}']
        if origin:
            headers.append('Origin: ' + origin)
        if value is not None:
            headers += ['Content-Type: application/json', 'Content-Length: ' + str(len(payload))]
        conn = Connection(('\r\n'.join(headers) + '\r\n\r\n').encode() + payload)
        with contextlib.redirect_stderr(io.StringIO()):
            handler_for(agent)(conn, ('127.0.0.1', 4567), SimpleNamespace(server_port=8765))
        raw = bytes(conn.output)
        header, body = raw.split(b'\r\n\r\n', 1)
        return header, body

    def test_ui_is_served_without_remote_dependencies(self):
        header, body = self.request(None, 'GET', '/')
        self.assertIn(b'200 OK', header)
        self.assertIn(b'Content-Security-Policy:', header)
        self.assertIn(b'<html', body)
        self.assertNotIn(b'cdn.', body)

    def test_cross_site_model_configuration_rejected(self):
        header, _ = self.request(None, 'POST', '/api/config', {}, origin='https://untrusted.invalid')
        self.assertIn(b'403 Forbidden', header)
        header, _ = self.request(None, 'GET', '/', host='untrusted.invalid:8765')
        self.assertIn(b'403 Forbidden', header)

    def test_data_directory_not_exposed(self):
        for path in ['/data/adult24.csv', '/.env', '/.cache/dialogue-connection-check.json', '/api/runs/../../.env']:
            header, _ = self.request(None, 'GET', path)
            self.assertIn(b'404 Not Found', header)

    def test_missing_real_data_is_honest_api_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            header, body = self.request(Agent(Path(temporary)), 'POST', '/api/ask', {'question':'按教育水平比较健康'})
        self.assertIn(b'200 OK', header)
        result = json.loads(body)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['survey']['rows'], [])
        self.assertNotIn('report',result['artifacts'])

    def test_browser_cannot_configure_or_probe_server_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(Path(temporary))
            with patch.object(agent, 'configure_model') as configure, patch.object(agent, 'test_connection') as probe:
                for path in ['/api/config', '/api/config/test']:
                    header, body = self.request(agent, 'POST', path, {'api_key':'UNIT_NOT_A_REAL_KEY'})
                    self.assertIn(b'405 Method Not Allowed', header)
                    self.assertNotIn(b'UNIT_NOT_A_REAL_KEY', body)
            configure.assert_not_called()
            probe.assert_not_called()

    def test_browser_status_does_not_reveal_backend_connection_parameters(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(Path(temporary))
            agent.configure_model({'provider':'openai','model':'unit-private-model',
                                   'base_url':'https://example.invalid/v1','api_key':'UNIT_NOT_A_REAL_KEY'})
            agent._dialogue_connection = {'status':'ok','model':'unit-private-model','request_id':'req_private',
                                         'response_id':'resp_private','usage':{'input_tokens':5}}
            header, body = self.request(agent, 'GET', '/api/status')
            self.assertIn(b'200 OK', header)
            for private in [b'UNIT_NOT_A_REAL_KEY',b'unit-private-model',b'example.invalid',
                            b'req_private',b'resp_private',b'base_url',b'input_tokens']:
                self.assertNotIn(private,body)
            self.assertEqual(json.loads(body)['dialogue']['connection']['status'], 'ok')

    def test_history_lists_only_registered_metadata_and_skips_partial_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(Path(temporary))
            run_id = '20261115T120000Z-123456789a'
            directory = Path(temporary) / 'runs' / run_id
            directory.mkdir()
            registration = {'question':'按教育水平比较健康', 'registered_at':'2026-11-15T12:00:00+00:00',
                            'spec':{'group_by':'education','analysis':'proportion'}}
            result = {'run_id':run_id, 'status':'ok', 'measurement':{'model':'test-model',
                      'calls':[{'raw_response':'PRIVATE_RESPONSE'}]}, 'api_key':'NOT_FOR_HISTORY'}
            (directory/'registration.json').write_text(json.dumps(registration))
            (directory/'result.json').write_text(json.dumps(result))
            broken = Path(temporary) / 'runs' / '20261115T130000Z-123456789b'
            broken.mkdir()
            (broken/'registration.json').write_text('{')
            header, body = self.request(agent,'GET','/api/runs')
            self.assertIn(b'200 OK',header)
            history = json.loads(body)['runs']
            self.assertEqual(len(history),1)
            self.assertEqual(history[0]['run_id'],run_id)
            self.assertNotIn('model',history[0])
            self.assertEqual(set(history[0]),{'run_id','question','status','group_by','analysis','created_at'})
            self.assertNotIn(b'PRIVATE_RESPONSE',body)
            self.assertNotIn(b'NOT_FOR_HISTORY',body)
            self.assertEqual(browser_payload(agent.history(limit=1)),history)
            self.assertEqual(agent.history(limit=1)[0]['model'],'test-model')
            with self.assertRaises(ValueError):
                agent.history(limit=True)


if __name__ == '__main__':
    unittest.main()
