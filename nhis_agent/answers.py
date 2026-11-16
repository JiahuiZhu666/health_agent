"""Readable, source-bound analysis answers; no statistics or model calls run here."""
import math
import re

LABELS = {
    'overall': 'Overall', 'all': 'Overall',
    'less_than_high_school': 'Less than high school', 'high_school': 'High school or GED',
    'some_college': 'Some college or an associate degree', 'bachelors_plus': 'Bachelor’s degree or higher',
    'hispanic': 'Hispanic, any race', 'nh_white': 'Non-Hispanic White',
    'nh_black': 'Non-Hispanic Black', 'nh_asian': 'Non-Hispanic Asian',
    'nh_other': 'Non-Hispanic other or multiple race', 'male': 'Male', 'female': 'Female',
    '25_34': 'Ages 25–34', '35_44': 'Ages 35–44', '45_54': 'Ages 45–54', '55_64': 'Ages 55–64',
    'below_100': 'Below 100% of the poverty threshold', '100_199': '100–199% of the poverty threshold',
    '200_399': '200–399% of the poverty threshold', '400_plus': 'At least 400% of the poverty threshold',
}
VARIABLES = {'education': 'Education', 'income': 'Income group', 'race': 'Race/ethnicity', 'sex': 'Sex',
             'age': 'Age group', 'age_years': 'Age in years', 'fair_poor': 'Fair or poor',
             'education_race': 'Education × race/ethnicity', 'overall': 'Overall'}
_RUN_ID = re.compile(r'\b\d{8}T\d{6}Z-[a-f0-9]{10}\b')
_SECRET = re.compile(r'\bsk-[A-Za-z0-9_-]{12,}')


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _safe(value):
    text = _SECRET.sub('[credential redacted]', str(value))
    return _RUN_ID.sub('the associated record', text).replace('|', '／').replace('\n', ' ').strip()


def category_name(value, fallback=None):
    """Translate registered category IDs, including cross-classified IDs."""
    value = str(value or '')
    if value in LABELS:
        return LABELS[value]
    pieces = re.split(r'[|:/]', value)
    normalized = [piece.split('=', 1)[-1] for piece in pieces]
    if normalized and all(piece in LABELS for piece in normalized):
        return ' × '.join(LABELS[piece] for piece in normalized)
    fallback_text = _safe(fallback or value or 'Registered group')
    return 'Registered group' if re.search(r'[\u3400-\u9fff]', fallback_text) else fallback_text


def scope_name(spec):
    """Describe only the population and filters present in the specification."""
    parts = []
    if spec.get('year') is not None:
        parts.append('NHIS ' + _safe(spec['year']))
    if finite(spec.get('age_min')) and finite(spec.get('age_max')):
        parts.append('U.S. adults aged {}–{}'.format(int(spec['age_min']), int(spec['age_max'])))
    elif parts:
        parts.append('the registered adult population')
    group = spec.get('group_by')
    if group in VARIABLES:
        parts.append('Grouped by {}'.format(VARIABLES[group]) if group != 'overall' else 'Overall analysis')
    restrictions = []
    for key in ('sex', 'age', 'education', 'race', 'income'):
        value = (spec.get('filters') or {}).get(key)
        if value is not None:
            values = value if isinstance(value, list) else [value]
            restrictions.append(', '.join(category_name(item) for item in values))
    if restrictions:
        parts.append('Restricted to ' + ', '.join(restrictions))
    return '; '.join(parts) + '. ' if parts else 'The population is defined by the registered analysis specification.'


def _count(value):
    if isinstance(value, dict):
        low, high = value.get('min'), value.get('max')
        if finite(low) and finite(high) and 0 <= low <= high and low == int(low) and high == int(high):
            return '{:,}'.format(int(low)) if low == high else '{:,}–{:,}'.format(int(low), int(high))
        return None
    if finite(value) and value >= 0 and value == int(value):
        return '{:,}'.format(int(value))
    return None


def _range_or_number(record, name):
    return _count(record.get(name + '_range')) or _count(record.get(name))


def _pct(value):
    return '{:.2f}%'.format(value * 100) if finite(value) and 0 <= value <= 1 else '—'


def _pp(value):
    return '{:+.2f}'.format(value * 100) if finite(value) else '—'


def _row_name(row):
    return category_name(row.get('group_id'), row.get('label'))


def _released(row):
    return row.get('status') == 'ok' and finite(row.get('estimate')) and 0 <= row['estimate'] <= 1


def _interval(row):
    low, high = row.get('ci_low'), row.get('ci_high')
    return _pct(low) + '–' + _pct(high) if finite(low) and finite(high) else 'Interval not recorded'


def _suppression_reason(value):
    reason = str(value or '').lower()
    translations = [
        ('denominator', 'Unweighted denominator below the publication floor'), ('effective sample', 'Insufficient effective sample size'),
        ('non-events', 'Too few events or non-events'), ('events', 'Too few events'),
        ('degrees of freedom', 'Insufficient design degrees of freedom'), ('relative standard', 'Excessive relative standard error'),
        ('interval width', 'Interval too wide'), ('confidence interval', 'Invalid confidence interval'),
        ('degenerate', 'Degenerate estimate or variance'), ('variance', 'Variance check failed'),
    ]
    known = [text for key, text in translations if key in reason and not (key == 'events' and 'non-events' in reason)]
    return ', '.join(dict.fromkeys(known)) if known else 'Did not pass publication checks'


def _rate_table(rows, cross=False):
    chosen = list(rows)
    intro = ''
    if cross or len(chosen) > 6:
        published = [row for row in rows if _released(row)]
        selected = []
        if published:
            selected = [min(published, key=lambda row: row['estimate'])]
            highest = max(published, key=lambda row: row['estimate'])
            if highest not in selected:
                selected.append(highest)
        for row in rows:
            if row not in selected and row.get('status') != 'ok':
                selected.append(row)
                break
        for row in rows:
            if row not in selected:
                selected.append(row)
            if len(selected) >= 4:
                break
        chosen = selected[:4]
        intro = ('The table shows only {} illustrative groups, including the highest and lowest released estimates '
                 'and a suppression example when available. It is not a complete ranking; '
                 'the remaining groups are in the statistical details.\n\n').format(len(chosen))
    lines = ['| Group | Valid n | Weighted proportion | SE (percentage points) | 95% CI |',
             '|---|---:|---:|---:|---|']
    for row in chosen:
        count = _range_or_number(row, 'n') or 'Not recorded'
        if _released(row):
            standard_error = '{:.3f}'.format(row['se'] * 100) if finite(row.get('se')) else '—'
            lines.append('| {} | {} | {} | {} | {} |'.format(_row_name(row), count, _pct(row['estimate']), standard_error, _interval(row)))
        else:
            lines.append('| {} (suppressed) | {} | — | — | — |'.format(_row_name(row), count))
    return intro + '\n'.join(lines)


def _contrast_name(contrast):
    target, reference = contrast.get('group_id'), contrast.get('reference_id')
    if target and reference:
        return category_name(target) + ' − ' + category_name(reference)
    return _safe(contrast.get('label') or 'Registered contrast')


def _valid_contrasts(survey):
    components = {row.get('group_id'): row for row in survey.get('rows', [])}
    return [row for row in (survey.get('gradient') or {}).get('contrasts', [])
            if row.get('status') == 'ok' and all(finite(row.get(k)) for k in ('estimate', 'ci_low', 'ci_high'))
            and row['ci_low'] <= row['estimate'] <= row['ci_high']
            and row.get('group_id') in components and row.get('reference_id') in components
            and all(_released(components[key]) for key in (row['group_id'], row['reference_id']))]


def _contrast_statement(contrast):
    difference = '{}: {} percentage points, with a 95% difference interval of {} to {} percentage points. '.format(
        _contrast_name(contrast), _pp(contrast['estimate']), _pp(contrast['ci_low']), _pp(contrast['ci_high']))
    if contrast['ci_low'] <= 0 <= contrast['ci_high']:
        return difference + 'The interval includes 0, so the direction of this difference remains uncertain. That does not establish that the groups are identical.'
    return difference + 'The interval excludes 0, providing evidence of a difference for this registered comparison. This remains a descriptive association.'


def _term_name(term):
    text = str(term or '')
    if text == '(Intercept)':
        return 'Intercept'
    for variable in ('education', 'income', 'race', 'age_years', 'age', 'sex'):
        if text.startswith(variable):
            category = text[len(variable):].lstrip('_:')
            return VARIABLES[variable] + ': ' + category_name(category) if category else VARIABLES[variable]
    return _safe(text)


def _valid_or(term):
    return (term.get('status', 'ok') == 'ok' and all(finite(term.get(k)) for k in ('odds_ratio', 'ci_low', 'ci_high'))
            and 0 < term['ci_low'] <= term['odds_ratio'] <= term['ci_high'])


def _regression_terms(regression, question=None):
    terms = [term for term in regression.get('coefficients', []) if _valid_or(term) and term.get('term') != '(Intercept)']
    query = str(question or '')
    income_focus = re.search(r'收入|income', query, re.I) and not re.search(r'教育|学历|education', query, re.I)
    focus = 'income' if income_focus else 'education'
    preferred = [term for term in terms if str(term.get('term', '')).startswith(focus)]
    return (preferred or terms)[:4]


def _formula_name(value):
    text = str(value or '')
    return re.sub(r'\b(?:fair_poor|education|income|race|sex|age_years|age)\b',
                  lambda match: VARIABLES[match.group()], text)


def _same_sample(survey, regression):
    unadjusted = regression.get('unadjusted') or {}
    confirmed = ('identical' in str(unadjusted.get('sample', '')).lower() or
                 any(check.get('name') == 'unadjusted_adjusted_identical_complete_case_domain' and check.get('status') == 'passed'
                     for check in survey.get('checks', [])))
    n, other_n = regression.get('n'), unadjusted.get('n')
    return confirmed and not (finite(n) and finite(other_n) and n != other_n)


def _regression_table(survey, regression, terms):
    unadjusted = {term.get('term'): term for term in (regression.get('unadjusted') or {}).get('coefficients', []) if _valid_or(term)}
    label = 'Unadjusted OR, same sample' if _same_sample(survey, regression) else 'Unadjusted OR'
    lines = ['| Model term | ' + label + ' | Adjusted OR | Adjusted 95% CI |', '|---|---:|---:|---|']
    for term in terms:
        original = unadjusted.get(term.get('term'))
        original_or = '{:.3f}'.format(original['odds_ratio']) if original else '—'
        lines.append('| {} | {} | {:.3f} | {:.3f}–{:.3f} |'.format(
            _term_name(term['term']), original_or, term['odds_ratio'], term['ci_low'], term['ci_high']))
    return '\n'.join(lines)


def _funnel(survey):
    denominator = survey.get('denominators') or {}
    points = []
    for key, label in [('full_sample_n', 'Full adult sample'), ('age_domain_n', 'Age domain'),
                       ('filtered_domain_n', 'After category filters'), ('valid_outcome_n', 'Valid outcome')]:
        count = _range_or_number(denominator, key)
        if count:
            points.append(label + ' ' + count)
    return ' → '.join(points)


def _missing_notes(survey, spec):
    names = {'outcome': 'Outcome', 'education': 'Education', 'race': 'Race/ethnicity', 'sex': 'Sex', 'age': 'Age'}
    relevant = ['outcome']
    if spec.get('analysis') == 'regression':
        relevant.extend(['education', 'race', 'sex', 'age'])
    elif spec.get('group_by') == 'education_race':
        relevant.extend(['education', 'race'])
    elif spec.get('group_by') in names:
        relevant.append(spec['group_by'])
    relevant.extend(key for key in (spec.get('filters') or {}) if key in names)
    missing = survey.get('missingness') or {}
    parts = []
    for variable in dict.fromkeys(relevant):
        value = missing.get(variable)
        maximum = value.get('max') if isinstance(value, dict) else value
        count = _count(value)
        if count and finite(maximum) and maximum > 0:
            parts.append(names[variable] + ' missing: ' + count + ' people')
    text = '; '.join(parts)
    if len(parts) > 1:
        text += '. Missingness can overlap across variables; these counts must not be added together'
    raw = ((survey.get('missingness_by_code') or {}).get('filtered_domain') or {}).get('PHSTAT_A') or {}
    code_parts = []
    for key, label in [('refused', 'Refused'), ('not_ascertained', 'Not ascertained'), ('dont_know', 'Don’t know')]:
        count = _count((raw.get(key) or {}).get('n_range'))
        maximum = ((raw.get(key) or {}).get('n_range') or {}).get('max')
        if count and finite(maximum) and maximum > 0:
            code_parts.append(label + ' ' + count + ' people')
    if code_parts:
        text += ('; ' if text else '') + 'Among invalid outcome responses: ' + ', '.join(code_parts)
    return text + '. ' if text else ''


def _cache_hit(result, survey):
    return (result.get('cache') or {}).get('hit') is True or bool((survey.get('provenance') or {}).get('cache_reuse'))


def _mi_info(survey, regression):
    record = survey.get('imputation') or {}
    count = record.get('m')
    if not finite(count):
        count = regression.get('imputation_count')
    return record, int(count) if finite(count) and count >= 1 and count == int(count) else None


def detail_requested(question):
    """Expand methods when requested, without making every result a lecture."""
    return bool(re.search(
        r'详细|一步步|计算步骤|计算过程|怎么算|怎么计算|如何计算|展开|'
        r'\bdetailed\b|\bin detail\b|how.{0,45}calculat|step.by.step|break.{0,8}down|'
        r'walk.{0,12}through|full (?:method|calculation)|show.{0,15}(?:work|steps)|explain.{0,15}method',
        str(question or ''), re.I))


def _method_summary(survey, spec, regression):
    """Keep the result's relevant methods; do not invent missing execution facts."""
    design, methods = survey.get('design') or {}, survey.get('methods') or {}
    imputation, m = _mi_info(survey, regression)
    full_design = ('The full adult survey design was retained before taking the analysis domain. '
                   if design.get('constructed_before_domain_subset') is True else
                   'The registered method retains the full adult design before domain analysis; its execution flag is absent here. ')
    outcome = 'Fair/poor is PHSTAT_A=4/5; invalid responses are excluded.'
    bullets = [full_design + outcome]
    if regression.get('status') == 'ok':
        model = 'Model: `' + _formula_name(regression['formula']) + '`. ' if regression.get('formula') else ''
        n = _range_or_number(regression, 'n')
        if n:
            model += 'Complete-case n=' + n + '. '
        if _same_sample(survey, regression):
            model += 'Unadjusted and adjusted models use the same complete-case sample. '
        elif _range_or_number(regression.get('unadjusted') or {}, 'n'):
            model += 'Unadjusted n=' + _range_or_number(regression['unadjusted'], 'n') + '; identical membership is not confirmed. '
        exclusions = _count(regression.get('complete_case_exclusions_range'))
        if exclusions:
            model += 'Relative to the category-filtered domain, complete-case exclusions total ' + exclusions + ', including missing outcomes. '
        bullets.append(model.strip() or 'Only the returned coefficients and intervals are quoted.')
    else:
        uncertainty = 'Proportions are survey-weighted; n counts valid respondents. '
        if 'taylor' in str(design.get('variance_method') or '').lower():
            uncertainty += 'SEs account for strata and PSUs using Taylor linearization. '
        ci_method = str(methods.get('proportion_ci') or '').lower()
        if 't-wald' in ci_method or 'pooled t' in ci_method:
            uncertainty += 'CIs use design or pooled t critical values. '
        if _valid_contrasts(survey):
            uncertainty += 'Difference intervals retain joint survey covariance.'
        bullets.append(uncertainty.strip())
    if m and m > 1:
        bullets.append(('All 10 official income imputations' if m == 10 else 'The recorded ' + str(m) + ' imputations') +
                       ' were pooled using Rubin’s rules, including within- and between-imputation uncertainty. '
                       'The n range is across imputations, not extra respondents.')
    elif imputation.get('required') is True:
        bullets.append('Income imputation is required, but the combined imputation count is missing; completion of all 10 cannot be confirmed.')
    return '\n'.join('- ' + text for text in bullets)


def _method_steps(result, survey, spec, regression):
    design = survey.get('design') or {}
    methods = survey.get('methods') or {}
    reused = _cache_hit(result, survey)
    steps = []
    if design.get('constructed_before_domain_subset') is True:
        weight, strata, psu = design.get('weight'), design.get('strata'), design.get('psu')
        variables = 'Use {} as the weight, {} as strata, and {} as PSU identifiers. '.format(weight, strata, psu) if all((weight, strata, psu)) else ''
        steps.append('**Retain the full survey design first.** ' + variables + 'Construct the design on all adults before restricting it to the registered age and category domain, retaining the contribution of stratification and clustering to variance.')
    else:
        steps.append('**Check the population definition first.** The registered procedure constructs the full adult design before subsetting the age and category domain. This record does not include a complete design-execution flag, so the table alone cannot confirm that step.')
    funnel = _funnel(survey)
    steps.append('**Define the outcome and denominator.** PHSTAT_A=4 or 5 is coded as fair or poor; 1–3 are the other valid responses. Refused, not ascertained, and don’t-know responses are excluded from the relevant denominator, rather than counted as healthy. ' +
                 ('Recorded counts are: ' + funnel + '. ' if funnel else 'A complete sample-flow record was not supplied, so intermediate counts are not filled in.'))
    if regression.get('status') == 'ok':
        formula = regression.get('formula')
        description = 'The recorded model is `' + _formula_name(formula) + '`. ' if formula else 'The complete model formula was not supplied.'
        n = _range_or_number(regression, 'n')
        unadjusted = regression.get('unadjusted') or {}
        old_n = _range_or_number(unadjusted, 'n')
        confirmed = _same_sample(survey, regression)
        if n:
            description += 'Adjusted-model complete-case n=' + n + '. '
        if old_n and n:
            multiple = _mi_info(survey, regression)[1]
            same_text = 'The unadjusted and adjusted models ' + ('within each imputation ' if multiple and multiple > 1 else '') + 'use the same complete-case sample'
            description += (same_text if confirmed else 'The recorded unadjusted-model sample size is ' + old_n) + '. '
        excluded = _count(regression.get('complete_case_exclusions_range'))
        if excluded:
            baseline = _range_or_number(survey.get('denominators') or {}, 'filtered_domain_n')
            description += 'Relative to the category-filtered domain' + (' (n=' + baseline + ')' if baseline else '') + ', complete-case processing excludes ' + excluded + ' people, including those with missing outcomes. '
        steps.append('**Compare groups in the registered model.** ' + description)
    else:
        steps.append('**Calculate each group’s weighted proportion.** Use `Σ(w×y) / Σw`: w is the survey weight; y is 1 for fair or poor and 0 for the other valid responses. Sum the weights for outcome=1 and divide by the total weight of valid responses in that group. This is a weighted proportion, not a simple headcount fraction.')
    imputation, m = _mi_info(survey, regression)
    if m and m > 1:
        description = ('Estimate each of all 10 official income imputations separately, then combine them' if m == 10 else 'The record reports combining {} imputations'.format(m))
        steps.append('**Include income-imputation uncertainty.** ' + description + '. Under Rubin’s rules, `T=W+(1+1/m)B` combines within-imputation sampling variance W and between-imputation variance B. The n range reflects counts across imputations; the sample size is not multiplied by the number of imputations.')
    elif imputation.get('required') is True:
        steps.append('**Check the income-imputation record.** Income imputation is required, but the number combined was not fully recorded. Completion of all 10 imputations cannot be confirmed.')
    if regression.get('status') == 'ok':
        steps.append('**Quantify coefficient uncertainty.** ' + ('The record uses design-based covariance estimates. Intervals are formed on the log-odds scale and then transformed to odds ratios. ' if regression.get('variance_method') else
                 'Only the returned ORs and intervals are quoted; the complete coefficient-variance procedure was not supplied. ') +
                 ('For imputed models, coefficients and full covariance matrices are pooled; t critical values use the corresponding degrees of freedom.' if m and m > 1 else ''))
    else:
        ci_method = str(methods.get('proportion_ci') or '').lower()
        variance = str(design.get('variance_method') or '').lower()
        detail = 'Strata and PSUs enter the Taylor-linearized standard error. ' if 'taylor' in variance else 'SE in the table is the returned standard error. '
        if 't-wald' in ci_method or 'pooled t' in ci_method:
            detail += 'Intervals use `estimate ± t critical value × SE`. The critical value depends on the design or pooled degrees of freedom; it is not fixed across all groups. '
        elif ci_method:
            detail += 'The intervals follow the recorded procedure; this explanation does not substitute another method. '
        else:
            detail += 'The interval algorithm or degrees of freedom were not supplied, so only the returned intervals are quoted; no error bounds are invented. '
        example = next((row for row in survey.get('rows', []) if _released(row)), None)
        if example:
            df = example.get('pooled_df') if m and m > 1 else example.get('design_df')
            if finite(df):
                detail += 'For the {} group, the recorded {}degrees of freedom are {:.1f}. '.format(_row_name(example), 'pooled ' if m and m > 1 else 'design ', df)
        steps.append('**Retain standard errors and intervals.** ' + detail)
    if spec.get('analysis') == 'gradient' and _valid_contrasts(survey):
        steps.append('**Calculate between-group differences separately.** Difference intervals use the registered contrast and joint covariance, retaining covariance between group estimates. They are not obtained by subtracting the endpoints of the separate group intervals.')
    elif regression.get('status') != 'ok':
        policy = methods.get('reliability_policy') or {}
        minimum = policy.get('minimum_unweighted_denominator')
        gate = 'The unweighted denominator must be at least {}. '.format(int(minimum)) if finite(minimum) else ''
        steps.append('**Apply publication checks.** ' + gate + 'Sample size, event counts, design degrees of freedom, variance and intervals must pass the registered checks. Failed cells remain suppressed rather than being filled with unstable estimates.')
    if reused:
        steps[0] = steps[0].replace('**Retain the full survey design first.** ', '**The original calculation retained the full design first.** ')
    return '\n'.join('{}. {}'.format(index, step) for index, step in enumerate(steps, 1))


def _model_note(result, rows):
    measurement = result.get('measurement') or {}
    state = measurement.get('status')
    if not state:
        return ''
    if state == 'not_requested':
        return 'Model measurement was not enabled for this run; the figures above are survey estimates.'
    if state == 'not_applicable':
        return 'Aggregate model questions do not produce regression coefficients, so there is no model-regression comparison.'
    valid, planned = measurement.get('valid_calls'), measurement.get('planned_calls')
    calls = ' (valid calls: {}/{})'.format(int(valid), int(planned)) if finite(valid) and finite(planned) else ' (aggregate call counts were not fully recorded)'
    if state in ('partial', 'failed', 'blocked'):
        prefix = ('Model measurement was only partially completed' if state == 'partial' else 'Model measurement did not complete') + calls + '; valid responses cover only the completed portion and must not be treated as a complete repeated measurement. '
    else:
        prefix = 'Model measurement is recorded as complete' + calls + '. '
    released_ids = {row.get('group_id') for row in rows if _released(row)}
    comparisons = [row for row in result.get('comparison', []) if row.get('group_id') in released_ids and finite(row.get('gap_pp'))]
    if comparisons:
        row = max(comparisons, key=lambda item: abs(item['gap_pp']))
        mean = row.get('model_mean')
        prefix += 'For {}, the model-minus-survey difference is {:+.2f} percentage points'.format(_row_name(row), row['gap_pp'])
        if finite(mean) and 0 <= mean <= 1:
            prefix += ', with a mean of valid model responses of ' + _pct(mean)
        if finite(row.get('valid_count')) and finite(row.get('total_count')):
            prefix += ' (valid calls for this group: {}/{})'.format(int(row['valid_count']), int(row['total_count']))
        prefix += '. '
        if finite(row.get('model_sd')):
            prefix += 'The SD of repeated model responses for this group is {:.2f} percentage points. '.format(row['model_sd'] * 100)
    if comparisons or state in ('ok', 'partial'):
        prefix += 'The model-response SD measures variation across answers, not the survey sampling SE; it is not pooled with the survey interval. Agreement with the survey mean does not make the model a substitute for the survey.'
    return prefix


def _blocked_answer(result, survey, spec):
    regression = survey.get('regression') or {}
    code = survey.get('code') or result.get('code')
    stages = {'invalid_spec': 'specification checks', 'data_preflight_failed': 'data-integrity checks',
              'r_runtime_unavailable': 'statistical-runtime checks', 'r_timeout': 'statistical execution',
              'r_analysis_failed': 'statistical execution', 'survey_engine_error': 'complex-survey estimation',
              'regression_gate_failed': 'regression-reliability checks', 'all_cells_suppressed': 'publication checks',
              'income_data_missing': 'income-imputation checks', 'no_releasable_gradient': 'gradient-publication checks'}
    stage = stages.get(code, 'statistical execution or publication checks')
    reason = survey.get('reason') or regression.get('reason') or result.get('reason')
    translations = {
        'all_cells_suppressed': 'all requested groups failed reliability checks',
        'r_runtime_unavailable': 'R or the required survey-analysis packages are unavailable',
        'income_data_missing': 'all 10 official income imputations have not been obtained and validated',
        'no_releasable_gradient': 'the required reference or comparison group has no releasable contrast',
    }
    if code in translations:
        explanation = translations[code]
    elif reason:
        explanation = re.sub(r'[-+]?\d+(?:\.\d+)?\s*%', '[unreleased value]', _safe(reason))[:180]
        if re.search(r'[\u3400-\u9fff]', explanation):
            explanation = 'a required data or statistical check failed; the original diagnostic is retained in the run details'
    else:
        explanation = 'the required specification, data or reliability conditions have not all been met'
    next_step = ('Specify a registered category, such as the education grouping or an explicit income threshold.' if code == 'invalid_spec' or result.get('status') == 'clarify' else
                 'Resolve the recorded data, runtime or publication-check issue before rerunning the same specification.')
    return ('**Conclusion: no usable statistical estimate was released.**\n\n' + scope_name(spec) + '\n\n'
            'The workflow stopped at **' + stage + '**: ' + explanation + '. No group proportion, difference or OR can be reported from this attempt, and unperformed steps must not be described as completed.\n\n' + next_step)


def build_analysis_answer(result, spec, previous=None, question=None):
    """Build explanatory Markdown solely from released, recorded analysis fields.

    ``previous`` is accepted for conversation integration; current statistics are
    never inferred from an earlier run. Run identifiers remain message metadata.
    """
    result = result if isinstance(result, dict) else {}
    spec = spec if isinstance(spec, dict) else {}
    detailed = detail_requested(question)
    survey = result.get('survey') or {}
    regression = survey.get('regression') or {}
    if (result.get('status') != 'ok' or survey.get('status') not in (None, 'ok') or
            regression.get('status') not in (None, 'ok')):
        return _blocked_answer(result, survey, spec)
    rows = survey.get('rows') or []
    released = [row for row in rows if _released(row)]
    contrasts = _valid_contrasts(survey)
    terms = _regression_terms(regression, question) if regression.get('status') == 'ok' else []
    if regression.get('status') == 'ok':
        if terms:
            term = terms[0]
            reference = (regression.get('references') or {}).get('education' if str(term['term']).startswith('education') else 'income' if str(term['term']).startswith('income') else '')
            reference_text = category_name(reference) if reference else 'the registered reference group'
            conclusion = 'For {} versus {}, the adjusted OR is {:.3f} (95% CI {:.3f}–{:.3f}), describing association conditional on the registered covariates.'.format(
                _term_name(term['term']), reference_text, term['odds_ratio'], term['ci_low'], term['ci_high'])
        else:
            conclusion = 'The regression returned, but the record does not provide complete, quotable non-intercept ORs and intervals. Missing coefficients are not invented.'
    elif contrasts:
        conclusion = _contrast_statement(contrasts[0])
    elif len(released) == 1:
        row = released[0]
        conclusion = 'For {}, the weighted proportion reporting fair or poor health is {} (95% CI {}).'.format(_row_name(row), _pct(row['estimate']), _interval(row))
    elif released:
        lookup = {row.get('group_id'): row for row in released}
        endpoints = {'education': ('less_than_high_school', 'bachelors_plus'), 'income': ('below_100', '400_plus')}.get(spec.get('group_by'))
        if endpoints and all(key in lookup for key in endpoints):
            first, last = [lookup[key] for key in endpoints]
        else:
            first, last = max(released, key=lambda row: row['estimate']), min(released, key=lambda row: row['estimate'])
        if endpoints and all(key in lookup for key in endpoints):
            conclusion = 'In this analysis population, the “{}” group has a fair-or-poor proportion of {}, compared with {} for the “{}” group. The first-minus-second point-estimate difference is {} percentage points.'.format(
                _row_name(first), _pct(first['estimate']), _pct(last['estimate']), _row_name(last), _pp(first['estimate'] - last['estimate']))
        else:
            conclusion = 'Among released estimates, {} has the highest point estimate ({}) and {} the lowest ({}), a difference of {} percentage points.'.format(
                _row_name(first), _pct(first['estimate']), _row_name(last), _pct(last['estimate']), _pp(first['estimate'] - last['estimate']))
    else:
        conclusion = 'No released group estimate is available to quote. Suppressed values are not used to rank groups or explain differences.'
    sections = ['**Conclusion:** ' + conclusion, scope_name(spec)]
    if _cache_hit(result, survey):
        sections.append('This run reused a validated statistical result with matching data, specification, code and runtime versions; **R was not rerun**. The steps below explain the original calculation.' if detailed else
                        'This reuses a validated cached survey result; **R was not rerun**.')
    if regression.get('status') == 'ok':
        if terms:
            sections.append(_regression_table(survey, regression, terms))
        formula = str(regression.get('formula') or '')
        references = regression.get('references') or {}
        reference_text = ', '.join(VARIABLES[key] + '=' + category_name(value) for key, value in references.items()
                                   if key in VARIABLES and re.search(r'\b' + re.escape(key) + r'\b', formula))
        detail = 'Reference groups: ' + reference_text + '. ' if reference_text else 'The complete reference-group information was not supplied.'
        if regression.get('income_adjusted') is True:
            detail += 'This model adjusts for income.'
        elif regression.get('income_adjusted') is False:
            detail += 'This is the reduced model without income, rather than the full income-adjusted model.'
        sections.append(detail)
    elif rows:
        sections.append(_rate_table(rows, spec.get('group_by') == 'education_race'))
    if contrasts:
        chosen = contrasts[:4]
        lines = ['| Registered contrast | Difference (percentage points) | 95% difference interval (percentage points) |', '|---|---:|---|']
        for contrast in chosen:
            lines.append('| {} | {} | {} to {} |'.format(_contrast_name(contrast), _pp(contrast['estimate']), _pp(contrast['ci_low']), _pp(contrast['ci_high'])))
        prefix = 'Only the first {} registered contrasts are shown here; the remainder are in the statistical details.\n\n'.format(len(chosen)) if len(contrasts) > len(chosen) else ''
        sections.append(prefix + '\n'.join(lines))
    sections.append(('### How the calculation works\n\n' + _method_steps(result, survey, spec, regression)) if detailed else
                    '**Method in brief**\n\n' + _method_summary(survey, spec, regression))
    interpretation = []
    if regression.get('status') == 'ok':
        interpretation.append('An OR compares odds, not the probability of poor health or a risk ratio. A multiplicative change in odds is not the same multiplicative change in probability. Changes between unadjusted and adjusted ORs do not establish an effect caused by a particular covariate.')
        if terms:
            first = terms[0]
            interpretation.append('This interval includes the no-association reference value of 1, so no association for this term under the registered adjusted model remains compatible with the data.' if first['ci_low'] <= 1 <= first['ci_high'] else
                                  'This interval excludes 1, supporting a conditional association under this registered model. Residual confounding and model specification remain limitations.')
    else:
        interpretation.append('A proportion is the weighted share of a group reporting fair or poor health. Subtracting proportions gives percentage points, not a relative percentage increase. The n in the table counts actual valid respondents, not an estimated national population.')
        if contrasts:
            if any(row['ci_low'] <= 0 <= row['ci_high'] for row in contrasts):
                interpretation.append('At least one registered difference interval includes 0, so that difference remains uncertain. Uncertainty does not establish that the groups are identical.')
            else:
                interpretation.append('The listed registered difference intervals exclude 0, but these comparisons do not automatically receive a multiplicity adjustment. They do not establish a general or causal conclusion.')
        elif len(released) > 1:
            interpretation.append('The between-group gap is currently a descriptive point-estimate comparison. Overlap between the separate 95% CIs is not a test of that difference; the difference and its joint variance must be estimated separately.')
    if detailed:
        interpretation.append('A 95% CI describes a procedure that would cover the population parameter in about 95% of repeated samples under the same design and interval assumptions. It is not a range of individual health outcomes. Cross-sectional associations do not establish causation.')
        sections.append('### How to interpret these results\n\n' + '\n\n'.join(interpretation))
    else:
        if regression.get('status') == 'ok':
            concise = ['OR compares odds, not the probability of poor health or a risk ratio.']
            if terms:
                first = terms[0]
                concise.append('The interval includes the no-association reference value of 1; no association for this term under the registered adjusted model remains compatible with the data.' if first['ci_low'] <= 1 <= first['ci_high'] else
                               'The interval excludes 1, supporting a conditional association under this model.')
        elif contrasts:
            concise = ['Differences are in percentage points. These exploratory comparisons have no automatic multiplicity adjustment.']
        elif len(released) > 1:
            concise = ['The gap is in percentage points. It is a point-estimate comparison; separate CIs do not test the difference.']
        else:
            concise = ['The CI describes sampling uncertainty in the group proportion.']
        concise.append('Cross-sectional associations do not establish causation.')
        sections.append(' '.join(concise))
    notes = []
    missing = _missing_notes(survey, spec)
    if missing:
        notes.append(missing)
    suppressed = [row for row in rows if row.get('status') != 'ok']
    if suppressed:
        reasons = list(dict.fromkeys(_suppression_reason(row.get('suppression_reason')) for row in suppressed))
        notes.append('{} group(s) are suppressed ({}); their values do not enter the conclusion or the model-gap comparison.'.format(len(suppressed), '; '.join(reasons)))
    model = _model_note(result, rows) if detailed or (result.get('measurement') or {}).get('status') not in ('not_requested', 'not_applicable') else ''
    if model:
        notes.append(model)
    if notes:
        sections.append(('### Denominators and completeness\n\n' if detailed else '') + '\n\n'.join(notes))
    return _RUN_ID.sub('the associated record', '\n\n'.join(sections))
