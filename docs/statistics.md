# Statistical contract and validation status

The statistical engine accepts the official **2024 NHIS Sample Adult CSV**, not a filtered extract. It estimates fair/poor self-rated health in civilian noninstitutionalized U.S. adults aged 25–64. There are no example estimates, synthetic result fallbacks, or substitute income analyses. Missing data or unavailable R dependencies produce an explicit blocked result.

**Validation update, 2026-11-15:** Python failure-gate tests pass. Seven analyses have now run successfully on the official downloaded data with project-local R 4.5.3, survey 4.5, jsonlite 2.0.0 and mitools 2.4: education rates and gradients, income rates and gradients, education × race, full adjusted regression, and explicitly reduced regression. R emitted no warnings. An independent Python streaming calculation matches the education weighted means to within 2.22e-16; it does not validate the design-based variances. Stata remains unexecuted. No R/Stata numerical agreement, NCHS external agreement, or full NCHS presentation-standard certification is claimed.

## Interface

```python
from nhis_agent.stats import analyze, readiness

readiness()  # ready, status, runtime location/version or actionable reason
analyze("data/adult24.csv", {
    "year": 2024,
    "outcome": "fair_poor",
    "analysis": "proportion",  # or gradient, regression
    "group_by": "education",
    "filters": {"sex": "female"},
    "min_n": 100,
    "adjustment": "full"
}, "runs/unique-run-id")
```

`group_by` accepts overall, education, race, sex, age, education_race, or income. Filters accept the registered IDs in `config/registry.json`; values may be one ID or a list of IDs. Estimates and confidence limits use a 0–1 proportion scale. Cross-classified IDs have the form `less_than_high_school|nh_white`. `min_n` may be increased but never decreased below 100. The year, age domain, outcome, confidence level, recodes, and model formulas are fixed; arbitrary R, formulas, and covariates are not accepted.

A successful proportion request contains `rows`, `denominators`, `missingness`, `design`, `imputation`, `methods`, `checks`, `provenance`, and runtime versions. Each row has group_id, label, unweighted n, estimate, se, ci_low, ci_high, status, and suppression_reason. Suppressed rows have null estimates, standard errors, and intervals. If every cell fails its reliability gates, the overall result is blocked. Successful regressions have an empty `rows` array and a typed `regression` object, preventing unadjusted proportions from standing in for a requested regression. A failed model blocks the entire requested regression.

Each run retains its normalized specification, exact command arguments, R output/error logs, structured result, data SHA-256, and hashes of the registry, R script, and Python bridge. Hashes identify the files used; they are not a CDC-provided authenticity signature.

## Source and coding

The [CDC 2024 documentation page](https://www.cdc.gov/nchs/nhis/documentation/2024-nhis.html) links the adult data, codebook, survey description, and separate multiply imputed income files. The [adult summary](https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Dataset_Documentation/NHIS/2024/adult-summary.pdf) identifies the registered variables. Code values and the 32,629 adult records were checked against indexed sections of the [official adult codebook, version 18 June 2025](https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Dataset_Documentation/NHIS/2024/adult-codebook.pdf). The complete 18 MB codebook was not downloaded in this build environment.

The registry defines PHSTAT_A values 4–5 as fair/poor and 1–3 as the comparison outcome; 7–9 are missing. Education uses EDUCP_A: 0–2, 3–4, 5–7, and 8–10. Race/ethnicity uses HISPALLP_A: Hispanic 1, non-Hispanic White 2, Black 3, Asian 4, and other/multiple 5–7. SEX_A is male 1 and female 2. AGEP_A defines four ten-year groups spanning 25–64. Refused, not ascertained, and unknown codes are excluded from the relevant denominator rather than counted as favorable health or a substantive category. Race/ethnicity in this public-use recode can include agency-imputed information.

## Survey design and intervals

The engine constructs `survey::svydesign(ids=~PPSU, strata=~PSTRAT, weights=~WTFA_A, nest=TRUE)` on **all 32,629 adults first**, then subsets the design to the age domain and requested categories. It never rebuilds a design from a prefiltered analysis sample. Variance uses Taylor linearization and a with-replacement first-stage approximation without a finite population correction, following the public-use variables described in the [2024 survey description](https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Dataset_Documentation/NHIS/2024/srvydesc-508.pdf) and [survey package design documentation](https://r-survey.r-forge.r-project.org/pkgdown/docs/reference/svydesign.html).

Rates are survey-weighted means of the binary outcome. Standard errors come from the design-based covariance matrix. Nonimputed 95% intervals are t-Wald intervals using the cell-domain design degrees of freedom. They are suppressed if nonfinite, outside [0,1], or otherwise invalid; limits are never silently clipped. The count n is the unweighted valid-outcome denominator. Weighted denominators and missing-outcome counts are retained separately. Unknown group values are excluded only when grouping, filtering, or modeling requires that variable; their missing counts are reported.

Education and income gradients are unadjusted risk differences against the bachelor's-or-higher or ≥400%-of-poverty group respectively. Education × race gradients compare education levels within each race group. Contrast variance uses the full joint survey covariance, including covariance between the two rates. Intervals use design-based t limits, or pooled t limits when income is involved. These are exploratory cross-sectional associations; no multiple-comparison adjustment or causal interpretation is claimed.

## Income and multiple imputation

Income requests require the separate official `adultinc24.csv`, either beside `adult24.csv` or supplied as `income_csv_path`. The [official income summary](https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Dataset_Documentation/NHIS/2024/adultinc-summary.pdf) identifies HHX, IMPNUM_A, and RATCAT_A. All ten IMPNUM_A values 1–10 must be present with exactly one record per adult HHX per imputation. Unmatched households, duplicated keys, invalid categories, or an incomplete panel block the analysis. The 326,290 income rows are never treated as 326,290 independent adults.

The RATCAT_A grouping follows Appendix D of the [CDC income technical document](https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Dataset_Documentation/NHIS/2024/NHIS2024-imputation-techdoc-508.pdf): codes 1–3 below 100% of poverty, 4–7 at 100–199%, 8–11 at 200–399%, and 12–14 at ≥400%. The single-imputation income variables in the adult file are ignored.

Within each of the ten household merges, the engine builds a full adult survey design and then applies the same domain definition. It pools estimates and complete covariance matrices with `mitools::MIcombine`: total variance is the mean within-imputation variance plus 1.1 times the between-imputation variance. Its finite complete-data degrees-of-freedom adjustment is used for t intervals; the exact implementation is available in the [mitools source](https://github.com/cran/mitools/blob/master/R/MI.R). The complete-data df is the minimum relevant cell-domain df, or adjusted-model residual df, across imputations. Counts and model denominators report the minimum and range across the ten imputations. This is an explicit conservative counting convention, not tenfold sample inflation.

## Regression

The default registered adjusted model is:

```
fair_poor ~ education + age + sex + race + income
```

All covariates are categorical. References are bachelor's degree or higher, age 25–34, female, non-Hispanic White, and ≥400% of poverty. Full regression requires all ten income imputations. Only an explicitly requested `adjustment="no_income"` uses the registered reduced model excluding income; the default model is never silently simplified.

Within every imputation, the unadjusted `fair_poor ~ education` model and the adjusted model use the **identical complete-case domain** defined by all variables of the adjusted model. Missing non-income covariates use complete-case exclusion, with exclusion counts retained. The engine fits `survey::svyglm(..., family=quasibinomial(link="logit"))`; [the survey documentation](https://r-survey.r-forge.r-project.org/pkgdown/docs/reference/svyglm.html) describes the design-based covariance and finite design degrees of freedom. Log-odds and full covariance matrices are pooled, and t limits are transformed to odds-ratio limits. Odds ratios are not risk ratios.

A model must converge, have full coefficient rank and finite covariance, retain all prespecified reference groups, have at least two observed levels of each factor, have at least ten events and ten non-events per observed factor level, have at least ten events/non-events per fitted parameter overall, and have at least eight residual design degrees of freedom. Coefficients with absolute log-odds over 20 block release as a stability check. These checks do not prove adequate specification or eliminate separation, confounding, or other model limitations. Filtering to a fixed value of a model factor can legitimately block the fixed registered regression.

## Reliability and independent validation

A rate is released only when every imputation meets unweighted n≥100, at least ten outcome events and ten non-events, and at least eight domain design degrees of freedom. Pooled variance must imply effective n≥30 via p(1−p)/Var(p). Relative standard error must be ≤30%, the 95% interval width ≤0.30, and the estimate/variance/interval must be finite and nondegenerate. Component suppression propagates to contrasts. These are conservative application gates, not a claim to reproduce all NCHS data presentation rules.

`reference.do` independently declares the Stata survey design and produces unfiltered marginal reference rates, including education × race cells. Given the income file, it exports each imputation's income rates and independently pools scalar rates with the same finite-df formula. It also saves adjusted/unadjusted per-imputation Stata regression estimates for a later covariance-level comparison. **Pooled regression parity is not yet implemented in the Stata reference export.** Stata logit and R quasibinomial model results require explicit covariance and degrees-of-freedom review before any agreement label.

Run Stata separately with the same official files:

```
do scripts/reference.do "data/adult24.csv" "runs/stata_reference.csv" "data/adultinc24.csv"
```

The reference script does not apply application suppression, so its raw tables are validation artifacts rather than publishable app results. It currently uses no categorical filters; only compare it to the identical unfiltered application specification. Registered absolute acceptance tolerances are 1e−6 for estimates and standard errors and 1e−5 for confidence bounds, after matching domain df and imputation handling. Any comparison needs recorded software versions, input hashes, matching cell IDs, denominators, and a genuine independently generated reference. Passing a Python test or running the R script twice is not independent validation.
