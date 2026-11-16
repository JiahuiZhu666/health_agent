"""Chart and overview turns use released conversation evidence, never fake plots."""
import copy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nhis_agent.conversation import ConversationService
from test_general_conversation import ConversationalFixture


class ConversationChartTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.agent = ConversationalFixture(self.folder.name)
        self.service = ConversationService(self.agent)
        self.cid = self.service.create()['id']

    def send(self,text,**kwargs):
        return self.service.send(self.cid,text,**kwargs)

    def discussion(self):
        self.send('Compare self-rated health by education')
        self.send('Switch to income')
        self.send('What about women?')

    def test_exact_reported_all_results_plot_request_returns_three_owned_panels(self):
        self.discussion()
        before = copy.deepcopy(self.service.get(self.cid)['context'])
        compositions = len(self.agent.compositions)
        reply = self.send('give me a whole plot of all results we have discussed',measure_model=True)
        self.assertEqual(reply['message']['kind'],'visualization')
        self.assertEqual(reply['message']['status'],'ok')
        charts = reply['message']['charts']
        self.assertEqual(len(charts),3)
        self.assertEqual([chart['groups'][0]['value'] for chart in charts],[20,20,30])
        self.assertIn('Female',charts[2]['subtitle'])
        self.assertNotIn('Female',charts[1]['subtitle'])
        self.assertEqual(len(self.agent.calls),3)
        self.assertEqual(self.agent.chat_calls,[])
        self.assertEqual(len(self.agent.compositions),compositions)
        self.assertEqual(reply['message']['generation']['type'],'visualization')
        self.assertEqual(reply['conversation']['context'],before)
        self.assertIsNone(reply['result'])
        self.assertIn('no survey calculations',reply['message']['content'])
        stored = self.service.get(self.cid)['messages'][-1]
        self.assertEqual(stored['charts'],charts)

    def test_followup_some_plot_and_bar_chart_do_not_recalculate_or_reprompt(self):
        self.discussion()
        for text in ('give me some plot','Show bar charts','把我们讨论过的所有结果画成图'):
            with self.subTest(text=text):
                reply = self.send(text)
                self.assertEqual(reply['message']['status'],'ok')
                self.assertEqual(len(reply['message']['charts']),3)
                self.assertNotIn('specify',reply['message']['content'].lower())
                if 'bar' in text:
                    self.assertTrue(all(chart['style']=='bar' for chart in reply['message']['charts']))
        self.assertEqual(len(self.agent.calls),3)

    def test_latest_and_named_older_result_are_selected_without_current_scope_substitution(self):
        self.discussion()
        latest = self.send('Plot the latest result')['message']['charts']
        self.assertEqual(len(latest),1)
        self.assertIn('Female',latest[0]['subtitle'])
        named = self.send('Plot the education results')['message']['charts']
        self.assertEqual(len(named),1)
        self.assertIn('education',named[0]['title'])
        self.assertNotIn('Female',named[0]['subtitle'])
        self.assertEqual(len(self.agent.calls),3)

    def test_no_result_chart_request_uses_the_users_prior_named_study_topic(self):
        self.send('受教育程度和自评健康之间的关系')
        self.assertEqual(self.agent.calls,[])
        self.assertEqual(len(self.agent.chat_calls),1)
        reply = self.send('给一些可视化的图表，我想看看详细的数据结果和相关性')
        self.assertEqual(reply['message']['kind'],'visualization')
        self.assertEqual(len(self.agent.calls),1)
        self.assertEqual(self.agent.calls[-1]['group_by'],'education')
        self.assertEqual(len(reply['message']['charts']),1)
        self.assertIn('verified survey calculation',reply['message']['content'])
        self.assertNotIn('no survey calculations',reply['message']['content'])

    def test_empty_conversation_plot_offers_actionable_start_and_all_continues(self):
        reply = self.send('give me some plot')
        self.assertEqual(reply['message']['status'],'clarify')
        self.assertNotIn('charts',reply['message'])
        self.assertEqual(self.agent.calls,[])
        self.assertEqual(reply['conversation']['context']['pending']['field'],'comparisons')
        reply = self.send('全部做一遍，我不知道该specify哪个')
        self.assertEqual(reply['message']['status'],'ok')
        self.assertEqual([spec['group_by'] for spec in self.agent.calls],['education','income','sex','race','age'])
        self.assertEqual(len(reply['message']['charts']),5)
        self.assertEqual(len(reply['message']['run_ids']),5)

    def test_all_comparisons_from_a_study_context_execute_each_once(self):
        self.send('Compare self-rated health by education')
        for text in ('都做一遍','Do all those comparisons','Compare all groups'):
            before = len(self.agent.calls)
            reply = self.send(text)
            self.assertEqual(len(self.agent.calls)-before,5)
            self.assertEqual(reply['message']['status'],'ok')
            self.assertEqual(len(reply['message']['charts']),5)
        before = len(self.agent.calls)
        reply = self.send('Plot all results we have discussed')
        self.assertEqual(len(self.agent.calls),before)
        self.assertEqual(len(reply['message']['charts']),12)
        self.assertIn('older results',reply['message']['content'])

    def test_batch_preserves_and_applies_the_requested_population_filters(self):
        reply = self.send('Compare all NHIS self-rated health group comparisons for women')
        self.assertEqual(reply['message']['status'],'ok')
        self.assertEqual(len(self.agent.calls),5)
        self.assertTrue(all(spec['filters']=={'sex':'female'} for spec in self.agent.calls))
        self.assertTrue(all('Female' in chart['subtitle'] for chart in reply['message']['charts']))

    def test_unmentioned_conversation_runs_are_never_included(self):
        self.discussion()
        other = self.service.create()['id']
        self.service.send(other,'Compare self-rated health by age')
        reply = self.send('Plot all results we have discussed')
        self.assertEqual(len(reply['message']['charts']),3)
        self.assertEqual(len(reply['message']['run_ids']),3)

    def test_failed_latest_run_is_never_plotted_and_old_results_retain_their_scopes(self):
        self.send('Compare self-rated health by education')
        self.agent.next_status='blocked'
        self.send('Switch to income')
        reply = self.send('Plot all results we have discussed')
        self.assertEqual(len(reply['message']['charts']),1)
        self.assertIn('education',reply['message']['charts'][0]['title'])
        self.assertNotIn('income',reply['message']['charts'][0]['title'])
        self.assertEqual(len(self.agent.calls),2)
        latest = self.send('Plot the latest result')
        self.assertEqual(latest['message']['status'],'blocked')
        self.assertNotIn('charts',latest['message'])

    def test_explanation_of_several_charts_keeps_all_their_sources(self):
        self.discussion()
        plotted = self.send('Plot all results we have discussed')
        sources = plotted['message']['run_ids']
        reply = self.send('Explain these charts')
        self.assertEqual(reply['message']['status'],'ok')
        self.assertEqual(reply['message']['run_ids'],sources)
        self.assertEqual(len(self.agent.calls),3)
        self.assertIn('Education',reply['message']['content'])
        self.assertIn('Income',reply['message']['content'])
        self.assertIn('Female',reply['message']['content'])

    def test_explicit_filter_clearing_does_not_keep_the_old_filtered_charts(self):
        self.send('Compare self-rated health by education')
        self.send('What about women?')
        for text in ('Remove the sex filter and plot education','Plot education with no filters'):
            reply = self.send(text)
            self.assertEqual(reply['message']['status'],'ok')
            self.assertEqual(len(reply['message']['charts']),1)
            self.assertNotIn('Female',reply['message']['charts'][0]['subtitle'])

    def test_new_age_filter_requires_a_matching_result_not_an_old_population_chart(self):
        self.discussion()
        reply = self.send('Plot education among ages 25-34')
        self.assertEqual(len(self.agent.calls),4)
        self.assertEqual(self.agent.calls[-1]['filters'],{'age':'25_34'})
        self.assertEqual(len(reply['message']['charts']),1)
        self.assertIn('Ages 25–34',reply['message']['charts'][0]['subtitle'])

    def test_implicit_chart_group_keeps_education_and_applies_new_sex_filter(self):
        self.send('Compare self-rated health by education')
        reply=self.send('Plot results for women')
        self.assertEqual(len(self.agent.calls),2)
        self.assertEqual(self.agent.calls[-1]['group_by'],'education')
        self.assertEqual(self.agent.calls[-1]['filters'],{'sex':'female'})
        self.assertEqual(len(reply['message']['charts']),1)
        self.assertIn('Female',reply['message']['charts'][0]['subtitle'])

    def test_implicit_chart_group_cannot_reuse_an_old_age_domain(self):
        self.send('Compare self-rated health by education')
        reply=self.send('Plot results for ages 25-34')
        self.assertEqual(len(self.agent.calls),2)
        self.assertEqual(self.agent.calls[-1]['group_by'],'education')
        self.assertEqual(self.agent.calls[-1]['filters'],{'age':'25_34'})
        self.assertEqual(len(reply['message']['charts']),1)
        self.assertIn('Ages 25–34',reply['message']['charts'][0]['subtitle'])

    def test_named_chart_population_cannot_borrow_a_narrower_extra_filter(self):
        self.send('Compare self-rated health by education')
        self.send('Plot education among women ages 25-34')
        self.assertEqual(self.agent.calls[-1]['filters'],{'sex':'female','age':'25_34'})
        reply=self.send('Plot education for women')
        self.assertEqual(len(self.agent.calls),3)
        self.assertEqual(self.agent.calls[-1]['filters'],{'sex':'female'})
        self.assertEqual(len(reply['message']['charts']),1)
        self.assertIn('Female',reply['message']['charts'][0]['subtitle'])
        self.assertNotIn('Ages 25–34',reply['message']['charts'][0]['subtitle'])

    def test_batch_merges_a_new_age_filter_with_the_current_population(self):
        self.send('Compare self-rated health by education')
        self.send('What about women?')
        before=len(self.agent.calls)
        reply=self.send('Compare all groups for ages 25-34')
        self.assertEqual(len(self.agent.calls)-before,5)
        for spec in self.agent.calls[before:]:
            self.assertEqual(spec['filters'],{'sex':'female','age':'25_34'})
        self.assertTrue(all('Female' in chart['subtitle'] and 'Ages 25–34' in chart['subtitle']
                            for chart in reply['message']['charts']))

    def test_quoted_instruction_and_negated_requests_do_not_start_or_plot_research(self):
        questions=['Translate "compare all groups" into French.',
                   'Explain the instruction "do all those comparisons".',
                   'Do not compare all groups; just summarize what we have.',
                   'Translate "plot all results" into French.',
                   'Do not plot the results; help me write a caption instead.']
        for question in questions:
            with self.subTest(question=question):
                self.send('Compare self-rated health by education')
                before=len(self.agent.calls)
                chat_before=len(self.agent.chat_calls)
                reply=self.send(question)
                self.assertEqual(len(self.agent.calls),before)
                self.assertEqual(len(self.agent.chat_calls),chat_before+1)
                self.assertEqual(reply['message']['kind'],'chat')
                self.assertNotIn('charts',reply['message'])

    def test_partially_failed_batch_keeps_last_successful_scope_for_followups(self):
        self.send('Compare self-rated health by education')
        self.send('What about women?')
        original=self.agent.analyze_spec
        def fail_age(spec,*args,**kwargs):
            if spec['group_by']=='age':self.agent.next_status='blocked'
            return original(spec,*args,**kwargs)
        with patch.object(self.agent,'analyze_spec',side_effect=fail_age):
            reply=self.send('Compare all groups')
        self.assertEqual(reply['message']['status'],'ok')
        self.assertEqual(len(reply['message']['charts']),4)
        self.assertIn('Some comparisons did not publish',reply['message']['content'])
        self.assertEqual(reply['conversation']['context']['spec']['group_by'],'race')
        self.assertEqual(reply['conversation']['context']['spec']['filters'],{'sex':'female'})
        self.assertNotIn(reply['conversation']['context']['last_attempt_run_id'],
                         [chart['source_run_id'] for chart in reply['message']['charts']])
        self.send('What about men?')
        self.assertEqual(self.agent.calls[-1]['group_by'],'race')
        self.assertEqual(self.agent.calls[-1]['filters'],{'sex':'male'})

    def test_wholly_failed_batch_does_not_replace_the_successful_context(self):
        saved=self.send('Compare self-rated health by education')['conversation']
        original=self.agent.analyze_spec
        def fail_every_group(spec,*args,**kwargs):
            self.agent.next_status='blocked'
            return original(spec,*args,**kwargs)
        with patch.object(self.agent,'analyze_spec',side_effect=fail_every_group):
            reply=self.send('Compare all groups')
        self.assertEqual(reply['message']['status'],'blocked')
        self.assertNotIn('charts',reply['message'])
        self.assertEqual(reply['conversation']['context']['spec'],saved['context']['spec'])
        self.assertEqual(reply['conversation']['last_run_id'],saved['last_run_id'])

    def test_art_and_ordinary_chat_after_research_still_use_the_conversation_api(self):
        self.discussion()
        self.send('Write a story with a plot twist about a cat.')
        self.send('Do all those comparisons')
        self.assertEqual(len(self.agent.calls),3)
        self.assertEqual(len(self.agent.chat_calls),2)
        self.assertEqual(self.service.get(self.cid)['messages'][-1]['kind'],'chat')

    def test_new_unregistered_populations_or_arbitrary_age_ranges_cannot_borrow_old_charts(self):
        self.discussion()
        for text in ('Plot NHIS results for Canada','Plot education for ages 20-30','Plot diabetes by education'):
            reply = self.send(text)
            self.assertIn(reply['message']['status'],('blocked','clarify'))
            self.assertNotIn('charts',reply['message'])
        self.assertEqual(len(self.agent.calls),3)


if __name__=='__main__':
    unittest.main()
