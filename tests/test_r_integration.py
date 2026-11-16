"""Optional local R integration; the explicit CI script requires R and fails if absent.

All generated inputs are synthetic and live in private temporary directories.
This test never imports the application server or reads real survey/chat data.
"""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.validate_fixture import ROOT, r_available, run_validation
from tests.fixtures.generate_survey import EXPECTED_ROWS, expected_metadata, generate


class SyntheticFixtureTests(unittest.TestCase):
    def test_fixture_is_deterministic_and_only_copied_registry_counts_change(self):
        registry = ROOT / 'config/registry.json'
        original = registry.read_bytes()
        with tempfile.TemporaryDirectory(prefix='nhis-fixture-generator-') as one, tempfile.TemporaryDirectory(prefix='nhis-fixture-generator-') as two:
            first, second = generate(one, registry), generate(two, registry)
            self.assertEqual(first['hashes'], second['hashes'])
            metadata = expected_metadata(first['rows'])
            pinned = json.loads((ROOT / 'tests/fixtures/expected_metadata.json').read_text())
            self.assertEqual(metadata, pinned)
            self.assertTrue(metadata['fixture_only'])
            self.assertEqual(metadata['full_sample_n'], EXPECTED_ROWS)
            copied = json.loads(first['registry'].read_text())
            self.assertEqual(copied['datasets']['adult_2024']['expected_rows'], 8000)
            self.assertEqual(copied['datasets']['income_2024']['expected_rows'], 80000)
            self.assertFalse(copied['_test_fixture_only']['production_fallback'])
        self.assertEqual(registry.read_bytes(), original)
        self.assertEqual(json.loads(original)['datasets']['adult_2024']['expected_rows'], 32629)

    def test_required_script_fails_honestly_when_explicit_runtime_is_absent(self):
        report = run_validation('/nonexistent/nhis-test-only/Rscript')
        self.assertEqual(report['status'], 'failed')
        self.assertIn('Rscript is required', report['error'])
        self.assertEqual(report['independent_stata_validation']['status'], 'pending')
        self.assertNotIn('runtime', report)


class RealRFixtureIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not r_available():
            raise unittest.SkipTest('Rscript with survey/jsonlite/mitools is unavailable; run scripts/validate_fixture.py in the required R CI job.')
        cls.report = run_validation()
        cls.checks = {item['name']: item for item in cls.report['checks']}

    def test_real_fixture_gate_passes_and_stata_remains_pending(self):
        self.assertEqual(self.report['status'], 'ok', json.dumps(self.report.get('failed_checks') or self.report.get('error'), indent=2))
        self.assertEqual(self.report['case_count'], 10)
        self.assertTrue(self.report['test_only'])
        self.assertFalse(self.report['production_fallback'])
        self.assertEqual(self.report['independent_stata_validation']['status'], 'pending')
        self.assertTrue(all(len(value) == 64 for value in self.report['source_sha256'].values()))
        self.assertIn('survey', self.report['runtime'])

    def test_real_full_design_and_item_specific_missingness_are_exercised(self):
        for name in ('insured_overall', 'uninsured_overall', 'fair_poor_overall', 'unmet_care_overall', 'delayed_female_education'):
            for suffix in ('full_design_before_domain', 'rebuilding_filtered_design_is_detectably_wrong',
                           'selected_outcome_descriptor', 'outcome_missing.refused', 'outcome_missing.blank', 'valid_outcome_n'):
                key = name + '.' + suffix
                self.assertIn(key, self.checks)
                self.assertTrue(self.checks[key]['passed'], key)

    def test_real_joint_covariance_MI_and_same_sample_logistic_are_exercised(self):
        for suffix in ('.rubin_total_variance', '.between_imputation_variance_is_nonzero', '.pooled_SE_is_not_mean_SE',
                       '.joint_covariance_is_nonzero', '.joint_variance_differs_from_independent_groups', '.same_sample_unadjusted',
                       '.naive_unadjusted_sample_is_larger', '.all_ten_models_converged'):
            matched = [item for name, item in self.checks.items() if name.endswith(suffix)]
            self.assertTrue(matched, suffix)
            self.assertTrue(all(item['passed'] for item in matched), suffix)

    def test_real_sparse_cells_and_all_sparse_domains_are_not_released(self):
        for key in ('sparse_cross_cells_really_exercise_suppression', 'all_sparse_domain_is_blocked',
                    'insured_uninsured_use_same_valid_denominator', 'insurance_complements_among_valid_responses',
                    'production_registry_was_not_modified'):
            self.assertIn(key, self.checks)
            self.assertTrue(self.checks[key]['passed'], key)


if __name__ == '__main__':
    unittest.main()
