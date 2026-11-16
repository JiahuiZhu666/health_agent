"""Offline outcome-coding and fixed-R-pipeline checks, never product estimates.

The optional R check evaluates only pure helper definitions with explicit test
vectors. It reads no survey records and calls no model or statistical engine.
Real-data integration and independent Stata agreement remain separate checks.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
R_SOURCE = ROOT / 'scripts' / 'analyze.R'


class RegisteredAccessOutcomeTests(unittest.TestCase):
    def test_official_registered_codes_and_polarity_are_preserved(self):
        registry=json.loads((ROOT/'config'/'registry.json').read_text(encoding='utf-8'))
        expected={
            'fair_poor':('PHSTAT_A',[4,5],[1,2,3]),
            'insured':('NOTCOV_A',[2],[1]),
            'uninsured':('NOTCOV_A',[1],[2]),
            'delayed_care_cost':('MEDDL12M_A',[1],[2]),
            'unmet_care_cost':('MEDNG12M_A',[1],[2]),
        }
        for identifier,(variable,one,zero) in expected.items():
            with self.subTest(outcome=identifier):
                item=registry['outcomes'][identifier]
                self.assertEqual(item['variable'],variable)
                self.assertEqual(item['one'],one)
                self.assertEqual(item['zero'],zero)
                self.assertEqual(item['missing'],[7,8,9])
                self.assertTrue(set(one).isdisjoint(zero))
                self.assertTrue(set(one+zero).isdisjoint(item['missing']))

    def test_internal_models_use_a_fixed_binary_response_not_user_formula_text(self):
        source=R_SOURCE.read_text(encoding='utf-8')
        self.assertIn('registry$outcomes[[spec$outcome]]',source)
        self.assertIn('base[[outcome_variable]]',source)
        self.assertIn('svymean(~outcome_value',source)
        self.assertIn('svyby(~outcome_value',source)
        self.assertIn('svyglm(outcome_value ~ education',source)
        self.assertNotRegex(source,r'~\s*fair_poor\b|\$fair_poor\b')
        self.assertNotIn('as.formula(formula_text)',source)
        self.assertNotIn('spec$formula',source)
        self.assertIn('outcome = c(list(id = spec$outcome), outcome)',source)

    def test_item_missingness_does_not_discard_the_full_survey_design(self):
        source=R_SOURCE.read_text(encoding='utf-8')
        self.assertIn('anyNA(base[structural])',source)
        self.assertNotIn('anyNA(base[needed])',source)
        structural=re.search(r'structural <- c\(([^\n]+)\)',source).group(1)
        for name in ('SRVY_YR','RECTYPE','HHX','WTFA_A','PSTRAT','PPSU'):
            self.assertIn('"'+name+'"',structural)
        for item in ('PHSTAT_A','NOTCOV_A','MEDDL12M_A','MEDNG12M_A'):
            self.assertNotIn('"'+item+'"',structural)
        self.assertLess(source.index('full <- svydesign('),source.index('subset(full, domain)'))
        self.assertIn('!is.na(d$variables$outcome_value)',source)
        self.assertIn('blank_n_range',source)
        self.assertIn('registered_missing_codes',source)

    def test_income_pooling_and_reliability_guards_are_retained_for_each_outcome(self):
        source=R_SOURCE.read_text(encoding='utf-8')
        self.assertIn('for (m in 1:10)',source)
        self.assertIn('MIcombine',source)
        self.assertIn('vcov(fit)',source)
        self.assertIn('- 2 * covariance[a, b]',source)
        self.assertIn('complete.cases(d$variables[all.vars(formula)])',source)
        self.assertIn('minimum_non_events',source)
        self.assertIn('maximum_relative_standard_error',source)
        self.assertIn('unadjusted_adjusted_identical_complete_case_domain',source)
        self.assertIn('answer$rows <- list()',source)

    def test_stata_reference_accepts_only_the_same_fixed_outcome_choices(self):
        source=(ROOT/'scripts'/'reference.do').read_text(encoding='utf-8')
        self.assertIn('args adult_csv output_csv income_csv outcome_id',source)
        self.assertIn('"fair_poor", "insured", "uninsured", "delayed_care_cost", "unmet_care_cost"',source)
        self.assertIn('(NOTCOV_A == 2) if inlist(NOTCOV_A, 1, 2)',source)
        self.assertIn('(`outcome_variable\' == 1) if inlist(`outcome_variable\', 1, 2)',source)
        self.assertIn('!missing(outcome_value)',source)
        self.assertIn('subpop(complete_full)',source)
        self.assertIn('forvalues m = 1/10',source)

    def test_r_binary_helper_excludes_unknown_refused_and_blank_and_keeps_fixed_formulas(self):
        project_r=ROOT/'.r-env'/'bin'/'Rscript'
        executable=str(project_r) if project_r.is_file() else shutil.which('Rscript')
        if not executable:
            self.skipTest('Rscript is unavailable; pure-R helper check is optional in stdlib CI.')
        expression=r'''
expressions <- parse(file=commandArgs(trailingOnly=TRUE)[[1]])
for (expression in expressions) {
  if (is.call(expression) && identical(expression[[1]], as.name("<-")) &&
      is.symbol(expression[[2]]) && as.character(expression[[2]]) %in%
      c("recode_binary", "fixed_model_formula")) eval(expression)
}
raw <- c(1, 2, 7, 8, 9, NA, 0, 3)
insured <- recode_binary(raw, 2, 1)
uninsured <- recode_binary(raw, 1, 2)
stopifnot(identical(insured, c(0, 1, rep(NA_real_, 6))))
stopifnot(identical(uninsured, c(1, 0, rep(NA_real_, 6))))
stopifnot(all(insured[1:2] + uninsured[1:2] == 1))
stopifnot(identical(recode_binary(c(1:5, 7, 8, 9, NA), c(4,5), 1:3),
                    c(0,0,0,1,1,rep(NA_real_,4))))
stopifnot(identical(all.vars(fixed_model_formula(TRUE)),
                    c("outcome_value","education","age","sex","race","income")))
stopifnot(identical(all.vars(fixed_model_formula(FALSE)),
                    c("outcome_value","education","age","sex","race")))
cat("Pure binary recoding, missingness and fixed-formula checks passed.\n")
'''
        result=subprocess.run([executable,'--vanilla','-e',expression,str(R_SOURCE)],
                              capture_output=True,text=True,timeout=30,check=False)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('checks passed',result.stdout)


if __name__=='__main__':
    unittest.main()
