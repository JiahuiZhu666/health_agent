version 17.0
clear all
set more off
set varabbrev off
/* Independent Stata reference, not executed in this build environment.
   Usage: do scripts/reference.do "data/adult24.csv" "runs/stata_reference.csv" "data/adultinc24.csv" "fair_poor"
   Optional fourth argument: fair_poor (default), insured, uninsured,
   delayed_care_cost, or unmet_care_cost. Official 2024 codebook coding only.
   Fixed unfiltered age-25–64 marginal estimates. Match identical specs before comparing.
   No output from this script is accepted as verified merely because it exists. */
args adult_csv output_csv income_csv outcome_id
if `"`outcome_id'"' == "" local outcome_id "fair_poor"
if !inlist(`"`outcome_id'"', "fair_poor", "insured", "uninsured", "delayed_care_cost", "unmet_care_cost") {
    display as error "Choose a registered binary outcome. Arbitrary variables and formulas are not accepted."
    exit 198
}
local outcome_variable "PHSTAT_A"
if inlist(`"`outcome_id'"', "insured", "uninsured") local outcome_variable "NOTCOV_A"
if `"`outcome_id'"' == "delayed_care_cost" local outcome_variable "MEDDL12M_A"
if `"`outcome_id'"' == "unmet_care_cost" local outcome_variable "MEDNG12M_A"
local item_variables "PHSTAT_A"
if `"`outcome_variable'"' != "PHSTAT_A" local item_variables "PHSTAT_A `outcome_variable'"
if `"`adult_csv'"' == "" | `"`output_csv'"' == "" {
    display as error "Supply the official adult CSV and output CSV paths."
    exit 198
}
log using `"`output_csv'.log"', text replace
import delimited using `"`adult_csv'"', varnames(1) case(upper) stringcols(_all) clear
keep SRVY_YR RECTYPE HHX WTFA_A PSTRAT PPSU AGEP_A `item_variables' EDUCP_A HISPALLP_A SEX_A
destring SRVY_YR RECTYPE WTFA_A PSTRAT PPSU AGEP_A `item_variables' EDUCP_A HISPALLP_A SEX_A, replace
assert _N == 32629
assert SRVY_YR == 2024 & RECTYPE == 10
isid HHX
assert WTFA_A > 0 & WTFA_A < . & !missing(PSTRAT, PPSU)
/* Item refusal, unascertained, unknown, and blank/skip values stay missing.
   NOTCOV_A is the edited coverage recode (1 uninsured, 2 insured).
   MEDDL12M_A/MEDNG12M_A concern cost-related care access in the past 12 months. */
generate byte outcome_value = .
if `"`outcome_id'"' == "fair_poor" {
    replace outcome_value = inlist(PHSTAT_A, 4, 5) if inrange(PHSTAT_A, 1, 5)
}
else if `"`outcome_id'"' == "insured" {
    replace outcome_value = (NOTCOV_A == 2) if inlist(NOTCOV_A, 1, 2)
}
else {
    replace outcome_value = (`outcome_variable' == 1) if inlist(`outcome_variable', 1, 2)
}
/* Safe zero only outside a valid-outcome subpopulation preserves the full design. */
generate byte outcome_safe = cond(missing(outcome_value), 0, outcome_value)
generate byte education = .
replace education = 1 if inrange(EDUCP_A, 0, 2)
replace education = 2 if inlist(EDUCP_A, 3, 4)
replace education = 3 if inrange(EDUCP_A, 5, 7)
replace education = 4 if inrange(EDUCP_A, 8, 10)
generate byte race = HISPALLP_A if inrange(HISPALLP_A, 1, 4)
replace race = 5 if inrange(HISPALLP_A, 5, 7)
generate byte sex = SEX_A if inlist(SEX_A, 1, 2)
generate byte age = .
replace age = 1 if inrange(AGEP_A, 25, 34)
replace age = 2 if inrange(AGEP_A, 35, 44)
replace age = 3 if inrange(AGEP_A, 45, 54)
replace age = 4 if inrange(AGEP_A, 55, 64)
generate byte domain = inrange(AGEP_A, 25, 64)
/* Declare before any domain selection; never keep/drop the 25–64 sample first. */
svyset PPSU [pweight = WTFA_A], strata(PSTRAT) vce(linearized) singleunit(missing)
tempfile adult reference income_long
save `adult'
tempname results
postfile `results' str64 group_id long n double estimate se ci_low ci_high design_df using `reference', replace

capture program drop reference_cell
program define reference_cell, rclass
    syntax, Condition(string)
    tempvar good psutag stratumtag
    generate byte `good' = domain & (`condition') & !missing(outcome_value)
    quietly count if `good'
    local sample_n = r(N)
    egen byte `psutag' = tag(PSTRAT PPSU) if `good'
    quietly count if `psutag' == 1
    local n_psu = r(N)
    egen byte `stratumtag' = tag(PSTRAT) if `good'
    quietly count if `stratumtag' == 1
    local cell_df = `n_psu' - r(N)
    /* Match R's cell-domain degf for t limits; svy retains the complete design for variance. */
    quietly svy, subpop(`good'): mean outcome_safe
    return scalar estimate = _b[outcome_safe]
    return scalar se = _se[outcome_safe]
    return scalar n = `sample_n'
    return scalar df = `cell_df'
    return scalar ci_low = _b[outcome_safe] - invttail(`cell_df', .025) * _se[outcome_safe]
    return scalar ci_high = _b[outcome_safe] + invttail(`cell_df', .025) * _se[outcome_safe]
end
reference_cell, condition("1")
post `results' ("overall") (r(n)) (r(estimate)) (r(se)) (r(ci_low)) (r(ci_high)) (r(df))
local education_ids "less_than_high_school high_school some_college bachelors_plus"
local race_ids "hispanic nh_white nh_black nh_asian nh_other"
local sex_ids "male female"
local age_ids "25_34 35_44 45_54 55_64"
foreach variable in education race sex age {
    local code = 0
    foreach id of local `variable'_ids {
        local ++code
        reference_cell, condition("`variable' == `code'")
        post `results' ("`id'") (r(n)) (r(estimate)) (r(se)) (r(ci_low)) (r(ci_high)) (r(df))
    }
}
local e = 0
foreach eid of local education_ids {
    local ++e
    local r = 0
    foreach rid of local race_ids {
        local ++r
        reference_cell, condition("education == `e' & race == `r'")
        post `results' ("`eid'|`rid'") (r(n)) (r(estimate)) (r(se)) (r(ci_low)) (r(ci_high)) (r(df))
    }
}
postclose `results'
use `reference', clear
generate str24 outcome = `"`outcome_id'"'
format estimate se ci_low ci_high %21.15g
export delimited using `"`output_csv'"', replace

/* Explicit reduced model, on one common complete-case domain. Log output only. */
use `adult', clear
generate byte complete_no_income = domain & !missing(outcome_value, education, age, sex, race)
svy, subpop(complete_no_income): logit outcome_safe ib4.education
svy, subpop(complete_no_income): logit outcome_safe ib4.education ib1.age ib2.sex ib2.race

/* Optional income: validate all ten complete household panels, then estimate
   each panel with its full adult design. Do not estimate the stacked panels. */
if `"`income_csv'"' != "" {
    import delimited using `"`income_csv'"', varnames(1) case(upper) stringcols(_all) clear
    keep RECTYPE HHX IMPNUM_A RATCAT_A
    destring RECTYPE IMPNUM_A RATCAT_A, replace
    assert _N == 326290
    assert RECTYPE == 30 & inrange(IMPNUM_A, 1, 10) & IMPNUM_A == floor(IMPNUM_A)
    assert inrange(RATCAT_A, 1, 14)
    isid HHX IMPNUM_A
    bysort HHX: assert _N == 10
    drop RECTYPE
    merge m:1 HHX using `adult', assert(match) nogen
    generate byte income = .
    replace income = 1 if inrange(RATCAT_A, 1, 3)
    replace income = 2 if inrange(RATCAT_A, 4, 7)
    replace income = 3 if inrange(RATCAT_A, 8, 11)
    replace income = 4 if inrange(RATCAT_A, 12, 14)
    save `income_long'
    tempname byimp
    tempfile byimp_file
    postfile `byimp' int imputation income long n double estimate variance df using `byimp_file', replace
    forvalues m = 1/10 {
        use `income_long', clear
        /* Keeping one entire imputation is valid; retaining all adults is essential. */
        keep if IMPNUM_A == `m'
        assert _N == 32629
        svyset PPSU [pweight = WTFA_A], strata(PSTRAT) vce(linearized) singleunit(missing)
        forvalues category = 1/4 {
            reference_cell, condition("income == `category'")
            post `byimp' (`m') (`category') (r(n)) (r(estimate)) (r(se)^2) (r(df))
        }
        generate byte complete_full = domain & !missing(outcome_value, education, age, sex, race, income)
        /* Exactly the same sample in the two models within this imputation. */
        svy, subpop(complete_full): logit outcome_safe ib4.education
        estimates save `"`output_csv'.unadjusted_imp`m'.ster"', replace
        svy, subpop(complete_full): logit outcome_safe ib4.education ib1.age ib2.sex ib2.race ib4.income
        estimates save `"`output_csv'.adjusted_imp`m'.ster"', replace
    }
    postclose `byimp'
    use `byimp_file', clear
    generate str24 outcome = `"`outcome_id'"'
    export delimited using `"`output_csv'.income_by_imputation.csv"', replace
    /* Scalar Rubin pooling, independently expressed in Stata. The complete-data
       df correction matches mitools MIcombine's finite-df formula, not merely
       Stata's default pooled asymptotic interval. */
    bysort income: egen double qbar = mean(estimate)
    bysort income: egen double within = mean(variance)
    bysort income: egen double between_sd = sd(estimate)
    generate double between = between_sd^2
    bysort income: egen double df_complete = min(df)
    bysort income: egen long n_min = min(n)
    bysort income: egen long n_max = max(n)
    bysort income: keep if _n == 1
    generate double total_variance = within + 1.1 * between
    generate double rvi = 1.1 * between / within
    generate double df_old = 9 * (1 + 1 / rvi)^2 if rvi > 0
    generate double df_observed = ((df_complete + 1) / (df_complete + 3)) * df_complete * within / (within + between)
    generate double pooled_df = cond(rvi == 0, df_observed, 1 / (1 / df_old + 1 / df_observed))
    replace estimate = qbar
    generate double se = sqrt(total_variance)
    generate double ci_low = estimate - invttail(pooled_df, .025) * se
    generate double ci_high = estimate + invttail(pooled_df, .025) * se
    generate str16 group_id = "below_100" if income == 1
    replace group_id = "100_199" if income == 2
    replace group_id = "200_399" if income == 3
    replace group_id = "400_plus" if income == 4
    keep outcome group_id n_min n_max estimate se ci_low ci_high pooled_df within between
    rename n_min n
    format estimate se ci_low ci_high %21.15g
    export delimited using `"`output_csv'.income_pooled.csv"', replace
}
display as text "Reference output generated; numerical cross-engine validation and presentation reliability review remain required."
log close
