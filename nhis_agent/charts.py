"""Pure chart specifications from released local survey results.

No estimates, intervals, contrasts, or model predictions are calculated here.
Each panel retains one run's population and filters; separate runs are never
combined into a synthetic population or comparison interval.
"""
import math
import re

from .answers import category_name, scope_name, VARIABLES

_RUN_ID = re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}')
_SECRET = re.compile(r'\bsk-[A-Za-z0-9_-]{8,}')
_KINDS = {'auto','proportion','gradient','regression'}
_STYLES = {'point','bar','forest'}


def _finite(value):
    try:
        return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)
    except OverflowError:
        return False


def _text(value):
    return _SECRET.sub('[credential redacted]',str(value)).replace('\n',' ').strip()


def _count(value):
    return _finite(value) and value >= 100 and int(value)==value


def _proportion(row):
    if not isinstance(row,dict) or row.get('status')!='ok':
        return False
    value,low,high=(row.get(key) for key in ('estimate','ci_low','ci_high'))
    if not all(_finite(item) for item in (value,low,high)) or not 0 <= low <= value <= high <= 1:
        return False
    if 'n' in row and not _count(row['n']):
        return False
    return 'se' not in row or (_finite(row['se']) and row['se'] > 0)


def _point(label,value,low,high,record,group_id=None):
    point={'label':label,'value':value,'low':low,'high':high}
    if _count(record.get('n')):
        point['n']=int(record['n'])
    if isinstance(group_id,str) and group_id:
        point['group_id']=_text(group_id)
    return point


def _coefficient_label(term,references):
    for variable in ('education','income','race','age_years','age','sex'):
        if term.startswith(variable):
            category=term[len(variable):].lstrip('_:')
            label=VARIABLES[variable] + (': '+category_name(category) if category else '')
            reference=references.get(variable) if isinstance(references,dict) else None
            if category and isinstance(reference,str):
                label+=' vs '+category_name(reference)
            return label
    return _text(term)


def _groups(result,kind):
    survey=result['survey']
    rows=survey.get('rows') or []
    if not isinstance(rows,list):
        rows=[]
    published={row['group_id']:row for row in rows if _proportion(row) and isinstance(row.get('group_id'),str)}
    groups=[]
    if kind=='proportion':
        for row in rows:
            if _proportion(row):
                groups.append(_point(category_name(row.get('group_id'),row.get('label')),
                    row['estimate']*100,row['ci_low']*100,row['ci_high']*100,row,row.get('group_id')))
    elif kind=='gradient':
        gradient=survey.get('gradient')
        contrasts=gradient.get('contrasts',[]) if isinstance(gradient,dict) else []
        for row in contrasts if isinstance(contrasts,list) else []:
            if not isinstance(row,dict) or row.get('status')!='ok':
                continue
            target,reference=row.get('group_id'),row.get('reference_id')
            if not isinstance(target,str) or not isinstance(reference,str) or target not in published or reference not in published:
                continue
            value,low,high=(row.get(key) for key in ('estimate','ci_low','ci_high'))
            if not all(_finite(item) for item in (value,low,high)) or not (-1 <= value <= 1 and low <= value <= high):
                continue
            if 'se' in row and not (_finite(row['se']) and row['se'] > 0):
                continue
            groups.append(_point(category_name(target)+' − '+category_name(reference),
                value*100,low*100,high*100,{},target))
    else:
        regression=survey.get('regression')
        if (not isinstance(regression,dict) or regression.get('status')!='ok' or not _count(regression.get('n'))
                or not isinstance(regression.get('unadjusted'),dict)
                or regression['unadjusted'].get('n')!=regression['n']):
            return []
        coefficients=regression.get('coefficients',[])
        for row in coefficients if isinstance(coefficients,list) else []:
            if (not isinstance(row,dict) or row.get('status','ok')!='ok' or not isinstance(row.get('term'),str)
                    or not row['term'] or row['term']=='(Intercept)'):
                continue
            value,low,high=(row.get(key) for key in ('odds_ratio','ci_low','ci_high'))
            if not all(_finite(item) for item in (value,low,high)) or not 0 < low <= value <= high:
                continue
            if 'se' in row and not (_finite(row['se']) and row['se'] > 0):
                continue
            groups.append(_point(_coefficient_label(row['term'],regression.get('references',{})),
                value,low,high,regression,row['term']))
    return groups


def make_charts(results,kind='auto',style='point'):
    """Return separate trusted panels, preserving the supplied discussion order."""
    if kind not in _KINDS or style not in _STYLES:
        raise ValueError('Choose a supported chart kind and style.')
    if isinstance(results,dict):
        results=[results]
    if results is None:
        return []
    if not isinstance(results,(list,tuple)):
        raise ValueError('Chart inputs must be saved result dictionaries.')
    charts=[];seen=set()
    for result in results:
        if (not isinstance(result,dict) or result.get('status')!='ok'
                or not isinstance(result.get('run_id'),str) or not _RUN_ID.fullmatch(result['run_id'])
                or not isinstance(result.get('survey'),dict) or result['survey'].get('status')!='ok'
                or not isinstance(result.get('spec'),dict)):
            continue
        spec=result['spec']
        if (spec.get('year',2024)!=2024 or spec.get('outcome','fair_poor')!='fair_poor'
                or spec.get('age_min',25)!=25 or spec.get('age_max',64)!=64 or spec.get('confidence',.95)!=.95):
            continue
        selected=kind if kind!='auto' else spec.get('analysis','proportion')
        if selected not in ('proportion','gradient','regression'):
            continue
        chart_id=result['run_id']+'-'+selected
        if chart_id in seen:
            continue
        groups=_groups(result,selected)
        if not groups:
            continue
        unit={'proportion':'percent','gradient':'percentage_points','regression':'odds_ratio'}[selected]
        titles={'proportion':'Fair or poor self-rated health',
                'gradient':'Group differences in fair or poor self-rated health',
                'regression':'Adjusted odds ratios for fair or poor self-rated health'}
        title=titles[selected]
        if selected=='proportion' and spec.get('group_by') not in (None,'overall'):
            title+=' by '+VARIABLES.get(spec['group_by'],_text(spec['group_by'])).lower()
        subtitle=scope_name(spec).strip()+' Points and recorded 95% confidence intervals.'
        imputation=result['survey'].get('imputation')
        if isinstance(imputation,dict) and imputation.get('required') is True and imputation.get('m')==10:
            subtitle+=' Income estimates pool all 10 official imputations; n is the minimum across imputations.'
        labels={'proportion':'Fair or poor self-rated health (%)',
                'gradient':'Difference (percentage points)',
                'regression':'Adjusted odds ratio (log scale)'}
        charts.append({'id':chart_id,'title':title,'subtitle':subtitle,'kind':selected,
                       'style':'forest' if selected=='regression' else style,
                       'x_label':labels[selected],'reference':1 if selected=='regression' else 0,
                       'groups':groups,'source_run_id':result['run_id'],'unit':unit})
        seen.add(chart_id)
    return charts


def chart_request(text,has_results=True):
    """Recognize requests to plot saved results, leaving general art/chat alone."""
    if not has_results or not isinstance(text,str) or not text.strip():
        return None
    q=text.lower().strip()
    chart=re.search(r'\b(?:plots?|charts?|graphs?|figures?|visuali[sz](?:e|ation|ations))\b|'
                    r'画.{0,4}图|绘.{0,3}图|作图|图表|柱状图|森林图|可视化',q)
    if not chart:
        return None
    if (re.match(r'^(?:explain|interpret|how (?:do i|to) read|what (?:do|does).{0,15}mean|解释|解读|怎么看)',q)
            and not re.search(r'\b(?:draw|show|give|make|create|generate|render)\b|帮我画|画一|绘制',q)):
        return None
    if re.search(r'\b(?:novel|story|movie|film|fiction|screenplay|plot twist|portrait|landscape|logo|artwork)\b|'
                 r'小说|故事|电影|剧情|肖像|风景|画.{0,4}(?:猫|狗|人物|人像)',q):
        return None
    if (re.search(r'\b(?:sine|cosine|parabola|equation|normal distribution)\b|'
                  r'\by\s*=|\bx\s*\^\s*\d|正弦|余弦|函数|抛物线|正态分布',q)
            and not re.search(r'\bnhis\b|self.?rated|fair.{0,5}poor|自评健康',q)):
        return None
    if re.search(r'\bfigure\s+out\b',q) and not re.search(r'\b(?:plots?|charts?|graphs?)\b|可视化|画图',q):
        return None
    if (re.match(r'^(?:what (?:is|are)|define|explain (?:a|an)|how (?:do|does)|什么是)',q)
            and not re.search(r'\b(?:draw|show|give|make|create|generate)\b|帮我|给我|画',q)):
        return None
    scope='latest' if re.search(r'\b(?:latest|last|current|this result|this run|most recent)\b|\b(?:this|that)\s+(?:as|in|using)\b|最新|最近|刚才|当前|本次|这次|最后',q) else 'all'
    if re.search(r'\ball\b|whole|everything|discussed|全部|所有|总览|讨论过|一起',q):
        scope='all'
    regression = re.search(r'odds.?ratios?|regression|logistic|回归|优势比|比值比',q) or re.search(r'(?<![A-Za-z])OR(?![A-Za-z])',text)
    kind='regression' if regression else 'gradient' if re.search(r'gradients?|contrasts?|differences?|梯度|差值|差异',q) else 'proportion' if re.search(r'proportions?|prevalence|percentages?|比例|患病率',q) else 'auto'
    style='bar' if re.search(r'\bbars?\b|柱状',q) else 'forest' if re.search(r'\bforest\b|森林',q) or kind=='regression' else 'point'
    return {'scope':scope,'kind':kind,'style':style}
