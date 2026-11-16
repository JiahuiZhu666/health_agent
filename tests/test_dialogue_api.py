"""Isolated integration checks; patched transport is not live API validation."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nhis_agent.conversation import ConversationService
from nhis_agent.engine import Agent


class DialogueApiTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'OPENAI_API_KEY':'', 'NHIS_MODEL_NAME':'', 'MODEL_NAME':''})
        self.environment.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.agent = Agent(Path(self.temporary.name))
        self.config = {'provider':'openai', 'base_url':'https://api.openai.com/v1',
                       'model':'unit-request-model', 'api_key':'UNIT_CREDENTIAL_NOT_A_REAL_KEY'}

    def tearDown(self):
        self.temporary.cleanup()
        self.environment.stop()

    def configured(self):
        self.agent.configure_model(self.config)

    @staticmethod
    def response(config, system, user, **kwargs):
        payload = json.loads(user)
        content = json.dumps({'blocks':[{'kind':'explanation','value':'Start with the recorded result, then explain the calculation.'}] +
                              [{'kind':'evidence','value':fact['id']} for fact in payload['facts']]}, ensure_ascii=False)
        return {'status':'ok', 'content':content, 'provider':'openai', 'model':'unit-returned-model',
                'response_id':'resp_unit_example', 'request_id':'req_unit_example',
                'usage':{'input_tokens':100,'output_tokens':30}, 'http_status':200,
                'elapsed_ms':12, 'message':''}

    def test_saving_credentials_does_not_claim_a_successful_connection(self):
        with patch('nhis_agent.chat_api.request') as request:
            self.configured()
        request.assert_not_called()
        status = self.agent.dialogue_status()
        self.assertTrue(status['configured'])
        self.assertEqual(status['connection']['status'], 'unverified')
        self.assertNotIn(self.config['api_key'], json.dumps(status))
        self.assertFalse(list(Path(self.temporary.name).rglob('.env')))

    def test_blank_key_preserved_for_same_endpoint_but_not_a_different_host(self):
        self.configured()
        self.agent.configure_model(dict(self.config, api_key=''))
        self.assertEqual(self.agent._model['api_key'], self.config['api_key'])
        self.agent.configure_model(dict(self.config, base_url='https://different.invalid/v1', api_key=''))
        self.assertFalse(self.agent.dialogue_status()['configured'])

    def test_connection_test_records_only_actual_safe_metadata(self):
        self.configured()
        response = dict(self.response(self.config, '', json.dumps({'facts':[]})), content='OK')
        with patch('nhis_agent.chat_api.test_connection', return_value=response) as request:
            result = self.agent.test_connection()
        request.assert_called_once()
        self.assertEqual(result['status'], 'ok')
        state = result['dialogue']['connection']
        self.assertEqual(state['model'], 'unit-returned-model')
        self.assertEqual(state['response_id'], 'resp_unit_example')
        self.assertIn('checked_at', state)
        self.assertNotIn('content', state)
        self.assertNotIn(self.config['api_key'], json.dumps(result))

    def test_normal_explanation_uses_api_with_blind_measurement_off(self):
        self.configured()
        service = ConversationService(self.agent)
        cid = service.create()['id']
        with patch('nhis_agent.chat_api.request', side_effect=self.response) as request, \
                patch('nhis_agent.models.measure') as measurement:
            result = service.send(cid, '95%置信区间是什么意思', measure_model=False)
        request.assert_called_once()
        measurement.assert_not_called()
        self.assertEqual(result['message']['generation']['status'], 'ok')
        self.assertEqual(result['message']['generation']['model'], 'unit-returned-model')
        self.assertIn('95%', result['message']['content'])
        self.assertIn('Start with the recorded result', result['message']['content'])
        self.assertIsNone(result['result'])
        sent = request.call_args.args[2]
        self.assertNotIn(self.config['api_key'], sent)
        stored = (Path(self.temporary.name)/'conversations'/(cid+'.json')).read_text()
        self.assertNotIn(self.config['api_key'], stored)
        self.assertIn('resp_unit_example', stored)

    def test_api_failure_is_visible_and_does_not_retry(self):
        self.configured()
        service = ConversationService(self.agent)
        cid = service.create()['id']
        failed = {'status':'failed','content':'', 'http_status':401,
                  'error_code':'authentication_error','message':'身份验证失败。'}
        with patch('nhis_agent.chat_api.request', return_value=failed) as request:
            result = service.send(cid, '你好')
        request.assert_called_once()
        self.assertEqual(result['message']['generation']['status'], 'failed')
        self.assertIn('conversation service', result['message']['content'])
        self.assertEqual(self.agent.dialogue_status()['connection']['status'], 'failed')

    def test_missing_configuration_is_never_reported_as_an_api_answer(self):
        service = ConversationService(self.agent)
        cid = service.create()['id']
        with patch('nhis_agent.chat_api.request') as request:
            result = service.send(cid, '你好')
        request.assert_not_called()
        self.assertEqual(result['message']['generation']['status'], 'not_configured')
        self.assertIn('conversation service', result['message']['content'])

    def test_altered_numbers_fail_validation_after_api_response(self):
        self.configured()
        invalid = {'status':'ok', 'content':json.dumps({'blocks':[{'kind':'explanation','value':'比例为99.9%'}]}),
                   'http_status':200,'response_id':'resp_observed'}
        with patch('nhis_agent.chat_api.request', return_value=invalid):
            result = self.agent.compose_reply('解释一下', 'The survey estimate is 20.00%.')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['api_status'], 'ok')
        self.assertEqual(result['response_id'], 'resp_observed')
        self.assertEqual(result['error_code'], 'answer_validation_failed')
        self.assertIsNone(result['content'])

    def test_registered_block_does_not_send_a_request_to_the_dialogue_api(self):
        self.configured()
        service = ConversationService(self.agent)
        cid = service.create()['id']
        with patch('nhis_agent.chat_api.request') as request:
            result = service.send(cid, 'Analyze NHIS health among children in China')
        request.assert_not_called()
        self.assertEqual(result['message']['status'], 'blocked')
        self.assertEqual(result['message']['generation']['status'], 'local')


if __name__ == '__main__':
    unittest.main()
