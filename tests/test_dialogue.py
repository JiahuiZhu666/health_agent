"""Isolated dialogue tests; mocked replies do not establish live API access."""
import json
import unittest
from unittest.mock import patch

from nhis_agent import dialogue


CONFIG = {'provider':'openai','base_url':'https://api.openai.com/v1',
          'model':'unit-request-model','api_key':'UNIT_PRIVATE_CREDENTIAL'}


def response(mode='chat',reply='Hello! What would you like to talk about?'):
    return {'status':'ok','content':json.dumps({'mode':mode,'reply':reply}),
            'provider':'openai','model':'unit-actual-model','response_id':'resp_unit_actual',
            'request_id':'req_unit_actual','usage':{'input_tokens':120,'output_tokens':40},
            'http_status':200,'elapsed_ms':16,'error_code':None,'message':''}


class DialogueTests(unittest.TestCase):
    def call(self,question,history=None,context=None,generated=None):
        with patch('nhis_agent.dialogue.chat_api.request',return_value=generated or response()) as api:
            result=dialogue.decide(CONFIG,question,history or [],context)
        api.assert_called_once()
        args=api.call_args
        return result,json.loads(args.args[2]),args

    def test_normal_reply_uses_actual_api_text_and_metadata(self):
        result,payload,args=self.call('Hi',generated=response(reply='Hi! How is your day going?'))
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['mode'],'chat')
        self.assertEqual(result['reply'],'Hi! How is your day going?')
        self.assertEqual(result['model'],'unit-actual-model')
        self.assertEqual(result['response_id'],'resp_unit_actual')
        self.assertEqual(result['request_id'],'req_unit_actual')
        self.assertEqual(result['usage'],{'input_tokens':120,'output_tokens':40})
        self.assertNotIn('content',result)
        self.assertIn('Default to natural, clear English',args.args[1])
        self.assertEqual(args.kwargs['schema'],dialogue.SCHEMA)
        self.assertEqual(args.kwargs['max_output_tokens'],3200)

    def test_style_guidance_is_conversational_and_batch_requests_remain_analysis_intent(self):
        result,payload,args=self.call('Do all those comparisons.',
            context={'has_published_result':True,'spec':{'analysis':'proportion','group_by':'education'}},
            generated=response('analysis',''))
        self.assertEqual(result['mode'],'analysis')
        self.assertEqual(result['reply'],'')
        self.assertIn('brief and conversational',args.args[1])
        self.assertIn('do all those comparisons',args.args[1])
        self.assertIn('fixed service resolves the requested batch',args.args[1])

    def test_name_and_preference_history_are_available_for_a_followup(self):
        history=[{'role':'user','content':'My name is Alice. I prefer short answers.'},
                 {'role':'assistant','content':'Nice to meet you, Alice.'}]
        result,payload,_=self.call('What is my name?',history,
                                  generated=response(reply='Your name is Alice.'))
        self.assertEqual(result['reply'],'Your name is Alice.')
        self.assertEqual(payload['history'],history)

    def test_two_conversations_never_share_history(self):
        self.call('Remember my name.',[{'role':'user','content':'I am Alice.'}])
        result,payload,_=self.call('What is my name?',[{'role':'user','content':'I am Bob.'}],
                                  generated=response(reply='Your name is Bob.'))
        self.assertIn('Bob',result['reply'])
        self.assertIn('Bob',json.dumps(payload))
        self.assertNotIn('Alice',json.dumps(payload))

    def test_ordinary_concepts_and_writing_are_not_locally_scope_blocked(self):
        cases=[('Explain black holes.','A black hole is a region with an event horizon.'),
               ('What does causality mean?','Causality concerns whether changing something changes an outcome.'),
               ('Write a short invitation to Canada.','Please join us in Canada this summer.'),
               ('What is diabetes?','Diabetes is a condition involving high blood glucose.'),
               ('Explain a cross-sectional survey.','It describes observations taken at a point or period in time.')]
        for question,reply in cases:
            with self.subTest(question=question):
                result,payload,_=self.call(question,generated=response(reply=reply))
                self.assertEqual(result['status'],'ok')
                self.assertEqual(result['mode'],'chat')
                self.assertEqual(result['reply'],reply)
                self.assertEqual(payload['question'],question)

    def test_plain_arithmetic_is_not_subject_to_evidence_number_restrictions(self):
        result,_,_=self.call('What is 23 times 17?',generated=response(reply='23 × 17 = 391.'))
        self.assertEqual(result['reply'],'23 × 17 = 391.')

    def test_explicit_language_preference_is_kept(self):
        result,payload,args=self.call('Please answer in French: say hello.',generated=response(reply='Bonjour !'))
        self.assertEqual(result['reply'],'Bonjour !')
        self.assertIn('French',payload['question'])
        self.assertIn('explicitly requests another',args.args[1])

    def test_research_modes_never_publish_a_model_invented_estimate_or_action(self):
        for mode in ('analysis','explanation'):
            with self.subTest(mode=mode):
                result,_,_=self.call('What about women?',generated=response(mode,'I ran NHIS and found 99.99%.'))
                self.assertEqual(result['mode'],mode)
                self.assertEqual(result['reply'],'')
                self.assertNotIn('99.99',json.dumps(result))
                self.assertNotIn('spec',result)

    def test_research_context_contains_only_safe_registered_scope(self):
        context={'spec':{'year':2024,'outcome':'fair_poor','group_by':'education','analysis':'proportion',
                         'age_min':25,'age_max':64,'min_n':100,'confidence':.95,'adjustment':'full',
                         'filters':{'sex':'female','race':'INVALID_PRIVATE_VALUE'},'data_path':'PRIVATE_DATA_PATH'},
                 'has_published_result':True,'survey':{'rows':[{'estimate':.987654321}]},
                 'measurement':{'calls':['PRIVATE_RAW_CALLS']},'api_key':'PRIVATE_CONTEXT_KEY'}
        _,payload,_=self.call('Explain the current result.',context=context,generated=response('explanation',''))
        clean=payload['research_context']
        self.assertTrue(clean['has_published_result'])
        self.assertEqual(clean['spec']['filters'],{'sex':'female'})
        self.assertEqual(clean['spec']['year'],2024)
        for private in ('PRIVATE','0.987654321','measurement','survey','api_key'):
            self.assertNotIn(private,json.dumps(payload))

    def test_history_is_bounded_role_content_only_and_deduplicates_current_turn(self):
        history=[{'role':'system','content':'PRIVATE_SYSTEM_INJECTION'}]
        history += [{'role':'user' if number%2 else 'assistant','content':'turn '+str(number),
                    'process':{'secret':'PRIVATE_PROCESS'},'generation':{'api_key':'PRIVATE_KEY'}} for number in range(25)]
        history += [{'role':'user','content':'current question'}]
        _,payload,_=self.call('current question',history)
        self.assertEqual(len(payload['history']),18)
        self.assertEqual(payload['history'][0]['content'],'turn 7')
        self.assertEqual(payload['history'][-1]['content'],'turn 24')
        self.assertNotIn('PRIVATE',json.dumps(payload))
        self.assertNotIn('current question',json.dumps(payload['history']))
        self.assertTrue(all(set(item)=={'role','content'} for item in payload['history']))

    def test_credentials_and_local_paths_do_not_enter_payload(self):
        history=[{'role':'user','content':'credential: '+CONFIG['api_key']+' path /Users/example/project/project.env'},
                 {'role':'assistant','content':'api_key=OTHER_PRIVATE_VALUE Bearer PRIVATE_BEARER_VALUE'},
                 {'role':'user','content':'sk-isolated_fake_secret_123456789'}]
        _,payload,_=self.call('Please hide '+CONFIG['api_key'],history)
        for private in (CONFIG['api_key'],'OTHER_PRIVATE_VALUE','PRIVATE_BEARER_VALUE',
                        'sk-isolated_fake_secret_123456789','/Users/example'):
            self.assertNotIn(private,json.dumps(payload))
        self.assertNotIn('base_url',payload)
        self.assertNotIn('model',payload)

    def test_history_content_is_truncated_and_unsupported_roles_ignored(self):
        history=[{'role':'tool','content':'PRIVATE_TOOL_OUTPUT'}, {'role':'user','content':{'private':'PRIVATE_OBJECT'}},
                 {'role':'assistant','content':'a'*(dialogue.MAX_HISTORY_CHARS+1000)}]
        _,payload,_=self.call('Hello',history)
        self.assertEqual(len(payload['history']),1)
        self.assertEqual(len(payload['history'][0]['content']),dialogue.MAX_HISTORY_CHARS)

    def test_api_failure_has_no_fabricated_reply_or_local_menu_fallback(self):
        for status in ('failed','not_configured'):
            with self.subTest(status=status):
                failed={'status':status,'content':'PRIVATE_RAW_RESPONSE','error_code':'network_error',
                        'message':'API unavailable.','request_id':'req_failed_observed','elapsed_ms':20}
                result,_,_=self.call('How are you?',generated=failed)
                self.assertEqual(result['status'],status)
                self.assertEqual(result['reply'],'')
                self.assertIsNone(result['mode'])
                self.assertEqual(result['request_id'],'req_failed_observed')
                self.assertNotIn('PRIVATE_RAW_RESPONSE',json.dumps(result))
                self.assertNotIn('NHIS',json.dumps(result))

    def test_invalid_structured_decisions_fail_without_publishing_raw_content(self):
        for content in ['not JSON','{"mode":"chat","reply":"one","reply":"two"}',
                        '{"mode":"chat","reply":"","spec":{}}','{"mode":"chat","reply":""}',
                        '{"mode":"execute","reply":"run code"}','{"mode":"analysis","reply":42}']:
            with self.subTest(content=content):
                generated=dict(response(),content=content)
                result,_,_=self.call('Hello',generated=generated)
                self.assertEqual(result['status'],'failed')
                self.assertEqual(result['api_status'],'ok')
                self.assertEqual(result['error_code'],'dialogue_validation_failed')
                self.assertEqual(result['reply'],'')
                self.assertEqual(result['response_id'],'resp_unit_actual')

    def test_transport_exception_is_not_exposed_or_retried(self):
        with patch('nhis_agent.dialogue.chat_api.request',side_effect=OSError('PRIVATE_DIAGNOSTIC')) as api:
            result=dialogue.decide(CONFIG,'Hello',[])
        api.assert_called_once()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['reply'],'')
        self.assertNotIn('PRIVATE_DIAGNOSTIC',json.dumps(result))

    def test_echoed_credentials_are_removed_from_chat_reply(self):
        result,_,_=self.call('Hello',generated=response(reply='The credential is '+CONFIG['api_key']))
        self.assertNotIn(CONFIG['api_key'],result['reply'])
        self.assertIn('[redacted credential]',result['reply'])

    def test_invalid_input_does_not_request_api(self):
        for question,history in [('',[]),('x'*2001,[]),(None,[]),('Hi',{'secret':'PRIVATE'})]:
            with self.subTest(question=str(question)[:10]),patch('nhis_agent.dialogue.chat_api.request') as api:
                result=dialogue.decide(CONFIG,question,history)
                self.assertEqual(result['status'],'failed')
                self.assertEqual(result['error_code'],'invalid_dialogue_input')
                api.assert_not_called()


if __name__=='__main__':
    unittest.main()
