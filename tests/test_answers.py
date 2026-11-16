"""Scientific and disclosure constraints for source-bound answer formatting.

All numerical records below are test fixtures, never product fallback results.
"""
import copy
import re
import unittest

from nhis_agent.answers import build_analysis_answer, category_name


SPEC = {'year': 2024, 'age_min': 25, 'age_max': 64, 'analysis': 'proportion',
        'group_by': 'education', 'filters': {}, 'outcome': 'fair_poor'}


def fixture():
    categories = [('less_than_high_school', .321, 500), ('high_school', .24, 800),
                  ('some_college', .19, 900), ('bachelors_plus', .123, 1200)]
    rows = [{'group_id': group, 'label': 'English label', 'estimate': value, 'se': .01,
             'ci_low': value - .02, 'ci_high': value + .02, 'n': n,
             'status': 'ok', 'design_df': 80} for group, value, n in categories]
    return {'status': 'ok', 'run_id': '20261116T020249Z-4574ceecd8',
            'survey': {'status': 'ok', 'rows': rows,
                'design': {'weight': 'WTFA_A', 'strata': 'PSTRAT', 'psu': 'PPSU',
                           'constructed_before_domain_subset': True, 'variance_method': 'Taylor linearization'},
                'methods': {'proportion_ci': 'Design-based t-Wald interval',
                            'reliability_policy': {'minimum_unweighted_denominator': 100}},
                'denominators': {'full_sample_n': 9000, 'age_domain_n': 3600,
                                 'filtered_domain_n': 3500, 'valid_outcome_n': 3400},
                'missingness': {'outcome': {'min': 100, 'max': 100}, 'education': {'min': 70, 'max': 70}},
                'imputation': {'required': False, 'm': 1}},
            'measurement': {'status': 'not_requested'}}


def regression_fixture():
    result = fixture()
    result['survey']['rows'] = []
    result['survey']['denominators'].update(filtered_domain_n=3400, valid_outcome_n=3300)
    result['survey']['regression'] = {
        'status': 'ok', 'n': 3000, 'n_range': {'min': 3000, 'max': 3000},
        'formula': 'fair_poor ~ education + age + sex + race + income',
        'income_adjusted': True, 'imputation_count': 10,
        'references': {'education': 'bachelors_plus', 'age': '25_34', 'sex': 'female',
                       'race': 'nh_white', 'income': '400_plus'},
        'complete_case_exclusions_range': {'min': 400, 'max': 400},
        'variance_method': 'Design-based sandwich covariance; pooled log-odds and covariance matrices',
        'coefficients': [{'term': 'educationless_than_high_school', 'odds_ratio': 2.4,
                          'ci_low': 1.8, 'ci_high': 3.2, 'se': .1}],
        'unadjusted': {'n': 3000, 'sample': 'Identical complete-case domain as the adjusted model',
                      'coefficients': [{'term': 'educationless_than_high_school', 'odds_ratio': 4.1,
                                        'ci_low': 3.0, 'ci_high': 5.2, 'se': .2}]}}
    result['survey']['imputation'] = {'required': True, 'm': 10}
    return result


class AnswerTests(unittest.TestCase):
    def test_legacy_nonenglish_labels_and_errors_do_not_mix_output_languages(self):
        value = fixture()
        value['survey']['rows'][0]['label'] = '高中以下'
        self.assertNotRegex(build_analysis_answer(value, SPEC), r'[\u3400-\u9fff]')
        value['status'] = 'blocked'
        value['survey']['reason'] = '样本检查尚未完成'
        self.assertNotRegex(build_analysis_answer(value, SPEC), r'[\u3400-\u9fff]')

    def test_basic_answer_has_actual_table_and_explained_design(self):
        answer = build_analysis_answer(fixture(), SPEC)
        self.assertTrue(answer.startswith('**Conclusion:**'))
        self.assertIn('32.10%', answer)
        self.assertIn('12.30%', answer)
        self.assertIn('+19.80 percentage points', answer)
        self.assertIn('### How the calculation works', answer)
        self.assertIn('### How to interpret these results', answer)
        self.assertIn('Construct the design on all adults before restricting', answer)
        self.assertIn('Σ(w×y) / Σw', answer)
        self.assertIn('t critical value × SE', answer)
        self.assertIn('degrees of freedom are 80.0', answer)
        self.assertNotIn('1.96', answer)
        self.assertNotIn('English label', answer)
        self.assertNotIn('monotonic', answer)
        for group in ('Less than high school', 'High school or GED', 'Some college or an associate degree', 'Bachelor’s degree or higher'):
            self.assertIn('| ' + group + ' |', answer)
        self.assertIn('9,000 → Age domain 3,600 → After category filters 3,500 → Valid outcome 3,400', answer)
        self.assertGreater(len(answer.split()), 300)
        self.assertNotRegex(answer, r'[\u3400-\u9fff]')

    def test_scope_filters_and_ids_are_translated(self):
        spec = dict(SPEC, filters={'sex': 'female', 'race': ['nh_black']})
        answer = build_analysis_answer(fixture(), spec)
        self.assertIn('Restricted to Female, Non-Hispanic Black', answer)
        self.assertNotIn('female', answer)
        self.assertNotIn('nh_black', answer)
        self.assertEqual(category_name('less_than_high_school|nh_asian'), 'Less than high school × Non-Hispanic Asian')

    def test_no_run_identifier_or_missing_real_count_is_invented(self):
        result = {'status': 'ok', 'run_id': '20261116T020249Z-4574ceecd8',
                  'survey': {'rows': [{'group_id': 'overall', 'status': 'ok', 'estimate': .2}]}}
        answer = build_analysis_answer(result, {'group_by': 'overall'})
        self.assertIn('20.00%', answer)
        self.assertIn('Not recorded', answer)
        self.assertIn('A complete sample-flow record was not supplied', answer)
        self.assertNotIn('32,629', answer)
        self.assertNotIn('20261116T020249Z-4574ceecd8', answer)
        self.assertNotIn('degrees of freedom of', answer)
        self.assertNotIn('Construct the design on all adults before restricting', answer)

    def test_source_fields_are_not_mutated(self):
        result, spec = fixture(), copy.deepcopy(SPEC)
        before = copy.deepcopy((result, spec))
        build_analysis_answer(result, spec)
        self.assertEqual((result, spec), before)

    def test_suppressed_value_is_never_released_even_in_model_comparison(self):
        result = fixture()
        row = result['survey']['rows'][0]
        row.update(status='suppressed', n=40, estimate=.987654, se=.987654,
                   ci_low=.987654, ci_high=.987654, suppression_reason='Unweighted denominator below 100')
        result['measurement'] = {'status': 'ok', 'valid_calls': 15, 'planned_calls': 15}
        result['comparison'] = [{'group_id': row['group_id'], 'model_mean': .987654,
                                 'gap_pp': 98.7654, 'valid_count': 15, 'total_count': 15}]
        answer = build_analysis_answer(result, SPEC)
        self.assertIn('Less than high school (suppressed) | 40 | — | — | —', answer)
        self.assertNotIn('98.77', answer)
        self.assertNotIn('987654', answer)
        self.assertIn('1 group(s) are suppressed', answer)
        self.assertNotIn('maximum', answer)

    def test_cross_table_is_explicitly_selected_not_full_ranking(self):
        result = fixture()
        result['survey']['rows'] = [dict(result['survey']['rows'][0], group_id=education+'|'+race,
                                        estimate=.1+index*.01, ci_low=.08+index*.01, ci_high=.12+index*.01)
            for index, (education, race) in enumerate((
                ('less_than_high_school','nh_white'), ('high_school','nh_white'), ('some_college','nh_white'),
                ('bachelors_plus','nh_white'), ('less_than_high_school','nh_black'), ('high_school','nh_black'),
                ('some_college','nh_black'), ('bachelors_plus','nh_black')))]
        answer = build_analysis_answer(result, dict(SPEC, group_by='education_race'))
        self.assertIn('only 4 illustrative groups', answer)
        self.assertIn('not a complete ranking', answer)
        table = answer.split('### How the calculation works')[0]
        self.assertEqual(sum(line.startswith('| ') for line in table.splitlines()) - 1, 4)
        self.assertNotIn('less_than_high_school', answer)

    def gradient(self, low=.08, high=.16):
        result = fixture()
        result['survey']['gradient'] = {'contrasts': [{
            'group_id': 'less_than_high_school', 'reference_id': 'bachelors_plus',
            'status': 'ok', 'estimate': .12, 'se': .02, 'ci_low': low, 'ci_high': high}]}
        return build_analysis_answer(result, dict(SPEC, analysis='gradient'))

    def test_gradient_uses_recorded_joint_interval(self):
        answer = self.gradient()
        self.assertIn('+12.00 percentage points', answer)
        self.assertIn('+8.00 to +16.00', answer)
        self.assertIn('interval excludes 0', answer)
        self.assertIn('covariance', answer)
        self.assertIn('multiplicity', answer)
        self.assertNotIn('p=', answer.lower())
        self.assertNotIn('p-value', answer)

    def test_gradient_containing_zero_is_uncertain_not_no_difference(self):
        answer = self.gradient(-.02, .25)
        self.assertIn('interval includes 0', answer)
        self.assertIn('remains uncertain', answer)
        self.assertNotIn('prove no difference', answer)

    def test_component_suppression_removes_even_mislabeled_published_contrast(self):
        result = fixture()
        result['survey']['rows'][0]['status'] = 'suppressed'
        result['survey']['gradient'] = {'contrasts': [{'group_id': 'less_than_high_school',
            'reference_id': 'bachelors_plus', 'status': 'ok', 'estimate': .8877, 'ci_low': .88, 'ci_high': .9}]}
        answer = build_analysis_answer(result, dict(SPEC, analysis='gradient'))
        self.assertNotIn('88.77', answer)

    def test_missing_reference_component_never_passes_vacuous_validation(self):
        result = fixture()
        result['survey']['rows'] = [row for row in result['survey']['rows'] if row['group_id'] != 'bachelors_plus']
        result['survey']['gradient'] = {'contrasts': [{'group_id': 'less_than_high_school',
            'reference_id': 'bachelors_plus', 'status': 'ok', 'estimate': .8877, 'ci_low': .88, 'ci_high': .9}]}
        answer = build_analysis_answer(result, dict(SPEC, analysis='gradient'))
        self.assertNotIn('88.77', answer)
        self.assertNotIn('95% difference interval', answer)
        result['survey']['gradient']['contrasts'][0].pop('reference_id')
        answer = build_analysis_answer(result, dict(SPEC, analysis='gradient'))
        self.assertNotIn('88.77', answer)

    def test_regression_or_references_sample_and_mi_are_recorded(self):
        answer = build_analysis_answer(regression_fixture(), dict(SPEC, analysis='regression'))
        self.assertIn('OR is 2.400', answer)
        self.assertIn('4.100 | 2.400 | 1.800–3.200', answer)
        self.assertIn('Unadjusted OR, same sample', answer)
        self.assertIn('the same complete-case sample', answer)
        self.assertIn('n=3,000', answer)
        self.assertIn('complete-case processing excludes 400 people, including those with missing outcomes', answer)
        self.assertIn('Relative to the category-filtered domain', answer)
        self.assertIn('Age group=Ages 25–34', answer)
        self.assertIn('Income group=At least 400% of the poverty threshold', answer)
        self.assertIn('Fair or poor ~ Education + Age group + Sex + Race/ethnicity + Income group', answer)
        self.assertIn('all 10 official income imputations', answer)
        self.assertIn('not the probability of poor health or a risk ratio', answer)

    def test_equal_sample_count_alone_does_not_prove_same_sample(self):
        result = regression_fixture()
        result['survey']['regression']['unadjusted'].pop('sample')
        answer = build_analysis_answer(result, dict(SPEC, analysis='regression'))
        self.assertNotIn('Unadjusted OR, same sample', answer)
        self.assertNotIn('use the same complete-case sample', answer)
        self.assertIn('unadjusted-model sample size is 3,000', answer)

    def test_adjusted_or_interval_with_one_does_not_claim_no_conditional_effect(self):
        result = regression_fixture()
        result['survey']['regression']['coefficients'][0].update(odds_ratio=1.2, ci_low=.8, ci_high=1.8)
        answer = build_analysis_answer(result, dict(SPEC, analysis='regression'))
        self.assertIn('interval includes the no-association reference value of 1', answer)
        self.assertIn('no association for this term under the registered adjusted model', answer)
        self.assertNotIn('unconditional association', answer)

    def test_reduced_model_does_not_imply_income_adjustment(self):
        result = regression_fixture()
        model = result['survey']['regression']
        model.update(income_adjusted=False, formula='fair_poor ~ education + age + sex + race')
        answer = build_analysis_answer(result, dict(SPEC, analysis='regression', adjustment='no_income'))
        self.assertIn('reduced model without income', answer)
        self.assertNotIn('Income group=', answer)

    def test_adjusting_for_income_does_not_displace_education_question_focus(self):
        result = regression_fixture()
        result['survey']['regression']['coefficients'].append({'term': 'incomebelow_100',
            'odds_ratio': 5.1, 'ci_low': 4.2, 'ci_high': 6.2})
        answer = build_analysis_answer(result, dict(SPEC, analysis='regression'), question='教育与健康的关联，调整收入后如何？')
        self.assertIn('Education: Less than high school', answer.split('\n\n')[0])
        self.assertNotIn('Income group: Below', answer.split('\n\n')[0])

    def test_mi_range_and_finite_df_explained_without_sample_inflation(self):
        result = fixture()
        result['survey']['imputation'] = {'required': True, 'm': 10}
        result['survey']['methods']['proportion_ci'] = 'Rubin pooled t-Wald interval'
        result['survey']['denominators']['filtered_domain_n_range'] = {'min': 700, 'max': 740}
        result['survey']['rows'][0].update(n_range={'min': 100, 'max': 110}, pooled_df=50.5)
        answer = build_analysis_answer(result, SPEC)
        self.assertIn('700–740', answer)
        self.assertIn('100–110', answer)
        self.assertIn('T=W+(1+1/m)B', answer)
        self.assertIn('pooled degrees of freedom are 50.5', answer)
        self.assertIn('sample size is not multiplied by the number of imputations', answer)
        self.assertNotIn('sample of 7,400', answer)

    def test_partial_model_retains_actual_calls_mean_and_units(self):
        result = fixture()
        result['measurement'] = {'status': 'partial', 'valid_calls': 4, 'planned_calls': 15}
        result['comparison'] = [{'group_id': 'less_than_high_school', 'model_mean': .4,
                                 'gap_pp': 7.9, 'model_sd': .03, 'valid_count': 4, 'total_count': 15}]
        answer = build_analysis_answer(result, SPEC)
        self.assertIn('partially completed (valid calls: 4/15)', answer)
        self.assertIn('40.00%', answer)
        self.assertIn('+7.90 percentage points', answer)
        self.assertIn('3.00 percentage points', answer)
        self.assertIn('not the survey sampling SE', answer)
        self.assertNotIn('all 15 calls completed', answer)

    def test_cache_reuse_never_claims_fresh_R_or_prints_source_identifier(self):
        result = fixture()
        result['cache'] = {'hit': True, 'source_run_id': '20261116T020249Z-4574ceecd8'}
        answer = build_analysis_answer(result, SPEC)
        self.assertIn('R was not rerun', answer)
        self.assertIn('original calculation', answer)
        self.assertNotIn('20261116T020249Z-4574ceecd8', answer)

    def test_blocked_record_does_not_publish_any_estimates_or_executed_steps(self):
        result = fixture()
        result.update(status='blocked', code='r_runtime_unavailable', reason='Missing R')
        answer = build_analysis_answer(result, SPEC)
        self.assertIn('statistical-runtime checks', answer)
        self.assertIn('no usable statistical estimate was released', answer)
        self.assertNotIn('32.10%', answer)
        self.assertNotIn('### How the calculation works', answer)
        self.assertNotIn('| Group', answer)

    def test_submodel_failure_blocks_proportion_fallback(self):
        result = fixture()
        result['survey']['regression'] = {'status': 'blocked', 'reason': 'Sparse model cells'}
        answer = build_analysis_answer(result, dict(SPEC, analysis='regression'))
        self.assertNotIn('32.10%', answer)
        self.assertIn('no usable statistical estimate was released', answer)

    def test_explicit_survey_failure_blocks_even_if_outer_status_is_ok(self):
        result = fixture()
        result['survey']['status'] = 'failed'
        answer = build_analysis_answer(result, SPEC)
        self.assertNotIn('32.10%', answer)
        self.assertIn('no usable statistical estimate was released', answer)

    def test_nonregistered_arbitrary_contrasts_are_not_suggested(self):
        for grouping in ('race', 'sex', 'age'):
            answer = build_analysis_answer(fixture(), dict(SPEC, group_by=grouping))
            self.assertNotIn('A useful next step', answer)


if __name__ == '__main__':
    unittest.main()
