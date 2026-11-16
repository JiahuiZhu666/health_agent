"""Artificial aggregates verify event-specific wording; never product estimates."""
import copy
import unittest
from unittest.mock import patch

from nhis_agent.answers import build_analysis_answer
from nhis_agent.explanations import build_explanation
from nhis_agent.charts import make_charts
from nhis_agent import models
from nhis_agent.outcomes import get_outcome, ACCESS_OUTCOMES
from tests.test_answers import fixture, regression_fixture, SPEC


def event_result(code, index=1, regression=False):
    result = regression_fixture() if regression else fixture()
    spec = dict(SPEC, outcome=code, analysis='regression' if regression else 'proportion')
    result['run_id'] = '20261116T120000Z-{:010x}'.format(index)
    result['spec'] = spec
    result['survey']['outcome'] = get_outcome(code)
    if regression:
        result['survey']['regression']['formula'] = code + ' ~ education + age + sex + race + income'
    return result, spec


class OutcomePresentationTests(unittest.TestCase):
    def test_each_new_answer_names_its_source_event_and_correct_codes(self):
        for code in ACCESS_OUTCOMES:
            with self.subTest(code=code):
                result, spec = event_result(code)
                before = copy.deepcopy(result)
                text = build_analysis_answer(result, spec, question='Show the calculation step by step.')
                descriptor = get_outcome(code)
                self.assertIn(descriptor['label'], text)
                self.assertIn(descriptor['variable'] + '=' + ', '.join(map(str, descriptor['one'])), text)
                self.assertIn('32.10%', text)
                self.assertIn('12.30%', text)
                self.assertIn('adults aged 25–64', text)
                self.assertIn(descriptor['limitation'], text)
                self.assertNotIn('PHSTAT_A', text)
                self.assertNotIn('reporting fair or poor health', text)
                self.assertNotIn('counted as healthy', text)
                self.assertEqual(result, before)

    def test_table_and_regression_headings_identify_the_modeled_event(self):
        for code in ACCESS_OUTCOMES:
            with self.subTest(code=code):
                result, spec = event_result(code)
                self.assertIn('Weighted proportion: ' + get_outcome(code)['label'], build_analysis_answer(result, spec))
                result, spec = event_result(code, regression=True)
                text = build_analysis_answer(result, spec)
                self.assertIn('Adjusted OR: ' + get_outcome(code)['label'], text)
                self.assertIn('2.400', text)
                self.assertNotIn('poor health', text)

    def test_item_specific_missingness_uses_the_selected_variable(self):
        result, spec = event_result('delayed_care_cost')
        result['survey']['missingness_by_code'] = {'filtered_domain': {
            'MEDDL12M_A': {'refused': {'n_range': {'min': 11, 'max': 11}}},
            'PHSTAT_A': {'refused': {'n_range': {'min': 777, 'max': 777}}}}}
        text = build_analysis_answer(result, spec)
        self.assertIn('Among invalid MEDDL12M_A responses: Refused 11 people', text)
        self.assertNotIn('777', text)

    def test_explanations_keep_each_event_and_do_not_reuse_health_coding(self):
        for code in ACCESS_OUTCOMES:
            for question in ('How did you calculate that?', 'What variables were used?', 'How do survey weights work?', 'What does that mean?'):
                with self.subTest(code=code, question=question):
                    result, _ = event_result(code)
                    text, _ = build_explanation(question, result)
                    self.assertIn(get_outcome(code)['label'].lower(), text.lower())
                    self.assertNotIn('PHSTAT_A', text)
                    self.assertNotIn('fair-or-poor indicator', text)
                    self.assertNotIn('probability of poor health', text)

    def test_new_outcome_descriptor_cannot_relabel_legacy_statistics(self):
        for mutation in ('missing', 'wrong_id', 'wrong_variable'):
            with self.subTest(mutation=mutation):
                result, spec = event_result('insured')
                if mutation == 'missing':
                    result['survey'].pop('outcome')
                else:
                    result['survey']['outcome']['id' if mutation == 'wrong_id' else 'variable'] = 'fair_poor' if mutation == 'wrong_id' else 'PHSTAT_A'
                answer = build_analysis_answer(result, spec)
                explanation = build_explanation('How did you calculate that?', result)[0]
                self.assertNotIn('32.10%', answer)
                self.assertNotIn('32.10%', explanation)
                self.assertEqual(make_charts(result), [])
        old = fixture()
        self.assertIn('32.10%', build_analysis_answer(old, SPEC))

    def test_blocked_new_outcome_keeps_the_actual_runtime_failure(self):
        result, spec = event_result('insured')
        result['status'] = 'blocked'
        result['survey'] = {'status': 'blocked', 'code': 'r_runtime_unavailable'}
        text = build_analysis_answer(result, spec)
        self.assertIn('R or the required survey-analysis packages are unavailable', text)
        self.assertNotIn('descriptor does not match', text)

    def test_chart_panels_keep_event_ids_labels_and_original_percentage_values(self):
        results = [event_result(code, index)[0] for index, code in enumerate(ACCESS_OUTCOMES, 1)]
        charts = make_charts(results)
        self.assertEqual(len(charts), 4)
        for chart, code in zip(charts, ACCESS_OUTCOMES):
            self.assertEqual(chart['outcome_id'], code)
            self.assertEqual(chart['outcome']['variable'], get_outcome(code)['variable'])
            self.assertIn(get_outcome(code)['label'], chart['title'])
            self.assertEqual(chart['x_label'], get_outcome(code)['label'] + ' (%)')
            self.assertAlmostEqual(chart['groups'][0]['value'], 32.1)
            self.assertNotIn('Fair or poor', chart['title'])

    def test_blind_prompts_name_only_the_event_population_and_reference_period(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('No API calls permitted')):
            for code in ACCESS_OUTCOMES:
                descriptor = get_outcome(code)
                for version in range(3):
                    text = models.build_prompt(dict(SPEC, outcome=code), {'group_id': 'female', 'label': 'LEAKED 99.99% n=999'}, version)
                    self.assertIn(descriptor['event'], text)
                    self.assertIn(descriptor['time_window'], text)
                    self.assertNotIn('LEAKED', text)
                    self.assertNotIn('99.99', text)
                    self.assertNotIn('aged 18', text)
                    self.assertNotIn('fair or poor', text)
                self.assertEqual(models.prompt_registry(dict(SPEC, outcome=code))['outcome']['id'], code)
            self.assertEqual(models.prompt_registry(SPEC)['sha256'], models.TEMPLATE_HASH)
            self.assertEqual(models.build_prompt(SPEC, {'group_id': 'overall'}, 0), models.TEMPLATES[0].format(population=''))


if __name__ == '__main__':
    unittest.main()
