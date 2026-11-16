"""English explanations of saved NHIS results, with bilingual intent detection.

Only registered methods and observable artifacts are described. No private
reasoning is reconstructed, and no statistical program or model is called.
"""
import html
import math
import re

from .answers import LABELS as CATEGORY_LABELS, scope_name, category_name, _same_sample

GROUP_LABELS = {'overall': 'overall population', 'education': 'education', 'income': 'income',
                'race': 'race/ethnicity', 'sex': 'sex', 'age': 'age group',
                'education_race': 'education × race/ethnicity'}
_RUN_ID = re.compile(r'\b[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}\b')
_SECRET = re.compile(r'\bsk-[A-Za-z0-9_-]{12,}')
_SOURCE = 'https://www.cdc.gov/nchs/nhis/documentation/2024-nhis.html'
_STAGE_LABELS = {'data': 'official-data checks', 'statistics': 'survey estimation',
                 'validation': 'reliability checks', 'scope': 'specification checks',
                 'registration': 'analysis registration', 'model': 'model measurement',
                 'comparison': 'model–survey comparison', 'publication': 'publication checks',
                 'answer': 'answer preparation', 'understand': 'request interpretation'}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clean(value):
    text = _SECRET.sub('[credential redacted]', _RUN_ID.sub('the associated run record', str(value)))
    return html.escape(text, quote=False).replace('|', r'\|')


def _english_diagnostic(value):
    if re.search(r'[\u3400-\u9fff]', str(value)):
        return 'The original diagnostic is preserved in the run details; no missing condition is inferred from this excerpt.'
    return _clean(value)


def _label(value):
    return _clean(category_name(value, 'the registered group'))


def _row_name(row):
    group = row.get('group_id')
    if group and all(part in CATEGORY_LABELS for part in str(group).split('|')):
        return 'All registered adults' if group == 'overall' else _label(group)
    fallback = row.get('label') or group or 'the registered group'
    return _clean(fallback) if not re.search(r'[\u3400-\u9fff]', str(fallback)) else 'The registered group'


def _contrast_name(row):
    if row.get('group_id') and row.get('reference_id'):
        return _label(row['group_id']) + ' minus ' + _label(row['reference_id'])
    return _english_diagnostic(row.get('label') or 'The registered contrast')


def _num(value):
    return f'{int(value):,}' if _finite(value) and float(value).is_integer() else f'{value:,.2f}' if _finite(value) else 'Not recorded'


def _pct(value):
    return f'{value * 100:.2f}%' if _finite(value) and 0 <= value <= 1 else 'Not recorded'


def _pp(value):
    return f'{value * 100:+.2f}' if _finite(value) else 'Not recorded'


def _range(value):
    if not isinstance(value, dict) or not _finite(value.get('min')) or not _finite(value.get('max')):
        return None
    return _num(value['min']) if value['min'] == value['max'] else _num(value['min']) + '–' + _num(value['max'])


def _survey(current):
    return current.get('survey', {}) if isinstance(current, dict) and isinstance(current.get('survey'), dict) else {}


def _success(current):
    return isinstance(current, dict) and current.get('status') == 'ok' and _survey(current).get('status') == 'ok'


def _rows(current):
    if not _success(current):
        return []
    return [row for row in _survey(current).get('rows', []) if isinstance(row, dict)
            and row.get('status') == 'ok' and _finite(row.get('estimate')) and 0 <= row['estimate'] <= 1]


def _ci(row):
    p, low, high = row.get('estimate'), row.get('ci_low'), row.get('ci_high')
    return _finite(p) and _finite(low) and _finite(high) and 0 <= low <= p <= high <= 1


def _contrasts(current):
    if not _success(current):
        return []
    components = {row.get('group_id'): row for row in _rows(current) if _ci(row)}
    return [row for row in (_survey(current).get('gradient') or {}).get('contrasts', [])
            if isinstance(row, dict) and row.get('status') == 'ok'
            and all(_finite(row.get(key)) for key in ('estimate', 'se', 'ci_low', 'ci_high'))
            and row['se'] > 0 and row['ci_low'] <= row['estimate'] <= row['ci_high']
            and row.get('group_id') in components and row.get('reference_id') in components]


def _terms(current):
    if not _success(current):
        return []
    regression = _survey(current).get('regression', {})
    if not isinstance(regression, dict) or regression.get('status') != 'ok':
        return []
    return [row for row in regression.get('coefficients', []) if isinstance(row, dict)
            and row.get('status', 'ok') == 'ok' and row.get('term') != '(Intercept)'
            and all(_finite(row.get(key)) for key in ('odds_ratio', 'ci_low', 'ci_high'))
            and 0 < row['ci_low'] <= row['odds_ratio'] <= row['ci_high']]


def _term_label(term):
    value = str(term)
    for prefix in ('education', 'income', 'race', 'sex', 'age'):
        if value.startswith(prefix) and value[len(prefix):] in CATEGORY_LABELS:
            return _label(value[len(prefix):])
    return _clean(value)


def _scope(current):
    spec = current.get('spec', {}) if isinstance(current, dict) else {}
    return scope_name(spec if isinstance(spec, dict) else {})


def _example(current):
    rows = _rows(current)
    if not rows:
        return None
    row = rows[0]
    text = 'For **' + _row_name(row) + '**, the survey-weighted proportion is **' + _pct(row['estimate']) + '**'
    if _ci(row):
        text += ', with a 95% interval of **' + _pct(row['ci_low']) + '–' + _pct(row['ci_high']) + '**'
    if _finite(row.get('n')):
        text += ' and an unweighted sample size of **' + _num(row['n']) + '**'
    return text + '.'


def _sections(parts):
    return '\n\n'.join('### ' + heading + '\n\n' + body for heading, body in parts if body)


def _intent(text):
    q = text.lower()
    patterns = [
        ('floor', r'(?:为什么|为何|解释).{0,12}样本.{0,8}100.{0,12}(?:不|抑制|显示)|发布门槛|样本下限.*为什么|(?:why|publication|release).{0,30}(?:100|sample floor)|why.{0,20}small samples'),
        ('age', r'(?:为什么|为何).{0,6}(?:只看|使用|选|限定|取).{0,4}25.{1,4}64|why.{0,30}25.{1,8}64'),
        ('mi', r'多重插补|rubin|(?<![a-z])mi(?![a-z])|multiple imputation|插补.{0,8}(?:怎么|如何|为什么|什么意思)|(?:怎么|如何).{0,8}插补'),
        ('weights', r'权重.{0,8}(?:怎么|如何|使用|用法)|为什么.{0,6}(?:用|要|需要).{0,3}加权|为何.{0,6}加权|什么是加权比例|加权比例(?:是什么|什么意思|怎么算|如何计算)|(?:how|why|what).{0,30}weight|^weights?\s*[?？]?$|survey weights'),
        ('variables', r'(?:用了|使用|包括|哪些|什么).{0,6}变量|变量.{0,6}(?:哪些|什么)|(?:what|which).{0,25}(?:variables|covariates)|variables used'),
        ('source', r'数据.{0,4}(?:来源|哪里|哪来)|来源.{0,4}是什么|where.*data|data source|source of (?:the )?data'),
        ('sample_filter', r'(?:怎么|如何).{0,4}筛选.{0,4}样本|样本.{0,4}(?:怎么|如何).{0,4}筛选|how.{0,30}(?:filter|select).{0,15}(?:sample|respondents)'),
        ('steps', r'怎么算|怎么计算|如何计算|怎么得出|计算公式|计算步骤|具体计算|计算过程|详细计算|解释步骤|一步步|为什么这样算|详细一点|再详细点|讲详细点|详细解释|展开讲讲|继续解释|具体一点|展开说|多说一点|how.*calculat|more detail|elaborate|step.by.step|break it down|walk me through|show (?:me )?the (?:steps|calculation)'),
        ('ci', r'置信区间|confidence interval|(?<![a-z])ci(?![a-z])'),
        ('suppression', r'为什么抑制|抑制原因|为何抑制|为什么不显示|没有数字|不发布|suppres|why.{0,20}(?:hidden|not shown|not published)'),
        ('sample', r'样本(?:量|数|有多少)|分母|sample size|denominator|how many.{0,20}(?:respondents|participants|people)'),
        ('significance', r'显著|significant|significance'),
        ('regression', r'优势比|回归系数|(?<![a-z])or(?![a-z])\s*(?:的\s*)?(?:是?什么意思|是什么|如何解释|怎么理解|代表什么|含义|意义|怎么看|解释)|(?:解释|什么是)\s*(?<![a-z])or(?![a-z])|比值比|odds ratio|regression coefficient|^(?:what (?:does|is)|explain|interpret|how.{0,12}interpret).{0,15}\bor\b'),
        ('model', r'模型.{0,8}(?:怎么|如何)|提示词|英文提问|用什么模型|model.*prompt|how.{0,15}(?:ask|query|measure).{0,15}model|model measurement|blind measurement'),
        ('comparison', r'和刚才|与刚才|上一次相比|跟之前|前后比较|compared.*last|compare.{0,12}previous'),
        ('summary', r'解释一下|这个差异说明什么|这意味着什么|意味着什么|总结|详细一点|详细解释|展开讲讲|继续解释|结果怎么看|怎么看这个结果|interpret|summari|what does (?:that|this|the result|it) mean|what do (?:these|the results) mean|explain (?:that|this|the result)'),
    ]
    return next((intent for intent, pattern in patterns if re.search(pattern, q)), None)


def _blocked(current):
    survey = _survey(current)
    process = current.get('process', []) if isinstance(current.get('process'), list) else []
    stops = [event for event in process if isinstance(event, dict) and event.get('status') in ('blocked', 'failed')]
    stopped = stops[-1] if stops else None
    reason = current.get('answer') or survey.get('reason') or survey.get('message')
    parts = [('The latest analysis did not release a successful result', 'The latest attempt was incomplete or failed a required check. This explanation will not substitute estimates from an earlier run.')]
    if stopped:
        label = _STAGE_LABELS.get(stopped.get('stage'), stopped.get('label') or 'the recorded check')
        body = 'The workflow stopped at **' + _english_diagnostic(label) + '**.'
        if stopped.get('detail'):
            body += '\n\n' + _english_diagnostic(stopped['detail'])
        parts.append(('Where it stopped', body))
    else:
        parts.append(('What the record establishes', 'The stopping stage is not recorded; it cannot be attributed to data, the statistical program or a model interface from this excerpt.'))
    if reason:
        parts.append(('Recorded reason', _english_diagnostic(reason)))
    suppressed = [row for row in survey.get('rows', []) if isinstance(row, dict) and row.get('status') == 'suppressed']
    if suppressed:
        parts.append(('Unreleased cells', '\n'.join('- ' + _row_name(row) + ': ' + _english_diagnostic(row.get('suppression_reason') or 'Publication checks failed') for row in suppressed[:6])))
    parts.append(('Next step', 'Resolve the recorded missing condition or clarify the specification before rerunning. A failed check does not mean the group’s health proportion is zero.'))
    return _sections(parts), 'explanation'


def _sample(current):
    survey = _survey(current)
    denominators = survey.get('denominators', {})
    lines = []
    for key, label in [('full_sample_n', 'Full public adult sample'), ('age_domain_n', 'Age domain'),
                       ('filtered_domain_n', 'After category filters'), ('valid_outcome_n', 'Valid-outcome denominator')]:
        if _finite(denominators.get(key)):
            lines.append('- ' + label + ': unweighted n=' + _num(denominators[key]) + '.')
    parts = [('Keep the denominators distinct', 'Unweighted n counts actual respondents. A weighted denominator describes their represented population, while the valid analysis denominator also depends on the outcome and required variables. These quantities are not interchangeable.'),
             ('Recorded sample flow', '\n'.join(lines) if lines else 'The complete sample-flow counts were not recorded. Adding the displayed groups does not reconstruct the overall denominator.')]
    table = ['| Group | Unweighted n |', '|---|---:|']
    for row in _rows(current)[:8]:
        if _finite(row.get('n')):
            count = _num(row['n'])
            interval = _range(row.get('n_range'))
            if interval and interval != count:
                count += ' (range ' + interval + ')'
            table.append('| ' + _row_name(row) + ' | ' + count + ' |')
    if len(table) > 2:
        parts.append(('Actual denominators for released groups', '\n'.join(table)))
    regression = survey.get('regression', {})
    if regression.get('status') == 'ok' and _finite(regression.get('n')):
        body = 'The complete-case regression denominator is **n=' + _num(regression['n']) + '**.'
        if _same_sample(survey, regression):
            body += ' The unadjusted and adjusted models use the same analysis sample.'
        elif regression.get('unadjusted', {}).get('n') == regression['n']:
            body += ' Their recorded sample sizes are equal; equal counts alone do not verify identical membership.'
        exclusions = _range(regression.get('complete_case_exclusions_range'))
        if exclusions:
            body += ' Relative to the category-filtered domain, complete-case exclusions are ' + exclusions + ', including missing outcomes.'
        parts.append(('Regression denominator', body))
    if survey.get('imputation', {}).get('required') is True:
        parts.append(('Counting across income imputations', 'These counts are not added across imputations. The minimum valid n and its range are reported; repeated imputations do not create additional independent respondents.'))
    return _sections(parts), 'explanation'


def _mi(current):
    survey = _survey(current); imp = survey.get('imputation', {})
    parts = [('Why pool imputations?', 'Income nonresponse introduces additional uncertainty. The registered procedure uses all 10 official income imputations, matched to the adult file by HHX. Each imputation first receives the full survey design and is then restricted to the same domain.'),
             ('What is combined?', 'Average the estimates and retain both **within-imputation variance** and **between-imputation variance**. Rubin’s total variance is `T = Ū + (1 + 1/m) × B`. Standard errors must not be averaged, and a single imputation must not stand in for all imputations. Intervals use the appropriate pooled degrees of freedom.')]
    if imp.get('required') is True and _finite(imp.get('m')):
        body = 'The record reports **' + _num(imp['m']) + ' imputations**.'
        if imp.get('method'):
            body += ' Recorded pooling method: ' + _english_diagnostic(imp['method']) + '.'
        parts.append(('What this analysis actually used', body))
        row = next((row for row in _rows(current) if isinstance(row.get('imputation'), dict)
                    and _finite(row['imputation'].get('within_variance')) and _finite(row['imputation'].get('between_variance'))), None)
        if row and imp['m'] > 1:
            inside, between = row['imputation']['within_variance'], row['imputation']['between_variance']
            total = inside + (1 + 1 / imp['m']) * between
            if total >= 0:
                parts.append(('An example from the recorded result', _row_name(row) + ' has within-imputation variance `' + f'{inside:.8g}' + '` and between-imputation variance `' + f'{between:.8g}' + '`. Applying the recorded imputation count gives total variance `' + f'{total:.8g}' + '`. These are variances on the proportion scale, not percentage points or respondent counts.'))
    elif imp.get('required') is False:
        parts.append(('Status of this analysis', 'This record explicitly says that **income imputation was not required**. The procedure above describes income analyses; it does not mean this run performed 10 imputed analyses.'))
    else:
        parts.append(('Limits of this record', 'The actual imputation count and pooling status were not supplied, so use of all 10 imputations cannot be confirmed.'))
    parts.append(('Sample size is not multiplied by ten', 'The imputations concern the same survey respondents. Income-cell n is reported as the minimum and range of the valid denominators; the 10 panels must not be stacked and treated as independent samples.'))
    return _sections(parts), 'method'


def _steps(current):
    survey = _survey(current)
    parts = [('1. Fix the question and population', _scope(current) + ' The outcome is fair or poor self-rated health. Grouping, missing-data rules and model formulas come from the registered specification, rather than being changed after inspecting results.')]
    design = survey.get('design', {})
    body = 'Construct the survey design on the full adult file with `WTFA_A` weights, `PSTRAT` strata and `PPSU` sampling units before restricting it to the analysis domain.'
    body += ' This record confirms that order.' if design.get('constructed_before_domain_subset') is True else ' This is the registered method; this excerpt does not contain a design flag sufficient to confirm its execution.'
    count = survey.get('denominators', {}).get('full_sample_n')
    if _finite(count): body += ' The recorded full sample is n=' + _num(count) + '.'
    parts.append(('2. Retain the full design before subsetting', body))
    body = 'Code `PHSTAT_A=4 or 5` as 1 and `1, 2 or 3` as 0. Refused, don’t-know and not-ascertained responses are excluded, not counted as healthy. Each proportion uses valid outcomes and the variables required for its grouping.'
    for key, label in [('age_domain_n', 'Age domain'), ('filtered_domain_n', 'Filtered domain'), ('valid_outcome_n', 'Valid-outcome denominator')]:
        count = survey.get('denominators', {}).get(key)
        if _finite(count): body += '\n- ' + label + ': n=' + _num(count) + '.'
    parts.append(('3. Define the outcome and valid denominator', body))
    regression = survey.get('regression', {})
    if regression.get('status') == 'ok':
        body = 'Fit the registered survey logistic model, retain its coefficients and design covariance, and transform log-odds and their intervals to the OR scale.'
        if _finite(regression.get('n')): body += ' Complete-case n=' + _num(regression['n']) + '.'
        if _same_sample(survey, regression): body += ' The record confirms that the unadjusted and adjusted models use the same sample.'
        exclusions = _range(regression.get('complete_case_exclusions_range'))
        if exclusions: body += ' Relative to the category-filtered domain, ' + exclusions + ' complete-case exclusions include missing outcomes.'
        parts.append(('4. Estimate the registered model', body))
    else:
        body = 'For each group, calculate `Σ(weight × outcome indicator) / Σweight`. Survey standard errors use Taylor linearization with strata and PSUs, rather than a simple binomial calculation based only on headcounts.'
        example = _example(current)
        if example: body += '\n\nA recorded example: ' + example
        parts.append(('4. Estimate proportions and uncertainty', body))
    extra = []
    imp = survey.get('imputation', {})
    if imp.get('required') is True and _finite(imp.get('m')):
        extra.append('This income analysis records ' + _num(imp['m']) + ' imputations; estimates and covariance matrices are combined using Rubin’s rules.')
    contrasts = _contrasts(current)
    if contrasts:
        row = contrasts[0]
        extra.append('A difference does not use the sum of the separate standard errors: its joint variance retains between-group covariance. The recorded ' + _contrast_name(row) + ' difference is ' + _pp(row['estimate']) + ' percentage points, with a 95% difference interval of ' + _pp(row['ci_low']) + ' to ' + _pp(row['ci_high']) + ' percentage points.')
    if not extra:
        extra.append('A requested group gradient needs a separately estimated difference and joint covariance. Overlap of marginal group intervals is not a test of the difference.')
    parts.append(('5. Handle imputation and group comparisons', '\n\n'.join(extra)))
    evidence = []
    cache = current.get('cache', {}) if isinstance(current, dict) else {}
    if cache.get('hit') is True: evidence.append('This run reused a statistical cache with matching data, specification, code and R versions; R was not rerun. These steps explain the matched aggregate result.')
    elif cache.get('hit') is False: evidence.append('The run records that the statistical cache was not reused.')
    process = current.get('process', []) if isinstance(current, dict) else []
    completed = [_STAGE_LABELS.get(event.get('stage'), 'a recorded analysis stage') for event in process if isinstance(event, dict) and event.get('status') == 'complete']
    if completed: evidence.append('Recorded completed stages include: ' + ', '.join(dict.fromkeys(completed)) + '.')
    digest = survey.get('provenance', {}).get('sha256') or (current.get('data', {}).get('sha256') if isinstance(current, dict) else None)
    if isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest): evidence.append('An input-file SHA256 is saved and can be matched to the local data record.')
    evidence.append('Only estimates passing reliability checks are released. R/Stata numerical agreement and the external NCHS comparison still require independent validation.')
    parts.append(('6. Check reliability and retain the audit record', '\n\n'.join(evidence)))
    return _sections(parts), 'method'


def _confidence(current):
    parts = [('What a 95% interval means', 'It describes sampling uncertainty when estimating a population proportion under the survey design. Over repeated samples with the same design and interval method, about 95% of the intervals would cover the true population proportion. It is not a range containing 95% of individuals, nor a 95% probability statement about a fixed parameter after observing its interval.')]
    example = _example(current)
    if example:
        parts.append(('Read the current result in that light', example + ' The point estimate gives the estimated level; the interval describes uncertainty under this design.'))
        row = _rows(current)[0]; detail = []
        if _finite(row.get('se')): detail.append('The recorded standard error is ' + f'{row["se"] * 100:.3f}' + ' percentage points.')
        if _finite(row.get('design_df')): detail.append('The cell has design degrees of freedom of ' + _num(row['design_df']) + '.')
        if detail: parts.append(('How the interval is constructed', ' '.join(detail) + ' The registered procedure uses design-based t intervals; income analyses also account for between-imputation variance and pooled degrees of freedom. A fixed 1.96 multiplier is not appropriate for every cell.'))
    else: parts.append(('No quotable interval is available', 'This excerpt does not contain a fully recorded, releasable proportion interval. The method can be explained without inventing an example for this run.'))
    contrasts = _contrasts(current)
    if contrasts:
        row = contrasts[0]
        parts.append(('For group comparisons, use the difference interval', 'The recorded ' + _contrast_name(row) + ' difference interval is ' + _pp(row['ci_low']) + ' to ' + _pp(row['ci_high']) + ' percentage points. Its joint covariance directly describes uncertainty in this difference.'))
    else: parts.append(('Separate intervals are not a difference test', 'Whether group intervals overlap does not replace a test of their difference. The difference and its joint variance must be estimated separately; missing difference intervals and p-values are not calculated during an explanation.'))
    return _sections(parts), 'method'


def _regression(current, significance=False):
    survey = _survey(current); regression = survey.get('regression', {}); terms = _terms(current)
    parts = [('Distinguish odds ratios from proportions', 'A logistic-regression OR is a **ratio of odds** relative to a reference category, not a population proportion or a risk ratio. An adjusted OR describes association under the registered covariate model; it cannot be directly converted to a risk difference or causal effect.')]
    if regression.get('status') == 'ok' and terms:
        term = terms[0]
        parts.append(('Use a recorded coefficient as an example', 'For **' + _term_label(term['term']) + '**, the adjusted OR is **' + f'{term["odds_ratio"]:.3f}' + '** (95% interval **' + f'{term["ci_low"]:.3f}–{term["ci_high"]:.3f}' + '**). The direction is relative to the registered reference group; it is not that group’s absolute probability of poor health.'))
        sample = []
        if _finite(regression.get('n')):
            sample.append('Complete-case n=' + _num(regression['n']) + '.')
            if _same_sample(survey, regression): sample.append('The unadjusted education model and adjusted model use the same analysis sample.')
            elif regression.get('unadjusted', {}).get('n') == regression['n']: sample.append('The recorded model sample sizes are equal; this alone does not confirm identical membership.')
        if isinstance(regression.get('formula'), str): sample.append('The registered formula is `' + _clean(regression['formula']) + '`.')
        if sample: parts.append(('Adjustment and analysis sample', ' '.join(sample)))
        if significance:
            table = ['| Model term | Adjusted OR | 95% interval | Includes 1? |', '|---|---:|---|---|']
            for row in terms[:5]: table.append('| ' + _term_label(row['term']) + ' | ' + f'{row["odds_ratio"]:.3f}' + ' | ' + f'{row["ci_low"]:.3f}–{row["ci_high"]:.3f}' + ' | ' + ('Yes' if row['ci_low'] <= 1 <= row['ci_high'] else 'No') + ' |')
            parts.append(('Use the appropriate null reference', '\n'.join(table)))
    else: parts.append(('Limits of the current record', 'No released regression coefficient is available to quote. A proportion table does not generate adjusted ORs. This is a method explanation, not evidence that a new model was fitted.'))
    parts.append(('How to interpret the comparison', 'The no-association reference for an OR is 1. An interval containing 1 does not prove no association, and an interval excluding 1 does not prove causation. These exploratory comparisons do not automatically receive a multiplicity adjustment.'))
    return _sections(parts), 'explanation' if terms else 'method'


def _significance(current):
    contrasts = _contrasts(current)
    if contrasts:
        table = ['| Contrast | Difference (percentage points) | 95% difference interval | Includes 0? |', '|---|---:|---|---|']
        for row in contrasts[:6]: table.append('| ' + _contrast_name(row) + ' | ' + _pp(row['estimate']) + ' | ' + _pp(row['ci_low']) + ' to ' + _pp(row['ci_high']) + ' | ' + ('Yes' if row['ci_low'] <= 0 <= row['ci_high'] else 'No') + ' |')
        return _sections([('The recorded difference intervals can be assessed directly', '\n'.join(table)), ('Why use these intervals?', 'A difference variance includes covariance between the group estimates. Whether its 95% interval includes 0 addresses uncertainty in that registered contrast; overlap of marginal intervals is not a substitute.'), ('Limits of the conclusion', 'Including 0 does not establish no association; excluding 0 does not establish causation. No new p-values or multiplicity corrections are generated here.')]), 'explanation'
    if _terms(current): return _regression(current, True)
    return _sections([('Group significance cannot be determined from this record', 'There is no computed joint variance for a group difference and no released coefficient interval to interpret. Separate proportions or an incomplete contrast record are insufficient.'), ('What is needed?', 'A specific contrast and its joint covariance must be estimated. Education or income can use the registered gradient analysis.'), ('Why interval overlap is insufficient', 'Overlap between separate group intervals is not a formal test of their difference. Their individual 95% intervals are not a 95% interval for the difference. Missing intervals and p-values cannot be supplied through prose.')]), 'explanation'


def _summary(current):
    rows = _rows(current); contrasts = _contrasts(current)
    if _terms(current): return _regression(current)
    if not rows: return _sections([('There are not enough released estimates to interpret', 'No group proportion in this excerpt passes the status and value checks; a conclusion cannot be filled in from incomplete content.'), ('A useful next question', 'Run a specific registered grouping, or ask about variables, sample selection or calculation methods.')]), 'clarification'
    parts = [_scope(current).strip()]
    if len(rows) == 1: parts.append(_example(current) or 'The cell was released but its explanatory fields are incomplete.')
    else:
        low, high = min(rows, key=lambda row: row['estimate']), max(rows, key=lambda row: row['estimate'])
        parts.append('Among released groups, **' + _row_name(high) + '** reports fair or poor health most often (' + _pct(high['estimate']) + '), compared with ' + _pct(low['estimate']) + ' for **' + _row_name(low) + '**. The point-estimate gap is **' + f'{(high["estimate"] - low["estimate"]) * 100:.2f}' + ' percentage points**. Suppressed cells are excluded.')
    if contrasts:
        row = contrasts[0]
        parts.append('For the registered ' + _contrast_name(row) + ' comparison, the difference is **' + _pp(row['estimate']) + ' percentage points** (95% difference interval **' + _pp(row['ci_low']) + ' to ' + _pp(row['ci_high']) + '**), using joint survey covariance. ' +
                     ('Because that interval includes zero, the difference remains uncertain.' if row['ci_low'] <= 0 <= row['ci_high'] else
                      'The interval excludes zero, supporting a difference in this comparison.'))
    elif len(rows) > 1:
        parts.append('That gap does not establish statistical significance without its joint variance. Separate group intervals are not a test of the difference.')
    parts[-1] += ' This describes self-rated health, not a clinical diagnosis, and cannot establish causation.'
    measurement = current.get('measurement', {})
    if measurement.get('status') in ('partial', 'failed', 'blocked'):
        counts = ''
        if _finite(measurement.get('valid_calls')) and _finite(measurement.get('planned_calls')): counts = ' Valid calls: ' + _num(measurement['valid_calls']) + '/' + _num(measurement['planned_calls']) + '.'
        parts.append('Model measurement status: **' + _clean(measurement['status']) + '**.' + counts + ' Incomplete calls are not a complete repeated experiment; survey group differences are not model-error estimates.')
    return '\n\n'.join(parts), 'explanation'


def build_explanation(text, current, previous=None):
    """Return ``(English Markdown, kind)`` for a recognized explanation."""
    if not isinstance(text, str): return None
    intent = _intent(text)
    if intent is None: return None
    if isinstance(current, dict) and (current.get('status') in ('blocked', 'failed', 'clarify') or _survey(current).get('status') == 'blocked'): return _blocked(current)
    current = current or {}
    if intent == 'steps':
        answer, kind = _steps(current)
        if re.search(r'详细一点|再详细点|讲详细点|详细解释|展开讲讲|继续解释|具体一点|展开说|多说一点|more detail|elaborate|break it down', text.lower()): kind = 'explanation'
        return answer, kind
    if intent == 'ci': return _confidence(current)
    if intent == 'mi': return _mi(current)
    if intent == 'sample': return _sample(current)
    if intent == 'significance': return _significance(current)
    if intent == 'regression': return _regression(current)
    if intent == 'summary':
        if not current: return _sections([('A result is needed to interpret a particular difference', 'This conversation has no quotable survey result yet. Ask for a registered analysis such as an education comparison, then ask about proportions, intervals or sample sizes.'), ('You can still ask about methods', 'Ask “How did you calculate that?” or “Break down the calculation.” A method explanation does not rerun the survey or independent model measurement; conversation API calls are audited separately.')]), 'clarification'
        return _summary(current)
    if intent == 'source':
        parts = [('Where the data come from', 'The benchmark is the official **NHIS 2024 Sample Adult** public-use file from NCHS at the U.S. CDC. Income analyses use all 10 official income imputations.'), ('How inputs are traced', '[Official CDC data and documentation](' + _SOURCE + '). Inputs are traced through local download records, checksums, the registered specification and statistical-code versions.')]
        digest = _survey(current).get('provenance', {}).get('sha256')
        parts.append(('What this record verifies', 'This result includes an input-CSV SHA256 that can be checked against the local source record.' if isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest) else 'The full input hash is absent from this excerpt. A source description alone does not authenticate the file used in this run.'))
        return _sections(parts), 'method'
    if intent == 'age': return _sections([('A prespecified research population', 'The project chose ages 25–64 to focus on working-age adults, after the age when most formal education is completed. This is a choice of research scope.'), ('Why fix it in advance?', 'Changing cut points after seeing the results can change the denominator. A registered range keeps the question, survey estimate and model prompt aligned to the same population.'), ('Other ages require another specification', 'The current results are not generalized to all ages; a different age range needs a separate registered analysis.')]), 'method'
    if intent == 'floor': return _sections([('The floor of 100 is not a health proportion', 'The registered application rule suppresses cells with unweighted n<100. Sparse estimates may be unstable for group comparisons.'), ('Reaching 100 is not sufficient', 'Event and non-event counts, design degrees of freedom, effective sample size, relative standard error and interval width must also pass checks.'), ('What a missing value means', 'Suppression means neither a zero proportion nor an absence of health differences. These are conservative application checks, not certification of the full NCHS presentation standards.')]), 'method'
    if intent == 'variables':
        parts = [('Outcome and grouping variables', '`PHSTAT_A` records self-rated health; 4 or 5 defines fair or poor. Registered groupings use `EDUCP_A` for education, income-to-poverty categories, `HISPALLP_A` for race/ethnicity, `AGEP_A` for age and `SEX_A` for sex.'), ('Survey-design variables', '`WTFA_A` is the final annual weight; `PSTRAT` and `PPSU` are the public-use pseudo-strata and pseudo-PSUs. They affect population estimates and their variances.')]
        if current.get('spec'): parts.append(('Specification used for this result', _scope(current) + ' Full and explicitly reduced no-income models remain distinct.'))
        return _sections(parts), 'method'
    if intent == 'weights': return _sections([('Why a headcount fraction is insufficient', 'Respondents represent different shares of the population. The weighted proportion is `Σ(weight × fair-or-poor indicator) / Σweight`; the denominator includes valid outcomes and required grouping values within the current domain.'), ('Reading the recorded result', _example(current) or 'No released proportion is available to quote. The formula describes the registered method, not a new estimate.'), ('Weights do not create independent respondents', 'Standard errors still account for strata and PSUs. A large weight does not turn a respondent into additional independent observations. Unweighted n, the weighted population denominator and effective sample size have different meanings.')]), 'method'
    if intent == 'sample_filter':
        parts = [('Build the design before defining the domain', 'Retain the complete adult public-use sample for the complex survey design, then restrict the analysis to ages 25–64 and the selected categories. Do not first drop other ages and rebuild a simplified design.'), ('Which records enter the denominator?', 'The binary outcome uses valid PHSTAT_A responses. Descriptive proportions require the variables relevant to their grouping. Regressions use complete cases for the registered model, with the unadjusted and adjusted models sharing the analysis sample.')]
        if current.get('spec'): parts.append(('Current restrictions', _scope(current) + ' Use recorded sample-flow counts rather than summing group tables.'))
        return _sections(parts), 'method'
    if intent == 'suppression':
        hidden = [row for row in _survey(current).get('rows', []) if isinstance(row, dict) and row.get('status') == 'suppressed']
        parts = [('Why a value is not shown', 'A suppressed cell failed a publication check; its proportion was not estimated as zero. Other reliability checks can prevent publication even after n≥100.')]
        if hidden: parts.append(('Actual recorded reasons', '\n'.join('- ' + _row_name(row) + ': ' + _english_diagnostic(row.get('suppression_reason') or 'No specific reason recorded') + ('; n=' + _num(row['n']) if _finite(row.get('n')) else '') + '.' for row in hidden[:8])))
        else: parts.append(('What can be established?', 'No cell with a specific suppression reason was supplied. An unrecorded failure reason cannot be assigned to it.'))
        parts.append(('Next step', 'Inspect the actual denominator, event counts and interval record before considering a separately registered, broader grouping. Do not repeatedly change thresholds to retain only attractive estimates.'))
        return _sections(parts), 'explanation'
    if intent == 'model':
        measurement = current.get('measurement', {})
        parts = [('Conversation and independent measurement are separate', 'The conversation API receives the current question and released aggregate evidence to organize the answer. It receives no individual survey records, and its prose is not an independent blind-measurement result.'), ('How blind-measurement prompts work', 'The independently measured model receives only registered group definitions and English aggregate-proportion questions, not individual records, sample sizes or benchmark proportions. Repetitions follow the registered plan; failures and refusals are retained without retrying until success.')]
        if measurement.get('model'):
            meta = 'Model and provider identifiers are held in the server-side measurement record.'
            for key, label in [('planned_calls', 'Planned calls'), ('valid_calls', 'Valid calls'), ('prompt_versions', 'Prompt versions'), ('repeats', 'Repeats per version')]:
                if _finite(measurement.get(key)): meta += ' ' + label + ': ' + _num(measurement[key]) + '.'
            parts.append(('What was actually recorded', meta))
        else: parts.append(('No independent measurement evidence', 'There is no valid model-measurement record to quote, so completion of a blind measurement cannot be claimed. Conversation-call audits are kept separately on the server.'))
        parts.append(('What the comparison measures', 'The model mean minus the survey proportion is a percentage-point gap. Variation across repeated model answers and survey sampling uncertainty are distinct and must not be conflated.'))
        return _sections(parts), 'method'
    # The conversation layer retains its previous-run comparison handler.
    return None
