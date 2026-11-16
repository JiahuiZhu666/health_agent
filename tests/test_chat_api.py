"""Isolated transport tests only: these mocks do not verify any live account."""
import http.client
import io
import json
import socket
import tempfile
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from nhis_agent import chat_api, transport
from nhis_agent.budget import ModelBudget
from nhis_agent.telemetry import Telemetry


CONFIG = {'provider':'openai', 'base_url':'https://api.openai.com/v1',
          'model':'requested-test-model', 'api_key':'isolated-test-credential'}
SCHEMA = {'type':'object', 'properties':{'answer':{'type':'string'}},
          'required':['answer'], 'additionalProperties':False}


def response(content='Hello', **updates):
    value = {'id':'resp_actual_test', 'model':'returned-test-model', 'status':'completed',
             'output':[{'type':'reasoning','summary':[{'text':'DO_NOT_EXPOSE_REASONING'}]},
                       {'type':'message','role':'assistant','content':[{'type':'output_text','text':content}]}],
             'usage':{'input_tokens':12,'output_tokens':7,'total_tokens':19}}
    value.update(updates)
    return value


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status
        self.headers = Message()
        self.headers['x-request-id'] = 'req_actual_test'
        for name,value in (headers or {}).items():
            self.headers[name] = str(value)
        self.read_limit = None

    def getcode(self):
        return self.status

    def read(self, limit):
        self.read_limit = limit
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ChatAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        budget, telemetry = ModelBudget(temporary.name), Telemetry(temporary.name)
        for target, value in [('_budget_for', budget), ('_telemetry_for', telemetry)]:
            patcher = patch.object(transport, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
    def test_official_responses_payload_and_actual_metadata(self):
        fake = FakeResponse(response('实际回答'))
        with patch.object(chat_api,'_send',return_value=fake) as send:
            actual = chat_api.request(CONFIG,'system instruction','user question',max_output_tokens=3200)
        self.assertEqual(actual['status'],'ok')
        self.assertEqual(actual['content'],'实际回答')
        self.assertEqual(actual['model'],'returned-test-model')
        self.assertEqual(actual['response_id'],'resp_actual_test')
        self.assertEqual(actual['request_id'],'req_actual_test')
        self.assertEqual(actual['http_status'],200)
        self.assertEqual(actual['usage']['total_tokens'],19)
        req,timeout = send.call_args.args
        self.assertEqual(req.full_url,'https://api.openai.com/v1/responses')
        self.assertGreater(timeout,0)
        self.assertLessEqual(timeout,60)
        body=json.loads(req.data)
        self.assertEqual(body['instructions'],'system instruction')
        self.assertEqual(body['input'],'user question')
        self.assertFalse(body['store'])
        self.assertFalse(body['stream'])
        self.assertEqual(body['max_output_tokens'],3200)
        for name in ('temperature','tools','tool_choice','previous_response_id'):
            self.assertNotIn(name,body)
        self.assertEqual(fake.read_limit,chat_api.MAX_RESPONSE_BYTES+1)
        self.assertNotIn('DO_NOT_EXPOSE_REASONING',json.dumps(actual))
        self.assertNotIn(CONFIG['api_key'],json.dumps(actual))

    def test_strict_schema_official_and_compatible(self):
        with patch.object(chat_api,'_send',return_value=FakeResponse(response('{"answer":"ok"}'))) as send:
            chat_api.request(CONFIG,'system','question',schema=SCHEMA)
        format=json.loads(send.call_args.args[0].data)['text']['format']
        self.assertEqual(format,{'type':'json_schema','name':'nhis_agent_response','strict':True,'schema':SCHEMA})
        config=dict(CONFIG,base_url='https://compatible.example/v1/chat/completions',timeout=12)
        compatible={'id':'chat_actual','model':'compatible-returned',
                    'choices':[{'message':{'role':'assistant','content':'{"answer":"ok"}'},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':8,'completion_tokens':4,'total_tokens':12}}
        with patch.object(chat_api,'_send',return_value=FakeResponse(compatible)) as send:
            actual=chat_api.request(config,'system','question',SCHEMA)
        req,timeout=send.call_args.args
        self.assertEqual(req.full_url,config['base_url'])
        self.assertGreater(timeout,0)
        self.assertLessEqual(timeout,12)
        body=json.loads(req.data)
        self.assertEqual(body['response_format']['json_schema']['schema'],SCHEMA)
        self.assertTrue(body['response_format']['json_schema']['strict'])
        self.assertEqual(body['max_tokens'],3200)
        self.assertNotIn('max_output_tokens',body)
        self.assertEqual(actual['model'],'compatible-returned')
        self.assertEqual(actual['usage']['prompt_tokens'],8)

    def test_gpt_56_official_evidence_editing_uses_low_reasoning(self):
        for model in ('gpt-5.6-sol','gpt-5.6'):
            with self.subTest(model=model),patch.object(chat_api,'_send',return_value=FakeResponse(response())) as send:
                actual=chat_api.request(dict(CONFIG,model=model),'system','question',SCHEMA)
            self.assertEqual(actual['status'],'ok')
            body=json.loads(send.call_args.args[0].data)
            self.assertEqual(body['reasoning'],{'effort':'low'})
            self.assertEqual(body['max_output_tokens'],3200)
            self.assertNotIn('temperature',body)
        with patch.object(chat_api,'_send',return_value=FakeResponse(response())) as send:
            chat_api.request(dict(CONFIG,model='gpt-5.6-sol',reasoning_effort='medium'),'system','question')
        self.assertEqual(json.loads(send.call_args.args[0].data)['reasoning'],{'effort':'medium'})

    def test_gpt_56_connection_probe_uses_none_without_mutating_config(self):
        for model in ('gpt-5.6-sol','gpt-5.6'):
            config=dict(CONFIG,model=model,reasoning_effort='medium')
            with self.subTest(model=model),patch.object(chat_api,'_send',return_value=FakeResponse(response('OK'))) as send:
                actual=chat_api.test_connection(config)
            self.assertEqual(actual['status'],'ok')
            body=json.loads(send.call_args.args[0].data)
            self.assertEqual(body['reasoning'],{'effort':'none'})
            self.assertEqual(body['max_output_tokens'],128)
            self.assertEqual(config['reasoning_effort'],'medium')
            send.assert_called_once()

    def test_reasoning_option_not_sent_to_other_models_or_compatible_hosts(self):
        with patch.object(chat_api,'_send',return_value=FakeResponse(response())) as send:
            chat_api.test_connection(CONFIG)
        self.assertNotIn('reasoning',json.loads(send.call_args.args[0].data))
        config=dict(CONFIG,model='gpt-5.6-sol',base_url='https://compatible.example/v1')
        compatible={'choices':[{'message':{'content':'OK'},'finish_reason':'stop'}]}
        with patch.object(chat_api,'_send',return_value=FakeResponse(compatible)) as send:
            chat_api.test_connection(config)
        body=json.loads(send.call_args.args[0].data)
        self.assertNotIn('reasoning',body)
        self.assertNotIn('reasoning_effort',body)

    def test_invalid_supported_model_reasoning_option_never_sends(self):
        with patch.object(chat_api,'_send') as send:
            actual=chat_api.request(dict(CONFIG,model='gpt-5.6-sol',reasoning_effort='invalid'),'system','question')
        self.assertEqual(actual['error_code'],'invalid_config')
        send.assert_not_called()

    def test_ollama_is_explicit_no_authorization_and_actual_counts(self):
        config={'provider':'ollama','base_url':'http://127.0.0.1:11434/api','model':'local-model','timeout':120}
        payload={'model':'local-returned','done':True,'message':{'role':'assistant','content':'local answer'},
                 'prompt_eval_count':21,'eval_count':5,'total_duration':1000,'other_private_field':'ignored'}
        with patch.object(chat_api,'_send',return_value=FakeResponse(payload)) as send:
            actual=chat_api.request(config,'system','question',SCHEMA,max_output_tokens=700)
        req,timeout=send.call_args.args
        self.assertEqual(req.full_url,'http://127.0.0.1:11434/api/chat')
        self.assertGreater(timeout,0)
        self.assertLessEqual(timeout,60)
        self.assertIsNone(req.get_header('Authorization'))
        body=json.loads(req.data)
        self.assertEqual(body['format'],SCHEMA)
        self.assertEqual(body['options'],{'num_predict':700})
        self.assertEqual(actual['content'],'local answer')
        self.assertEqual(actual['model'],'local-returned')
        self.assertEqual(actual['usage'],{'prompt_eval_count':21,'eval_count':5,'total_duration':1000})

    def test_missing_configuration_never_attempts_transport(self):
        with patch.dict('os.environ',{},clear=True),patch.object(chat_api,'_send') as send:
            actual=chat_api.request({'model':'','api_key':''},'system','question')
        self.assertEqual(actual['status'],'not_configured')
        self.assertIsNone(actual['model'])
        send.assert_not_called()

    def test_invalid_configuration_and_request_do_not_leak_or_send(self):
        with patch.object(chat_api,'_send') as send:
            actual=chat_api.request(dict(CONFIG,base_url='http://remote.example/v1'),'system','question')
            self.assertEqual(actual['error_code'],'invalid_config')
            actual=chat_api.request(CONFIG,'system','question',max_output_tokens=True)
            self.assertEqual(actual['error_code'],'invalid_request')
        send.assert_not_called()

    def test_usage_is_a_numeric_allowlist_and_model_is_not_fabricated(self):
        usage={'input_tokens':12,'output_tokens':True,'total_tokens':-1,'private':'private data',
               'completion_tokens':10**400,
               'input_tokens_details':{'cached_tokens':4,'secret':'do not include'},
               'output_tokens_details':{'reasoning_tokens':3,'private':8}}
        payload=response(usage=usage)
        payload.pop('model');payload.pop('id')
        with patch.object(chat_api,'_send',return_value=FakeResponse(payload)):
            actual=chat_api.request(CONFIG,'system','question')
        self.assertEqual(actual['status'],'ok')
        self.assertIsNone(actual['model'])
        self.assertIsNone(actual['response_id'])
        self.assertEqual(actual['usage'],{'input_tokens':12,'input_tokens_details':{'cached_tokens':4},
                                          'output_tokens_details':{'reasoning_tokens':3}})

    def test_http_error_classified_without_reading_or_returning_error_body(self):
        class UnreadableErrorBody(io.BytesIO):
            def read(self,*args):
                raise AssertionError('Error body must not be read or logged')
        headers=Message();headers['x-request-id']='req_actual_error'
        error=urllib.error.HTTPError('https://api.openai.com/v1/responses',401,
              'UNSAFE_DETAIL '+CONFIG['api_key'],headers,UnreadableErrorBody(b'PRIVATE_RAW_BODY'))
        with patch.object(chat_api,'_send',side_effect=error) as send:
            actual=chat_api.request(CONFIG,'system','question')
        send.assert_called_once()
        self.assertEqual(actual['status'],'failed')
        self.assertEqual(actual['error_code'],'authentication_error')
        self.assertEqual(actual['http_status'],401)
        self.assertEqual(actual['request_id'],'req_actual_error')
        for private in ('UNSAFE_DETAIL','PRIVATE_RAW_BODY',CONFIG['api_key']):
            self.assertNotIn(private,json.dumps(actual))

    def test_network_timeout_have_one_attempt_and_static_errors(self):
        for error,code in [(urllib.error.URLError('private URL and credential'),'network_error'),
                           (socket.timeout('private details'),'timeout')]:
            with self.subTest(code=code),patch.object(chat_api,'_send',side_effect=error) as send:
                actual=chat_api.request(CONFIG,'system','question')
                self.assertEqual(actual['error_code'],code)
                self.assertNotIn('private',json.dumps(actual))
                send.assert_called_once()

    def test_response_size_and_json_limits(self):
        for fake,code in [(FakeResponse(b'x'*(chat_api.MAX_RESPONSE_BYTES+1)),'response_too_large'),
                          (FakeResponse(b'{}',headers={'Content-Length':chat_api.MAX_RESPONSE_BYTES+1}),'response_too_large'),
                          (FakeResponse(b'invalid JSON'),'invalid_response')]:
            with self.subTest(code=code),patch.object(chat_api,'_send',return_value=fake):
                actual=chat_api.request(CONFIG,'system','question')
                self.assertEqual(actual['error_code'],code)
                self.assertEqual(actual['content'],'')

    def test_interrupted_http_body_is_a_static_failure(self):
        fake=FakeResponse(response())
        with patch.object(fake,'read',side_effect=http.client.IncompleteRead(b'PRIVATE_PARTIAL_BODY')):
            with patch.object(chat_api,'_send',return_value=fake) as send:
                actual=chat_api.request(CONFIG,'system','question')
        self.assertEqual(actual['error_code'],'invalid_response')
        self.assertNotIn('PRIVATE_PARTIAL_BODY',json.dumps(actual))
        send.assert_called_once()

    def test_incomplete_refusal_or_tool_call_never_becomes_success(self):
        cases=[(response('TRUNCATED_TEXT',status='incomplete'),'incomplete_response'),
               (response(output=[{'type':'message','role':'assistant','content':[{'type':'refusal','refusal':'PRIVATE_REFUSAL'}]}]),'refusal'),
               ({'model':'compatible','choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{}]}}]},'unsupported_tool_call')]
        for payload,code in cases:
            config=CONFIG if 'output' in payload else dict(CONFIG,base_url='https://compatible.example/v1')
            with self.subTest(code=code),patch.object(chat_api,'_send',return_value=FakeResponse(payload)):
                actual=chat_api.request(config,'system','question')
                self.assertEqual(actual['error_code'],code)
                self.assertEqual(actual['content'],'')

    def test_secret_echo_is_redacted_from_generated_text_and_metadata(self):
        payload=response('Echo '+CONFIG['api_key'],model=CONFIG['api_key'])
        with patch.object(chat_api,'_send',return_value=FakeResponse(payload)):
            actual=chat_api.request(CONFIG,'system','question')
        self.assertNotIn(CONFIG['api_key'],json.dumps(actual))
        self.assertIn('[REDACTED]',actual['content'])

    def test_connection_uses_one_generation_and_checks_actual_ok(self):
        for content,status in [('OK','ok'),('not OK','failed')]:
            with self.subTest(content=content),patch.object(chat_api,'_send',return_value=FakeResponse(response(content))) as send:
                actual=chat_api.test_connection(CONFIG)
                self.assertEqual(actual['status'],status)
                self.assertEqual(actual['response_id'],'resp_actual_test')
                self.assertEqual(actual['request_id'],'req_actual_test')
                self.assertEqual(actual['usage']['total_tokens'],19)
                self.assertIn('Reply only OK',json.loads(send.call_args.args[0].data)['input'])
                send.assert_called_once()

    def test_redirects_do_not_forward_credentials(self):
        self.assertIsNone(chat_api._NoRedirect().redirect_request(None,None,302,'redirect',{},'https://other.example'))
        with patch.object(chat_api,'_send',return_value=FakeResponse({},status=302)) as send:
            actual=chat_api.request(CONFIG,'system','question')
        self.assertEqual(actual['error_code'],'redirect_refused')
        send.assert_called_once()


if __name__=='__main__':
    unittest.main()
