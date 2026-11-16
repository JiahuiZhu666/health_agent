"""Presentation fixtures are synthetic aggregates, never product estimates.

No dataset, saved conversation, environment file, model service or R process
is loaded. Metadata counts explicitly supplied here are the only data facts.
"""
import copy
import unittest
from unittest.mock import patch

from nhis_agent.answers import build_analysis_answer
from nhis_agent.outcomes import get_outcome
from nhis_agent.presentation import (analysis_answer, category_label, dataset_answer,
                                     dataset_topic, explanation_answer, language_followup, outcome_label,
                                     paired_care_answer, select_language)


def metadata():
    return {'ready': True, 'year': 2024, 'row_count': 32629,
            'source': 'https://www.cdc.gov/nchs/nhis/documentation/2024-nhis.html',
            'downloaded_at': '2026-11-16T12:00:00+00:00', 'income_ready': True,
            'file': '/SYNTHETIC_PRIVATE_ROOT/adult24.csv',
            'datasets': {'adult': {'row_count': 32629},
                         'income': {'row_count': 326290, 'imputations': 10}}}


def fixture(outcome='delayed_care_cost'):
    spec = {'year': 2024, 'age_min': 25, 'age_max': 64, 'group_by': 'age',
            'outcome': outcome, 'analysis': 'proportion', 'filters': {'sex': ['female']}}
    rows = [{'group_id': group, 'label': 'Synthetic fixture label', 'n': n, 'status': 'ok',
             'estimate': estimate, 'ci_low': low, 'ci_high': high, 'se': .01}
            for group, n, estimate, low, high in [
                ('25_34', 617, .1234, .1034, .1434), ('35_44', 733, .2345, .2145, .2545),
                ('45_54', 811, .1678, .1478, .1878), ('55_64', 859, .0912, .0712, .1112)]]
    return {'status': 'ok', 'spec': spec, 'survey': {'status': 'ok', 'outcome': get_outcome(outcome),
            'rows': rows, 'design': {'weight': 'WTFA_A', 'strata': 'PSTRAT', 'psu': 'PPSU',
                'constructed_before_domain_subset': True, 'variance_method': 'Taylor linearization'},
            'methods': {'proportion_ci': 'Design-based t-Wald interval'},
            'denominators': {'full_sample_n': 5100, 'age_domain_n': 4300,
                             'filtered_domain_n': 3050, 'valid_outcome_n': 3020},
            'missingness': {'outcome': {'min': 30, 'max': 30}},
            'imputation': {'required': False, 'm': 1}}, 'measurement': {'status': 'not_requested'}}


class PresentationTests(unittest.TestCase):
    def setUp(self):
        for target in ('urllib.request.urlopen', 'subprocess.run'):
            guard = patch(target, side_effect=AssertionError('Presentation must not request external work.'))
            guard.start(); self.addCleanup(guard.stop)

    def test_language_is_explicit_persistent_and_english_by_default(self):
        context = {'unrelated': {'keep': True}}
        self.assertEqual(select_language('这个数据库有多少条数据，都是啥时候的数据', context), 'en')
        self.assertNotIn('response_language', context)
        self.assertEqual(select_language('请用中文回答', context), 'zh')
        self.assertEqual(select_language('Compare age groups', context), 'zh')
        self.assertEqual(select_language('Answer in English please', context), 'en')
        self.assertEqual(context, {'unrelated': {'keep': True}, 'response_language': 'en'})
        self.assertEqual(select_language('Translate the previous response into Chinese.', context), 'zh')

    def test_language_only_followups_do_not_hide_new_analysis_or_database_question(self):
        for text in ('用中文回答上面的问题', '请把上述回答改成中文', 'Answer that in Chinese.',
                     'Explain the previous result in English please', '中文版本',
                     '请用中文解释这两个指标', 'Explain both indicators in Chinese'):
            with self.subTest(text=text):
                self.assertTrue(language_followup(text))
        for text in ('用中文回答这个数据库有多少条数据', '用中文重新按年龄分析',
                     'Answer in Chinese and compare income groups', 'What is a confidence interval?',
                     'Explain the sample size in English'):
            with self.subTest(text=text):
                self.assertFalse(language_followup(text))

    def test_dataset_topics_do_not_capture_current_analysis_denominators(self):
        for text in ('这个数据库有多少条数据，都是啥时候的数据', '你这个数据总共有多少条记录？调查年份是哪一年？', '数据从哪里来的？',
                     'How many rows are in the data?', 'What year is this dataset?',
                     'Is the NHIS database updated in real time?', 'How old is the data?'):
            with self.subTest(text=text): self.assertTrue(dataset_topic(text))
        for text in ('这次分析有多少有效样本？', '当前分组的样本量是多少？',
                     'How many valid respondents are in this analysis?', 'What is a sample weight?',
                     'Compare delayed care by age', 'What is the data in this analysis?'):
            with self.subTest(text=text): self.assertFalse(dataset_topic(text))

    def test_exact_database_question_uses_verified_file_count_and_survey_year(self):
        state = metadata(); before = copy.deepcopy(state)
        for language in ('en', 'zh'):
            answer = dataset_answer(state, language)
            for number in ('32,629', '2024', '326,290', '10', '2026-11-16'):
                self.assertIn(number, answer)
            self.assertNotIn('SYNTHETIC_PRIVATE_ROOT', answer)
            self.assertIn('https://www.cdc.gov/', answer)
        self.assertIn('不是实时数据库', dataset_answer(state, 'zh'))
        self.assertIn('不是调查发生的时间', dataset_answer(state, 'zh'))
        self.assertEqual(state, before)
        self.assertEqual(dataset_answer({'data': state}, 'zh'), dataset_answer(state, 'zh'))

    def test_unready_status_never_quotes_expected_counts_year_or_income_rows(self):
        state = metadata(); state['ready'] = False
        for language in ('en', 'zh'):
            answer = dataset_answer(state, language)
            for hidden in ('32,629', '326,290', '2024', '2026-11-16'):
                self.assertNotIn(hidden, answer)

    def test_missing_metadata_is_not_filled_from_registry_or_download_date(self):
        state = {'ready': True, 'downloaded_at': '2026-11-16T12:00:00Z'}
        answer = dataset_answer(state, 'zh')
        self.assertNotIn('32,629', answer); self.assertNotIn('2024 年', answer)
        self.assertIn('没有提供可确认的调查年份', answer)
        state = metadata(); state['income_ready'] = False
        self.assertNotIn('326,290', dataset_answer(state, 'zh'))

    def test_unsafe_metadata_urls_and_timestamp_text_are_not_links_or_exposed(self):
        state = metadata(); state['source'] = 'javascript:alert(1)'
        state['downloaded_at'] = '<script>UNTRUSTED_TIMESTAMP</script>'
        answer = dataset_answer(state, 'zh')
        self.assertNotIn('javascript:', answer); self.assertNotIn('UNTRUSTED_TIMESTAMP', answer)

    def test_english_is_the_established_renderer_without_numerical_changes(self):
        result = fixture()
        self.assertEqual(analysis_answer(result, result['spec'], 'More detail', 'en'),
                         build_analysis_answer(result, result['spec'], question='More detail'))

    def test_chinese_age_cost_results_keep_every_published_row_and_interval(self):
        for outcome in ('delayed_care_cost', 'unmet_care_cost'):
            result = fixture(outcome); before = copy.deepcopy(result)
            answer = analysis_answer(result, result['spec'], language='zh')
            for row in result['survey']['rows']:
                for key in ('estimate', 'ci_low', 'ci_high'):
                    self.assertIn('{:.2f}%'.format(row[key] * 100), answer)
                self.assertIn(format(row['n'], ','), answer)
            self.assertIn(outcome_label(outcome, 'zh'), answer)
            self.assertIn('25–64岁', answer); self.assertIn('女性', answer)
            self.assertIn('点估计排序', answer); self.assertIn('不能据此直接认定', answer)
            self.assertIn('不证明所有医疗服务都及时获得', answer)
            self.assertNotIn('Fair or poor', answer)
            self.assertEqual(result, before)

    def test_coding_and_requested_detail_keep_item_specific_denominator_and_design(self):
        result = fixture()
        answer = analysis_answer(result, result['spec'], '详细解释计算过程', 'zh')
        for text in ('MEDDL12M_A', '1记为事件1', '2记为0', '7、8、9', '不能当作事件0',
                     '完整成人样本', 'WTFA_A', 'PSTRAT', 'PPSU', 'Σ(w×y) / Σw',
                     '5,100', '4,300', '3,050', '3,020', '缺失=30'):
            self.assertIn(text, answer)

    def test_suppressed_and_invalid_cells_never_release_hidden_estimates(self):
        for changes in ({'status': 'suppressed'}, {'ci_high': float('inf')}, {'ci_low': .99}):
            result = fixture()
            result['survey']['rows'][0].update(estimate=.876543, ci_low=.856543, ci_high=.896543)
            result['survey']['rows'][0].update(changes)
            answer = analysis_answer(result, result['spec'], language='zh')
            self.assertNotIn('87.65', answer); self.assertNotIn('85.65', answer)
            self.assertIn('未发布', answer)

    def test_new_outcomes_require_their_own_id_and_source_variable(self):
        for descriptor in (None, {'id': 'delayed_care_cost'}, get_outcome('fair_poor'),
                           {'id': 'delayed_care_cost', 'variable': 'PHSTAT_A'}):
            result = fixture(); result['survey']['outcome'] = descriptor
            answer = analysis_answer(result, result['spec'], language='zh')
            self.assertIn('不能把旧结果改名', answer); self.assertNotIn('12.34%', answer)

    def test_sparse_blocked_or_clarify_records_preserve_actual_reason(self):
        for status in ('blocked', 'clarify'):
            result = {'status': status, 'reason': '请选择年龄分组或总体分析。'}
            answer = analysis_answer(result, {'outcome': 'delayed_care_cost'}, language='zh')
            self.assertIn(result['reason'], answer)
            self.assertNotIn('R失败', answer); self.assertNotIn('12.34%', answer)

    def test_gradient_preserves_every_released_contrast_and_joint_ci_without_clipping(self):
        result = fixture(); result['spec']['analysis'] = 'gradient'
        result['survey']['gradient'] = {'status': 'ok', 'contrasts': [
            {'group_id': group, 'reference_id': '55_64', 'status': 'ok',
             'estimate': .1011 + index * .01, 'ci_low': -.1111 - index * .01,
             'ci_high': 1.1234 + index * .01} for index, group in enumerate(('25_34', '35_44', '45_54'))]}
        answer = analysis_answer(result, result['spec'], language='zh')
        for row in result['survey']['gradient']['contrasts']:
            for key in ('estimate', 'ci_low', 'ci_high'):
                self.assertIn('{:+.2f}'.format(row[key] * 100), answer)
        self.assertIn('联合抽样协方差', answer); self.assertIn('包含0', answer)
        result['survey']['rows'][-1]['status'] = 'suppressed'
        answer = analysis_answer(result, result['spec'], language='zh')
        self.assertNotIn('+112.34', answer)

    def test_regression_preserves_all_published_terms_intervals_model_and_same_sample(self):
        result = fixture('uninsured'); result['spec'].update(group_by='education', analysis='regression')
        result['survey']['rows'] = []
        coefficients = [{'term': 'educationless_than_high_school', 'odds_ratio': 2.345,
                         'ci_low': 1.234, 'ci_high': 3.456},
                        {'term': 'sexmale', 'odds_ratio': .987, 'ci_low': .765, 'ci_high': 1.234}]
        result['survey']['regression'] = {'status': 'ok', 'n': 2719,
            'formula': 'uninsured ~ education + age + sex + race + income',
            'coefficients': coefficients, 'references': {'education': 'bachelors_plus', 'sex': 'female'},
            'income_adjusted': True, 'imputation_count': 10,
            'unadjusted': {'n': 2719, 'sample': 'Identical complete-case domain',
                'coefficients': [{'term': 'educationless_than_high_school', 'odds_ratio': 4.567,
                                  'ci_low': 3.456, 'ci_high': 5.678}]}}
        result['survey']['imputation'] = {'required': True, 'm': 10, 'method': 'Rubin pooling'}
        answer = analysis_answer(result, result['spec'], '详细解释计算过程', 'zh')
        for value in ('2.345', '1.234', '3.456', '0.987', '0.765', '4.567', '5.678', '2,719'):
            self.assertIn(value, answer)
        self.assertIn('参照组：教育程度=学士及以上；性别=女性', answer)
        self.assertIn('已确认使用同一完整病例样本', answer)
        self.assertIn('10份收入插补', answer); self.assertIn('Rubin', answer)
        self.assertIn('协变量条件下无关联（OR=1）', answer)
        self.assertNotIn('无条件关联', answer)
        result['survey']['regression']['unadjusted']['n'] = 2718
        self.assertIn('尚未确认两模型', analysis_answer(result, result['spec'], language='zh'))

    def test_legacy_fair_poor_without_descriptor_remains_supported(self):
        result = fixture('fair_poor'); result['survey'].pop('outcome')
        answer = analysis_answer(result, result['spec'], language='zh')
        self.assertIn('自评健康一般或较差', answer); self.assertIn('PHSTAT_A', answer)
        self.assertIn('12.34%', answer)

    def test_localized_heading_helpers_only_read_registered_metadata(self):
        self.assertEqual(category_label('less_than_high_school|nh_asian', 'zh'), '高中以下 × 非西班牙裔亚裔')
        self.assertEqual(category_label('25_34', 'en'), 'Ages 25–34')
        self.assertEqual(outcome_label('insured', 'en'), get_outcome('insured')['label'])
        self.assertEqual(outcome_label('unmet_care_cost', 'zh'), '过去12个月因费用未获得所需医疗服务')

    def care_pair(self):
        delayed, unmet = fixture(), fixture('unmet_care_cost')
        unmet['survey']['rows'][2].update(estimate=.3456, ci_low=.3256, ci_high=.3656)
        unmet['survey']['denominators']['valid_outcome_n'] = 2991
        return [delayed, unmet]

    def test_compact_pair_keeps_all_source_values_in_one_localized_table(self):
        results = self.care_pair(); before = copy.deepcopy(results)
        for language in ('en', 'zh'):
            answer = paired_care_answer(results, language=language)
            self.assertIsNotNone(answer)
            self.assertEqual(answer.count('|---|'), 1)
            for result in results:
                for row in result['survey']['rows']:
                    for field in ('estimate', 'ci_low', 'ci_high'):
                        self.assertIn('{:.2f}%'.format(row[field] * 100), answer)
            self.assertIn('23.45%', answer); self.assertIn('34.56%', answer)
            self.assertIn('3,020', answer); self.assertIn('2,991', answer)
            self.assertNotIn('insurance', answer.lower()); self.assertNotIn('教育', answer)
            self.assertNotIn('###', answer); self.assertNotIn('SE ', answer)
            if language == 'zh':
                self.assertIn('**35–44岁**', answer); self.assertIn('**45–54岁**', answer)
                self.assertIn('不能相加', answer); self.assertIn('每个指标', answer)
            else:
                self.assertIn('**Ages 35–44**', answer); self.assertIn('**Ages 45–54**', answer)
                self.assertIn('cannot be added', answer); self.assertIn('each indicator', answer)
        self.assertEqual(results, before)
        self.assertEqual(paired_care_answer(list(reversed(results))), paired_care_answer(results))

    def test_pair_suppression_hides_every_unreleased_value_and_keeps_all_age_rows(self):
        results = self.care_pair()
        results[0]['survey']['rows'][0].update(status='suppressed', estimate=.987654,
                                              ci_low=.977654, ci_high=.997654, n=9)
        for language in ('en', 'zh'):
            answer = paired_care_answer(results, language=language)
            self.assertIn('| ' + category_label('25_34', language) + ' | — |', answer)
            for value in ('98.77', '97.77', '99.77', '987654'):
                self.assertNotIn(value, answer)
            self.assertEqual(len([line for line in answer.splitlines() if line.startswith('| ')]), 5)

    def test_pair_rejects_mismatched_scope_unregistered_events_and_incomplete_rows(self):
        mutations = [
            lambda value: value.append(fixture('insured')),
            lambda value: value[1].update(status='blocked'),
            lambda value: value[1]['survey'].update(status='blocked'),
            lambda value: value[1]['spec'].update(outcome='insured'),
            lambda value: value[1]['survey']['outcome'].update(variable='PHSTAT_A'),
            lambda value: value[1]['survey']['outcome'].pop('id'),
            lambda value: value[1]['spec'].update(filters={'sex': ['male']}),
            lambda value: value[1]['spec'].update(year=2023),
            lambda value: value[1]['spec'].update(age_max=65),
            lambda value: value[1]['spec'].update(analysis='gradient'),
            lambda value: value[1]['spec'].update(group_by='income'),
            lambda value: value[1]['survey']['rows'].pop(),
            lambda value: value[1]['survey']['rows'].append(copy.deepcopy(value[1]['survey']['rows'][0])),
            lambda value: value[1]['survey']['rows'][0].update(ci_low=.99),
            lambda value: value[1]['survey']['rows'][0].update(estimate=float('nan')),
            lambda value: value[1]['survey']['rows'][0].update(status='unknown'),
            lambda value: value[1]['spec'].update(filters=['female']),
            lambda value: value[1]['survey'].update(gradient='malformed'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                results = self.care_pair(); mutate(results)
                self.assertIsNone(paired_care_answer(results))

    def test_pair_detailed_requests_keep_the_full_calculation_renderers(self):
        for question in ('详细解释这两个指标是怎么计算的', 'Show the full calculation step by step'):
            self.assertIsNone(paired_care_answer(self.care_pair(), question, 'zh'))

    def test_pair_never_infers_valid_denominators_by_summing_age_rows(self):
        results = self.care_pair()
        for result in results:
            result['survey'].pop('denominators')
        answer = paired_care_answer(results, language='zh')
        self.assertNotIn('有效 n=', answer)
        self.assertNotIn('3,020', answer); self.assertNotIn('2,991', answer)
        self.assertNotIn('3,020', answer)

    def test_pair_preserves_tied_highest_groups_without_a_unique_ranking(self):
        results = self.care_pair()
        results[0]['survey']['rows'][0].update(estimate=.2345, ci_low=.2145, ci_high=.2545)
        answer = paired_care_answer(results, language='zh')
        self.assertIn('**25–34岁、35–44岁**', answer)

    def test_chinese_missingness_only_describes_the_requested_analysis_fields(self):
        result = fixture()
        result['survey']['missingness'].update(education={'min': 88, 'max': 88},
                                               age={'min': 12, 'max': 12}, sex={'min': 3, 'max': 3})
        answer = analysis_answer(result, result['spec'], language='zh')
        self.assertIn('年龄组缺失=12', answer); self.assertIn('性别缺失=3', answer)
        self.assertNotIn('教育程度缺失', answer)
        result['spec'].update(analysis='regression')
        self.assertIn('教育程度缺失=88', analysis_answer(result, result['spec'], language='zh'))

    def test_dataset_documentation_link_never_uses_the_data_zip_download(self):
        state = metadata(); state['source'] = 'https://ftp.cdc.gov/pub/test/adult24csv.zip'
        for language in ('en', 'zh'):
            answer = dataset_answer(state, language)
            self.assertIn('https://www.cdc.gov/nchs/nhis/documentation/2024-nhis.html', answer)
            self.assertNotIn('adult24csv.zip', answer)

    def test_chinese_sample_followup_only_uses_observed_denominators_without_rate_table(self):
        result = fixture()
        answer = explanation_answer(result, result['spec'], '这次分析样本量是多少', 'zh')
        for count in ('3,020', '5,100', '4,300', '3,050'):
            self.assertIn(count, answer)
        self.assertNotIn('|---', answer); self.assertNotIn('12.34%', answer)
        self.assertIn('不是调查权重扩展后的全国人口', answer)
        result['survey'].pop('denominators')
        self.assertIn('不能从各组人数推算补齐', explanation_answer(result, result['spec'], '分母是多少', 'zh'))

    def test_chinese_ci_and_weights_followups_do_not_repeat_the_results_table(self):
        result = fixture()
        ci = explanation_answer(result, result['spec'], '置信区间是什么意思', 'zh')
        self.assertIn('重复抽样', ci); self.assertIn('不是个人取值范围', ci)
        self.assertNotIn('12.34%', ci); self.assertNotIn('|---', ci)
        weights = explanation_answer(result, result['spec'], '权重怎么用', 'zh')
        for field in ('WTFA_A', 'PSTRAT', 'PPSU'):
            self.assertIn(field, weights)
        self.assertNotIn('|---', weights)
        self.assertIsNone(explanation_answer(result, result['spec'], 'What is a CI?', 'en'))

    def test_selective_explanation_still_requires_the_matching_released_outcome(self):
        result = fixture(); result['survey']['outcome'] = get_outcome('fair_poor')
        answer = explanation_answer(result, result['spec'], '样本量多少', 'zh')
        self.assertIn('不能把旧结果改名', answer); self.assertNotIn('3,020', answer)
        result = fixture()
        self.assertEqual(explanation_answer(result, result['spec'], '总结结果', 'zh'),
                         analysis_answer(result, result['spec'], '总结结果', 'zh'))


if __name__ == '__main__':
    unittest.main()
