"""Deterministic bilingual presentation of verified metadata and released results.

These helpers do not load data, execute statistics, or call a language model.
Language preference belongs to conversation context; numerical facts belong to
the supplied status/result objects and are never filled from expected counts.
"""
import html
import math
import re
from datetime import datetime
from urllib.parse import urlsplit

from . import answers
from .data import SOURCE_PAGE
from .outcomes import allowed_outcomes, get_outcome


_LANGUAGE = re.compile(
    r'(?:用|使用|改用|换成|改成|切换到|切换为|请说|请讲|译成|翻译成)\s*(?P<zh>中文|汉语|英文|英语)'
    r'|(?P<cnversion>中文|汉语|英文|英语)(?:版本|版|回答|回复|解释)'
    r'|\b(?:answer|reply|respond|write|explain|say|translate)\b[^.!?\n]{0,100}?\b(?:in|into)\s+(?P<en>Chinese|English)\b'
    r'|\b(?P<version>Chinese|English)\s+(?:version|please|answer|reply)\b'
    r'|\bin\s+(?P<trailing>Chinese|English)\s*(?:please)?[.!?\s]*$'
    r'|^\s*(?P<bare>中文|汉语|英文|英语|Chinese|English)\s*[。.!?？]*$', re.I)
_ZH_CATEGORIES = {
    'overall': '总体', 'all': '总体', 'less_than_high_school': '高中以下',
    'high_school': '高中或同等学历', 'some_college': '部分大学教育或副学士学位',
    'bachelors_plus': '学士及以上', 'male': '男性', 'female': '女性',
    'hispanic': '西班牙裔（不限种族）', 'nh_white': '非西班牙裔白人',
    'nh_black': '非西班牙裔黑人', 'nh_asian': '非西班牙裔亚裔',
    'nh_other': '非西班牙裔其他或多种族', '25_34': '25–34岁', '35_44': '35–44岁',
    '45_54': '45–54岁', '55_64': '55–64岁', 'below_100': '低于贫困线100%',
    '100_199': '贫困线100%至不足200%', '200_399': '贫困线200%至不足400%',
    '400_plus': '贫困线400%及以上',
}
_ZH_VARIABLES = {'overall': '总体', 'education': '教育程度', 'income': '收入组',
                 'race': '种族／族裔', 'sex': '性别', 'age': '年龄组',
                 'age_years': '年龄（岁）', 'education_race': '教育程度×种族／族裔'}
_ZH_OUTCOMES = {'fair_poor': '自评健康一般或较差', 'insured': '有医疗保险',
                'uninsured': '无医疗保险', 'delayed_care_cost': '过去12个月因费用推迟就医',
                'unmet_care_cost': '过去12个月因费用未获得所需医疗服务'}


def _requested_language(text):
    matches = list(_LANGUAGE.finditer(str(text or '')))
    if not matches:
        return None
    value = next(value for value in matches[-1].groups() if value).lower()
    return 'zh' if value in ('中文', '汉语', 'chinese') else 'en'


def select_language(text, context):
    """Return zh/en; persist an explicit request in the supplied context only."""
    requested = _requested_language(text)
    if isinstance(context, dict):
        if requested:
            context['response_language'] = requested
        return requested or (context.get('response_language') if context.get('response_language') in ('zh', 'en') else 'en')
    return requested or 'en'


def language_followup(text):
    """Recognize a language-only rewrite of a prior reply, never new analysis."""
    value = str(text or '').strip()
    if not _requested_language(value):
        return False
    # Remove directive words and references, retaining all topic/analysis words.
    value = re.sub(r'这[两三四]个(?:指标|结果|回答)|(?:上述|刚才|之前|上一次|上一轮)(?:的)?分析|上一个(?:问题|回答|结果)|\b(?:both|these|those)\s+(?:indicators|outcomes|analyses|results)\b', '', value, flags=re.I)
    value = re.sub(r'\b(?:chinese|english|please|can|could|would|you|answer|reply|respond|write|explain|say|translate|in|into|to|the|a|an|that|this|it|above|previous|last|result|results|response|question|again|version|of|me|for)\b', '', value, flags=re.I)
    value = re.sub(r'中文|汉语|英文|英语|麻烦|请|可以|能否|能不能|使用|改用|用|换成|改成|切换到|切换为|来|再|重新|回答|回复|解释|说明|表达|说|讲|输出|翻译|一下|一遍|版本|版|上面|上述|之前|刚才|这个|那个|这些|那些|这轮|上一轮|问题|结果|内容|的|成|为|把', '', value)
    return not re.sub(r'[\s，。！？、,.!?;；:："“”‘’\-]', '', value)


def dataset_topic(text):
    """Separate dataset facts from the denominator of a particular analysis."""
    value = str(text or '').lower()
    if re.search(r'这次分析|本次分析|当前分析|这个分析|这轮分析|有效样本|分析样本|当前分组|这组|these (?:analysis|group)|this (?:analysis|group)|current (?:analysis|sample)|valid (?:sample|respondents)|complete.case', value):
        return False
    database = re.search(r'数据|\b(?:database|dataset|data|nhis)\b', value)
    fact = re.search(r'多少|几条|几行|记录|样本量|数据量|人数|什么时候|啥时候|哪年|几年|年份|来源|哪里|来自|更新|实时|最新|时间|how many|rows?|records?|sample size|when|year|source|fresh|current|latest|real.time|updat|how old', value)
    return bool(database and fact) or bool(re.search(r'(?:数据|data).{0,12}(?:哪年|什么时候|啥时候|which year|what year|come from|collected|up.to.date)', value))


def _finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _count(value):
    if isinstance(value, dict):
        low, high = value.get('min'), value.get('max')
        if all(_finite(item) and item >= 0 and int(item) == item for item in (low, high)) and low <= high:
            return format(int(low), ',') if low == high else '{}–{}'.format(format(int(low), ','), format(int(high), ','))
        return None
    return format(int(value), ',') if _finite(value) and value >= 0 and int(value) == value else None


def _record_count(record, name):
    return _count(record.get(name + '_range')) or _count(record.get(name))


def _text(value):
    value = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[credential redacted]', str(value or ''))
    value = re.sub(r'\b\d{8}T\d{6}Z-[a-f0-9]{10}\b', '关联记录', value)
    return html.escape(value, quote=False).replace('|', '／').replace('`', '′').replace('\n', ' ').strip()


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).isoformat()
    except ValueError:
        return None


def dataset_answer(state, language='en'):
    """Render verified data.status metadata, never registry expected counts."""
    state = state if isinstance(state, dict) else {}
    data = state.get('data') if isinstance(state.get('data'), dict) else state
    zh = language == 'zh'
    if data.get('ready') is not True:
        return ('当前还没有通过核验、可用的数据，因此无法确认数据库实际记录数或调查年份。请等待数据准备与完整性核验完成。' if zh else
                'No verified dataset is ready, so the actual record count and survey year cannot be confirmed. Data preparation and integrity checks must finish first.')
    datasets = data.get('datasets') if isinstance(data.get('datasets'), dict) else {}
    adult = datasets.get('adult') if isinstance(datasets.get('adult'), dict) else {}
    rows = _count(data.get('row_count')) or _count(data.get('adult_row_count')) or _count(adult.get('row_count'))
    year = data.get('year')
    year = str(int(year)) if _finite(year) and 1900 <= year <= 2200 and int(year) == year else None
    if zh:
        lines = [('当前已核验的 NHIS 成人样本文件有 **' + rows + ' 条记录**。') if rows else '成人样本文件已通过核验，但当前状态没有提供可确认的记录数。']
        lines.append(('这些是 **' + year + ' 年 NHIS 调查**的数据，不是实时数据库。') if year else '当前状态没有提供可确认的调查年份；下载时间不能替代调查年份。')
    else:
        lines = [('The verified NHIS Sample Adult file contains **' + rows + ' records**.') if rows else 'The adult file is verified, but its actual record count is not supplied in the current status.']
        lines.append(('These are **' + year + ' NHIS survey** data, not a real-time database.') if year else 'The survey year is not supplied in the verified status; a download date is not a survey year.')
    downloaded = _timestamp(data.get('downloaded_at'))
    if downloaded:
        lines.append(('本地下载记录时间为 ' + downloaded + '；这是获取文件的时间，不是调查发生的时间。') if zh else
                     'The recorded download time is ' + downloaded + '; this is when the files were obtained, not when the survey was conducted.')
    income = datasets.get('income') if isinstance(datasets.get('income'), dict) else {}
    income_rows, imputations = _count(income.get('row_count')), _count(income.get('imputations'))
    if data.get('income_ready') is True and income_rows:
        if zh:
            lines.append('已核验的收入文件有 **' + income_rows + ' 条记录**' + ('，包含 **' + imputations + ' 份插补**' if imputations else '') + '。这些是重复插补记录，不能作为不同受访者重复计数。')
        else:
            lines.append('The verified income file contains **' + income_rows + ' records**' + (' across **' + imputations + ' imputations**' if imputations else '') + '. Imputation records must not be counted as additional people.')
    # status.source can be the verified CSV ZIP URL after adult metadata is
    # merged. The documentation label must always lead to the release page.
    source = SOURCE_PAGE
    try:
        parsed = urlsplit(source)
        valid_source = parsed.scheme == 'https' and not parsed.username and not parsed.password and (parsed.hostname == 'cdc.gov' or str(parsed.hostname).endswith('.cdc.gov')) and not re.search(r'[\s()<>]', source)
    except (ValueError, TypeError):
        valid_source = False
    if valid_source:
        lines.append('[CDC 官方 NHIS 数据说明](' + source + ')' if zh else '[Official CDC NHIS documentation](' + source + ')')
    return '\n\n'.join(lines)


def _category(value, fallback='已注册分组'):
    value = str(value or '')
    if value in _ZH_CATEGORIES:
        return _ZH_CATEGORIES[value]
    parts = [part.split('=', 1)[-1] for part in re.split(r'[|:/]', value)]
    if parts and all(part in _ZH_CATEGORIES for part in parts):
        return ' × '.join(_ZH_CATEGORIES[part] for part in parts)
    return _text(fallback or value or '已注册分组')


def outcome_label(code='fair_poor', language='en'):
    """Registered event heading, without changing its numerical definition."""
    metadata = get_outcome(code)
    return _ZH_OUTCOMES[code] if language == 'zh' else metadata['label']


def category_label(value, language='en'):
    """Registered category heading, including cross-classified identifiers."""
    return _category(value) if language == 'zh' else answers.category_name(value)


def paired_care_answer(results, question='', language='en'):
    """Combine only the matching age-specific delayed/unmet cost-care pair.

    This is presentation of two independent released results, not a new
    estimator. None requests the established full rendering whenever pairing
    would hide scope, reliability, or requested calculation detail.
    """
    if answers.detail_requested(question) or not isinstance(results, (list, tuple)) or len(results) != 2:
        return None
    pair = ('delayed_care_cost', 'unmet_care_cost')
    age_ids = ('25_34', '35_44', '45_54', '55_64')
    by_outcome, shared_scope = {}, None
    for result in results:
        if not isinstance(result, dict) or result.get('status') != 'ok':
            return None
        spec, survey = result.get('spec'), result.get('survey')
        if not isinstance(spec, dict) or not isinstance(survey, dict) or survey.get('status') != 'ok':
            return None
        if not isinstance(spec.get('filters', {}), dict) or any(survey.get(key) is not None and not isinstance(survey[key], dict) for key in ('regression', 'gradient', 'denominators', 'design')):
            return None
        code = spec.get('outcome')
        descriptor = survey.get('outcome')
        if (code not in pair or code in by_outcome or not isinstance(descriptor, dict)
                or descriptor.get('id', descriptor.get('code')) != code
                or descriptor.get('variable') != get_outcome(code)['variable']
                or spec.get('analysis') != 'proportion' or spec.get('group_by') != 'age'
                or spec.get('year') != 2024 or spec.get('age_min') != 25 or spec.get('age_max') != 64
                or spec.get('confidence', .95) != .95):
            return None
        # Include every registered scope/control field, so this compact path
        # cannot combine results that differ in an unfamiliar future setting.
        scope = {key: value for key, value in spec.items() if key != 'outcome'}
        if shared_scope is not None and scope != shared_scope:
            return None
        shared_scope = scope
        if (survey.get('regression') or {}).get('status') is not None or (survey.get('gradient') or {}).get('contrasts'):
            return None
        rows = survey.get('rows')
        if not isinstance(rows, list) or len(rows) <= 1:
            return None
        selected_age = (spec.get('filters') or {}).get('age')
        selected_age = selected_age if isinstance(selected_age, list) else [selected_age] if selected_age is not None else list(age_ids)
        if not selected_age or any(value not in age_ids for value in selected_age):
            return None
        lookup = {}
        for row in rows:
            if not isinstance(row, dict) or row.get('group_id') not in selected_age or row.get('group_id') in lookup:
                return None
            if row.get('status') not in ('ok', 'suppressed') or (row['status'] == 'ok' and not _released(row)):
                return None
            lookup[row['group_id']] = row
        if set(lookup) != set(selected_age) or not any(_released(row) for row in rows):
            return None
        by_outcome[code] = (result, lookup)
    if set(by_outcome) != set(pair):
        return None
    delayed, unmet = (by_outcome[code][1] for code in pair)
    if set(delayed) != set(unmet):
        return None
    zh = language == 'zh'

    def highest(code):
        rows = [row for row in by_outcome[code][1].values() if _released(row)]
        value = max(row['estimate'] for row in rows)
        labels = [category_label(group, language) for group in age_ids if group in by_outcome[code][1]
                  and _released(by_outcome[code][1][group]) and by_outcome[code][1][group]['estimate'] == value]
        return ('、' if zh else ', ').join(labels), _pct(value)

    delayed_group, delayed_value = highest(pair[0])
    unmet_group, unmet_value = highest(pair[1])
    if zh:
        lead = ('在已发布的年龄组点估计中，因费用推迟就医以**{}**最高（{}）；因费用未获得所需医疗服务以**{}**最高（{}）。这只是各指标内部的点估计排序，不能直接认定组间差异具有统计学意义。').format(delayed_group, delayed_value, unmet_group, unmet_value)
        heading = '| 年龄组 | 因费用推迟就医（加权%，95% CI） | 因费用未获得所需医疗服务（加权%，95% CI） |'
    else:
        lead = ('Among released age-group point estimates, delayed care due to cost is highest for **{}** ({}), while unmet medical care due to cost is highest for **{}** ({}). These are rankings within each indicator, not evidence that age-group differences are statistically significant.').format(delayed_group, delayed_value, unmet_group, unmet_value)
        heading = '| Age group | Delayed care due to cost (weighted %, 95% CI) | Unmet medical care due to cost (weighted %, 95% CI) |'
    sections = [lead, _scope(shared_scope) if zh else answers.scope_name(shared_scope)]
    table = [heading, '|---|---:|---:|']
    suppressed = False
    for group in age_ids:
        if group not in delayed:
            continue
        cells = []
        for lookup in (delayed, unmet):
            row = lookup[group]
            if _released(row):
                cells.append('{}（{}–{}）'.format(_pct(row['estimate']), _pct(row['ci_low']), _pct(row['ci_high'])) if zh else
                             '{} ({}–{})'.format(_pct(row['estimate']), _pct(row['ci_low']), _pct(row['ci_high'])))
            else:
                cells.append('—'); suppressed = True
        table.append('| {} | {} | {} |'.format(category_label(group, language), *cells))
    sections.append('\n'.join(table))
    counts = []
    for code in pair:
        count = _record_count(by_outcome[code][0]['survey'].get('denominators') or {}, 'valid_outcome_n')
        if count:
            label = ('推迟就医' if code == pair[0] else '未获得所需医疗服务') if zh else ('Delayed care' if code == pair[0] else 'Unmet care')
            counts.append(label + ('有效 n=' if zh else ' valid n=') + count)
    if counts:
        sections.append(('各指标的有效回答人数分别为：' + '；'.join(counts) + '。') if zh else 'Valid respondent counts are recorded separately: ' + '; '.join(counts) + '.')
    if zh:
        method = '表中比例及95% CI来自各指标各自的复杂抽样分析，使用调查权重；每个指标以本题有效回答为分母，缺失回答不记为“否”。n为实际有效受访者数，不是加权后的全国人口。'
        limits = '两项均只反映**过去12个月因费用**造成的医疗障碍，不代表所有原因的医疗可及性；回答“否”不证明所有服务都及时获得。同一人可能同时报告两种障碍，两列比例不能相加。各组分别的置信区间是否重叠不能替代使用联合协方差的差值检验，横断面结果不证明因果关系。'
    else:
        method = 'The proportions and 95% CIs come from each indicator’s own complex-survey analysis using survey weights. Each item uses its own valid-response denominator; missing answers are not coded as no. n counts actual valid respondents, not the weighted national population.'
        limits = 'Both indicators concern barriers **due to cost in the past 12 months**, not access for every reason; a no response does not establish that all care was obtained promptly. The same person may report both barriers, so the two percentages cannot be added. Overlap of separate group CIs is not a difference test using joint covariance, and these cross-sectional findings do not establish causation.'
    if all((by_outcome[code][0]['survey'].get('design') or {}).get('constructed_before_domain_subset') is True for code in pair):
        method += '两项均先保留完整成人抽样设计，再选择分析域。' if zh else ' Both retain the full adult survey design before selecting the analysis domain.'
    if suppressed:
        limits += '“—”表示该组指标受抑制，未参与排序。' if zh else ' An em dash marks a suppressed cell, which is excluded from rankings.'
    sections.extend([method, limits])
    return '\n\n'.join(sections)


def _scope(spec):
    parts = []
    if _count(spec.get('year')):
        parts.append('NHIS ' + str(int(spec['year'])) + '年调查')
    if _finite(spec.get('age_min')) and _finite(spec.get('age_max')):
        parts.append('美国' + str(int(spec['age_min'])) + '–' + str(int(spec['age_max'])) + '岁成人')
    group = spec.get('group_by')
    if group in _ZH_VARIABLES:
        parts.append('总体分析' if group == 'overall' else '按' + _ZH_VARIABLES[group] + '分组')
    restrictions = []
    for key, value in (spec.get('filters') or {}).items():
        if key in _ZH_VARIABLES:
            restrictions.extend(_category(item) for item in (value if isinstance(value, list) else [value]))
    if restrictions:
        parts.append('限于' + '、'.join(restrictions))
    return '；'.join(parts) + '。' if parts else '人群范围以本次已注册分析口径为准。'


def _released(row):
    return (isinstance(row, dict) and row.get('status') == 'ok'
            and all(_finite(row.get(key)) for key in ('estimate', 'ci_low', 'ci_high'))
            and 0 <= row['ci_low'] <= row['estimate'] <= row['ci_high'] <= 1)


def _pct(value):
    return '{:.2f}%'.format(value * 100)


def _pp(value):
    return '{:+.2f}'.format(value * 100)


def _blocked(result, survey):
    regression = survey.get('regression') or {}
    reason = survey.get('reason') or regression.get('reason') or result.get('reason') or result.get('answer') or result.get('message')
    # Diagnostics may describe gates but must not quote an unreleased estimate.
    reason = _text(re.sub(r'[-+]?\d+(?:\.\d+)?\s*%', '[未发布数值]', str(reason or '')))
    prefix = '还需要确认分析口径。' if result.get('status') == 'clarify' else '本次没有可发布的统计结果。'
    return prefix + ('\n\n记录中的原因：' + reason if reason else '\n\n当前记录没有提供具体原因；请查看关联记录中的检查结果。')


def _term_name(value):
    value = str(value or '')
    for variable in ('education', 'income', 'race', 'age_years', 'age', 'sex'):
        if value.startswith(variable):
            category = value[len(variable):].lstrip('_:')
            return _ZH_VARIABLES[variable] + ('：' + _category(category, category) if category else '')
    return _text(value)


def _valid_or(term):
    return (isinstance(term, dict) and term.get('status', 'ok') == 'ok' and term.get('term') != '(Intercept)'
            and all(_finite(term.get(key)) for key in ('odds_ratio', 'ci_low', 'ci_high'))
            and 0 < term['ci_low'] <= term['odds_ratio'] <= term['ci_high'])


def _methods(survey, spec, metadata, question):
    design, imputation = survey.get('design') or {}, survey.get('imputation') or {}
    regression = survey.get('regression') or {}
    variable = metadata['variable']
    positive = '、'.join(map(str, metadata['one']))
    negative = '、'.join(map(str, metadata['zero']))
    missing = '、'.join(map(str, metadata['missing']))
    lines = ['指标编码：`' + variable + '` 中，' + positive + '记为事件1（' + _ZH_OUTCOMES[metadata['id']] + '），' + negative + '记为0；' + missing + '及其他未列为有效回答的代码不进入该指标分母，不能当作事件0。']
    if metadata['id'] in ('insured', 'uninsured'):
        lines.append('保险指标描述访谈时的覆盖情况，不代表全年持续参保，也不等于医疗服务一定可及。')
    elif metadata['id'] in ('delayed_care_cost', 'unmet_care_cost'):
        lines.append('该指标仅反映过去12个月因费用造成的医疗障碍；回答“否”不证明所有医疗服务都及时获得。分母为目标人群中对此题作出有效回答者，并非仅有就医需求者。')
    if design.get('constructed_before_domain_subset') is True:
        lines.append('先在完整成人样本上建立复杂抽样设计，再限制到年龄和分类筛选所定义的分析域。')
    else:
        lines.append('注册方法要求先建立完整成人抽样设计再取分析域；本记录未提供该步骤的完成标记。')
    if answers.detail_requested(question):
        fields = [('weight', '权重'), ('strata', '分层'), ('psu', '初级抽样单位')]
        recorded = [label + ' `' + _text(design[key]) + '`' for key, label in fields if design.get(key)]
        if recorded:
            lines.append('设计字段：' + '；'.join(recorded) + '。')
        lines.append('加权比例为 `Σ(w×y) / Σw`；表中 n 是未加权的有效受访者数，不是全国人口数。')
        if 'taylor' in str(design.get('variance_method', '')).lower():
            lines.append('记录的方差方法为 Taylor 线性化，保留分层和抽样单位结构。')
        ci_method = str((survey.get('methods') or {}).get('proportion_ci', '')).lower()
        if 't-wald' in ci_method or 'pooled t' in ci_method:
            lines.append('置信区间使用记录中的设计或合并自由度对应的 t 临界值；不是一律使用1.96。')
    m = imputation.get('m', regression.get('imputation_count'))
    required = imputation.get('required') is True or spec.get('group_by') == 'income' or 'income' in (spec.get('filters') or {}) or regression.get('income_adjusted') is True
    if required:
        if _finite(m) and m == int(m) and m > 1:
            lines.append('本次记录包含' + str(int(m)) + '份收入插补；合并分析须同时计入插补内和插补间的不确定性。' + ('已记录使用 Rubin 合并规则。' if 'rubin' in str(imputation.get('method', '')).lower() else '本记录未给出完整的插补合并方法说明。') + '各插补的 n 范围不表示新增受访者。')
            if m != 10:
                lines.append('记录的插补数不是完整10份，不能声称已使用全部官方收入插补。')
        else:
            lines.append('该口径需要全部10份官方收入插补，但本记录没有可确认的合并份数。')
    return '\n'.join('- ' + line for line in lines)


def explanation_answer(result, spec, question, language='en'):
    """Selective Chinese follow-ups; None preserves the existing English path."""
    if language != 'zh':
        return None
    result = result if isinstance(result, dict) else {}
    spec = spec if isinstance(spec, dict) else {}
    survey = result.get('survey') if isinstance(result.get('survey'), dict) else {}
    regression = survey.get('regression') or {}
    code = spec.get('outcome', 'fair_poor')
    descriptor = survey.get('outcome')
    if (result.get('status') != 'ok' or survey.get('status') not in (None, 'ok')
            or regression.get('status') not in (None, 'ok') or code not in allowed_outcomes()
            or not answers.outcome_matches(survey, spec)
            or (code != 'fair_poor' and (not isinstance(descriptor, dict)
                or descriptor.get('id', descriptor.get('code')) != code
                or descriptor.get('variable') != get_outcome(code)['variable']))):
        return analysis_answer(result, spec, question, language)
    query = str(question or '').lower()
    event = _ZH_OUTCOMES[code]
    if re.search(r'置信区间|\bci\b|confidence interval', query):
        return ('这里的95% CI是“' + event + '”这一群体指标的置信区间，用来描述抽样估计的不确定性。'
                '在相同抽样设计和区间假设下重复抽样，这种构造区间的方法约有95%的区间能覆盖总体参数；'
                '它不是个人取值范围，也不是说已算出的固定区间有95%的概率包含固定参数。\n\n'
                '各年龄组分别的区间是否重叠不能替代组间差值检验；差值需要自己的估计及联合协方差。横断面关联也不能证明因果关系。')
    if re.search(r'样本|分母|受访者|人数|\bsample\b|denominator|respondents?|how many people', query):
        denominator = survey.get('denominators') or {}
        count = _record_count(denominator, 'valid_outcome_n')
        lead = ('这次分析中，“' + event + '”的有效回答人数为 **' + count + '**。') if count else '本记录没有提供可确认的该指标总有效回答人数，不能从各组人数推算补齐。'
        points = [label + ' ' + value for key, label in (('full_sample_n', '完整成人样本'), ('age_domain_n', '年龄域'),
                  ('filtered_domain_n', '分类筛选后'), ('valid_outcome_n', '本指标有效回答')) for value in [_record_count(denominator, key)] if value]
        sections = [lead, _scope(spec)]
        if points:
            sections.append('记录的样本口径：' + ' → '.join(points) + '。')
        model_n = _record_count(regression, 'n') if regression.get('status') == 'ok' else None
        if model_n:
            sections.append('回归模型的完整病例 n=' + model_n + '；它与只要求本题有效回答的分母是不同口径。')
        missing = _count((survey.get('missingness') or {}).get('outcome'))
        if missing:
            sections.append('记录的本指标缺失回答数为' + missing + '，不作为事件0计入有效分母。')
        note = '这些 n 都是实际受访者人数，不是调查权重扩展后的全国人口。不同指标的有效回答可能不同。'
        if (survey.get('imputation') or {}).get('required') is True or spec.get('group_by') == 'income' or 'income' in (spec.get('filters') or {}) or regression.get('income_adjusted') is True:
            note += '收入插补记录不代表额外受访者。'
        sections.append(note)
        return '\n\n'.join(sections)
    if re.search(r'权重|加权|抽样|方法|怎么算|怎么计算|如何计算|计算过程|计算步骤|\bweights?\b|weighted|method|how.{0,30}calculat|pstrat|ppsu|wtfa', query):
        detail = question if answers.detail_requested(question) else '详细解释计算过程' if re.search(r'权重|加权|抽样|weights?|weighted|pstrat|ppsu|wtfa', query) else question
        return ('“' + event + '”的计算口径如下。\n\n' + _scope(spec) + '\n\n'
                + _methods(survey, spec, get_outcome(code), detail)
                + '\n\n这些方法说明来自保存的分析记录，本轮没有因此重新计算。横断面关联不能证明因果关系。')
    return analysis_answer(result, spec, question, language)


def analysis_answer(result, spec, question='', language='en'):
    """Render existing released numbers; English preserves the established answer."""
    if language != 'zh':
        return answers.build_analysis_answer(result, spec, question=question)
    result = result if isinstance(result, dict) else {}
    spec = spec if isinstance(spec, dict) else {}
    survey = result.get('survey') if isinstance(result.get('survey'), dict) else {}
    regression = survey.get('regression') or {}
    if result.get('status') != 'ok' or survey.get('status') not in (None, 'ok') or regression.get('status') not in (None, 'ok'):
        return _blocked(result, survey)
    code = spec.get('outcome', 'fair_poor')
    descriptor = survey.get('outcome')
    if (code not in allowed_outcomes() or not answers.outcome_matches(survey, spec)
            or (code != 'fair_poor' and (not isinstance(descriptor, dict) or descriptor.get('id', descriptor.get('code')) != code or descriptor.get('variable') != get_outcome(code)['variable']))):
        return '本次没有可发布的统计结果。记录中的指标及来源变量与请求口径不匹配，不能把旧结果改名为另一指标。请按目标指标重新分析。'
    metadata = get_outcome(code); event = _ZH_OUTCOMES[code]
    rows = [row for row in (survey.get('rows') or []) if isinstance(row, dict)]
    published = [row for row in rows if _released(row)]
    sections = ['**' + event + '**\n\n' + _scope(spec)]
    terms = [term for term in regression.get('coefficients', []) if _valid_or(term)] if regression.get('status') == 'ok' else []
    if terms:
        first = terms[0]
        sections.append('已发布模型中，' + _term_name(first.get('term')) + '的调整后 OR 为 **{:.3f}**（95% CI：{:.3f}–{:.3f}）。它描述在模型所列协变量条件下与“'.format(first['odds_ratio'], first['ci_low'], first['ci_high']) + event + '”的关联。')
    elif len(published) == 1:
        row = published[0]
        sections.append(_category(row.get('group_id'), row.get('label')) + '中，“' + event + '”的加权比例为 **' + _pct(row['estimate']) + '**（95% CI：' + _pct(row['ci_low']) + '–' + _pct(row['ci_high']) + '）。')
    elif published:
        high, low = max(published, key=lambda row: row['estimate']), min(published, key=lambda row: row['estimate'])
        sections.append('在已发布组别中，' + _category(high.get('group_id'), high.get('label')) + '的点估计最高（' + _pct(high['estimate']) + '），' + _category(low.get('group_id'), low.get('label')) + '最低（' + _pct(low['estimate']) + '）。这是点估计排序，不能据此直接认定组间差异具有统计学意义。')
    else:
        sections.append('当前记录没有可引用的已发布比例或非截距 OR；缺失或受抑制的数值不用于结论。')
    if (result.get('cache') or {}).get('hit') is True or (survey.get('provenance') or {}).get('cache_reuse'):
        sections.append('本轮复用了已核验的统计缓存；**没有重新运行 R**。下述方法说明针对原始计算。')
    if rows:
        table = ['| 组别 | 有效 n | ' + event + '（加权%） | 95% CI |', '|---|---:|---:|---|']
        for row in rows:
            label = _category(row.get('group_id'), row.get('label'))
            count = _record_count(row, 'n') or '未记录'
            table.append('| {} | {} | {} | {} |'.format(label, count, _pct(row['estimate']), _pct(row['ci_low']) + '–' + _pct(row['ci_high'])) if _released(row) else '| {}（未发布） | {} | — | — |'.format(label, count))
        sections.append('\n'.join(table))
    components = {row.get('group_id'): row for row in published}
    gradient = survey.get('gradient') or {}
    contrasts = [row for row in gradient.get('contrasts', []) if isinstance(row, dict) and row.get('status') == 'ok'
                 and all(_finite(row.get(key)) for key in ('estimate', 'ci_low', 'ci_high'))
                 and -1 <= row['estimate'] <= 1 and row['ci_low'] <= row['estimate'] <= row['ci_high']
                 and row.get('group_id') in components and row.get('reference_id') in components] if gradient.get('status') in (None, 'ok') else []
    if contrasts:
        table = ['| 已注册比较 | 比例差（百分点） | 差值95% CI（百分点） |', '|---|---:|---|']
        for row in contrasts:
            table.append('| {} − {} | {} | {} 至 {} |'.format(_category(row['group_id']), _category(row['reference_id']), _pp(row['estimate']), _pp(row['ci_low']), _pp(row['ci_high'])))
        sections.append('\n'.join(table))
        sections.append('差值区间使用联合抽样协方差；区间包含0意味着该比较的方向仍不确定，并不证明两组相同。未自动进行多重比较调整。')
    if terms:
        unadjusted = regression.get('unadjusted') or {}
        originals = {term.get('term'): term for term in unadjusted.get('coefficients', []) if _valid_or(term)} if unadjusted.get('status') in (None, 'ok') else {}
        table = ['| 模型项 | 未调整OR（95% CI） | 调整后OR（95% CI） |', '|---|---:|---:|']
        for term in terms:
            original = originals.get(term.get('term'))
            before = '{:.3f}（{:.3f}–{:.3f}）'.format(original['odds_ratio'], original['ci_low'], original['ci_high']) if original else '—'
            table.append('| {} | {} | {:.3f}（{:.3f}–{:.3f}） |'.format(_term_name(term.get('term')), before, term['odds_ratio'], term['ci_low'], term['ci_high']))
        sections.append('\n'.join(table))
        model = '模型：`' + _text(regression['formula']) + '`。' if regression.get('formula') else '本记录没有提供模型公式。'
        for record, label in ((regression, '调整模型完整病例 n'), (unadjusted, '未调整模型 n')):
            count = _record_count(record, 'n')
            if count:
                model += label + '=' + count + '。'
        model += '两模型已确认使用同一完整病例样本。' if answers._same_sample(survey, regression) else '尚未确认两模型的完整病例成员相同，不能把 OR 变化全部归因于协变量调整。'
        references = [_ZH_VARIABLES[key] + '=' + _category(value) for key, value in (regression.get('references') or {}).items() if key in _ZH_VARIABLES and re.search(r'\b' + re.escape(key) + r'\b', str(regression.get('formula', '')))]
        if references:
            model += '参照组：' + '；'.join(references) + '。'
        model += '模型包含收入调整。' if regression.get('income_adjusted') is True else '这是不含收入调整的简化模型。' if regression.get('income_adjusted') is False else ''
        exclusions = _count(regression.get('complete_case_exclusions_range'))
        if exclusions:
            model += '相对于分类筛选后的分析域，完整病例分析排除' + exclusions + '人（包括结局缺失者）。'
        sections.append(model)
        sections.append('OR 是优势比，不是概率或风险比。95% CI 包含1时，数据仍与该项在注册模型的协变量条件下无关联（OR=1）的情形相容；这不证明没有关联。调整前后 OR 的变化不能证明某个协变量的因果作用。')
    sections.append(('### 计算过程' if answers.detail_requested(question) else '**方法简述**') + '\n\n' + _methods(survey, spec, metadata, question))
    denominator = survey.get('denominators') or {}
    flow = [label + ' ' + count for key, label in (('full_sample_n', '完整成人样本'), ('age_domain_n', '年龄域'), ('filtered_domain_n', '分类筛选后'), ('valid_outcome_n', '本指标有效回答')) for count in [_record_count(denominator, key)] if count]
    if flow:
        sections.append('样本口径：' + ' → '.join(flow) + '。')
    missingness = survey.get('missingness') or {}
    relevant = {'outcome'}
    if spec.get('analysis') == 'regression' or regression.get('status') == 'ok':
        relevant.update(('education', 'age', 'sex', 'race'))
        if regression.get('income_adjusted') is True:
            relevant.add('income')
    elif spec.get('group_by') == 'education_race':
        relevant.update(('education', 'race'))
    elif spec.get('group_by') in _ZH_VARIABLES:
        relevant.add(spec['group_by'])
    relevant.update(key for key in (spec.get('filters') or {}) if key in _ZH_VARIABLES)
    notes = [(_ZH_OUTCOMES[code] if key == 'outcome' else _ZH_VARIABLES[key]) + '缺失=' + count for key, value in missingness.items() if key in relevant for count in [_count(value)] if count]
    if notes:
        sections.append('；'.join(notes) + '。不同变量缺失可能重叠，不能把这些人数相加。')
    suppressed = sum(not _released(row) for row in rows)
    if suppressed:
        sections.append(str(suppressed) + '个组别没有通过发布条件或缺少有效区间，比例与区间均未列出，也不参与排序或组间结论。')
    sections.append('95% CI 反映抽样估计的不确定性，不是个人取值范围。横断面关联不能证明因果关系。' + ('各组各自的区间是否重叠不能替代差值的正式检验。' if len(published) > 1 and not contrasts else ''))
    return '\n\n'.join(sections)
