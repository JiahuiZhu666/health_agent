"""Fixtures exercise failure gates only; they are never application result data."""
import csv
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nhis_agent import stats


class SpecificationTests(unittest.TestCase):
    def test_registered_income_is_supported_and_full_model_remains_full(self):
        spec = stats._normalise_spec({"analysis": "regression"})
        self.assertEqual(spec["adjustment"], "full")
        self.assertEqual(stats._normalise_spec({"group_by": "income"})["group_by"], "income")
        self.assertEqual(stats._normalise_spec({"analysis": "gradient", "group_by": "income"})["group_by"], "income")

    def test_custom_formula_and_lowered_sample_gate_are_rejected(self):
        for spec in ({"formula": "fair_poor ~ 1"}, {"min_n": 99}, {"min_n": True},
                     {"year": 2023}, {"outcome": "anything"}, {"filters": {"race": "unknown"}}):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                stats._normalise_spec(spec)

    def test_filters_are_allowlisted_and_lists_normalized(self):
        spec = stats._normalise_spec({"filters": {"sex": "female", "income": ["below_100"]}})
        self.assertEqual(spec["filters"], {"sex": ["female"], "income": ["below_100"]})

    def test_gradient_cannot_silently_drop_its_reference(self):
        for variable, value in (("education", "less_than_high_school"), ("income", "below_100")):
            with self.subTest(variable=variable), self.assertRaisesRegex(ValueError, "reference"):
                stats._normalise_spec({"analysis": "gradient", "group_by": variable,
                                       "filters": {variable: value}})

    def test_no_income_adjustment_is_preserved_in_an_income_domain(self):
        spec = stats._normalise_spec({"analysis": "regression", "adjustment": "no_income",
                                     "filters": {"income": "below_100"}})
        self.assertEqual(spec["adjustment"], "no_income")
        self.assertEqual(spec["filters"]["income"], ["below_100"])

    def test_unavailable_runtime_never_produces_estimates(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(stats, "_preflight", return_value={"path": "/not_used.csv"}), \
                    patch.object(stats, "readiness", return_value={"ready": False, "reason": "R absent"}):
                result = stats.analyze("/not_used.csv", {}, directory)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["rows"], [])
            self.assertEqual(result["code"], "r_runtime_unavailable")
            self.assertTrue((Path(directory) / "analysis_result.json").is_file())

    def test_full_regression_requires_income_before_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(stats, "_preflight", return_value={"path": "/not_used.csv"}), \
                    patch.object(stats, "readiness") as runtime:
                result = stats.analyze("/not_used.csv", {"analysis": "regression"}, directory)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("all 10", result["reason"].lower())
            runtime.assert_not_called()

    def test_project_local_R_is_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / ".r-env" / "bin" / "Rscript"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture")
            executable.chmod(0o755)
            completed = subprocess.CompletedProcess([], 0, "4.5.0\n4.4-2\n2.0.0\n2.4", "")
            with patch.object(stats, "ROOT", root), patch.object(stats.shutil, "which", return_value="/old/Rscript"), \
                    patch.object(stats.subprocess, "run", return_value=completed) as process:
                result = stats.readiness()
            self.assertTrue(result["ready"])
            self.assertEqual(result["rscript"], str(executable))
            self.assertEqual(result["mitools_version"], "2.4")
            self.assertEqual(process.call_args[0][0][0], str(executable))
            self.assertIn("sep=intToUtf8(10)", process.call_args[0][0][-1])


class DataPreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.csv = self.root / "adult24.csv"
        self.income = self.root / "adultinc24.csv"
        self.registry = self.root / "registry.json"
        # Smaller record count is confined to a mocked test registry.
        self.registry.write_text(json.dumps({"documentation_url": "https://www.cdc.gov/nchs/nhis/documentation/2024-nhis.html",
            "datasets": {"adult_2024": {"expected_rows": 4, "url": "https://ftp.cdc.gov/fixture"}}}))
        self.rows = [{"SRVY_YR": 2024, "RECTYPE": 10, "HHX": "H{:06d}".format(i), "WTFA_A": 100,
                      "PSTRAT": 1 if i < 2 else 2, "PPSU": i % 2 + 1,
                      "AGEP_A": 30, "PHSTAT_A": 4, "EDUCP_A": 8, "HISPALLP_A": 2, "SEX_A": 2}
                     for i in range(4)]
        self.write_adult()
        self.registry_patch = patch.object(stats, "REGISTRY_PATH", self.registry)
        self.registry_patch.start()

    def tearDown(self):
        self.registry_patch.stop()
        self.directory.cleanup()

    def write_adult(self):
        with self.csv.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=stats.REQUIRED)
            writer.writeheader(); writer.writerows(self.rows)

    def write_income(self, rows):
        with self.income.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["RECTYPE", "HHX", "IMPNUM_A", "RATCAT_A"])
            writer.writeheader(); writer.writerows(rows)

    def income_rows(self):
        return [{"RECTYPE": 30, "HHX": adult["HHX"], "IMPNUM_A": m, "RATCAT_A": m % 14 + 1}
                for m in range(1, 11) for adult in self.rows]

    def test_valid_full_design_reports_checksum_and_df(self):
        result = stats._preflight(self.csv)
        self.assertEqual(result["records"], 4)
        self.assertEqual(result["design_df"], 2)
        self.assertEqual(len(result["sha256"]), 64)

    def test_missing_record_nonpositive_weight_duplicate_household_invalid_code(self):
        for mutation in (lambda rows: rows.pop(), lambda rows: rows[0].update(WTFA_A=0),
                         lambda rows: rows[0].update(HHX=rows[1]["HHX"]),
                         lambda rows: rows[0].update(EDUCP_A=12)):
            old = [dict(row) for row in self.rows]
            mutation(self.rows)
            self.write_adult()
            with self.assertRaises(ValueError):
                stats._preflight(self.csv)
            self.rows = old

    def test_singleton_stratum_is_rejected(self):
        self.rows[0]["PPSU"] = self.rows[1]["PPSU"]
        self.write_adult()
        with self.assertRaisesRegex(ValueError, "fewer than two"):
            stats._preflight(self.csv)

    def test_income_complete_ten_imputation_merge(self):
        self.write_income(self.income_rows())
        result = stats._income_preflight(self.income, self.csv)
        self.assertEqual(result["imputations"], 10)
        self.assertEqual(result["records_per_imputation"], 4)
        self.assertEqual(result["records"], 40)

    def test_income_missing_imputation_or_duplicate_key_is_rejected(self):
        rows = self.income_rows()
        for bad in (rows[:-4], rows + [rows[0]]):
            self.write_income(bad)
            with self.assertRaises(ValueError):
                stats._income_preflight(self.income, self.csv)

    def test_income_unmatched_household_invalid_category_and_fractional_imputation(self):
        for update in ({"HHX": "unknown"}, {"RATCAT_A": 99}, {"IMPNUM_A": 1.5}):
            rows = self.income_rows()
            rows[0].update(update)
            self.write_income(rows)
            with self.assertRaises(ValueError):
                stats._income_preflight(self.income, self.csv)


if __name__ == "__main__":
    unittest.main()
