"""Conversation integration fixtures, not live model or survey results."""
import copy
import tempfile
import unittest

from test_conversation import FakeAgent
from nhis_agent.conversation import ConversationService


class ConversationalFixture(FakeAgent):
    def __init__(self, root):
        super().__init__(root)
        self.chat_calls = []
        self.compositions = []
        self.next_decision = None

    def decide_reply(self, question, history, research_context=None):
        self.chat_calls.append({'question':question,'history':copy.deepcopy(history),
                                'context':copy.deepcopy(research_context)})
        answer = self.next_decision or {'status':'ok','mode':'chat','reply':'Sure, let’s talk about that.'}
        self.next_decision = None
        return dict(answer, response_id='resp_unit_conversation', model='unit_fixture_model')

    def compose_reply(self, question, draft):
        self.compositions.append((question,draft))
        return {'status':'ok','content':draft,'response_id':'resp_unit_evidence','model':'unit_fixture_model'}


class GeneralConversationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.agent = ConversationalFixture(self.folder.name)
        self.service = ConversationService(self.agent)
        self.cid = self.service.create()['id']

    def tearDown(self):
        self.folder.cleanup()

    def send(self, text):
        return self.service.send(self.cid, text)

    def test_greeting_remembers_name_and_prior_assistant_reply(self):
        self.agent.next_decision={'status':'ok','mode':'chat','reply':'Nice to meet you, Alex.'}
        first=self.send('Hi, my name is Alex.')
        self.agent.next_decision={'status':'ok','mode':'chat','reply':'You told me your name is Alex.'}
        second=self.send('What is my name?')
        history=self.agent.chat_calls[-1]['history']
        self.assertEqual([m['role'] for m in history],['user','assistant'])
        self.assertIn('Alex',history[0]['content'])
        self.assertEqual(history[1]['content'],first['message']['content'])
        self.assertIn('Alex',second['message']['content'])
        self.assertEqual(second['message']['kind'],'chat')
        self.assertEqual(self.agent.calls,[])
        self.assertEqual(self.agent.compositions,[])

    def test_general_chat_can_discuss_topics_outside_the_survey_scope(self):
        for question in ['Help me write a friendly email to my cousin in Canada.',
                         'Explain black holes.', 'What does causality mean?',
                         'Explain diabetes in general terms.', 'I had a rough day. Can we talk?']:
            with self.subTest(question=question):
                reply=self.send(question)
                self.assertEqual(reply['message']['kind'],'chat')
                self.assertEqual(reply['message']['status'],'ok')
                self.assertNotIn('NHIS 2024',reply['message']['content'])
                self.assertIsNone(reply['result'])
        self.assertEqual(self.agent.calls,[])

    def test_ordinary_arithmetic_is_not_blocked_by_research_numeric_guards(self):
        self.agent.next_decision={'status':'ok','mode':'chat','reply':'2 + 2 = 4.'}
        reply=self.send('What is 2 + 2?')
        self.assertEqual(reply['message']['content'],'2 + 2 = 4.')
        self.assertEqual(reply['message']['generation']['status'],'ok')
        self.assertEqual(self.agent.compositions,[])

    def test_clear_research_and_followups_keep_the_fixed_statistical_flow(self):
        self.send('Compare self-rated health by education')
        female=self.send('What about women?')
        income=self.send('Switch to income')
        self.assertEqual(len(self.agent.calls),3)
        self.assertEqual(self.agent.calls[1]['group_by'],'education')
        self.assertEqual(self.agent.calls[1]['filters'],{'sex':'female'})
        self.assertEqual(self.agent.calls[2]['group_by'],'income')
        self.assertEqual(self.agent.calls[2]['filters'],{'sex':'female'})
        self.assertEqual(self.agent.chat_calls,[])
        self.assertEqual(female['message']['kind'],'analysis')
        self.assertEqual(income['message']['kind'],'analysis')

    def test_explicit_unregistered_research_cannot_be_routed_as_chat(self):
        reply=self.send('Calculate NHIS diabetes prevalence by education')
        self.assertEqual(reply['message']['status'],'blocked')
        self.assertEqual(self.agent.chat_calls,[])
        self.assertEqual(self.agent.calls,[])

    def test_explicit_survey_numbers_do_not_depend_on_a_compare_verb(self):
        for question in ['What percentage of NHIS 2024 adults report fair or poor health by education?',
                         'Show NHIS 2024 fair or poor health by education.']:
            with self.subTest(question=question):
                reply=self.send(question)
                self.assertEqual(reply['message']['kind'],'analysis')
                self.assertEqual(reply['message']['status'],'ok')
        self.assertEqual(self.agent.chat_calls,[])
        self.assertEqual(len(self.agent.calls),2)

    def test_ordinary_country_or_highest_topic_after_research_stays_chat(self):
        for question in ['What about Canada’s music scene?', 'What is the highest mountain in Canada?']:
            with self.subTest(question=question):
                self.send('Compare self-rated health by education')
                reply=self.send(question)
                self.assertEqual(reply['message']['kind'],'chat')
                self.assertEqual(reply['message']['status'],'ok')
                self.assertNotIn('run_id',reply['message'])
        self.assertEqual(len(self.agent.calls),2)

    def test_ambiguous_chat_reference_cannot_attach_old_research_on_bad_intent(self):
        self.send('Compare self-rated health by education')
        self.send('Tell me about black holes')
        self.agent.next_decision={'status':'ok','mode':'explanation','reply':''}
        reply=self.send('Explain that')
        self.assertEqual(reply['message']['kind'],'chat')
        self.assertEqual(reply['message']['status'],'clarify')
        self.assertEqual(reply['message']['source']['type'],'conversation')
        self.assertEqual(reply['message']['generation']['status'],'local')
        self.assertNotIn('run_id',reply['message'])
        self.assertIsNone(self.agent.chat_calls[-1]['context']['spec'])
        self.assertFalse(self.agent.chat_calls[-1]['context']['has_published_result'])
        self.assertEqual(len(self.agent.calls),1)

    def test_general_chat_preserves_research_scope_without_attaching_old_statistics(self):
        self.send('Compare self-rated health by education')
        previous=copy.deepcopy(self.service.get(self.cid)['context']['spec'])
        chat=self.send('Can you help me draft an email?')
        self.assertEqual(chat['conversation']['context']['spec'],previous)
        self.assertEqual(chat['message']['source']['type'],'conversation')
        self.assertNotIn('run_id',chat['message'])
        self.assertIsNone(chat['result'])
        self.send('Explain that')
        self.assertEqual(len(self.agent.chat_calls),2)
        self.assertEqual(len(self.agent.calls),1)

    def test_context_is_confined_to_one_conversation(self):
        self.send('My name is Alex.')
        other=self.service.create()['id']
        self.service.send(other,'What is my name?')
        self.assertEqual(self.agent.chat_calls[-1]['history'],[])
        self.assertIsNone(self.agent.chat_calls[-1]['context']['spec'])

    def test_general_chat_connection_failure_is_not_a_fake_menu_reply(self):
        self.agent.next_decision={'status':'failed','mode':None,'error_code':'network_error'}
        reply=self.send('Hello, can we talk?')
        self.assertEqual(reply['message']['status'],'blocked')
        self.assertEqual(reply['message']['generation']['status'],'failed')
        self.assertIn('conversation service',reply['message']['content'])
        self.assertNotIn('compare self-rated health',reply['message']['content'].lower())
        self.assertEqual(self.agent.calls,[])


if __name__ == '__main__':
    unittest.main()
