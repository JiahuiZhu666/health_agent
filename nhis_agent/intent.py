"""Versioned intent constraints and contextual patches, before legal specs.

This conservative resolver covers the versioned evaluation corpus, not arbitrary
natural language. Unknown explicit requests are never evidence of a default.
The registered router and statistics validator remain separate execution gates.
"""
from dataclasses import asdict, dataclass
import copy
import hashlib
import json
import re

SCHEMA_VERSION = 1
FILTER_VALUES = {
    'sex':('male','female'),
    'education':('less_than_high_school','high_school','some_college','bachelors_plus'),
    'race':('hispanic','nh_white','nh_black','nh_asian','nh_other'),
    'age':('25_34','35_44','45_54','55_64'),
    'income':('below_100','100_199','200_399','400_plus'),
}


@dataclass(frozen=True)
class Constraint:
    field: str
    text: str
    start: int
    end: int
    value: str = ''
    reason: str = ''


@dataclass(frozen=True)
class FilterOperation:
    field: str
    operator: str
    values: tuple
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class AnalysisPatch:
    operations: tuple = ()
    schema_version: int = SCHEMA_VERSION

    def apply(self, spec):
        result = copy.deepcopy(spec)
        filters = result.setdefault('filters',{})
        for operation in self.operations:
            if operation.field not in FILTER_VALUES or operation.operator not in ('include','exclude','clear'):
                raise ValueError('The requested filter operation is not registered.')
            if operation.operator=='clear':
                filters.pop(operation.field,None)
                continue
            if not operation.values or not set(operation.values).issubset(FILTER_VALUES[operation.field]):
                raise ValueError('The requested filter category is not registered.')
            if operation.operator=='include':
                values = list(operation.values)
            else:
                current = filters.get(operation.field, list(FILTER_VALUES[operation.field]))
                current = current if isinstance(current,list) else [current]
                values = [value for value in current if value not in operation.values]
            if not values:
                raise ValueError('The inclusion and exclusion conditions leave no registered population. Please clarify the intended population.')
            filters[operation.field] = values[0] if len(values)==1 else values
        return result


@dataclass(frozen=True)
class AnalysisIntent:
    question: str
    recognized_constraints: tuple = ()
    unsupported_constraints: tuple = ()
    ambiguous_constraints: tuple = ()
    operations: tuple = ()
    followup: bool = False
    schema_version: int = SCHEMA_VERSION

    @property
    def resolution(self):
        return 'clarify' if self.unsupported_constraints or self.ambiguous_constraints else 'ready'

    @property
    def patch(self):
        return AnalysisPatch(self.operations)

    def document(self):
        value = asdict(self)
        value.pop('question',None)
        value['resolution'] = self.resolution
        return value

    def clarification(self):
        issues = self.unsupported_constraints + self.ambiguous_constraints
        named = ', '.join('“'+item.text[:100]+'”' for item in issues[:3])
        reasons = ' '.join(dict.fromkeys(item.reason for item in issues if item.reason))
        return ('I can’t map '+named+' to the verified analysis rules. '+reasons+
                ' Please clarify or remove that requirement; I have not substituted a different indicator or population.')


@dataclass(frozen=True)
class ExecutionPlan:
    spec_json: str
    fingerprints_json: str
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def freeze(cls, spec, fingerprints):
        return cls(json.dumps(spec,sort_keys=True,separators=(',',':'),allow_nan=False),
                   json.dumps(fingerprints,sort_keys=True,separators=(',',':'),allow_nan=False))

    def document(self):
        return {'schema_version':self.schema_version,'spec':json.loads(self.spec_json),
                'fingerprints':json.loads(self.fingerprints_json),
                'plan_hash':hashlib.sha256((self.spec_json+'\n'+self.fingerprints_json).encode()).hexdigest()}


@dataclass(frozen=True)
class PublishedResult:
    run_id: str
    status: str
    outcome: str
    schema_version: int = SCHEMA_VERSION


def coverage_check(intent, spec):
    """Reject contradictions between recognized requirements and a legal spec.

    Missing explicit requirements retain contextual defaults. Multiple requested
    outcomes/groupings permit individual batch members; this is a consistency
    gate, not proof that an entire natural-language request has been understood.
    Return a clarification string, or None when the recognized scope is covered.
    """
    if intent.resolution != 'ready':
        return intent.clarification()
    explicit = intent.recognized_constraints
    outcomes = {code for item in explicit if item.field == 'outcome'
                for code in item.value.split(',') if code}
    specific_insurance = {item.value for item in explicit
                          if item.field == 'outcome' and item.value in ('insured', 'uninsured')}
    if specific_insurance:
        outcomes.difference_update(('insured', 'uninsured'))
        outcomes.update(specific_insurance)
    if outcomes and spec.get('outcome', 'fair_poor') not in outcomes:
        return ('The analysis specification does not match the indicator explicitly requested. '
                'Please confirm the intended indicator before any statistics or model measurement are run.')
    groups = {item.value for item in explicit if item.field == 'group_by' and item.value}
    if {'education', 'race'} <= groups:
        groups.add('education_race')
    if groups and spec.get('group_by', 'overall') not in groups:
        return ('The analysis specification does not match the grouping explicitly requested. '
                'Please confirm the intended grouping before any statistics or model measurement are run.')
    filters = spec.get('filters') or {}
    for field, categories in FILTER_VALUES.items():
        operations = [item for item in intent.operations if item.field == field]
        if not operations:
            continue
        allowed, exact = set(categories), False
        for operation in operations:
            if operation.operator == 'clear':
                allowed, exact = set(categories), True
            elif operation.operator == 'include':
                allowed, exact = set(operation.values), True
            elif operation.operator == 'exclude':
                allowed.difference_update(operation.values)
        actual = filters.get(field, categories)
        actual = {actual} if isinstance(actual, str) else set(actual)
        if not actual or not actual <= set(categories) or (actual != allowed if exact else not actual <= allowed):
            return ('The analysis specification does not match the requested '+field+
                    ' inclusion, exclusion, or filter removal. Please confirm the intended population '
                    'before any statistics or model measurement are run.')
    return None


_UNKNOWN = (
    ('outcome',r'\b(?:asthma|arthritis|dementia|alzheimer\w*|migraine\w*|stroke|kidney disease|heart disease|life expectancy|hospitali[sz]ations?|blood pressure|blood glucose)\b|哮喘|关节炎|痴呆|阿尔茨海默|偏头痛|中风|肾病|心脏病|预期寿命|住院率|血压|血糖',
     'That indicator has not been registered; self-rated health cannot stand in for it.'),
    ('population',r'\b(?:married|unmarried|divorced|widowed|marital(?: status)?|employed|unemployed|employment(?: status)?|retired|retirees|occupation|urban|rural|gardeners|veterans|students|patients|citizenship|disabled|disabilities|disability)\b|已婚|未婚|离婚|丧偶|婚姻|就业|失业|退休|职业|城镇|农村|在校|退伍|公民身份|残障|残疾',
     'That population restriction or covariate is not implemented in the verified analysis.'),
)
_CATEGORY = {
    'sex':{'female':r'\bwomen\b|\bfemales?\b|女性','male':r'\bmen\b|\bmales?\b|男性'},
    'education':{'less_than_high_school':r'less than high school|未完成高中|未高中毕业|高中以下',
                 'high_school':r'high school (?:graduate|diploma)|高中毕业',
                 'some_college':r'some college|associate degree|部分大学|大专',
                 'bachelors_plus':r'bachelor[’\x27]?s?(?: degree)?(?: or higher)?|本科及以上|本科以上'},
    'race':{'hispanic':r'\bhispanic\b|西语裔','nh_white':r'non-hispanic white|非西语裔白人','nh_black':r'non-hispanic black|非西语裔黑人',
            'nh_asian':r'non-hispanic asian|非西语裔亚裔','nh_other':r'non-hispanic other|非西语裔其他'},
    'age':{'25_34':r'25\s*[-–—至到]\s*34(?!\s*%)','35_44':r'35\s*[-–—至到]\s*44(?!\s*%)',
           '45_54':r'45\s*[-–—至到]\s*54(?!\s*%)','55_64':r'55\s*[-–—至到]\s*64(?!\s*%)'},
    'income':{'below_100':r'below\s*100\s*%|below (?:the )?poverty (?:line|threshold)|贫困线以下|低于贫困线100%',
              '100_199':r'100\s*[-–—至到]\s*199\s*%', '200_399':r'200\s*[-–—至到]\s*399\s*%',
              '400_plus':r'400\s*%(?:.{0,25})(?:above|higher|more|以上)'},
}
_NEGATIVE = re.compile(r'(?:\b(?:excluding|exclude|except|without|not including|not|neither)\s+(?:(?:the|all|any)\s+)*|排除|不包括|不包含|剔除|非|不是)\s*$',re.I)
_DOUBLE = re.compile(r'\b(?:not|don.t|do not)\s+(?:exclude|excluding|omit)|\bnot\s+(?:without|only)\b|不排除|并非不|不是不|不只|不仅|不要只看',re.I)
_CLEAR = {
    'sex':r'(?:remove|clear)\s+(?:the\s+)?(?:sex|gender)\s+filters?|取消(?:性别|男女)筛选|去掉性别(?:限制|筛选)|不限性别',
    'education':r'(?:remove|clear)\s+(?:the\s+)?education\s+filters?|取消教育筛选',
    'income':r'(?:remove|clear)\s+(?:the\s+)?income\s+filters?|取消收入筛选',
    'race':r'(?:remove|clear)\s+(?:the\s+)?race\s+filters?|取消(?:族裔|种族)筛选',
    'age':r'(?:remove|clear)\s+(?:the\s+)?age\s+filters?|取消年龄筛选',
}
_METRIC_GENERIC = set('nhis the a an all overall registered health healthcare medical care service services indicator indicators outcome outcomes self rated fair poor percentage percentages prevalence proportion proportions rate rates distribution distributions estimates estimate groups group education income sex gender race ethnicity age insurance coverage insured uninsured delayed unmet cost related needed access timely of and or by in for on'.split())
_METRIC_GENERIC.update('due to weighted survey weights calculation calculations method methods confidence interval intervals me this that these those how valid responses high school graduate graduates less than bachelor bachelors college associate degree female male women men'.split())
_KNOWN_ADJUSTMENT = set('for with using only the a an all full model covariates variables education educational attainment income poverty to category categories age ages group groups sex gender race ethnicity ethnic hispanic and or without excluding excluded no in regression logistic adjustment adjusted odds ratio ratios interpretation estimates estimate same sample'.split())
_POPULATION_WORDS=set('the a an all only adults adult women men females males population respondents participants ages age aged years year old between and or to with valid responses response records record less than high school graduates graduate some college associate degree bachelor bachelors higher non hispanic white black asian other poverty line threshold below above of income female male education educated'.split())
_GROUP_WORDS=set('education educational attainment income poverty ratio federal level fpl age ages group groups sex gender race racial ethnicity ethnic hispanic and or versus vs by overall cross stratified distribution distributions health outcomes indicators nhis'.split())
_GROUP_NAMES = {
    'education': r'\beducation(?:al attainment)?\b|教育(?:程度|水平)?|学历',
    'income': r'\bincome\b|收入|贫困(?:线|率)?',
    'sex': r'\b(?:sex|gender)\b|性别|男女',
    'race': r'\b(?:race|ethnicity)\b|族裔|种族',
    'age': r'\bage(?: groups?)?\b|年龄(?:组)?',
}


def inspect_intent(question):
    q = question.lower() if isinstance(question,str) else ''
    recognized,unsupported,ambiguous,operations = [],[],[],[]
    def record(destination,field,match,reason='',value=''):
        destination.append(Constraint(field,question[match.start():match.end()],match.start(),match.end(),value,reason))
    for field,pattern,reason in _UNKNOWN:
        for match in re.finditer(pattern,q):record(unsupported,field,match,reason)
    for match in _DOUBLE.finditer(q):
        record(ambiguous,'negation',match,'Double negation or a nonexclusive qualifier needs clarification.')
    # Capture unknown named estimands instead of treating every unknown noun as health.
    for pattern in (r'\b(?:compare|calculate|estimate|show)\s+(.+?)\s+(?:prevalence|proportions?|rates?)\b',
                    r'\b(?:prevalence|rate|proportion)\s+of\s+(.+?)(?=\s+(?:by|among|in|for)\b|[?.!]|$)',
                    r'([^，。？！、\s]{1,20})(?:患病率|发病率)'):
        for match in re.finditer(pattern,q):
            term=match.group(1).strip()
            words=set(re.findall(r'[a-z]+',term))
            known=bool(re.search(r'自评健康|医疗保险|医保|医疗服务|延迟就医|未就医',term)) or (bool(words) and words.issubset(_METRIC_GENERIC))
            if not known and not any(item.start<=match.start(1)<item.end for item in unsupported):
                unsupported.append(Constraint('outcome',question[match.start(1):match.end(1)],match.start(1),match.end(1),reason='The explicitly named indicator is not registered.'))
    # Population qualifiers not expressible by the allowlisted categories are unresolved.
    for match in re.finditer(r'\b(?:among|restricted to|limited to|for)\s+([^,.;?!]+)',q):
        term=match.group(1)
        stop=re.search(r'\s+(?:by|using|in nhis|with \d{2}\s*(?:%|percent)\s+confidence|and (?:show|explain))\b',term)
        if stop:term=term[:stop.start()]
        if not re.search(r'\b(?:adults?|women|men|female|male|population|respondents|ages?|aged|education|income|non-hispanic|hispanic|bachelor|poverty)\b|25\s*[-–]\s*64',term):
            # Method questions ('for this estimate') and fixed-source descriptions are not restrictions.
            if not re.search(r'\b(?:this|that|these|the|each|all|a|an|missing|refused|non|medical|care|confidence|one|every)\b',term):
                record(unsupported,'population',match,'That explicit population qualifier has no registered category.')
        if re.match(r'(?:among|restricted to|limited to)\b',match.group(0)):
            residue=set(re.findall(r'[a-z]+',term))-_POPULATION_WORDS
            if residue:
                record(unsupported,'population',match,'At least one explicit population condition has no registered category.')
    # Audit explicit grouping clauses even when a known dimension is also
    # present ("by education and religion" must not silently become education).
    for match in re.finditer(r'\bby\s+([^.;?!]+)',q):
        if re.search(r'\bstep\s+$',q[:match.start()]):continue
        term=re.split(r'\b(?:among|restricted|limited|in|for|with|excluding|except|without|not|only|using|and then|and show|and explain)\b',match.group(1),maxsplit=1)[0]
        if set(re.findall(r'[a-z]+',term))-_GROUP_WORDS:
            record(unsupported,'group_by',match,'At least one explicitly named grouping variable is not registered.')
        for value, pattern in _GROUP_NAMES.items():
            for named in re.finditer(pattern, term):
                start = match.start(1)+named.start()
                end = match.start(1)+named.end()
                recognized.append(Constraint('group_by',question[start:end],start,end,value))
    # Deliberately bounded Chinese grouping clauses. A known indicator later in
    # the sentence cannot turn an unknown explicit dimension into "overall".
    for match in re.finditer(r'按(?:照)?([^，。？！]{1,40}?)(?=比较|分组|统计|分析|查看|展示)',q):
        term = match.group(1)
        residual = term
        for value, pattern in _GROUP_NAMES.items():
            for named in re.finditer(pattern,term):
                start = match.start(1)+named.start()
                end = match.start(1)+named.end()
                recognized.append(Constraint('group_by',question[start:end],start,end,value))
            residual = re.sub(pattern,'',residual)
        residual = re.sub(r'以及|和|与|及|组|、|[×*x/\s]','',residual)
        if residual:
            record(unsupported,'group_by',match,'At least one explicitly named grouping variable is not registered.')
    for match in re.finditer(r'\b(?:adjust(?:ed|ing)?|control(?:led|ling)?)\s+(?:for\s+)?([^.;?!]+)',q):
        record(recognized,'adjustment',match,value=match.group(1))
        clause=match.group(1)
        if re.search(r'\b(?:occupation|marital|employment|bmi|smoking|region|height|weight|religion|wealth|language)\b',clause):
            record(unsupported,'adjustment',match,'The requested adjustment variable is not in the fixed model.')
        elif set(re.findall(r'[a-z]+',clause)) - _KNOWN_ADJUSTMENT:
            record(unsupported,'adjustment',match,'At least one explicitly named adjustment has no registered model term.')
    for match in re.finditer(r'(?:调整|控制|校正)([^，。？！]+)',q):
        record(recognized,'adjustment',match,value=match.group(1))
        residual=re.sub(r'教育程度|教育|学历|收入|贫困|年龄组|年龄|性别|族裔|种族|模型|回归|变量|因素|完整|全部|所有|只|仅|和|及|与|、|\s','',match.group(1))
        if residual:
            record(unsupported,'adjustment',match,'At least one requested adjustment is outside the registered model.')
    for match in re.finditer(r'(?<!\d)((?:19|20)\d{2})(?!\d)',q):
        record(recognized if match.group(1)=='2024' else unsupported,'year',match,
               '' if match.group(1)=='2024' else 'Only the verified 2024 survey release is supported.',match.group(1))
    for code,pattern in (
        ('fair_poor',r'self.?rated health|fair\s*(?:/|or)\s*poor(?: health)?|自评健康|健康一般或较差'),
        ('insured,uninsured',r'(?:health )?insurance coverage|health insurance|医疗保险|健康保险|医保|notcov_a'),
        ('insured',r'\binsured\b|有医保|已参保'),
        ('uninsured',r'\buninsured\b|without (?:health )?insurance|not covered|无医保|无保险|未参保'),
        ('delayed_care_cost',r'delayed? (?:medical )?care|延迟就医|推迟就医|meddl12m_a'),
        ('unmet_care_cost',r'unmet (?:medical )?care|未获得.{0,6}医疗|未就医|medng12m_a')):
        for match in re.finditer(pattern,q):record(recognized,'outcome',match,value=code)
    for code,pattern in (('education',r'\bby education\b|\bswitch to education\b|按教育'),
                         ('income',r'\bby income\b|\bswitch to income\b|按收入'),
                         ('sex',r'\bby sex\b|\bby gender\b|按性别'),
                         ('race',r'\bby race\b|\bby ethnicity\b|按族裔|按种族'),
                         ('age',r'\bby age\b|按年龄组')):
        for match in re.finditer(pattern,q):record(recognized,'group_by',match,value=code)
    for match in re.finditer(r'(?:(\d{2}(?:\.\d+)?)\s*(?:%|percent)\s*(?:confidence|ci)|(?:confidence|\bci\b)\D{0,8}(\d{2}(?:\.\d+)?)\s*(?:%|percent))',q):
        value=match.group(1) or match.group(2)
        record(recognized if float(value)==95 else unsupported,'confidence',match,
               '' if float(value)==95 else 'The verified interval level is 95%.',value)
    clears=[]
    for match in re.finditer(r'\b(?:clear|remove)\s+all\s+filters?\b|\bwithout\s+(?:(?:any|all)\s+)?filters?\b|取消所有筛选|清空筛选',q):
        for field in FILTER_VALUES:
            operations.append(FilterOperation(field,'clear',(),question[match.start():match.end()],match.start(),match.end()))
        clears.append((match.start(),match.end()))
    for field,pattern in _CLEAR.items():
        for match in re.finditer(pattern,q):
            operation=FilterOperation(field,'clear',(),question[match.start():match.end()],match.start(),match.end())
            operations.append(operation);clears.append((match.start(),match.end()));record(recognized,field,match,value='clear')
    for field,categories in _CATEGORY.items():
        mentions=[]
        for value,pattern in categories.items():
            for match in re.finditer(pattern,q):
                if any(left<=match.start()<right for left,right in clears):continue
                prefix=q[max(0,match.start()-45):match.start()]
                negative=bool(_NEGATIVE.search(prefix))
                operator='exclude' if negative else 'include'
                mentions.append((match.start(),match.end(),operator,value))
        mentions=[item for item in mentions if not any(other[3]!=item[3] and other[0]<=item[0] and item[1]<=other[1] for other in mentions)]
        mentions.sort()
        for index,item in enumerate(mentions):
            if index and item[2]=='include' and mentions[index-1][2]=='exclude':
                separator=q[mentions[index-1][1]:item[0]]
                if re.fullmatch(r'\s*(?:and|or|nor|、|和|及)\s*',separator):
                    mentions[index]=(item[0],item[1],'exclude',item[3])
        # Build category operations before compiling a legal statistical spec.
        excluded=[item for item in mentions if item[2]=='exclude']
        included=[item for item in mentions if item[2]=='include']
        if excluded:
            values=tuple(dict.fromkeys(item[3] for item in sorted(excluded)))
            left=min(item[0] for item in excluded);right=max(item[1] for item in excluded)
            operations.append(FilterOperation(field,'exclude',values,question[left:right],left,right))
            recognized.append(Constraint(field,question[left:right],left,right,','.join(values)))
            if set(values)==set(FILTER_VALUES[field]) or any(item[3] in values for item in included):
                ambiguous.append(Constraint(field,question[left:right],left,right,reason='The inclusion and exclusion conditions conflict or exclude every category.'))
        for item in included:
            recognized.append(Constraint(field,question[item[0]:item[1]],item[0],item[1],item[3]))
        if included and not excluded:
            ordered=sorted(included)
            left=min(item[0] for item in included);right=max(item[1] for item in included)
            operations.append(FilterOperation(field,'include',tuple(dict.fromkeys(item[3] for item in ordered)),question[left:right],left,right))
    for match in re.finditer(r'\b(?:excluding|exclude|except|without|not including|neither)\b|排除|不包括|不包含|剔除',q):
        if any(op.operator=='exclude' and 0<=op.start-match.end()<=25 for op in operations):continue
        if re.match(r'\s+(?:health\s+)?insurance\b',q[match.end():]) and match.group(0)=='without':continue
        if re.match(r'\s+(?:(?:any|all)\s+)?filters?\b',q[match.end():]):continue
        record(ambiguous,'negation',match,'The excluded condition could not be mapped to a registered population; please name an allowed category.')
    followup=bool(re.match(r'^(?:please\s+)?(?:what about|and\b|now\b|only\b|just\b|switch\b|change\b|exclude\b|excluding\b|remove\b|clear\b|except\b|那|那么|只看|仅看|排除|剔除|不包括|去掉|取消|切换|换成|改成)',q.strip()))
    unique=lambda items:tuple({(item.field,item.start,item.end):item for item in items}.values())
    return AnalysisIntent(question,unique(recognized),unique(unsupported),unique(ambiguous),tuple(sorted(operations,key=lambda item:item.start)),followup)
