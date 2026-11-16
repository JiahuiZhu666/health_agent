"""Versioned semantic evaluation; passing a corpus is not universal NLP proof."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nhis_agent.spec import route, requested_outcomes
from nhis_agent.intent import inspect_intent, coverage_check, AnalysisPatch, ExecutionPlan, FILTER_VALUES
from nhis_agent.conversation import ConversationService
from nhis_agent.engine import Agent
from nhis_agent.stats import _normalise_spec
from test_conversation import FakeAgent


class IntentFidelityTests(unittest.TestCase):
    def test_shared_cost_barrier_questions_cover_both_batch_members_and_the_explicit_age_group(self):
        for text in (
            '哪些年龄组更常因费用而延迟或未获得医疗服务？',
            '哪个年龄组因为费用而推迟或未能就医？',
            '哪些年龄群体因费用延迟就医或未就医？',
            'Compare cost-related delayed and unmet medical care by age group',
            'Which age groups have more cost-related delayed or unmet care?',
            'Which age groups more often delay or fail to obtain medical care because of cost?',
        ):
            with self.subTest(question=text):
                intent=inspect_intent(text)
                result=route(text)
                self.assertEqual(result['status'],'ready',result)
                self.assertEqual(result['spec']['group_by'],'age')
                expected={'delayed_care_cost','unmet_care_cost'}
                self.assertEqual(set(requested_outcomes(text)),expected)
                recognized={code for item in intent.recognized_constraints if item.field=='outcome'
                            for code in item.value.split(',')}
                self.assertEqual(recognized,expected)
                self.assertIn('age',{item.value for item in intent.recognized_constraints
                                     if item.field=='group_by'})
                for outcome in expected:
                    self.assertIsNone(coverage_check(intent,dict(result['spec'],outcome=outcome)))
                self.assertIsNotNone(coverage_check(intent,dict(result['spec'],outcome='fair_poor')))
                self.assertIsNotNone(coverage_check(intent,dict(result['spec'],group_by='education')))
                for item in intent.recognized_constraints:
                    self.assertEqual(text[item.start:item.end],item.text)

    def test_exact_reported_question_reaches_only_its_matching_typed_batch_members(self):
        text='哪些年龄组更常因费用而延迟或未获得医疗服务？'
        base=route(text)['spec']
        with TemporaryDirectory() as folder,patch.dict(os.environ,{},clear=True):
            agent=Agent(folder)
            with patch.object(agent,'_execute_spec',return_value={'status':'ok','test_only':True}) as execute, \
                 patch('nhis_agent.engine.stats.analyze') as statistics, \
                 patch('nhis_agent.engine.models.measure') as measure:
                for outcome in ('delayed_care_cost','unmet_care_cost'):
                    self.assertEqual(agent.analyze_spec(dict(base,outcome=outcome),text)['status'],'ok')
                self.assertEqual(execute.call_count,2)
                for wrong in (dict(base,outcome='fair_poor'),dict(base,group_by='income')):
                    self.assertEqual(agent.analyze_spec(wrong,text,measure_model=True)['status'],'clarify')
                self.assertEqual(execute.call_count,2)
                statistics.assert_not_called()
                measure.assert_not_called()

    def test_single_cost_event_cannot_pass_coverage_for_the_other_event(self):
        for text,selected,other in (
            ('Compare delayed care by age group','delayed_care_cost','unmet_care_cost'),
            ('Compare unmet medical care by age group','unmet_care_cost','delayed_care_cost'),
            ('按年龄组比较延迟就医比例','delayed_care_cost','unmet_care_cost'),
            ('按年龄组比较未就医比例','unmet_care_cost','delayed_care_cost'),
            ('Compare insured proportions by education','insured','uninsured'),
            ('Compare uninsured proportions by education','uninsured','insured'),
        ):
            with self.subTest(question=text):
                result=route(text)
                self.assertEqual(result['status'],'ready',result)
                self.assertEqual(requested_outcomes(text),[selected])
                self.assertIsNone(coverage_check(inspect_intent(text),dict(result['spec'],outcome=selected)))
                self.assertIsNotNone(coverage_check(inspect_intent(text),dict(result['spec'],outcome=other)))

    def test_cost_reason_is_not_a_causal_effect_request(self):
        self.assertEqual(route('Which age groups delay medical care because of cost?')['status'],'ready')
        for text in ('Does education cause poor self-rated health?',
                     'Estimate the causal effect of income on self-rated health'):
            with self.subTest(question=text):
                self.assertEqual(route(text)['status'],'blocked')

    def test_unknown_explicit_chinese_group_and_unqualified_blood_glucose_are_unresolved(self):
        for text in ('按宗教比较自评健康', '按教育和宗教比较自评健康',
                     'Compare blood glucose by education'):
            with self.subTest(question=text):
                intent=inspect_intent(text)
                self.assertEqual(intent.resolution,'clarify')
                self.assertIn(route(text)['status'],('clarify','blocked'))
                self.assertNotIn('spec',route(text))
                for item in intent.unsupported_constraints:
                    self.assertEqual(text[item.start:item.end],item.text)

    def test_known_grouping_and_outcome_requests_remain_supported(self):
        for text in ('按教育比较自评健康', '按照性别比较自评健康',
                     '按年龄组比较自评健康', '按收入比较自评健康',
                     '按收入组比较，并解释计算步骤',
                     'Compare self-rated health by education',
                     'Compare uninsured proportions by education',
                     'Show health insurance coverage distribution'):
            with self.subTest(question=text):
                result=route(text)
                self.assertEqual(result['status'],'ready',result)
                self.assertIsNone(coverage_check(inspect_intent(text),result['spec']))

    def test_typed_legal_spec_cannot_override_explicit_question_before_execution(self):
        base={'outcome':'fair_poor','analysis':'proportion','group_by':'education','filters':{}}
        cases=[
            ('Compare uninsured proportions by education',base),
            ('Compare insured proportions by education',dict(base,outcome='uninsured')),
            ('Compare self-rated health by education',dict(base,group_by='income')),
            ('按教育比较自评健康',dict(base,group_by='income')),
            ('Compare self-rated health by education excluding women',dict(base,filters={'sex':'female'})),
            ('Compare self-rated health by education among women',dict(base,filters={'sex':'male'})),
            ('Clear the sex filter',dict(base,filters={'sex':'female'})),
            ('Clear all filters',dict(base,filters={'age':'25_34'})),
        ]
        with TemporaryDirectory() as folder,patch.dict(os.environ,{},clear=True):
            agent=Agent(folder)
            with patch.object(agent,'_execute_spec',side_effect=AssertionError('No statistical or paid work')) as execute, \
                 patch('nhis_agent.engine.stats.analyze') as statistics, \
                 patch('nhis_agent.engine.models.measure') as measure:
                for text,spec in cases:
                    with self.subTest(question=text,spec=spec):
                        _normalise_spec(spec)  # every rejected candidate is structurally legal
                        result=agent.analyze_spec(spec,text,measure_model=True)
                        self.assertEqual(result['status'],'clarify',result)
                        self.assertNotIn('run_id',result)
                        self.assertIn('does not match',result['answer'])
                execute.assert_not_called()
                statistics.assert_not_called()
                measure.assert_not_called()
            self.assertEqual(list((Path(folder)/'runs').iterdir()),[])

    def test_filter_coverage_normalizes_sets_and_preserves_valid_contextual_subsets(self):
        base={'outcome':'fair_poor','group_by':'education','filters':{}}
        valid=[
            ('Compare self-rated health by education among women and men',base),
            ('Clear the sex filter',dict(base,filters={'sex':['female','male']})),
            ('Clear all filters',base),
            ('Exclude women',dict(base,filters={'sex':['male'],'age':'25_34'})),
            ('Exclude Hispanic adults',dict(base,filters={'race':'nh_white'})),
            ('Only women',dict(base,filters={'sex':'female','income':'below_100'})),
            ('Clear the sex filter; then only women',dict(base,filters={'sex':'female'})),
            ('What about the standard errors?',dict(base,filters={'sex':'female'})),
        ]
        for text,spec in valid:
            with self.subTest(question=text,spec=spec):
                self.assertIsNone(coverage_check(inspect_intent(text),spec))
        invalid=[
            ('Only women',dict(base,filters={'sex':['female','male']})),
            ('Exclude women',base),
            ('Clear the sex filter',dict(base,filters={'sex':'female'})),
            ('Exclude Hispanic adults',dict(base,filters={'race':['hispanic','nh_white']})),
        ]
        for text,spec in invalid:
            with self.subTest(question=text,spec=spec):
                self.assertIsNotNone(coverage_check(inspect_intent(text),spec))
        all_categories={name:list(reversed(values)) for name,values in FILTER_VALUES.items()}
        self.assertIsNone(coverage_check(inspect_intent('Clear all filters'),dict(base,filters=all_categories)))

    def test_each_explicit_batch_member_is_covered_without_accepting_an_unrequested_member(self):
        base={'outcome':'fair_poor','group_by':'education','filters':{}}
        text='Compare self-rated health and uninsured proportions by education and income'
        intent=inspect_intent(text)
        self.assertEqual(intent.resolution,'ready',intent.document())
        for outcome in ('fair_poor','uninsured'):
            for group in ('education','income'):
                self.assertIsNone(coverage_check(intent,dict(base,outcome=outcome,group_by=group)))
        self.assertIsNotNone(coverage_check(intent,dict(base,outcome='delayed_care_cost')))
        self.assertIsNotNone(coverage_check(intent,dict(base,group_by='sex')))
        for outcome in ('insured','uninsured'):
            self.assertIsNone(coverage_check(inspect_intent('Show health insurance coverage distribution'),
                                             dict(base,outcome=outcome,group_by='overall')))
        self.assertIsNone(coverage_check(inspect_intent('Compare self-rated health by education and race'),
                                         dict(base,group_by='education_race')))

    def test_contextual_followups_and_method_questions_do_not_require_default_substitution(self):
        base={'outcome':'uninsured','analysis':'proportion','group_by':'income','filters':{'sex':'female'}}
        with TemporaryDirectory() as folder,patch.dict(os.environ,{},clear=True):
            agent=Agent(folder)
            with patch.object(agent,'_execute_spec',return_value={'status':'ok','test_only':True}) as execute:
                for text in ('What about the standard errors?', 'How did you calculate that?',
                             'Break it down', 'Only women'):
                    with self.subTest(question=text):
                        self.assertEqual(agent.analyze_spec(base,text)['status'],'ok')
                self.assertEqual(execute.call_count,4)

    def test_versioned_evaluation_corpus_has_no_severe_silent_substitution(self):
        corpus=json.loads((Path(__file__).parent/'fixtures'/'semantic-v1.json').read_text())
        self.assertEqual(corpus['schema_version'],1)
        for case in corpus['cases']:
            with self.subTest(id=case['id'],question=case['question']):
                result=route(case['question'])
                if case.get('ready'):
                    self.assertEqual(result['status'],'ready',result)
                    _normalise_spec(result['spec'])
                    for key,value in case.get('spec',{}).items():
                        self.assertEqual(result['spec'][key],value)
                else:
                    self.assertIn(result['status'],('clarify','blocked'),result)
                    self.assertNotIn('spec',result)

    def test_constraint_spans_refer_to_original_request(self):
        text='Compare asthma prevalence by education among married adults in NHIS 2024'
        intent=inspect_intent(text)
        self.assertEqual(intent.resolution,'clarify')
        self.assertEqual({item.field for item in intent.unsupported_constraints},{'outcome','population'})
        for item in intent.unsupported_constraints+intent.recognized_constraints:
            self.assertEqual(text[item.start:item.end],item.text)
        self.assertEqual(intent.document()['schema_version'],1)

    def test_unknown_constraints_never_start_engine_or_blind_measurement(self):
        with TemporaryDirectory() as folder:
            agent=Agent(folder)
            with patch.object(agent,'_execute_spec',side_effect=AssertionError('No statistical execution')):
                for text in ('Compare asthma prevalence by education in NHIS 2024',
                             'Compare fair/poor health by education among married adults'):
                    result=agent.ask(text,measure_model=True)
                    self.assertIn(result['status'],('clarify','blocked'))
            self.assertEqual(list((Path(folder)/'runs').iterdir()),[])

    def test_all_conversation_execution_entrances_apply_constraints(self):
        with TemporaryDirectory() as folder:
            agent=FakeAgent(folder);service=ConversationService(agent);cid=service.create()['id']
            service.send(cid,'Compare self-rated health by education')
            for text in ('Compare asthma prevalence by education in NHIS 2024',
                         'Plot fair/poor health by education among married adults',
                         'Compare all groups among married adults',
                         'What about unemployed adults?'):
                result=service.send(cid,text,measure_model=True)
                self.assertIn(result['message']['status'],('clarify','blocked'))
                self.assertNotIn('charts',result['message'])
            self.assertEqual(len(agent.calls),1)

    def test_exclusion_patch_retains_context_and_does_not_broaden_an_empty_domain(self):
        with TemporaryDirectory() as folder:
            agent=FakeAgent(folder);service=ConversationService(agent);cid=service.create()['id']
            service.send(cid,'Compare self-rated health by education')
            result=service.send(cid,'Exclude women')
            self.assertEqual(result['message']['status'],'ok')
            self.assertEqual(agent.calls[-1]['group_by'],'education')
            self.assertEqual(agent.calls[-1]['filters'],{'sex':'male'})
            service.send(cid,'What about women?')
            before=len(agent.calls)
            result=service.send(cid,'Exclude women')
            self.assertEqual(result['message']['status'],'clarify')
            self.assertEqual(len(agent.calls),before)

    def test_execution_plan_is_frozen_and_never_contains_credentials(self):
        spec={'outcome':'fair_poor','filters':{'sex':'female'}}
        plan=ExecutionPlan.freeze(spec,{'registry':'hash'})
        original=plan.document();spec['filters']['sex']='male'
        self.assertEqual(plan.document(),original)
        self.assertEqual(original['spec']['filters']['sex'],'female')


if __name__=='__main__':
    unittest.main()
