"""Saved-result explanation fixtures; never product estimates or model calls."""
import copy
import unittest
from unittest.mock import patch

from nhis_agent.explanations import build_explanation


def result():
    return {'status':'ok','run_id':'20261115T120000Z-123456789a',
      'spec':{'year':2024,'group_by':'education','analysis':'proportion','filters':{'sex':'female'}},
      'survey':{'status':'ok','rows':[
        {'group_id':'less_than_high_school','label':'Less than high school','status':'ok','estimate':.3,
         'se':.02,'ci_low':.26,'ci_high':.34,'n':400,'n_range':{'min':400,'max':400},'design_df':609},
        {'group_id':'bachelors_plus','label':"Bachelor's degree or higher",'status':'ok','estimate':.1,
         'se':.01,'ci_low':.08,'ci_high':.12,'n':500,'n_range':{'min':500,'max':500},'design_df':609}],
        'denominators':{'full_sample_n':32629,'age_domain_n':19646,'filtered_domain_n':1000,'valid_outcome_n':900},
        'design':{'constructed_before_domain_subset':True,'weight':'WTFA_A','strata':'PSTRAT','psu':'PPSU'},
        'imputation':{'required':False,'m':1},'provenance':{'sha256':'a'*64}},
      'cache':{'hit':True,'source_run_id':'20261114T120000Z-123456789b'},
      'process':[{'stage':'data','label':'official-data checks','status':'complete'},
                 {'stage':'statistics','label':'survey estimation','status':'complete'},
                 {'stage':'validation','label':'reliability checks','status':'complete'}],
      'measurement':{'status':'not_requested'}}


class ExplanationTests(unittest.TestCase):
    def test_common_english_followups_return_english_without_execution(self):
        questions = ['How did you calculate that?', 'Break it down', 'What does that mean?',
                     'What is the sample size?', 'What is the data source?',
                     'Explain Rubin pooling', 'How do the survey weights work?',
                     'What does OR mean?', 'How should I interpret the confidence interval?']
        with patch('urllib.request.urlopen', side_effect=AssertionError('No API calls allowed')), \
                patch('subprocess.run', side_effect=AssertionError('No execution allowed')):
            for question in questions:
                with self.subTest(question=question):
                    answer = build_explanation(question, result())
                    self.assertIsNotNone(answer)
                    self.assertNotRegex(answer[0], r'[\u3400-\u9fff]')

    def test_unrecognized_analysis_request_is_left_to_router(self):
        self.assertIsNone(build_explanation('那女性呢',result()))
        self.assertIsNone(build_explanation('按收入呢',result()))
        self.assertIsNone(build_explanation('天气怎么样',result()))
        self.assertIsNone(build_explanation(None,result()))

    def test_ci_abbreviation_next_to_chinese_is_recognized(self):
        for question in ['CI是什么意思？','CI如何解释']:
            with self.subTest(question=question):
                answer,_=build_explanation(question,result())
                self.assertIn('about 95% of the intervals',answer)
                self.assertIn('30.00%',answer)

    def test_or_abbreviation_requires_an_explanatory_question(self):
        for question in ['OR是什么意思？','OR如何解释','解释OR']:
            with self.subTest(question=question):
                answer,_=build_explanation(question,result())
                self.assertIn('ratio of odds',answer)
                self.assertIn('or a risk ratio',answer)
        for question in ['education or income','should I use education or explain income?','major group']:
            with self.subTest(question=question):
                self.assertIsNone(build_explanation(question,result()))

    def test_mi_abbreviation_next_to_chinese_avoids_english_word_fragments(self):
        answer,_=build_explanation('MI是什么意思？',result())
        self.assertIn('within-imputation variance',answer)
        self.assertIsNone(build_explanation('minimum sample',result()))

    def test_more_detail_routes_to_observable_calculation_not_short_summary(self):
        for question in ['详细一点','再详细点','继续解释','具体一点','展开说','多说一点',
                         '具体计算过程','解释步骤','一步步','more detail','elaborate','step by step']:
            with self.subTest(question=question):
                answer,kind=build_explanation(question,result())
                self.assertIn(kind,('method','explanation'))
                self.assertIn('### 1.',answer)
                self.assertIn('### 6.',answer)
                self.assertIn('30.00%',answer)
                self.assertIn('26.00%–34.00%',answer)
                self.assertIn('weight',answer)
                self.assertIn('Taylor',answer)
                self.assertNotIn('hidden reasoning',answer)
        self.assertEqual(build_explanation('展开讲讲',result())[1],'explanation')
        self.assertEqual(build_explanation('你是怎么算的',result())[1],'method')

    def test_step_explanation_uses_actual_design_denominators_and_cache(self):
        answer,_=build_explanation('你是怎么算的',result())
        for value in ['32,629','19,646','1,000','900','400','WTFA_A','PSTRAT','PPSU','R was not rerun','SHA256']:
            self.assertIn(value,answer)
        self.assertNotIn('20261115T120000Z-123456789a',answer)
        self.assertNotIn('20261114T120000Z-123456789b',answer)

    def test_interpretation_quotes_real_numbers_without_causal_mechanisms(self):
        answer,kind=build_explanation('这个差异说明什么',result())
        self.assertEqual(kind,'explanation')
        for value in ['Female','30.00%','10.00%','20.00 percentage points','does not establish statistical significance']:
            self.assertIn(value,answer)
        self.assertNotIn('causes',answer)
        self.assertGreaterEqual(answer.count('### '),3)

    def test_ci_example_and_joint_comparison_limits(self):
        answer,_=build_explanation('置信区间是什么意思',result())
        self.assertIn('about 95% of the intervals',answer)
        self.assertIn('not a range containing 95% of individuals',answer)
        self.assertIn('30.00%',answer)
        self.assertIn('2.000 percentage points',answer)
        self.assertIn('609',answer)
        self.assertIn('overlap',answer)
        self.assertIn('joint variance',answer)

    def test_samples_never_infer_unrecorded_denominators(self):
        partial={'status':'ok','survey':{'status':'ok','rows':[{'group_id':'overall','status':'ok','estimate':.2}]}}
        answer,_=build_explanation('样本有多少',partial)
        self.assertIn('complete sample-flow counts were not recorded',answer)
        self.assertNotIn('32,629',answer)
        self.assertNotIn('n=0',answer)
        answer,_=build_explanation('样本有多少',result())
        self.assertIn('unweighted n=1,000',answer)
        self.assertIn('900',answer)
        self.assertIn('400',answer)
        self.assertIn('500',answer)

    def test_mi_explains_all_ten_and_minimum_count_without_stacking(self):
        value=result()
        value['survey']['imputation']={'required':True,'m':10,'method':'Rubin pooling via MIcombine'}
        value['survey']['rows'][0]['imputation']={'within_variance':.0003,'between_variance':.0001}
        value['survey']['rows'][0]['n_range']={'min':400,'max':450}
        answer,_=build_explanation('Rubin多重插补怎么合并',value)
        self.assertIn('10 imputations',answer)
        self.assertIn('Ū + (1 + 1/m) × B',answer)
        self.assertIn('0.00041',answer)
        self.assertIn('10 panels must not be stacked',answer)
        sample,_=build_explanation('样本有多少',value)
        self.assertIn('400–450',sample)
        self.assertIn('minimum valid n and its range',sample)

    def test_mi_not_claimed_for_a_nonincome_run(self):
        answer,_=build_explanation('MI怎么计算',result())
        self.assertIn('income imputation was not required',answer)
        self.assertIn('does not mean this run performed 10',answer)

    def test_blocked_current_never_reuses_previous_numbers(self):
        value={'status':'blocked','run_id':'20261115T120000Z-123456789a','answer':'Input integrity checks failed',
               'survey':{'status':'blocked','rows':[{'status':'ok','estimate':.9999,'group_id':'overall'}]},
               'process':[{'stage':'data','label':'official-data checks','status':'blocked','detail':'The adult file is missing design columns'}]}
        for question in ['解释一下','你怎么算的','置信区间是什么意思']:
            answer,kind=build_explanation(question,value,previous=result())
            self.assertEqual(kind,'explanation')
            self.assertIn('The latest analysis did not release a successful result',answer)
            self.assertIn('official-data checks',answer)
            self.assertIn('missing design columns',answer)
            self.assertNotIn('99.99%',answer)
            self.assertNotIn('30.00%',answer)
            self.assertNotIn(value['run_id'],answer)

    def test_suppression_uses_only_actual_reasons(self):
        value=result()
        value['survey']['rows'].append({'group_id':'nh_other','status':'suppressed','n':42,
             'estimate':.987654,'suppression_reason':'Unweighted denominator below 100'})
        answer,_=build_explanation('为什么抑制',value)
        self.assertIn('below 100',answer)
        self.assertIn('n=42',answer)
        self.assertIn('was not estimated as zero',answer)
        self.assertNotIn('98.77%',answer)

    def test_significance_requires_joint_difference_interval(self):
        answer,_=build_explanation('统计显著吗',result())
        self.assertIn('no computed joint variance for a group difference',answer)
        self.assertIn('overlap',answer)
        self.assertNotIn('p=',answer)
        value=result()
        value['survey']['gradient']={'contrasts':[{'status':'ok','group_id':'less_than_high_school',
            'reference_id':'bachelors_plus','label':'低教育减本科及以上','estimate':.2,'se':.03,'ci_low':.14,'ci_high':.26}]}
        answer,_=build_explanation('统计显著吗',value)
        self.assertIn('+20.00',answer)
        self.assertIn('+14.00 to +26.00',answer)
        self.assertIn('Includes 0?',answer)
        self.assertIn('joint survey',build_explanation('解释一下',value)[0])

    def test_contrast_requires_both_published_components(self):
        for missing in ['suppressed_reference','missing_reference_row','missing_reference_id','missing_target_id']:
            with self.subTest(missing=missing):
                value=result()
                contrast={'status':'ok','group_id':'less_than_high_school','reference_id':'bachelors_plus',
                          'estimate':.2,'se':.03,'ci_low':.14,'ci_high':.26}
                value['survey']['gradient']={'contrasts':[contrast]}
                if missing=='suppressed_reference':
                    value['survey']['rows'][1]['status']='suppressed'
                elif missing=='missing_reference_row':
                    value['survey']['rows'].pop()
                elif missing=='missing_reference_id':
                    contrast.pop('reference_id')
                else:
                    contrast.pop('group_id')
                answer,_=build_explanation('统计显著吗',value)
                self.assertIn('no computed joint variance for a group difference',answer)
                self.assertNotIn('+14.00',answer)
                self.assertNotIn('+26.00',answer)

    def test_regression_uses_or_scale_and_actual_shared_sample(self):
        value=result()
        value['survey']['rows']=[]
        value['survey']['regression']={'status':'ok','n':800,'unadjusted':{'n':800,'sample':'Identical complete-case domain as the adjusted model'},
          'formula':'fair_poor ~ education + age + sex + race + income',
          'coefficients':[{'term':'educationless_than_high_school','odds_ratio':2.3,'ci_low':1.5,'ci_high':3.4}]}
        answer,_=build_explanation('回归系数OR是什么意思',value)
        for phrase in ['2.300','1.500–3.400','n=800','same analysis sample','or a risk ratio']:
            self.assertIn(phrase,answer)
        significance,_=build_explanation('统计显著吗',value)
        self.assertIn('Includes 1?',significance)
        self.assertNotIn('risk increased by 130%',answer)

    def test_model_method_reads_metadata_never_raw_calls(self):
        value=result()
        value['measurement']={'status':'partial','model':'test-model','provider':'openai','planned_calls':15,
            'valid_calls':4,'repeats':5,'prompt_versions':3,'calls':[{'raw_response':'PRIVATE_RAW_DO_NOT_RENDER'}]}
        answer,_=build_explanation('模型是如何提问的',value)
        for phrase in ['server-side measurement record','Planned calls: 15','Valid calls: 4','not individual records']:
            self.assertIn(phrase,answer)
        self.assertNotIn('PRIVATE_RAW_DO_NOT_RENDER',answer)
        with patch('urllib.request.urlopen',side_effect=AssertionError('No API calls allowed')), patch('subprocess.run',side_effect=AssertionError('No execution allowed')):
            self.assertIsNotNone(build_explanation('模型是如何提问的',value))

    def test_generic_methods_without_result_do_not_claim_a_run(self):
        for question in ['你怎么算的','你用了哪些变量','权重怎么用','数据来源是什么','为什么只看25–64岁','为什么样本不足100不显示']:
            with self.subTest(question=question):
                answer,kind=build_explanation(question,None)
                self.assertEqual(kind,'method')
                self.assertNotIn('30.00%',answer)
                self.assertNotIn('32,629',answer)
        self.assertEqual(build_explanation('解释一下',None)[1],'clarification')


if __name__=='__main__':unittest.main()
