"""Persistent, contextual NHIS conversations over the registered analysis engine.

Dialogue planning is deterministic and never changes statistical rules. Only
observed execution events are streamed; hidden reasoning is not recorded.
"""
import copy
from contextlib import contextmanager
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from .spec import route, VERSION, GROUPS
from .stats import _normalise_spec
from .answers import build_analysis_answer, category_name, scope_name
from .explanations import build_explanation
from .jobs import ConversationBusy
from .charts import chart_request, make_charts

_CONVERSATION_ID = re.compile(r'[a-f0-9]{32}')
_RUN_ID = re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}')
_SECRET = re.compile(r'\bsk-[A-Za-z0-9_-]{12,}')
_CONVERSATION_LOCKS = {}
_CONVERSATION_LOCKS_GUARD = threading.Lock()
_GROUP_PATTERNS = {
    'education_race': r'(?:教育.{0,8}(?:族裔|种族)|(?:族裔|种族).{0,8}教育).{0,6}(?:交叉|分组)|education.{0,8}race',
    'education': r'教育|学历|education',
    'income': r'收入|income',
    'race': r'族裔|种族|race|ethnic',
    'sex': r'性别|sex|gender',
    'age': r'年龄组|age group|by age',
}
_DEFAULT_SPEC = {'version': VERSION, 'year': 2024, 'outcome': 'fair_poor',
                 'analysis': 'proportion', 'group_by': 'overall', 'filters': {},
                 'min_n': 100, 'age_min': 25, 'age_max': 64, 'confidence': .95, 'adjustment': 'full'}
_HELP = ('Hi! I can help you think through ideas, write and revise, explain concepts, or just chat. '
         'I can also analyze real NHIS 2024 survey data when you want evidence about self-rated health. '
         'What would you like to work on?')
_METHOD = ('The registered analysis builds a complex survey design from the full NHIS 2024 adult file '
           'using WTFA_A, PSTRAT, and PPSU before selecting adults aged 25–64 and any requested categories. '
           'PHSTAT_A codes 4 and 5 define fair/poor health. Invalid responses are excluded from the relevant denominator. '
           'Income analyses pool all ten official imputations using Rubin rules. Published estimates must pass reliability checks. '
           'These are cross-sectional associations, not causal effects.')
_SUGGESTIONS = ['Compare self-rated health by education', 'Compare self-rated health by income', 'How is this calculated?']
_EXPLANATION = re.compile(
    r'解释|怎么算|怎么计算|如何计算|怎么得出|计算公式|计算方法|方法说明|统计方法|置信区间|'
    r'样本(?:量|数|有多少)|分母|为什么|为何|抑制|偏差最大|差距最大|最大偏差|'
    r'和刚才|与刚才|上一次相比|跟之前|前后比较|模型.{0,8}(?:怎么|如何)|提示词|英文提问|用什么模型|'
    r'自评健康.{0,8}(?:是什么|什么意思|含义)|什么是自评健康|什么是加权比例|什么叫加权比例|'
    r'加权比例(?:是什么|什么意思|怎么算|如何计算)|差异说明什么|意味着什么|总结一下|结果怎么看|'
    r'展开讲讲|详细一点|再详细点|讲详细点|详细计算|详细解释|继续解释|具体一点|计算步骤|详细步骤|分析步骤|统计步骤|解释步骤|'
    r'具体计算过程|一步步|展开说|多说一点|rubin|多重插补|插补怎么|mi怎么|优势比|比值比|回归系数|'
    r'显著性|最高|最低|统计显著|显著吗|more detail|elaborate|step.by.step|odds ratio|multiple imputation|'
    r'model.*prompt|explain|confidence interval|denominator|sample size|how.*calculat|why|suppres|break it down|walk me through|what does (?:this|that) mean|tell me more|'
    r'largest.*(?:bias|gap)|compared.*last|highest|lowest|significance|significant|'
    r'(?<![a-z])(?:ci|mi)(?![a-z])|'
    r'(?<![a-z])or(?![a-z])\s*(?:的\s*)?(?:是?什么意思|是什么|如何解释|怎么理解|代表什么|含义|意义|怎么看|解释)|'
    r'(?:解释|什么是)\s*(?<![a-z])or(?![a-z])'
)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _redact(value):
    return _SECRET.sub('[credential hidden]', value)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _safe_facts(value, depth=0):
    """Keep concise observable facts, with no credentials, records, or raw calls."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        blocked = ('key', 'token', 'authorization', 'raw', 'record', 'call', 'secret', 'message')
        counts = {'planned_calls','valid_calls','total_calls','completed_calls','failed_calls','refused_calls',
                  'attempted_calls','input_tokens','output_tokens','row_count','record_count','elapsed_ms'}
        return {str(k)[:80]: _safe_facts(v, depth + 1) for k, v in list(value.items())[:24]
                if not any(word in str(k).lower() for word in blocked)
                or (str(k).lower() in counts and _finite(v))}
    if isinstance(value, list):
        return [_safe_facts(v, depth + 1) for v in value[:24]]
    if isinstance(value, str):
        return _redact(value)[:1200]
    if value is None or isinstance(value, bool) or _finite(value):
        return value
    return str(value)[:200]


def _validated(spec):
    # Do not accept paths or runtime options through a conversation's context.
    allowed = set(_DEFAULT_SPEC)
    candidate = {k: copy.deepcopy(v) for k, v in spec.items() if k in allowed}
    clean = _normalise_spec(candidate)
    result = dict(_DEFAULT_SPEC, **clean)
    result['filters'] = {k: v[0] if len(v) == 1 else v for k, v in clean['filters'].items()}
    return result


def _scope_label(spec):
    return scope_name(spec)


def _method_topic(text):
    q = text.lower()
    patterns = [
        ('variables', r'(?:which|what).{0,20}variables|variables.{0,20}(?:used|included)|(?:用了|使用|包括|哪些|什么).{0,6}变量|变量.{0,6}(?:哪些|什么)'),
        ('weights', r'(?:how|why|what).{0,30}(?:weights|weighted|weighting)|(?:weights|weighting).{0,20}(?:work|used|mean)|权重.{0,8}(?:怎么|如何|使用|用法)|为什么.{0,6}(?:用|要|需要).{0,3}加权|为何.{0,6}加权'),
        ('sample_filter', r'how.{0,25}(?:select|filter|restrict).{0,20}(?:sample|respondent)|(?:怎么|如何).{0,4}筛选.{0,4}样本|样本.{0,4}(?:怎么|如何).{0,4}筛选'),
        ('source', r'data source|where.{0,30}data|数据.{0,4}(?:来源|哪里|哪来)|来源.{0,4}是什么|where.*data'),
        ('age_rule', r'why.{0,30}25.{0,5}64|(?:为什么|为何).{0,6}(?:只看|使用|选|限定|取).{0,4}25.{1,4}64'),
        ('sample_rule', r'why.{0,30}(?:100|small sample|sample threshold)|(?:为什么|为何|解释).{0,12}样本.{0,8}100.{0,12}(?:不|抑制|显示)'),
    ]
    return next((topic for topic, pattern in patterns if re.search(pattern, q)), None)


def _rule_override(text):
    return bool(re.search(r'(?:改|调|设|降|变|换|允许|忽略|放宽|取消|提高).{0,8}(?:阈值|下限|门槛|置信|年龄|岁|\d)|(?:阈值|下限|门槛|置信|年龄).{0,8}(?:改|调|设|降|变|换|放宽|取消)',text.lower()))


def _analysis_request(text, routed):
    """Keep an explicit calculation command when it also asks for explanation."""
    if routed.get('status') != 'ready':
        return False
    q = re.sub(r'^(?:(?:请你|请|帮我|麻烦你|麻烦|能不能|可以帮我|可以)\s*)+', '', text.lower())
    clauses = re.split(r'(?:[，,；;。]\s*(?:(?:并且|并|同时|然后|再|顺便)\s*)?|(?:并且|并|同时|然后|再|顺便))'
                       r'(?:请)?(?:解释|说明|讲清|详细|展开|告诉我|说说|讲讲|给(?:出|我).{0,6}(?:步骤|解释))', q, maxsplit=1)
    primary = clauses[0].strip(' ，,；;。')
    if _method_topic(primary):
        return False
    if re.search(r'怎么算|怎么计算|如何计算|怎么得出|为什么|为何|什么是|是什么|什么意思|'
                 r'步骤|方法|公式|原理|(?:比较|分析|计算)(?:时|中|过程中)|样本有多少|的样本数|分母', primary):
        return False
    if re.search(r'^(?:what|show|tell me).{0,35}(?:percentage|proportion|prevalence|health rate)', primary):
        return route(primary).get('status') == 'ready'
    if re.match(r'^(?:解释|说明|详细|展开|继续解释|what|why|how|explain)', primary):
        return False
    command = re.match(r'^(?:按|比较|分析|计算|估计|拟合|改成|换成|改为|换为|改按|换按|'
                       r'切换|只看|仅看|筛选|取消|去掉|回到|恢复|清空|compare|analyse|analyze|calculate)', primary)
    if not command and len(clauses) == 1:
        return False
    primary_route = route(primary)
    return (primary_route.get('status') == 'ready' and
            primary_route['spec']['analysis'] == routed['spec']['analysis'])


def _explanation_target_mismatch(text, current):
    """Do not explain a named result using a different saved analysis."""
    if not current or current.get('status') != 'ok' or _method_topic(text):
        return False
    q = text.lower()
    if re.search(r'多重插补|rubin|(?<![a-z])mi(?![a-z])|收入插补', q):
        return False
    group = next((name for name, pattern in _GROUP_PATTERNS.items() if re.search(pattern, q)), None)
    result_question = re.search(r'结果|梯度|分组|组|比例|差异|差距|显著|最高|最低|置信区间|'
                                r'样本|回归系数|(?<![a-z])ci(?![a-z])', q)
    if not result_question:
        return False
    spec = current.get('spec') or {}
    requested_analysis = ('regression' if re.search(r'回归|logistic|regression', q) else
                          'gradient' if re.search(r'梯度|gradient', q) else None)
    if not group and not re.search(r'结果|本轮|这次|上次|刚才', q):
        return False
    if requested_analysis and requested_analysis != spec.get('analysis', 'proportion'):
        return True
    if spec.get('analysis') == 'regression':
        return False
    return bool(group and group != spec.get('group_by'))


def analysis_reply(result, spec, previous=None, question=None):
    """Write a readable answer from released statistics and recorded methods."""
    return build_analysis_answer(result, spec, previous=previous, question=question)


def _uses_research_flow(text, context):
    """Reserve fixed survey routing for research requests, not all conversation."""
    q = text.lower().strip()
    study = re.search(r'\bnhis\b|phstat|self.?rated|fair.{0,5}poor|自评健康', q)
    group = any(re.search(pattern, q) for pattern in _GROUP_PATTERNS.values())
    action = re.search(r'\b(?:compare|calculate|estimate|analy[sz]e|fit|run|breakdown)\b|'
                       r'比较|计算|估计|分析|回归|梯度|^按', q)
    if action and (study or (group and re.search(r'health|健康', q))):
        return True
    if (study and re.search(r'percentage|proportion|prevalence|statistics?|rates?|estimates?|\bshow\b|比例|百分比', q)
            and not re.search(r'\b(?:write|draft|rewrite|translate|proofread|email|essay)\b|翻译|润色|帮我写', q)):
        return True
    if (context.get('pending') or {}).get('field') == 'income' and re.search(r'100\s*%|100\s*[-–]\s*199|200\s*[-–]\s*399|400\s*%', q):
        return True
    technical = re.search(r'confidence interval|standard error|odds ratio|rubin|multiple imputation|'
                          r'sample size|denominator|survey weight|weighted proportion|'
                          r'置信区间|标准误|优势比|多重插补|分母|样本量|(?<![a-z])(?:ci|mi)(?![a-z])', q)
    if technical and (_EXPLANATION.search(q) or re.search(r'what|how|meaning|是什么|什么意思', q)):
        return True
    active = bool(context.get('spec') and context.get('last_turn_mode') != 'chat')
    if active:
        followup = re.fullmatch(
            r'(?:what about|and|now|only|just|switch(?: to)?|change(?: to)?|restrict(?: to)?|'
            r'remove|clear|(?:go )?back to)\s+(?:the\s+)?'
            r'(?:women|men|female|male|education|income|race|ethnicity|sex|age(?: groups?)?|'
            r'(?:(?:all|sex|education|income|race|age)\s+)?filters?|overall|full population|all adults|'
            r'china|canada|bachelor[’\x27]s(?: degree)?(?: or higher)?|less than high school|'
            r'25\s*[-–]\s*34|35\s*[-–]\s*44|45\s*[-–]\s*54|55\s*[-–]\s*64)'
            r'(?:\s+(?:only|instead|grouping))?[?!.\s]*', q)
        chinese_followup = re.fullmatch(
            r'(?:那|那么|只看|仅看|按|改成|换成|取消|去掉|恢复|回到|清空)'
            r'(?:女性|男性|教育|教育水平|收入|族裔|种族|性别|年龄组|总体|筛选|性别筛选|所有筛选|中国成年人|加拿大成年人)'
            r'(?:呢|分组|筛选|限定|[？?。！!\s])*', q)
        if followup or chinese_followup:
            return True
        if re.search(r'^(?:what about|only|just|switch|change|remove|clear|那|只看|改成|换成)', q) and re.search(r'\b(?:nhis|survey|study|results?)\b|本次分析|调查结果', q):
            return True
        if re.fullmatch(r'(?:女性|男性|female|male|women|men)(?:呢|only|的比例|[？?。.!！\s])*', q):
            return True
        if re.search(r'解释一下|详细一点|展开|和刚才|样本|为什么抑制|'
                     r'explain (?:this|that|these|the results?)|interpret (?:these|the) (?:charts?|plots?|graphs?)|what does (?:this|that|it) mean|'
                     r'break it down|more detail|walk me through|compared.*(?:last|previous)|'
                     r'which (?:published )?group.{0,20}(?:highest|lowest)|'
                     r'(?:highest|lowest).{0,15}(?:proportion|rate|group)|'
                     r'^(?:is (?:this|that)|are these).{0,10}significant|^why[?! .]*$', q):
            return True
    return False


def _chat_reference(text, context):
    return context.get('last_turn_mode') == 'chat' and bool(re.fullmatch(
        r'(?:explain (?:this|that|it)|what does (?:this|that|it) mean|tell me more|'
        r'more detail|break it down|why|解释一下|详细一点|为什么)[?!.？。！\s]*', text.lower().strip()))


def _generation_metadata(value):
    fields = ('status','provider','model','response_id','request_id','usage','elapsed_ms',
              'http_status','error_code','message','api_status')
    return {key:copy.deepcopy(value[key]) for key in fields if key in value}


def _conversation_only(text):
    """Instructions about words and explicit refusals are not tool commands."""
    q = text.lower().strip()
    return bool(re.search(r'\b(?:translate|rewrite|proofread|paraphrase)\b|翻译|润色|改写|'
                          r'\bexplain\s+(?:the\s+)?(?:instruction|phrase|sentence|request|word)|解释.{0,5}(?:这句话|这个指令|这个词)|'
                          r'\b(?:do not|don.t|never|avoid|stop)\s+(?:\w+\s+){0,3}(?:compare|plot|draw|chart|calculate|run)|'
                          r'(?:不要|别|不必|不用|无需).{0,8}(?:比较|计算|运行|画图|绘图|分析)', q))


def _chart_groups(text):
    q = re.sub(r'\b(?:sex|gender|education|income|race|age)\s+filters?\b|(?:性别|教育|收入|族裔|种族|年龄)筛选', '', text.lower())
    return [name for name,pattern in _GROUP_PATTERNS.items() if re.search(pattern,q)]


def _clear_chart_filters(text):
    return bool(re.search(r'\b(?:remove|clear|no|without)\s+(?:(?:all|the|any|sex|gender|education|income|race|age)\s+)*filters?\b|'
                          r'(?:取消|去掉|清空).{0,6}筛选|不限',text.lower()))


def _batch_request(text, context, messages):
    """Resolve an explicit request for all registered comparisons, not all topics."""
    q = text.lower().strip()
    all_groups = re.search(r'(?:all|every)\s+(?:(?:the|those|these|available|supported|population)\s+)?(?:comparisons?|groupings?|groups?|demographic groups?|analyses)|'
                           r'\b(?:all|every)\b.{0,80}\b(?:comparisons?|groupings?|groups?|analyses)\b|'
                           r'(?:全部|所有|每种).{0,10}(?:分组|比较|分析)|(?:都|全部).{0,4}(?:做|跑|算)(?:一遍|一下|一轮)?', q)
    if not all_groups or re.search(r'\b(?:plot|chart|graph|visualiz\w*)\b|画图|绘图|图表|可视化', q):
        return False
    if context.get('last_turn_mode') == 'chat' and not re.search(r'\bnhis\b|self.?rated|fair.{0,5}poor|自评健康', q):
        prior_user = next((str(item.get('content','')).lower() for item in reversed(messages) if item.get('role')=='user'), '')
        if not re.search(r'\bnhis\b|self.?rated|自评健康|fair/poor', prior_user) and (context.get('pending') or {}).get('field') != 'comparisons':
            return False
    if context.get('spec') or (context.get('pending') or {}).get('field') == 'comparisons':
        return True
    if re.search(r'\bnhis\b|self.?rated|fair.{0,5}poor|自评健康', q):
        return True
    for message in reversed(messages[-8:]):
        content = str(message.get('content', '')).lower()
        if re.search(r'\bnhis\b|self.?rated|自评健康|fair/poor', content):
            return True
    return False


class ConversationService:
    def __init__(self, agent):
        self.agent = agent
        self.root = Path(agent.root)
        self.directory = self.root / 'conversations'
        self.directory.mkdir(parents=True, exist_ok=True)
        self._locks = _CONVERSATION_LOCKS
        self._locks_guard = _CONVERSATION_LOCKS_GUARD

    def _path(self, conversation_id):
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.fullmatch(conversation_id):
            raise ValueError('Invalid conversation ID.')
        return self.directory / (conversation_id + '.json')

    def _lock(self, conversation_id):
        self._path(conversation_id)
        with self._locks_guard:
            key = (str(self.directory.resolve()), conversation_id)
            return self._locks.setdefault(key, threading.Lock())

    def _file_lock(self, conversation_id, directory_descriptor=None):
        # fcntl is part of the standard library on this Mac/Linux deployment.
        # The in-process lock still protects sessions on other platforms.
        try:
            import fcntl
        except ImportError:
            return None
        path = self._path(conversation_id).with_suffix('.lock')
        descriptor = os.open(str(path) if directory_descriptor is None else path.name,
                             os.O_CREAT | os.O_RDWR | getattr(os,'O_NOFOLLOW',0), 0o600,
                             dir_fd=directory_descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @contextmanager
    def _directory_guard(self):
        """Serialize creation and deletion without waiting for an active writer."""
        with self._locks_guard:
            key = (str(self.directory.resolve()), None)
            lock = self._locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ConversationBusy('Conversation history is being changed. Try again shortly.')
        directory_descriptor = file_descriptor = None
        try:
            directory_descriptor = os.open(str(self.directory), os.O_RDONLY | getattr(os,'O_DIRECTORY',0)
                                            | getattr(os,'O_NOFOLLOW',0))
            try:
                import fcntl
            except ImportError:
                pass
            else:
                file_descriptor = os.open('.directory.lock', os.O_CREAT | os.O_RDWR | getattr(os,'O_NOFOLLOW',0),
                                          0o600, dir_fd=directory_descriptor)
                fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield directory_descriptor
        except BlockingIOError:
            raise ConversationBusy('Conversation history is being changed. Try again shortly.') from None
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if directory_descriptor is not None:
                os.close(directory_descriptor)
            lock.release()

    def _regular_conversation(self, conversation_id, directory_descriptor):
        name = self._path(conversation_id).name
        try:
            return stat.S_ISREG(os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            return False

    def delete(self, conversation_id):
        """Delete one saved conversation, preserving statistics and lock inodes."""
        self._path(conversation_id)
        with self._directory_guard() as directory_descriptor:
            lock = self._lock(conversation_id)
            if not lock.acquire(blocking=False):
                raise ConversationBusy('This conversation is processing a message and cannot be deleted yet.')
            file_descriptor = None
            try:
                try:
                    file_descriptor = self._file_lock(conversation_id, directory_descriptor)
                except BlockingIOError:
                    raise ConversationBusy('This conversation is processing a message and cannot be deleted yet.') from None
                deleted = []
                if self._regular_conversation(conversation_id, directory_descriptor):
                    os.unlink(self._path(conversation_id).name, dir_fd=directory_descriptor)
                    deleted.append(conversation_id)
                return {'deleted':deleted, 'deleted_count':len(deleted)}
            finally:
                if file_descriptor is not None:
                    os.close(file_descriptor)
                lock.release()

    def clear(self):
        """Acquire every target lock before removing any saved conversation."""
        with self._directory_guard() as directory_descriptor:
            identifiers = sorted(name[:-5] for name in os.listdir(directory_descriptor)
                                 if re.fullmatch(r'[a-f0-9]{32}\.json', name)
                                 and self._regular_conversation(name[:-5], directory_descriptor))
            held = []
            try:
                for conversation_id in identifiers:
                    lock = self._lock(conversation_id)
                    if not lock.acquire(blocking=False):
                        raise ConversationBusy('A conversation is processing a message. No conversations were cleared.')
                    held.append([lock, None])
                    try:
                        held[-1][1] = self._file_lock(conversation_id, directory_descriptor)
                    except BlockingIOError:
                        raise ConversationBusy('A conversation is processing a message. No conversations were cleared.') from None
                deleted = []
                for conversation_id in identifiers:
                    if self._regular_conversation(conversation_id, directory_descriptor):
                        os.unlink(self._path(conversation_id).name, dir_fd=directory_descriptor)
                        deleted.append(conversation_id)
                return {'deleted':deleted, 'deleted_count':len(deleted)}
            finally:
                for lock, file_descriptor in reversed(held):
                    if file_descriptor is not None:
                        os.close(file_descriptor)
                    lock.release()

    def _load(self, conversation_id):
        path = self._path(conversation_id)
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError('Conversation not found.')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            raise ValueError('The saved conversation could not be read.')
        if not isinstance(value, dict) or value.get('id') != conversation_id or not isinstance(value.get('messages'), list):
            raise ValueError('The saved conversation has an invalid format.')
        return value

    def _save(self, conversation):
        path = self._path(conversation['id'])
        temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(conversation, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _run(self, run_id):
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            return None
        path = self.root / 'runs' / run_id / 'result.json'
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            return None
        try:
            if path.stat().st_size > 24 * 1024 * 1024:
                return None
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('run_id') != run_id:
                return None
            # The separate run endpoint keeps full measurement evidence. Chat
            # reopening needs estimates, not hundreds of raw model responses.
            value = copy.deepcopy(value)
            if isinstance(value.get('measurement'), dict):
                value['measurement'].pop('calls', None)
            return value
        except (OSError, ValueError, UnicodeError):
            return None

    def _discussed_results(self, conversation):
        """Select only run references belonging to this conversation/workspace."""
        identifiers = []
        for message in conversation.get('messages', []):
            if message.get('role') != 'assistant':
                continue
            source = message.get('source') or {}
            candidates = [message.get('run_id'), source.get('run_id')]
            candidates.extend(message.get('run_ids') or [])
            candidates.extend(source.get('run_ids') or [])
            for identifier in candidates:
                if isinstance(identifier, str) and _RUN_ID.fullmatch(identifier) and identifier not in identifiers:
                    identifiers.append(identifier)
        results = []
        for identifier in identifiers:
            result = self._run(identifier)
            if result and result.get('status') == 'ok':
                results.append(result)
        return results

    def _chart_spec(self, text, conversation, routed):
        """Use a named study grouping or the user's own recent study request."""
        groups = _chart_groups(text)
        context = conversation.get('context') or {}
        clearing = _clear_chart_filters(text)
        base = self._resolve(text, conversation, routed)[0] if (groups and routed.get('status')=='ready') or (clearing and context.get('spec')) else context.get('spec')
        if base is None:
            for message in reversed(conversation.get('messages', [])[:-1]):
                if message.get('role') != 'user':
                    continue
                question = str(message.get('content', ''))
                if (not re.search(r'\bnhis\b|self.?rated|自评健康|健康', question.lower())
                        or not any(re.search(pattern, question.lower()) for pattern in _GROUP_PATTERNS.values())):
                    continue
                candidate = route(question)
                if candidate.get('status') == 'ready':
                    base = candidate['spec']
                    break
        if base is None:
            return None
        spec = _validated(base)
        if clearing:
            if groups:
                spec['group_by'] = groups[0]
            if re.search(r'\b(?:no|without)\s+(?:(?:any|all)\s+)?filters?\b|(?:clear|remove)\s+all\s+filters?|取消(?:所有|全部)筛选|清空筛选|不限',text.lower()):
                spec['filters'] = {}
            return _validated(spec)
        scoped = route('Compare self-rated health by ' + spec['group_by'].replace('_',' ') + '. ' + text)
        if scoped.get('status') == 'ready':
            spec['filters'].update(copy.deepcopy(scoped['spec'].get('filters', {})))
        return _validated(spec)

    def _execute_analysis(self, spec, question, conversation, current, emit, *, measure_model=False, repeats=5, prompt_versions=3):
        """Run one registered specification, preserving successful follow-up scope."""
        context = conversation.setdefault('context', {})
        emit({'stage':'understand','label':'Analysis specification confirmed','status':'complete','detail':_scope_label(spec),
              'facts':{'group_by':spec['group_by'],'analysis':spec['analysis'],'filters':spec['filters']}})
        try:
            result = self.agent.analyze_spec(spec,question,measure_model=measure_model,repeats=repeats,
                                             prompt_versions=prompt_versions,on_progress=emit)
            if not isinstance(result, dict) or result.get('status') not in ('ok','blocked','clarify'):
                raise ValueError('Unexpected analysis response')
        except Exception:
            emit({'stage':'analysis','label':'Statistical execution did not finish','status':'failed',
                  'detail':'The analysis service failed. The unsuccessful specification was not made the basis for later follow-ups.'})
            return None, 'The analysis did not finish and no new statistical result was published. The previous successful scope is still saved.', 'blocked', None
        status = result.get('status', 'blocked')
        answer = analysis_reply(result,spec,previous=current if current and current.get('status')=='ok' else None,question=question)
        source = result.get('run_id')
        if not isinstance(source,str) or not _RUN_ID.fullmatch(source):
            source = None
        if source:
            context['last_attempt_run_id'] = source
        if status == 'ok':
            old_run = conversation.get('last_run_id')
            if old_run and old_run != source:
                context['previous_run_id'] = old_run
            context['spec'] = copy.deepcopy(spec)
            if source:
                conversation['last_run_id'] = source
        return result, answer, status, source

    @staticmethod
    def _chart_answer(charts, *, omitted=0, calculated=False):
        if not charts:
            return 'The saved results do not contain estimates that passed the checks needed for this chart. I have not filled in the missing values.'
        intro = ('Here is the chart from the verified survey calculation.' if calculated and len(charts)==1 else
                 'Here are the charts from the verified survey calculations.' if calculated else
                 'Here is the chart from the result we discussed.' if len(charts)==1 else
                 'Here are the results we discussed, shown as separate charts so their populations stay clear.')
        lines = [intro]
        if any(chart.get('kind') == 'proportion' for chart in charts):
            lines.append('The markers show the weighted share reporting fair or poor health; the horizontal lines show the 95% confidence intervals.')
        if any(chart.get('kind') == 'gradient' for chart in charts):
            lines.append('The difference charts use percentage points, with zero marking no difference from the reference group.')
        if any(chart.get('kind') == 'regression' for chart in charts):
            lines.append('The forest plots show adjusted odds ratios and their 95% confidence intervals; one marks no association in the fitted model.')
        if not calculated:
            lines.append('I used the saved estimates, so no survey calculations or independent model measurements were rerun.')
        if omitted:
            lines.append('This view includes the most recent {} results; {} older results remain in the conversation records.'.format(len(charts),omitted))
        lines.append('You can download each chart or open its source record below. These comparisons describe associations, not causal effects.')
        return '\n\n'.join(lines)

    def _public(self, conversation):
        value = copy.deepcopy(conversation)
        latest = (value.get('context') or {}).get('last_attempt_run_id') or value.get('last_run_id')
        value['latest_result'] = self._run(latest)
        return value

    def create(self):
        stamp = _now()
        value = {'id': uuid.uuid4().hex, 'title': 'New chat', 'created_at': stamp,
                 'updated_at': stamp, 'messages': [], 'context': {}, 'last_run_id': None}
        with self._directory_guard():
            self._save(value)
        return self._public(value)

    def list(self):
        values = []
        for path in self.directory.glob('*.json'):
            if not _CONVERSATION_ID.fullmatch(path.stem):
                continue
            try:
                value = self._load(path.stem)
            except (ValueError, FileNotFoundError):
                continue
            values.append({k: value.get(k) for k in ('id', 'title', 'created_at', 'updated_at', 'last_run_id')})
            values[-1]['message_count'] = len(value['messages'])
        return sorted(values, key=lambda item: item.get('updated_at') or '', reverse=True)

    def get(self, conversation_id):
        return self._public(self._load(conversation_id))

    def _explain(self, text, current, previous):
        detailed = build_explanation(text, current, previous)
        if detailed is not None:
            return detailed
        q = text.lower()
        if re.search(r'自评健康.{0,8}(?:是什么|什么意思|含义)|什么是自评健康|what (?:is|does).{0,20}self.rated health|self.rated health.{0,20}mean', q):
            return ('Self-rated health is the respondent’s assessment of their overall health, from excellent '
                    'through very good, good, fair, and poor. The registered outcome combines fair and poor '
                    '(PHSTAT_A=4 or 5) and compares them with the other valid responses. Refused, unknown, '
                    'and unascertained responses are excluded. This is a population measure, not a clinical diagnosis.'), 'method'
        if re.search(r'method|methodology|how.*calculat|怎么算|怎么计算|计算方法|方法', q):
            return _METHOD, 'method'
        if not current:
            return ('There is no published survey result in this conversation yet. '
                    'I can explain the methods or run a specific comparison first.'), 'clarification'
        if current.get('status') != 'ok':
            return ('The most recent analysis did not publish a usable result. '
                    'The saved record explains where it stopped; an earlier result is not a replacement for this attempt.'), 'explanation'
        survey = current.get('survey') or {}
        if re.search(r'偏差最大|差距最大|最大偏差|largest.*(?:bias|gap)|most.*bias', q):
            rows = [row for row in current.get('comparison', []) if _finite(row.get('gap_pp'))]
            if not rows:
                return ('There is no valid model–survey comparison in this result, so I cannot rank model bias. '
                        'The optional blind measurement is separate from the survey calculation.'), 'explanation'
            row = max(rows, key=lambda item: abs(item['gap_pp']))
            return ('The largest absolute model–survey gap is for {}: model minus survey is {:+.2f} percentage points. '
                    'This describes the completed model measurements, not a difference between populations.').format(
                        category_name(row.get('group_id'), row.get('label')), row['gap_pp']), 'explanation'
        if re.search(r'和刚才|与刚才|上一次相比|跟之前|前后比较|compare.*(?:previous|before)|compared.*last', q):
            if not previous:
                return 'There is only one published result to compare. Run another population or grouping first.', 'clarification'
            before = {row['group_id']:row for row in previous.get('survey', {}).get('rows', [])
                      if row.get('status') == 'ok' and _finite(row.get('estimate'))}
            rows = [row for row in survey.get('rows', []) if row.get('status') == 'ok'
                    and row.get('group_id') in before and _finite(row.get('estimate'))]
            if not rows:
                return 'The two results have no directly corresponding published groups, so I cannot subtract them.', 'clarification'
            lines = ['These are changes in point estimates (current minus previous):']
            lines.extend('- {}: {:+.2f} percentage points.'.format(
                category_name(row.get('group_id'), row.get('label')),
                100*(row['estimate']-before[row['group_id']]['estimate'])) for row in rows[:8])
            lines.append('The covariance and confidence interval of the change have not been calculated; this is not a significance test.')
            return '\n\n'.join(lines), 'explanation'
        if re.search(r'最高|最低|highest|lowest', q):
            rows = [row for row in survey.get('rows', []) if row.get('status') == 'ok' and _finite(row.get('estimate'))]
            if not rows:
                return 'There are no published group proportions to rank. Regression odds ratios are not population proportions.', 'clarification'
            chosen = []
            if re.search(r'最高|highest', q): chosen.append(('highest', max(rows, key=lambda r:r['estimate'])))
            if re.search(r'最低|lowest', q): chosen.append(('lowest', min(rows, key=lambda r:r['estimate'])))
            text = ' '.join('The {} published estimate is {}: {:.2f}%.'.format(
                direction, category_name(row.get('group_id'), row.get('label')), 100*row['estimate']) for direction,row in chosen)
            return text + ' Suppressed groups do not enter this ranking.', 'explanation'
        if re.search(r'为什么|为何|why', q):
            return ('The survey describes population differences, but this cross-sectional result does not identify their causes. '
                    'We can inspect sample selection, uncertainty, or the registered adjusted model next.'), 'explanation'
        return analysis_reply(current, current.get('spec') or _DEFAULT_SPEC, question=text), 'explanation'

    def _resolve(self, text, conversation, routed):
        q = text.lower()
        for _ in range(4):
            cleaned=re.sub(r'^(?:请你|请|帮我|麻烦你|麻烦|能不能|可以帮我|可以)\s*','',q)
            if cleaned==q:break
            q=cleaned
        context = conversation.get('context') or {}
        existing = context.get('spec')
        pending = context.get('pending')
        if pending and pending.get('field') == 'income' and routed.get('status') == 'clarify':
            # A numeric-only reply is meaningful after an explicit income-cut
            # clarification, but never before it and never for arbitrary text.
            if re.fullmatch(r'(?:低于|少于|below)\s*100\s*%[。.!！]?|(?:100\s*[-–—至到~]\s*199|200\s*[-–—至到~]\s*399)\s*%[。.!！]?|400\s*%\s*(?:及以上|以上)[。.!！]?', q):
                routed = route('收入为贫困线' + text)
        if pending and pending.get('field') == 'income' and routed.get('status') == 'ready':
            selected = routed.get('spec', {}).get('filters', {}).get('income')
            if selected:
                spec = copy.deepcopy(pending['spec'])
                spec.setdefault('filters', {})['income'] = selected
                return _validated(spec), None
        if re.search(r'低收入|高收入|low income|high income',q) and routed.get('status') == 'clarify':
            base = copy.deepcopy(existing or _DEFAULT_SPEC)
            if not existing:
                reduced = re.sub(r'低收入|高收入|low income|high income', '', q)
                maybe = route(reduced)
                if maybe.get('status') == 'ready':
                    base = maybe['spec']
            return None, {'field':'income','spec':base,'question':text}
        followup = bool(re.search(r'^(?:那|那么|再|换|改|按|切换|只看|仅看|筛选|取消|去掉|回到|恢复|继续|不再|不限|清空|what about|and |now |only |just |switch |change |restrict |filter |remove |clear |go back|back to|for |among )', q))
        if re.fullmatch(r'(?:女性|男性|female|male|women|men|未完成高中|高中以下|本科及以上)(?:呢|的比例|的情况|怎么样|如何|[？?。.!！\s])*',q):
            followup=True
        if not existing:
            if followup and not re.search(r'^按.+(?:比较|分析|健康|比例|回归|梯度)', q):
                return None, None
            return (routed.get('spec') if routed.get('status') == 'ready' else None), None
        if not followup:
            return (routed.get('spec') if routed.get('status') == 'ready' else None), None
        # A boundary clarification (e.g. an unregistered age or covariate) must
        # never become a contextual request just because it begins with "那".
        if routed.get('status') == 'clarify' and not re.search(r'取消.{0,5}(?:筛选|限定)|不限|回到总体|恢复总体|清空筛选|去掉.{0,5}筛选|(?:remove|clear).{0,20}filters?|(?:go )?back to (?:the )?(?:overall|full|all)', q):
            return None, None
        spec = copy.deepcopy(existing)
        filters = spec.setdefault('filters', {})
        if re.search(r'回到总体|恢复总体|回到全体|全体人群|总体人群|(?:go )?back to (?:the )?(?:overall|full population|all adults)', q):
            spec.update(group_by='overall',analysis='proportion',filters={})
            return _validated(spec), None
        if re.search(r'清空筛选|取消所有筛选|取消全部筛选|不再筛选|(?:clear|remove) (?:all |the )?filters?', q):
            filters.clear()
            return _validated(spec), None
        cleared = False
        for variable, name in [('sex','性别'),('education','教育'),('income','收入'),('race','族裔|种族'),('age','年龄')]:
            if re.search(r'(?:取消|去掉|不再).{0,4}(?:' + name + r').{0,4}(?:筛选|限定)|不限(?:' + name + r')|(?:remove|clear).{0,10}' + variable + r'.{0,10}filter', q):
                filters.pop(variable, None)
                cleared = True
        if cleared:
            return _validated(spec), None
        if routed.get('status') != 'ready':
            return None, None
        incoming = routed['spec']
        group_change = bool(re.search(r'(?:按|改成|换成|换为|改为|改按|换按|by\s|switch\s|change\s|group by\s)', q))
        new_group = None
        if group_change:
            for group, pattern in _GROUP_PATTERNS.items():
                if re.search(pattern, q):
                    new_group = group
                    break
        if new_group:
            spec['group_by'] = new_group
            for variable in (('education','race') if new_group == 'education_race' else (new_group,)):
                filters.pop(variable, None)
        filters.update(copy.deepcopy(incoming.get('filters', {})))
        if re.search(r'梯度|gradient', q):
            spec['analysis'] = 'gradient'
        elif re.search(r'回归|logistic|regression|调整|控制', q):
            spec['analysis'] = 'regression'
            spec['adjustment'] = incoming.get('adjustment', 'full')
        elif new_group and spec.get('analysis') == 'regression':
            # Switching to a grouping asks for rates; no hidden adjusted-model change.
            spec['analysis'] = 'proportion'
        if not new_group and not incoming.get('filters') and spec == existing:
            return None, None
        return _validated(spec), None

    def send(self, conversation_id, text, measure_model=False, repeats=5, prompt_versions=3, on_event=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('Enter a message of 1–2,000 characters.')
        if not isinstance(measure_model, bool) or type(repeats) is not int or not 1 <= repeats <= 5 or type(prompt_versions) is not int or not 1 <= prompt_versions <= 3:
            raise ValueError('Invalid blind-measurement settings: use 1–5 repetitions and 1–3 prompt versions.')
        lock = self._lock(conversation_id)
        if not lock.acquire(blocking=False):
            raise RuntimeError('This conversation is still processing its previous message. Please wait.')
        file_descriptor = None
        try:
            try:
                file_descriptor = self._file_lock(conversation_id)
            except BlockingIOError:
                raise RuntimeError('This conversation is still processing its previous message. Please wait.')
            conversation = self._load(conversation_id)
            text = _redact(text.strip())
            process = []
            def emit(event):
                if not isinstance(event, dict):
                    return
                event = {key:_safe_facts(event.get(key)) for key in ('stage','label','status','detail','timestamp','facts','elapsed_ms') if key in event}
                event.setdefault('timestamp',_now())
                if event.get('status') not in ('running','complete','blocked','failed','skipped'):
                    return
                process.append(event)
                if on_event:
                    try:
                        on_event(copy.deepcopy(event))
                    except Exception:
                        pass
            emit({'stage':'understand','label':'Read conversation context','status':'running','detail':'Read the saved research context before deciding whether survey work is needed.'})
            user = {'id':uuid.uuid4().hex,'role':'user','content':text,'created_at':_now(),
                    'kind':'user','status':'complete','process':[],'suggestions':[]}
            conversation['messages'].append(user)
            conversation['updated_at'] = _now()
            self._save(conversation)
            routed = route(text)
            result = None
            source_run_id = None
            context = conversation.setdefault('context', {})
            current = self._run(context.get('last_attempt_run_id') or conversation.get('last_run_id'))
            previous = self._run(context.get('previous_run_id'))
            decision = None
            charts = []
            referenced_runs = []
            conversation_only = _conversation_only(text)
            plot_request = None if conversation_only else chart_request(text, has_results=True)
            batch_requested = not conversation_only and _batch_request(text, context, conversation['messages'][:-1])
            chat_turn = False
            chat_reference_clarification = False
            initial_generation = None
            planner = getattr(self.agent, 'decide_reply', None)
            if callable(planner) and '[credential hidden]' not in text and not plot_request and not batch_requested and (conversation_only or not _uses_research_flow(text, context)):
                emit({'stage':'conversation','label':'Understand the conversation','status':'running',
                      'detail':'Use this conversation’s recent messages to distinguish chat from survey work.'})
                try:
                    ordinary_reference = _chat_reference(text, context)
                    decision = planner(text, conversation['messages'][:-1],
                                       {'spec':None if ordinary_reference else context.get('spec'),
                                        'has_published_result':False if ordinary_reference else bool(current and current.get('status')=='ok')})
                except Exception:
                    decision = {'status':'failed','error_code':'dialogue_service_error',
                                'message':'The conversation service could not complete this reply.'}
                if decision.get('status') != 'ok' or decision.get('mode') == 'chat':
                    chat_turn = True
                    initial_generation = _generation_metadata(decision)
                elif _chat_reference(text, context):
                    chat_turn = True
                    chat_reference_clarification = True
                    initial_generation = {'status':'local', 'message':'Clarify the conversational reference without reusing old survey evidence.'}
                emit({'stage':'conversation','label':'Conversation request understood',
                      'status':'complete' if decision.get('status')=='ok' else 'failed',
                      'detail':'No survey calculation or blind measurement was performed in this step.',
                      'facts':{'mode':decision.get('mode'), **_generation_metadata(decision)}})
            greeting = bool(re.fullmatch(r'(?:hi|hello|hey|你好|您好|嗨|在吗|早上好)[！!。.?？\s]*',text.lower()))
            acknowledgment = bool(re.fullmatch(r'(?:谢谢(?:你)?|多谢|好的|收到|明白了|了解了|thanks|thank you|ok|okay)[！!。.?？\s]*', text.lower()))
            help_question = bool(re.search(r'你能做什么|^怎么用[？?！!。\s]*$|怎么用这个|使用说明|^帮助|^help\b|what can you',text.lower()))
            method_topic = _method_topic(text)
            explanation = method_topic is not None or bool(_EXPLANATION.search(text.lower()))
            analysis_requested = explanation and _analysis_request(text, routed)
            if analysis_requested:
                explanation = False
            if decision and decision.get('status') == 'ok' and not chat_turn:
                explanation = decision.get('mode') == 'explanation'
            explaining_floor = (method_topic == 'sample_rule' and not _rule_override(text)
                                and routed.get('answer','').startswith('Confidence is fixed at'))
            if '[credential hidden]' in text:
                answer, status, kind = 'Please do not send credentials in chat. They have been hidden and were not sent to the model.', 'blocked', 'settings'
                emit({'stage':'scope','label':'Protect credentials','status':'blocked','detail':'Connection credentials are managed on the server.'})
            elif chat_turn:
                kind = 'chat'
                if chat_reference_clarification:
                    answer, status = 'Do you mean the topic we were just discussing, or would you like to return to the earlier survey results?', 'clarify'
                elif decision.get('status') == 'ok' and isinstance(decision.get('reply'), str) and decision['reply'].strip():
                    answer, status = decision['reply'], 'ok'
                else:
                    answer = ('I couldn’t reach the conversation service, so I can’t give you a model reply right now. '
                              'Your messages are saved. Please check the server connection and try again.')
                    status = 'blocked'
                    initial_generation['status'] = 'failed' if decision.get('status') != 'not_configured' else 'not_configured'
            elif routed.get('status') == 'blocked' and not explaining_floor:
                answer, status, kind = routed['answer'], 'blocked', 'blocked'
                emit({'stage':'scope','label':'Check the registered research scope','status':'blocked','detail':answer})
            elif ((plot_request or batch_requested) and routed.get('status') == 'clarify' and routed.get('answer')
                  and not routed['answer'].startswith(('For survey calculation,','Choose one grouping dimension,'))):
                answer, status, kind = routed['answer'], 'clarify', 'clarification'
                emit({'stage':'scope','label':'Confirm the requested chart population','status':'blocked','detail':answer})
            elif plot_request and plot_request.get('scope') == 'latest' and current and current.get('status') != 'ok':
                answer = 'The most recent analysis did not publish usable estimates, so there is no chart for that attempt. I can show the earlier published results if you ask for all the results we discussed.'
                status, kind = 'blocked', 'visualization'
                result, source_run_id = current, current.get('run_id')
            elif plot_request:
                kind = 'analysis'
                emit({'stage':'visualize','label':'Prepare charts','status':'running',
                      'detail':'Read released estimates referenced by this conversation, keeping each population separate.'})
                selected = self._discussed_results(conversation)
                named = _chart_groups(text)
                if 'education_race' in named:
                    named = ['education_race']
                if named:
                    selected = [item for item in selected if item.get('spec', {}).get('group_by') in named]
                target_spec = self._chart_spec(text, conversation, routed)
                scoped_route = route('Compare self-rated health by ' + ((target_spec or {}).get('group_by') or 'education').replace('_',' ') + '. ' + text)
                requested_filters = (scoped_route.get('spec') or {}).get('filters', {})
                if requested_filters and target_spec:
                    requested_filters = target_spec.get('filters', {})
                if requested_filters or _clear_chart_filters(text):
                    def matching_filters(item):
                        saved = item.get('spec', {}).get('filters') or {}
                        normalize = lambda filters:{key:sorted(value if isinstance(value,list) else [value]) for key,value in filters.items()}
                        return normalize(saved) == normalize(target_spec.get('filters', {}) if target_spec else requested_filters)
                    selected = [item for item in selected if matching_filters(item)]
                requested_kind = plot_request.get('kind', 'auto')
                if requested_kind != 'auto':
                    selected = [item for item in selected if item.get('spec', {}).get('analysis', 'proportion') == requested_kind]
                if re.search(r'\bwomen\b|\bfemale\b|女性', text.lower()):
                    selected = [item for item in selected if (item.get('spec', {}).get('filters') or {}).get('sex') in ('female', ['female'])]
                elif re.search(r'\bmen\b|\bmale\b|男性', text.lower()):
                    selected = [item for item in selected if (item.get('spec', {}).get('filters') or {}).get('sex') in ('male', ['male'])]
                if plot_request.get('scope') == 'latest':
                    selected = selected[-1:]
                omitted = max(0, len(selected)-12)
                selected = selected[-12:]
                calculated = False
                if not selected:
                    spec = target_spec
                    if spec is not None:
                        if requested_kind in ('gradient', 'regression'):
                            spec['analysis'] = requested_kind
                            spec = _validated(spec)
                        result, answer, status, source_run_id = self._execute_analysis(spec,text,conversation,current,emit)
                        if result and status == 'ok':
                            selected = [result]
                            calculated = True
                        elif result:
                            kind = 'analysis'
                    else:
                        answer = ('I don’t have a published survey result to plot yet. I can start with education and self-rated health, '
                                  'or compare all the registered population groups if you want an overview.')
                        status, kind = 'clarify', 'clarification'
                        context['pending'] = {'field':'comparisons','spec':copy.deepcopy(_DEFAULT_SPEC)}
                if selected:
                    charts = make_charts(selected,kind=requested_kind,style=plot_request.get('style','point'))
                    referenced_runs = list(dict.fromkeys(item['source_run_id'] for item in charts))
                    answer = self._chart_answer(charts,omitted=omitted,calculated=calculated)
                    status, kind = ('ok','visualization') if charts else ('clarify','clarification')
                    result, source_run_id = None, None
                if charts:
                    initial_generation = {'status':'local','type':'visualization',
                                          'message':'Charts were constructed directly from released survey estimates.'}
                emit({'stage':'visualize','label':'Charts ready' if charts else 'No chart was released',
                      'status':'complete' if charts else 'blocked',
                      'detail':'The chart values and confidence intervals come from the saved statistical records.' if charts else str(answer),
                      'facts':{'chart_count':len(charts),'source_run_ids':referenced_runs,'reused_results':not calculated}})
            elif batch_requested:
                base = _validated(context.get('spec') or (context.get('pending') or {}).get('spec') or _DEFAULT_SPEC)
                scope_route = route('Compare self-rated health by ' + base['group_by'].replace('_',' ') + '. ' + text)
                base['filters'].update(copy.deepcopy((scope_route.get('spec') or {}).get('filters', {})))
                context.pop('pending',None)
                completed = []
                sections = ['Here is an overview across education, income, sex, race/ethnicity, and age. I kept the current population filters and used the registered self-rated-health outcome.']
                for group in ('education','income','sex','race','age'):
                    spec = _validated(dict(base, group_by=group, analysis='proportion'))
                    item, draft, item_status, identifier = self._execute_analysis(spec,text,conversation,current,emit)
                    sections.append('### ' + GROUPS[group] + '\n\n' + str(draft).split('\n\n')[0])
                    if identifier:
                        referenced_runs.append(identifier)
                    if item_status == 'ok' and item:
                        completed.append(item)
                charts = make_charts(completed)
                if completed:
                    status, kind = 'ok', 'analysis'
                    if len(completed) != 5:
                        sections.append('Some comparisons did not publish a usable result; their notices are listed above. Only released estimates appear in the charts.')
                    sections.append('The charts show survey-weighted proportions and 95% confidence intervals. Income estimates pool all ten official imputations. These are descriptive associations, not causal effects. Open a chart’s source record for exact values and methods.')
                else:
                    status, kind = 'blocked', 'analysis'
                    sections[0] = 'I attempted all five registered comparisons, but no statistical estimates passed the release checks.'
                answer = '\n\n'.join(sections)
                result, source_run_id = None, None
            elif greeting or help_question:
                answer, status, kind = _HELP, 'ok', 'greeting' if greeting else 'help'
                emit({'stage':'understand','label':'Identify the request','status':'complete','detail':'Help does not run survey calculations or blind measurement. Conversation generation is recorded separately.'})
            elif '[credential hidden]' in text:
                answer, status, kind = 'Please do not send credentials in chat. They have been hidden and were not sent to the model.', 'blocked', 'settings'
                emit({'stage':'scope','label':'Protect credentials','status':'blocked','detail':'The credential was not passed to the analysis engine or model service.'})
            elif acknowledgment:
                if context.get('pending'):
                    answer = 'The income cutoff is still pending. Once you choose it, I will keep the existing grouping and other filters. Nothing has been recalculated.'
                elif conversation.get('last_run_id'):
                    answer = 'You’re welcome. The analysis scope and results are saved, so we can continue the discussion or change a filter.'
                else:
                    answer = 'Of course. What would you like to talk about or work on?'
                status, kind = 'ok', 'acknowledgment'
                emit({'stage':'reply','label':'Keep conversation context','status':'complete','detail':'An acknowledgment does not start survey calculations or blind measurement.'})
            elif explanation:
                last_assistant = next((item for item in reversed(conversation['messages'][:-1]) if item.get('role')=='assistant'), {})
                chart_sources = list(dict.fromkeys(item.get('source_run_id') for item in last_assistant.get('charts',[]) if isinstance(item,dict)))
                explain_multiple = (len(chart_sources)>1 and re.search(r'\b(?:these|all|charts?|plots?|graphs?)\b|这些|所有|全部|这几',text.lower())
                                    and not (routed.get('spec') or {}).get('filters')
                                    and not any(re.search(pattern,text.lower()) for pattern in _GROUP_PATTERNS.values()))
                incompatible = _explanation_target_mismatch(text, current)
                if current and current.get('status') == 'ok' and routed.get('status') == 'ready':
                    requested = routed['spec'].get('filters', {})
                    current_filters = current.get('spec', {}).get('filters', {})
                    observed_ids = {part for row in current.get('survey',{}).get('rows',[]) for part in str(row.get('group_id','')).split('|')}
                    for variable, value in requested.items():
                        values = value if isinstance(value,list) else [value]
                        have = current_filters.get(variable,[])
                        have = have if isinstance(have,list) else [have]
                        if set(values) != set(have) and not set(values).issubset(observed_ids):
                            incompatible = True
                if explain_multiple:
                    parts = ['Here is how to read the charts together. Each panel keeps its own grouping and population.']
                    for identifier in chart_sources:
                        item = self._run(identifier)
                        if item and item.get('status') == 'ok':
                            interpretation, _ = self._explain(text,item,None)
                            parts.append('### ' + _scope_label(item.get('spec') or {}) + '\n\n' + interpretation)
                            referenced_runs.append(identifier)
                    answer, kind = '\n\n'.join(parts), 'explanation'
                elif incompatible:
                    answer, kind = 'This explanation refers to a different population, grouping, or analysis from the saved result. Run the requested analysis first, such as an income gradient or a male-only comparison, so I can explain the matching evidence.', 'clarification'
                else:
                    answer, kind = self._explain(text,current,previous)
                status = 'clarify' if kind == 'clarification' else 'ok'
                source_run_id = current.get('run_id') if current and not explain_multiple else None
                if source_run_id:
                    result = current
                emit({'stage':'explain','label':'Explain the saved result','status':'complete' if status == 'ok' else 'blocked',
                      'detail':'Reuse the saved survey results and methods without recalculating or running a blind measurement.',
                      'facts':{'source_run_id':source_run_id,'reused_result':bool(current)}})
            else:
                try:
                    spec, pending = self._resolve(text,conversation,routed)
                    if spec is not None:
                        spec = _validated(spec)
                except (ValueError, TypeError, KeyError) as error:
                    spec, pending = None, None
                    routed = {'status':'clarify','answer':'This follow-up does not match the registered grouping or model rules: ' + str(error)}
                if spec is None:
                    if pending:
                        context['pending'] = pending
                        answer = 'Please choose the income-to-poverty category: below 100%, 100–199%, 200–399%, or 400% and above. I will keep the existing grouping and other filters.'
                    else:
                        answer = routed.get('answer') or 'Specify the research change, for example “women only,” “switch to income,” or “remove the sex filter.”'
                        if not context.get('pending') and decision and decision.get('mode') == 'analysis' and not context.get('spec'):
                            context['pending'] = {'field':'comparisons','spec':copy.deepcopy(_DEFAULT_SPEC)}
                            answer = ('I can start with education and self-rated health, or give you an overview across education, income, sex, race/ethnicity, and age. '
                                      'Say “compare all groups” if you want the full overview.')
                        if routed.get('status') == 'ready':
                            answer = 'There is no matching research context to continue, or the change is unclear. Ask a complete research question, such as “Compare self-rated health by education.”'
                    status, kind = 'clarify', 'clarification'
                    emit({'stage':'scope','label':'Confirm the analysis scope','status':'blocked','detail':answer})
                else:
                    context.pop('pending',None)
                    result, answer, status, source_run_id = self._execute_analysis(spec,text,conversation,current,emit,
                        measure_model=measure_model,repeats=repeats,prompt_versions=prompt_versions)
                    kind = 'analysis'
                    # Engine events may be returned even when no callback was emitted.
                    if result and not any(event.get('stage') not in ('understand','scope') for event in process):
                        for event in result.get('process') or []:
                            emit(event)
            if conversation['title'] in ('New chat', '\u65b0\u5bf9\u8bdd') and not greeting and not help_question:
                conversation['title'] = text[:32] + ('…' if len(text)>32 else '')
            generation = initial_generation or {'status':'local', 'message':'This is a registered-scope or connection notice.'}
            if status == 'ok' and kind in ('analysis','explanation','method','greeting','help','acknowledgment'):
                compose = getattr(self.agent, 'compose_reply', None)
                if callable(compose):
                    emit({'stage':'reply_api','label':'Prepare the research explanation','status':'running',
                          'detail':'Check the server connection and send only the question and published aggregate evidence.'})
                    try:
                        generated = compose(text, str(answer))
                    except Exception:
                        generated = {'status':'failed','error_code':'dialogue_service_error',
                                     'message':'The conversation service did not complete this request.'}
                    allowed = ('status','provider','model','response_id','request_id','usage','elapsed_ms',
                               'http_status','error_code','message','api_status')
                    generation = {key:copy.deepcopy(generated[key]) for key in allowed if key in generated}
                    generation.setdefault('status', 'failed')
                    if generated.get('status') == 'ok' and isinstance(generated.get('content'), str) and generated['content'].strip():
                        answer = generated['content']
                        emit({'stage':'reply_api','label':'Research explanation returned','status':'complete',
                              'detail':'The response passed the evidence-block and numeric checks.','facts':generation})
                    else:
                        if generation['status'] == 'ok':
                            generation.update(status='failed', error_code='empty_answer')
                        configured = generated.get('status') != 'not_configured'
                        note = ('**The model reply was not completed. The verified statistical output is shown below.**' if configured else
                                '**The conversation service is not connected. The verified statistical output is shown below.**')
                        answer = note + '\n\n' + str(answer)
                        emit({'stage':'reply_api','label':'No model explanation was published',
                              'status':'failed' if configured else 'skipped',
                              'detail':generated.get('message') or 'No usable model response was received.','facts':generation})
            suggestions = self._suggestions(kind,conversation)
            emit({'stage':'answer','label':'Reply ready','status':'complete' if status=='ok' else 'blocked',
                  'detail':'The reply and its actual sources are saved with this message.','facts':{'source_run_id':source_run_id}})
            message = {'id':uuid.uuid4().hex,'role':'assistant','content':_redact(str(answer)),
                       'created_at':_now(),'status':status,'kind':kind,'process':process,'suggestions':suggestions,
                       'generation':generation}
            if charts:
                message['charts'] = charts
            if decision and (not chat_turn or chat_reference_clarification):
                message['routing'] = _generation_metadata(decision)
            if referenced_runs:
                message['run_ids'] = referenced_runs
                message['source'] = {'type':'runs','run_ids':referenced_runs}
            elif source_run_id:
                message['run_id'] = source_run_id
                message['source'] = {'type':'run','run_id':source_run_id}
            else:
                message['source'] = {'type':'registered_method' if kind in ('method','help','greeting') else 'conversation'}
            conversation['messages'].append(message)
            if kind == 'chat':
                context['last_turn_mode'] = 'chat'
            elif kind in ('analysis','explanation','method','visualization'):
                context['last_turn_mode'] = 'research'
            conversation['updated_at'] = _now()
            self._save(conversation)
            return {'conversation_id':conversation_id,'conversation':self._public(conversation),
                    'message':copy.deepcopy(message),'result':result}
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            lock.release()

    def _suggestions(self, kind, conversation):
        context = conversation.get('context') or {}
        if context.get('pending',{}).get('field') == 'income':
            return ['Below 100% of poverty', '100–199% of poverty', '400% of poverty and above']
        if context.get('pending',{}).get('field') == 'comparisons':
            return ['Compare all groups', 'Compare self-rated health by education', 'Compare self-rated health by income']
        if kind == 'visualization':
            return ['Show bar charts', 'Explain these results', 'Compare all population groups']
        if conversation.get('last_run_id'):
            filtered = (context.get('spec') or {}).get('filters') or {}
            return ['Plot all results we have discussed', 'How was this calculated?',
                    'Remove the sex filter' if filtered.get('sex') else 'What about women?']
        return list(_SUGGESTIONS)
