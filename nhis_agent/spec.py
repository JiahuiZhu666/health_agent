"""Conservative bilingual routing to immutable, registered analysis specifications."""
import hashlib
import json
import re

VERSION = 'nhis-2024-phase1-v1'
GROUPS = {
    'overall': 'All adults', 'education': 'Education', 'race': 'Race and Hispanic ethnicity',
    'sex': 'Sex', 'age': 'Age group', 'education_race': 'Education × race/ethnicity', 'income': 'Income',
}

EDUCATION = {
    'less_than_high_school': ['未完成高中', '高中以下', '低于高中', 'less than high school', '未高中毕业'],
    'high_school': ['高中毕业', 'high school graduate', 'high school diploma'],
    'some_college': ['部分大学', '大专', 'some college', 'associate degree'],
    'bachelors_plus': ['本科及以上', '大学本科及以上', '本科以上', "bachelor", '大学毕业'],
}
RACES = {
    'hispanic': ['西语裔', 'hispanic'], 'nh_white': ['非西语裔白人', 'non-hispanic white'],
    'nh_black': ['非西语裔黑人', 'non-hispanic black'], 'nh_asian': ['非西语裔亚裔', 'non-hispanic asian'],
}


def fingerprint(spec):
    return hashlib.sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def requested_outcomes(question):
    """Select named registered events without treating no cost barrier as timely care."""
    q = str(question).lower()
    selected = []
    if re.search(r'自评健康|self.?rated|fair.{0,5}poor|phstat_a',q):
        selected.append('fair_poor')
    insurance = re.search(r'保险|医保|参保|\binsurance\b|\binsured\b|\buninsured\b|notcov_a',q) and not re.search(r'车险|寿险|财产保险|旅游保险|\b(?:car|auto|life|home|property|travel) insurance\b',q)
    if insurance:
        uninsured = re.search(r'未参保|未投保|没有.{0,4}(?:医保|保险)|无医保|无保险|\buninsured\b|without (?:health )?insurance|not covered',q)
        insured = re.search(r'有无|是否|coverage|covered|有医保|有保险|\binsured\b',q)
        if uninsured and not insured and not re.search(r'有无|whether',q):
            selected.append('uninsured')
        else:
            selected.extend(('insured','uninsured'))
    if re.search(r'meddl12m_a|延迟.{0,5}(?:就医|医疗|看病)|推迟.{0,5}(?:就医|医疗|看病)|\bdelay(?:ed|ing)?\b.{0,25}(?:care|medical|treatment)',q):
        selected.append('delayed_care_cost')
    if re.search(r'medng12m_a|未就医|未获得.{0,8}(?:医疗|服务)|未能.{0,5}(?:就医|看病)|无法.{0,5}(?:就医|看病)|'
                 r'未满足.{0,5}(?:医疗|需求)|\bunmet\b.{0,20}(?:care|medical|need)|(?:forgo|forego|forgone|foregone|without|did not get|not get).{0,25}(?:medical|needed care)',q):
        selected.append('unmet_care_cost')
    timely = re.search(r'及时就医|及时.{0,5}(?:医疗|看病)|timely.{0,12}(?:care|medical)|access to (?:medical |health )?care',q)
    broad = re.search(r'医疗服务指标|医疗服务.{0,10}分布|医疗可及性|医疗服务可及性|health.?care (?:access|service) (?:indicators?|measures?)',q)
    if broad:
        selected.extend(('insured','uninsured','delayed_care_cost','unmet_care_cost'))
    elif timely:
        selected.extend(('delayed_care_cost','unmet_care_cost'))
    elif re.search(r'就医费用障碍|医疗费用障碍|cost.?related barriers|cost barriers',q):
        selected.extend(('delayed_care_cost','unmet_care_cost'))
    return list(dict.fromkeys(selected))


def route(question):
    if not isinstance(question, str) or not question.strip():
        return {'status': 'clarify', 'answer': 'Enter a research question, such as “Compare fair/poor self-rated health by education among adults aged 25–64.”'}
    if len(question) > 2000:
        return {'status': 'blocked', 'answer': 'Please keep the question within 2,000 characters.'}
    q = question.strip().lower()
    outcomes = requested_outcomes(q)
    if outcomes and re.search(r'premium|deductible|copay|coinsurance|保费|免赔|赔付|共付',q):
        return {'status':'clarify','answer':'The verified tools measure insurance coverage and cost-related barriers to care. Premium, deductible, copayment, and claim amounts are separate measures that have not been added to these calculations.'}
    if ('fair_poor' in outcomes and len(outcomes)>1 and re.search(r'correlation|relationship|association between|相关|关系',q)):
        return {'status':'clarify','answer':'I can show the separate distributions of self-rated health, insurance, and cost-related access barriers. A relationship between those outcomes needs a separately checked cross-tabulation or model; I will not substitute their marginal distributions for that analysis.'}
    reasons = [
        (r'因果|导致|造成|causal|cause\b|治疗|处方|diagnos|treatment|服药', 'The survey tools estimate cross-sectional descriptions and associations, not causal effects, diagnoses, or treatments.'),
        (r'中国|加拿大|日本|英国|欧洲|美国之外|canad|china|japan|united kingdom|europe|outside the u\.?s|cfps|儿童|未成年|children|child\b|teen|孕妇|pregnan', 'The registered survey population is US NHIS 2024 Sample Adults aged 25–64. The requested population is outside that scope.'),
        (r'移民|出生地|来美|immigra|nativity', 'Immigrant status and country of birth are not yet supported by the verified survey tools. I can analyze self-rated health, insurance coverage, and cost-related barriers to medical care.'),
        (r'糖尿病|高血压|癌症|diabetes|cancer|hypertension|死亡|mortality|抑郁|depress|肥胖|obesity|\bbmi\b|残疾|disability|吸烟|smok', 'I can calculate self-rated health, insurance coverage, and cost-related delayed or unmet medical care from the verified data. The requested outcome or additional covariate has not been added to those tools yet.'),
        (r'州|县|城市|state-level|county|region|地区', 'The survey tools do not support regional or restricted geography analyses.'),
        (r'模拟|画像|persona|simulate', 'Profile simulation is not enabled. Blind model measurement uses aggregate proportion questions and only category definitions.'),
        (r'趋势|逐年|trend|多年', 'The registered analysis is a 2024 cross-section; multi-year trends are not enabled.'),
    ]
    for pattern, reason in reasons:
        if re.search(pattern, q):
            return {'status': 'blocked', 'answer': reason}
    years = re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', q)
    if any(y != '2024' for y in years):
        return {'status': 'blocked', 'answer': 'The registered survey year is 2024. Other years require a separate analysis registration.'}
    if (re.search(r'65\s*岁|65\s*years|老年|elderly|under\s*25|25岁以下|24岁以下|18岁以上|所有年龄|全年龄', q)
            or ('all adults' in q and not re.search(r'25\s*[-–—]\s*64', q))):
        return {'status': 'blocked', 'answer': 'The registered population is aged 25–64; its age bounds cannot be changed.'}
    if re.search(r'(?:(?:90|99)\s*%.{0,8}(?:置信|confidence|\bci\b)|(?:置信|confidence|\bci\b).{0,12}(?:90|99)\s*%)|下限.{0,3}\d|minimum sample|样本.{0,3}\d', q):
        return {'status': 'blocked', 'answer': 'Confidence is fixed at 95% and the publication floor is unweighted n≥100. Natural language cannot change these registered rules.'}
    range_matches = [m for m in re.finditer(r'(?<!\d)(\d{2,3})\s*[-–—至到~]\s*(\d{2,3})(?!\d)(\s*%)?', q) if not m[3]]
    ranges = [(m[1], m[2]) for m in range_matches]
    age_pairs = {('25','34'):'25_34',('35','44'):'35_44',('45','54'):'45_54',('55','64'):'55_64'}
    if any((a, b) != ('25', '64') and (a,b) not in age_pairs for a, b in ranges):
        return {'status': 'clarify', 'answer': 'The registered population is aged 25–64, with groups 25–34, 35–44, 45–54, and 55–64. Choose a registered group or the full population.'}
    explicit_ages = list(re.finditer(r'(?<!\d)\d{1,3}\s*(?:岁|years|\+)', q)) + list(re.finditer(r'(?:年龄|ages?|aged)\s*(?:为|是)?\s*\d{1,3}', q))
    for match in explicit_ages:
        if not any(start.start() <= match.start() < start.end() or start.start() < match.end() <= start.end() for start in range_matches):
            return {'status': 'clarify', 'answer': 'Use the registered 25–64 population or one of its four age groups. Individual ages and arbitrary age thresholds are not supported.'}
    if re.search(r'本科以下|大学以下|研究生|硕士|博士|graduate degree|native hawaiian|american indian|太平洋|夏威夷|原住民|少数族裔', q):
        return {'status': 'clarify', 'answer': 'The requested subgroup does not match a registered category. Use one of the four education categories or the Hispanic/non-Hispanic race categories.'}
    if re.search(r'(?<!非西语裔)(?<!non-hispanic )\b(?:white|black|asian)\b|(?<!非西语裔)(?:白人|黑人|亚裔)', q):
        return {'status': 'clarify', 'answer': 'Please specify ethnicity: White, Black, and Asian groups are non-Hispanic single-race categories. Hispanic is a separate group.'}
    dimension_hits = {
        'education': bool(re.search(r'教育|学历|高中|本科|大专|大学|education|college|bachelor', q)),
        'race': bool(re.search(r'族裔|种族|西语|白人|黑人|亚裔|race|ethnic|hispanic|asian|black|white', q)),
        'sex': bool(re.search(r'性别|男性|女性|男女|sex|gender|\bmale\b|\bfemale\b|\bmen\b|\bwomen\b', q)),
        'age': bool(re.search(r'年龄组|各年龄|年龄差异|age group|by age', q)),
        'income': bool(re.search(r'收入|贫困|income|poverty', q)),
    }
    analysis = 'regression' if re.search(r'回归|logistic|regression|调整|校正|控制', q) else ('gradient' if re.search(r'梯度|gradient', q) else 'proportion')
    if (analysis == 'regression' and 'fair_poor' in outcomes and any(code in ('insured','uninsured') for code in outcomes)
            and re.search(r'adjust|control|调整|控制|校正',q)):
        return {'status':'clarify','answer':'I can analyze insurance coverage as an outcome. Adding insurance as a predictor of self-rated health needs a separately checked model; the current adjusted model uses education, income, age group, sex, and race/ethnicity.'}
    if analysis == 'regression' and re.search(r'只调整|只控制|仅调整|仅控制|only adjust|only control|不含教育|不含年龄|不含性别|不含族裔', q):
        return {'status': 'clarify', 'answer': 'The registered regression includes education, income, age group, sex, and race/ethnicity. Choose the full model or explicitly request the registered model without income.'}
    if analysis == 'regression' and re.search(r'不含收入|不调整收入|不控制收入|排除收入|without income|excluding income', q):
        dimension_hits['income'] = False
    if re.search(r'低收入|高收入|low income|high income',q) and not re.search(r'贫困线|poverty',q):
        return {'status':'clarify','answer':'Choose an income-to-poverty category: below 100%, 100–199%, 200–399%, or 400% and above.'}
    filters = {}
    requested_ages = [age_pairs[(a,b)] for a,b in ranges if (a,b) in age_pairs]
    if len(set(requested_ages)) == 1:
        filters['age'] = requested_ages[0]
    elif len(set(requested_ages)) > 1:
        filters['age'] = list(dict.fromkeys(requested_ages))
        dimension_hits['age'] = True
    female = bool(re.search(r'女性|\bfemale\b|\bwomen\b', q))
    male = bool(re.search(r'男性|\bmale\b|\bmen\b', q))
    if female != male:
        filters['sex'] = 'female' if female else 'male'
    for key, aliases in [('education', EDUCATION), ('race', RACES)]:
        spans = [(group, m.start(), m.end()) for group, phrases in aliases.items() for phrase in phrases for m in re.finditer(re.escape(phrase), q)]
        matched = list({group for group, start, end in spans if not any(other != group and left <= start and end <= right for other, left, right in spans)})
        if len(matched) == 1:
            filters[key] = matched[0]
        elif len(matched) > 1:
            filters[key] = [category for category in aliases if category in matched]
    if dimension_hits['income']:
        income_patterns = {
            'below_100': r'贫困线以下|(?:低于|少于)(?:贫困线)?\s*100\s*%|below\s*100\s*%|below (?:the )?poverty (?:line|threshold)',
            '100_199': r'100\s*[-–—至到~]\s*199\s*%',
            '200_399': r'200\s*[-–—至到~]\s*399\s*%',
            '400_plus': r'400\s*%\s*(?:及以上|或以上|以上)|(?:≥|>=)\s*400\s*%|at or above\s*400\s*%|400\s*%(?:\s+of(?: the)? poverty(?: (?:line|threshold))?)?\s*(?:and above|or above|or more|or higher|and over)',
        }
        selected = [category for category, pattern in income_patterns.items() if re.search(pattern, q)]
        if selected:
            filters['income'] = selected[0] if len(selected) == 1 else selected
    grouping = [k for k, hit in dimension_hits.items() if hit and (k not in filters or isinstance(filters[k], list))]
    if 'education' in grouping and 'race' in grouping:
        group_by = 'education_race'
        grouping = [g for g in grouping if g not in ['education', 'race']]
    else:
        group_by = grouping[0] if grouping else 'overall'
        grouping = grouping[1:]
    if grouping and analysis != 'regression':
        return {'status': 'clarify', 'answer': 'Choose one grouping dimension, or education × race/ethnicity. Other characteristics can be restricted to specific categories.'}
    if not any(dimension_hits.values()) and not outcomes and not re.search(r'健康|health|总体|整体|比例|自评|fair|poor', q):
        return {'status': 'clarify', 'answer': 'For survey calculation, specify the fair/poor self-rated health comparison by education, income, sex, race/ethnicity, or age group.'}
    if re.search(r'很好|非常好|excellent|very good|五分类|原始分布', q):
        return {'status': 'blocked', 'answer': 'The registered binary outcome is PHSTAT_A=4 or 5 (fair/poor). The five-category distribution is a different outcome and is not combined with it.'}
    spec = {
        'version': VERSION, 'year': 2024, 'outcome': outcomes[0] if outcomes else 'fair_poor', 'analysis': analysis,
        'group_by': group_by, 'filters': filters, 'min_n': 100, 'age_min': 25, 'age_max': 64,
        'confidence': 0.95, 'adjustment': 'full',
    }
    if analysis == 'regression' and re.search(r'不含收入|不调整收入|不控制收入|排除收入|without income|excluding income', q):
        spec['adjustment'] = 'no_income'
    result = {'status': 'ready', 'spec': spec, 'spec_hash': fingerprint(spec)}
    if len(outcomes)>1:
        result['outcomes'] = outcomes
    return result
