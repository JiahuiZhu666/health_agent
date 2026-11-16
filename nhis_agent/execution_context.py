"""Context-local execution identity, checkpoints and non-retrying attempt hooks.

Transport and statistical code can use these hooks without importing a worker
or a conversation store. No network call, configuration read or job dispatch is
performed by this module.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
from dataclasses import dataclass, field
import re
import uuid
import time

from .job_store import SubmissionConflict, fingerprint


class ExecutionCancelled(BaseException):
    pass


class ExecutionDeadline(BaseException):
    pass


class UnknownExternalAttempt(BaseException):
    pass


ExternalOutcomeUnknown = UnknownExternalAttempt


class ReplayAvailable(RuntimeError):
    def __init__(self, result):
        super().__init__('A durable result already exists for this planned external attempt.')
        self.result = copy.deepcopy(result)


_CURRENT = ContextVar('nhis_execution_context', default=None)
_SECRET = re.compile(r'\bsk-[A-Za-z0-9_-]{8,}|\bBearer\s+[^\s,;]+|'
                     r'(?:api[ _-]?key|access[ _-]?token|secret|password|密钥|密码)\s*[:=：]\s*[^\s,;，；。]{4,}', re.I)
_PRIVATE_FIELDS = frozenset(('api_key','access_token','authorization','password','secret','cookie',
                            'set-cookie','headers','request_headers','response_headers'))
_RAW_INPUT_FIELDS = frozenset(('prompt','messages','history','input','raw_request','request_body'))


def safe_value(value, *, metadata=False):
    """Keep exact safe result values, excluding credentials and raw request input."""
    if isinstance(value, dict):
        return {key:safe_value(item,metadata=metadata) for key,item in value.items()
                if str(key).lower() not in _PRIVATE_FIELDS
                and not (metadata and str(key).lower() in _RAW_INPUT_FIELDS)}
    if isinstance(value, (list,tuple)):
        return [safe_value(item,metadata=metadata) for item in value]
    if isinstance(value, str):
        return _SECRET.sub('[credential hidden]',value)
    return value


def _step_key(value):
    if not isinstance(value,str) or not 1 <= len(value) <= 240 or any(ord(char)<32 for char in value):
        raise ValueError('Execution step identifiers must be bounded plain text.')
    return value


def external_identity(metadata):
    """Budget/trace identifiers document the attempt, not its scientific inputs."""
    if not isinstance(metadata,dict):
        raise ValueError('External request metadata must be an object.')
    return {key:value for key,value in metadata.items()
            if key not in ('reservation_id','trace_id','attempt_id','timestamp','started_at')}


@dataclass
class ExecutionContext:
    store: object = None
    workspace_id: str = ''
    turn_id: str = ''
    job_id: str = ''
    trace_id: str = field(default_factory=lambda:uuid.uuid4().hex)
    owner: str = ''
    run_id: str = None
    state_root: object = None
    client_message_id: str = None
    budget: object = None
    deadline: float = None

    @property
    def budget_state_dir(self):
        return self.state_root

    def identifiers(self):
        return {key:getattr(self,key) for key in ('trace_id','workspace_id','turn_id','job_id','run_id') if getattr(self,key)}

    def check_cancelled(self):
        if self.deadline is not None and time.monotonic()>=self.deadline:
            raise ExecutionDeadline('The execution reached its configured wall-clock deadline.')
        if self.store is None:
            return
        value = self.store.lease_status(self.workspace_id,self.job_id)
        if value['cancel_requested']:
            raise ExecutionCancelled('Cancellation was requested; no further execution step was started.')
        if (value['state'] != 'running' or value['lease_owner'] != self.owner
                or value['lease_expires_at'] <= self.store.clock()):
            from .job_store import LeaseLost
            raise LeaseLost('The worker no longer owns this execution lease.')

    def save_step(self,key,result,input_hash=None,kind='local'):
        if self.store is None:
            return
        self.check_cancelled()
        self.store.save_step(self.workspace_id,self.job_id,self.owner,_step_key(key),safe_value(result),input_hash,kind)

    def load_step(self,key,input_hash=None):
        if self.store is None:
            return None
        self.check_cancelled()
        return self.store.load_step(self.workspace_id,self.job_id,_step_key(key),input_hash)

    def link_messages(self, *, user_message_id=None, assistant_message_id=None):
        if self.store is not None:
            self.store.link_messages(self.workspace_id,self.job_id,user_message_id=user_message_id,
                                     assistant_message_id=assistant_message_id)

    def external_cached(self,attempt_key,request_meta=None):
        if self.store is None:
            return None
        self.check_cancelled()
        attempt = self.store.external(self.workspace_id,self.job_id,_step_key(attempt_key))
        if attempt is None:
            return None
        if request_meta is not None and attempt['request_hash'] != fingerprint(external_identity(safe_value(request_meta,metadata=True))):
            raise SubmissionConflict('A saved external attempt has different registered inputs.')
        if attempt['state'] != 'completed':
            raise UnknownExternalAttempt('A prior external request may have executed, but its result is unknown. It was not repeated.')
        return copy.deepcopy(attempt.get('result'))

    def external_start(self,kind,attempt_key,request_meta):
        if self.store is None:
            raise ValueError('A durable execution context is required to record an external attempt.')
        self.check_cancelled()
        key = _step_key(attempt_key)
        metadata = safe_value(request_meta,metadata=True)
        prior = self.store.external(self.workspace_id,self.job_id,key)
        if prior is not None:
            if prior['request_hash'] != fingerprint(external_identity(metadata)):
                raise SubmissionConflict('A planned external attempt changed its registered inputs.')
            if prior['state'] == 'completed':
                raise ReplayAvailable(prior.get('result'))
            raise UnknownExternalAttempt('This planned external request has an unknown result and must not be resubmitted.')
        try:
            return self.store.start_external(self.workspace_id,self.job_id,self.owner,key,_step_key(kind),metadata,
                                              request_hash=fingerprint(external_identity(metadata)))
        except SubmissionConflict:
            # A concurrent transport path used the same key. Never send twice.
            prior = self.store.external(self.workspace_id,self.job_id,key)
            if prior and prior['state'] == 'completed':
                raise ReplayAvailable(prior.get('result')) from None
            raise UnknownExternalAttempt('This planned external request is already recorded and was not repeated.') from None

    def external_finish(self,attempt_id,safe_result):
        if self.store is not None:
            # A late response is recorded even if cancellation was requested.
            self.store.finish_external(self.workspace_id,self.job_id,attempt_id,safe_value(safe_result))

    def external_unknown(self,attempt_id,error_code='external_delivery_unknown'):
        if self.store is not None:
            self.store.finish_external(self.workspace_id,self.job_id,attempt_id,unknown=True,error_code=_step_key(error_code))


def current():
    return _CURRENT.get()


@contextmanager
def bind(context):
    if isinstance(context,dict):
        context = ExecutionContext(**context)
    if not isinstance(context,ExecutionContext):
        raise TypeError('An ExecutionContext is required.')
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)
