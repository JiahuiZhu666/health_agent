"""Isolated medical-access conversation replay, never product survey estimates.

The agent below returns explicit aggregate fixtures. No .env, model service,
download, R process, or real conversation records are used by these checks.
"""
import copy
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_general_conversation import ConversationalFixture
from nhis_agent.conversation import ConversationService
from nhis_agent.outcomes import ACCESS_OUTCOMES, get_outcome
from nhis_agent.spec import route, requested_outcomes
from nhis_agent import stats


QUESTION='有无医疗保险、是否能及时就医等医疗服务指标如何分布？'
COUNTS={'insured':1500,'uninsured':1500,'delayed_care_cost':1300,'unmet_care_cost':1200,'fair_poor':1400}


class AccessFixture(ConversationalFixture):
    def __init__(self,root):
        super().__init__(root)
        self.saved={}
        self.blocked_outcomes=set()
        self.measurement_options=[]

    def analyze_spec(self,spec,question,measure_model=False,repeats=5,prompt_versions=3,on_progress=None):
        self.calls.append(copy.deepcopy(spec))
        self.measurement_options.append(measure_model)
        identifier='20261116T120000Z-{:010x}'.format(len(self.calls))
        outcome=spec['outcome'];definition=get_outcome(outcome)
        status='blocked' if outcome in self.blocked_outcomes else 'ok'
        count=COUNTS[outcome]
        if spec['group_by']=='overall':ids=['overall']
        elif spec['group_by']=='income':ids=['below_100','400_plus']
        else:ids=['less_than_high_school','bachelors_plus']
        rows=[]
        for index,group_id in enumerate(ids):
            uninsured=.2+index*.03
            estimate=1-uninsured if outcome=='insured' else uninsured if outcome=='uninsured' else .15+index*.02 if outcome=='delayed_care_cost' else .1+index*.02 if outcome=='unmet_care_cost' else .25+index*.02
            n=count if len(ids)==1 else count//2
            rows.append({'group_id':group_id,'label':'Isolated fixture group','status':'ok','n':n,
                         'estimate':estimate,'ci_low':estimate-.03,'ci_high':estimate+.03,'se':.01})
        result={'run_id':identifier,'status':status,'spec':copy.deepcopy(spec),
                'answer':'Isolated released fixture.' if status=='ok' else 'No releasable estimate for this fixture.',
                'survey':{'status':status,'outcome':definition,'rows':rows if status=='ok' else [],
                          'denominators':{'full_sample_n':3000,'age_domain_n':2000,'filtered_domain_n':1800,
                                          'valid_outcome_n':count},
                          'imputation':{'required':spec['group_by']=='income','m':10 if spec['group_by']=='income' else 1}},
                'measurement':{'status':'not_requested'},'comparison':[],'process':[]}
        directory=self.root/'runs'/identifier;directory.mkdir(parents=True)
        (directory/'result.json').write_text(json.dumps(result),encoding='utf-8')
        self.saved[identifier]=copy.deepcopy(result)
        if on_progress:
            on_progress({'stage':'statistics','label':'Fixture calculation complete','status':'complete',
                         'detail':'An isolated aggregate fixture was returned.','facts':{'outcome':outcome,'n':count}})
        return result


class AccessConversationTests(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.agent=AccessFixture(self.folder.name)
        self.service=ConversationService(self.agent)
        self.cid=self.service.create()['id']
        self.api=patch('nhis_agent.chat_api.request',side_effect=AssertionError('No real API calls in replay tests'))
        self.api.start();self.addCleanup(self.api.stop)
        self.process=patch('subprocess.run',side_effect=AssertionError('No real R or shell processes in replay tests'))
        self.process.start();self.addCleanup(self.process.stop)

    def send(self,text):return self.service.send(self.cid,text)

    def test_exact_user_question_requests_four_real_events_without_health_fallback(self):
        self.assertEqual(requested_outcomes(QUESTION),list(ACCESS_OUTCOMES))
        selected=route(QUESTION)
        self.assertEqual(selected['status'],'ready')
        self.assertEqual(selected['outcomes'],list(ACCESS_OUTCOMES))
        reply=self.send(QUESTION)
        self.assertEqual(reply['message']['status'],'ok')
        self.assertEqual(reply['message']['kind'],'analysis')
        self.assertEqual([spec['outcome'] for spec in self.agent.calls],list(ACCESS_OUTCOMES))
        self.assertTrue(all(spec['group_by']=='overall' for spec in self.agent.calls))
        self.assertEqual(self.agent.chat_calls,[])
        self.assertEqual(self.agent.measurement_options,[False]*4)
        charts=reply['message']['charts']
        self.assertEqual([chart['outcome_id'] for chart in charts],list(ACCESS_OUTCOMES))
        self.assertEqual(len({chart['source_run_id'] for chart in charts}),4)
        for chart in charts:
            code=chart['outcome_id']
            self.assertEqual(chart['outcome']['variable'],get_outcome(code)['variable'])
            self.assertEqual(chart['groups'][0]['n'],COUNTS[code])
            self.assertIn(get_outcome(code)['label'],chart['title'])
        text=reply['message']['content']
        self.assertNotIn('PHSTAT_A',text)
        self.assertNotIn('self-rated',text.lower())
        self.assertIn('1,500',text);self.assertIn('1,300',text);self.assertIn('1,200',text)
        self.assertIn('time of interview',text)
        self.assertIn('past 12 months',text)
        self.assertIn('does not establish that all needed care was timely',text)
        self.assertEqual(reply['conversation']['context']['active_outcomes'],list(ACCESS_OUTCOMES))

    def test_women_and_income_followups_repeat_the_active_four_and_preserve_filters(self):
        self.send(QUESTION)
        women=self.send('What about women?')
        self.assertEqual([spec['outcome'] for spec in self.agent.calls[4:]],list(ACCESS_OUTCOMES))
        self.assertTrue(all(spec['filters']=={'sex':'female'} for spec in self.agent.calls[4:]))
        self.assertTrue(all(spec['group_by']=='overall' for spec in self.agent.calls[4:]))
        self.assertEqual([chart['outcome_id'] for chart in women['message']['charts']],list(ACCESS_OUTCOMES))
        self.assertTrue(all('Female' in chart['subtitle'] for chart in women['message']['charts']))
        income=self.send('Switch to income')
        self.assertEqual([spec['outcome'] for spec in self.agent.calls[8:]],list(ACCESS_OUTCOMES))
        self.assertTrue(all(spec['group_by']=='income' for spec in self.agent.calls[8:]))
        self.assertTrue(all(spec['filters']=={'sex':'female'} for spec in self.agent.calls[8:]))
        self.assertEqual([chart['outcome_id'] for chart in income['message']['charts']],list(ACCESS_OUTCOMES))
        self.assertTrue(all(len(chart['groups'])==2 for chart in income['message']['charts']))

    def test_explicit_self_rated_health_resets_the_multi_indicator_bundle(self):
        self.send(QUESTION)
        result=self.send('Compare fair or poor self-rated health by education')
        self.assertEqual(len(self.agent.calls),5)
        self.assertEqual(self.agent.calls[-1]['outcome'],'fair_poor')
        self.assertEqual(self.agent.calls[-1]['group_by'],'education')
        self.assertNotIn('active_outcomes',result['conversation']['context'])
        self.send('What about women?')
        self.assertEqual(len(self.agent.calls),6)
        self.assertEqual(self.agent.calls[-1]['outcome'],'fair_poor')
        self.assertEqual(self.agent.calls[-1]['filters'],{'sex':'female'})

    def test_general_insurance_concepts_use_chat_without_calculating_a_distribution(self):
        for question in ['What is health insurance?',
                         'Explain how health insurance works in general.',
                         'What does an insurance deductible mean?']:
            with self.subTest(question=question):
                reply=self.send(question)
                self.assertEqual(reply['message']['kind'],'chat')
                self.assertEqual(reply['message']['status'],'ok')
                self.assertNotIn('charts',reply['message'])
        self.assertEqual(self.agent.calls,[])
        self.assertEqual(len(self.agent.chat_calls),3)

    def test_translating_quoted_medical_access_question_is_not_an_analysis_command(self):
        reply=self.send('Translate this into English: "'+QUESTION+'"')
        self.assertEqual(reply['message']['kind'],'chat')
        self.assertEqual(self.agent.calls,[])
        self.assertEqual(len(self.agent.chat_calls),1)
        self.assertNotIn('charts',reply['message'])

    def test_named_coverage_explanation_uses_coverage_not_the_latest_unmet_care_result(self):
        self.send(QUESTION)
        reply=self.send('How is insurance coverage calculated?')
        self.assertEqual(len(self.agent.calls),4)
        self.assertIn(reply['message']['kind'],('explanation','method'))
        source=self.agent.saved[reply['message']['run_id']]
        self.assertEqual(source['spec']['outcome'],'insured')
        self.assertIn('NOTCOV_A',reply['message']['content'])
        self.assertNotIn('MEDNG12M_A',reply['message']['content'])

    def test_unsupported_relationship_and_insurance_amounts_do_not_become_coverage(self):
        for text in ('Calculate the correlation between insurance and self-rated health',
                     'Show the distribution of insurance deductibles'):
            self.agent.next_decision={'status':'ok','mode':'analysis','reply':''}
            reply=self.send(text)
            self.assertIn(reply['message']['status'],('clarify','blocked'))
            self.assertEqual(self.agent.calls,[])
            self.assertNotIn('charts',reply['message'])

    def test_partial_failure_omits_unreleased_panel_and_never_substitutes_health(self):
        self.agent.blocked_outcomes={'unmet_care_cost'}
        reply=self.send(QUESTION)
        self.assertEqual([chart['outcome_id'] for chart in reply['message']['charts']],list(ACCESS_OUTCOMES[:-1]))
        self.assertRegex(reply['message']['content'].lower(),r'no (?:usable|releasable).*estimate')
        self.assertNotIn('PHSTAT_A',reply['message']['content'])
        self.assertEqual([spec['outcome'] for spec in self.agent.calls],list(ACCESS_OUTCOMES))

    def test_a_second_conversation_does_not_inherit_the_active_outcome_bundle(self):
        self.send(QUESTION)
        other=self.service.create()['id']
        reply=self.service.send(other,'What about women?')
        self.assertEqual(len(self.agent.calls),4)
        self.assertNotIn('active_outcomes',reply['conversation']['context'])
        self.assertEqual(reply['message']['kind'],'chat')


class AccessOutcomePreflightTests(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        self.csv=self.root/'adult24.csv'
        self.registry=self.root/'registry.json'
        self.registry.write_text(json.dumps({'documentation_url':'https://www.cdc.gov/nchs/nhis/documentation/2024-nhis.html',
            'datasets':{'adult_2024':{'expected_rows':4,'url':'https://ftp.cdc.gov/isolated-fixture'}}}))
        self.registry_patch=patch.object(stats,'REGISTRY_PATH',self.registry)
        self.registry_patch.start();self.addCleanup(self.registry_patch.stop)

    def write(self,variable=None,values=None):
        names=list(stats.REQUIRED)
        if variable and variable not in names:names.append(variable)
        with self.csv.open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=names);writer.writeheader()
            for index in range(4):
                row={name:1 for name in names}
                row.update(SRVY_YR=2024,RECTYPE=10,HHX='FIXTURE_'+str(index),WTFA_A=100,
                           PSTRAT=1 if index<2 else 2,PPSU=index%2+1,AGEP_A=40,PHSTAT_A=4,
                           EDUCP_A=8,HISPALLP_A=2,SEX_A=2)
                if variable:row[variable]=values[index] if values is not None else 1
                writer.writerow(row)

    def test_selected_new_outcome_column_is_required(self):
        self.write()
        for outcome in ('insured','uninsured','delayed_care_cost','unmet_care_cost'):
            with self.subTest(outcome=outcome),self.assertRaisesRegex(ValueError,get_outcome(outcome)['variable']):
                stats._preflight(self.csv,outcome)

    def test_unknown_new_outcome_codes_are_rejected_before_any_estimation(self):
        for outcome in ('insured','delayed_care_cost','unmet_care_cost'):
            for invalid in (0,3,99,'NaN','unexpected'):
                with self.subTest(outcome=outcome,invalid=str(invalid)):
                    self.write(get_outcome(outcome)['variable'],[1,2,invalid,8])
                    with self.assertRaises(ValueError):stats._preflight(self.csv,outcome)

    def test_blank_and_item_missing_codes_do_not_remove_full_design_records(self):
        for outcome in ('insured','delayed_care_cost','unmet_care_cost'):
            with self.subTest(outcome=outcome):
                self.write(get_outcome(outcome)['variable'],['',7,8,9])
                result=stats._preflight(self.csv,outcome)
                self.assertEqual(result['records'],4)
                self.assertEqual(result['design_df'],2)
                self.assertEqual(result['outcome']['id'],outcome)


if __name__=='__main__':unittest.main()
