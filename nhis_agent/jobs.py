"""Durable jobs: fenced leases, idempotent admission and explicit safe recovery."""
import copy
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
import os

from .execution_context import ExecutionContext, ExecutionCancelled, ExecutionDeadline, UnknownExternalAttempt, bind, safe_value
from .job_store import JobStore, SubmissionConflict, ActiveJobConflict, LeaseLost, fingerprint, timestamp, workspace_identity


class ConversationBusy(ValueError):
    pass


def _state_status(state):
    # Existing clients poll running and consume complete.response.
    return 'running' if state in ('queued','running') else 'complete' if state in ('succeeded','blocked') else 'failed'


class ChatJobs:
    def __init__(self, service, *, capacity=None, state_dir=None, store=None,
                 workspace_id=None, lease_seconds=30, heartbeat_interval=5, reconcile=True):
        self.service = service
        self._lock = threading.RLock()
        self._threads = {}
        self._stopping = threading.Event()
        seconds=os.environ.get('NHIS_JOB_WALL_SECONDS','900')
        if not seconds.isascii() or not seconds.isdigit() or not 1<=int(seconds)<=3600:
            raise ValueError('NHIS_JOB_WALL_SECONDS must be an integer from 1 to 3600.')
        self.wall_seconds=int(seconds)
        self._capacity = capacity or threading.BoundedSemaphore(4)
        self._temporary = None
        root = getattr(service,'root',None) or getattr(getattr(service,'agent',None),'root',None)
        if root is None:
            self._temporary = tempfile.TemporaryDirectory(prefix='nhis-jobs-test-service-')
            root = Path(self._temporary.name)
        self.workspace_root = Path(root).expanduser().resolve()
        self.store = store or JobStore(state_dir or self.workspace_root)
        self.workspace_id = workspace_id or workspace_identity(self.workspace_root)
        self.state_root = self.store.state_root
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_interval = min(float(heartbeat_interval),self.lease_seconds/3)
        if self.lease_seconds <= 0 or self.heartbeat_interval <= 0:
            raise ValueError('Worker lease and heartbeat durations must be positive.')
        if reconcile:self.store.reconcile(self.workspace_id)

    def __del__(self):
        temporary = getattr(self,'_temporary',None)
        if temporary is not None:
            try:
                temporary.cleanup()
            except Exception:
                pass

    @staticmethod
    def _request(text,measure_model,repeats,prompt_versions):
        if not isinstance(text,str) or not 1 <= len(text.strip()) <= 2000:
            raise ValueError('Enter a message of 1–2,000 characters.')
        if type(measure_model) is not bool or type(repeats) is not int or not 1 <= repeats <= 5 or type(prompt_versions) is not int or not 1 <= prompt_versions <= 3:
            raise ValueError('The model measurement option must be Boolean, with 1–5 repetitions and 1–3 prompt versions.')
        return {'message':text.strip(),'measure_model':measure_model,'repeats':repeats,'prompt_versions':prompt_versions}

    @staticmethod
    def _client_id(value):
        if value is None:
            return uuid.uuid4().hex
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}',value):
            raise ValueError('client_message_id must be an opaque identifier of 8–128 safe characters.')
        return value

    def _public(self,row,*,idempotent=False):
        state = row['state']
        unknown = bool(row.get('has_unknown_attempts')) or any(item['state'] in ('started','unknown') for item in row.get('external_attempts',[]))
        value = {key:copy.deepcopy(row[key]) for key in ('job_id','turn_id','conversation_id','trace_id','client_message_id') if key in row}
        value.update(id=row['job_id'],status=_state_status(state),execution_state=state,
                     created_at=timestamp(row['created_at']),updated_at=timestamp(row['updated_at']),
                     cancel_requested=bool(row['cancel_requested']),recovery_required=bool(row['recovery_required']),
                     can_resume=bool(row['recovery_required'] and state in ('queued','unknown') and not unknown and not row['cancel_requested']),
                     idempotent=idempotent)
        if row.get('cancel_requested_at') is not None:
            value['cancel_requested_at'] = timestamp(row['cancel_requested_at'])
        if row['cancel_requested']:
            value['cancellation_effect'] = 'cancelled' if state == 'cancelled' else 'too_late' if state in ('succeeded','blocked') else 'requested'
        if row.get('error_message'):
            value['error'] = row['error_message']
        if row.get('error_code'):
            value['error_code'] = row['error_code']
        return value

    def lookup_submission(self,conversation_id,text,measure_model=False,repeats=5,prompt_versions=3,*,client_message_id,connection=None):
        request = self._request(text,measure_model,repeats,prompt_versions)
        client = self._client_id(client_message_id)
        with self.store.transaction(connection) as transaction:
            self.service.get(conversation_id)
            row = self.store.lookup(self.workspace_id,conversation_id,client,fingerprint(request),connection=transaction)
        if row is None:
            return None
        row['client_message_id'] = client
        return self._public(row,idempotent=True)

    def has_active(self,*,connection=None):
        # Called while the workspace LRU lock is held. Read-only state avoids
        # taking SQLite's admission writer in the reverse lock order.
        return any(row['state']=='running' or not row['recovery_required']
                   for row in self.store.active(self.workspace_id,connection=connection))

    def active_job(self,conversation_id):
        self.store.reconcile(self.workspace_id)
        rows = self.store.active(self.workspace_id,conversation_id)
        return rows[-1]['job_id'] if rows else None

    def latest_job(self,conversation_id):
        identifier = self.store.latest(self.workspace_id,conversation_id)
        return self.get(identifier) if identifier else None

    def submit(self,conversation_id,text,measure_model=False,repeats=5,prompt_versions=3,*,
               admission=None,client_message_id=None,connection=None):
        if self._stopping.is_set():
            raise ConversationBusy('The service is shutting down; this message was not accepted.')
        request = self._request(text,measure_model,repeats,prompt_versions)
        client = self._client_id(client_message_id)
        if connection is not None and admission is None:
            raise ValueError('Transactional job admission requires a post-commit admission future.')
        # SQLite serializes admission/deletion; never wait for its writer
        # while holding the thread-map lock (public quota already owns it).
        reserved = False
        try:
            with self.store.transaction(connection) as transaction:
                self.service.get(conversation_id)
                original = self.store.lookup(self.workspace_id,conversation_id,client,fingerprint(request),connection=transaction)
                if original is not None:
                    original['client_message_id'] = client
                    return self._public(original,idempotent=True)
                if not self._capacity.acquire(blocking=False):
                    raise ConversationBusy('The service is processing other conversations. Try again shortly.')
                reserved = True
                row,created = self.store.create(self.workspace_id,conversation_id,client,fingerprint(request),
                                               safe_value(request),connection=transaction)
        except ActiveJobConflict as error:
            if reserved:self._capacity.release()
            raise ConversationBusy(str(error)) from None
        except BaseException:
            if reserved:self._capacity.release()
            raise
        row['client_message_id'] = client
        if not created:
            self._capacity.release()
            return self._public(row,idempotent=True)
        try:
            self._dispatch(row['job_id'],admission=admission,capacity_reserved=True)
        except BaseException:
            self._capacity.release()
            if connection is None:
                self.store.fail_queued(self.workspace_id,row['job_id'],code='worker_start_failed')
            raise
        return self._public(row)

    def _dispatch(self,job_id,*,admission=None,capacity_reserved=False):
        if not capacity_reserved and not self._capacity.acquire(blocking=False):
            raise ConversationBusy('The service is processing other conversations. Try again shortly.')
        def execute():
            owner = uuid.uuid4().hex
            stopped = threading.Event()
            pulse = None
            claimed = False
            try:
                if admission is not None and not admission.result():
                    try:
                        self.store.fail_queued(self.workspace_id,job_id)
                    except FileNotFoundError:
                        pass
                    return
                if self._stopping.is_set():
                    return
                claimed = self.store.claim(self.workspace_id,job_id,owner,lease_seconds=self.lease_seconds)
                if not claimed:
                    return
                def heartbeat():
                    while not stopped.wait(self.heartbeat_interval):
                        try:
                            if not self.store.heartbeat(self.workspace_id,job_id,owner,lease_seconds=self.lease_seconds):
                                return
                        except Exception:
                            return
                pulse = threading.Thread(target=heartbeat,daemon=True,name='nhis-heartbeat-'+job_id[:8])
                pulse.start()
                row = self.store.get(self.workspace_id,job_id)
                request = self.store.request(self.workspace_id,job_id)
                context = ExecutionContext(store=self.store,workspace_id=self.workspace_id,turn_id=row['turn_id'],
                    job_id=job_id,trace_id=row['trace_id'],owner=owner,state_root=self.state_root,
                    client_message_id=row['client_message_id'])
                context.deadline=time.monotonic()+max(0,row['started_at']+self.wall_seconds-self.store.clock())
                def observe(event):
                    if isinstance(event,dict):
                        self.store.event(self.workspace_id,job_id,safe_value(event))
                with bind(context):
                    replay=getattr(self.service,'completed_turn',None)
                    response=replay(row['conversation_id'],row['turn_id']) if callable(replay) else None
                    if response is not None:
                        context.link_messages(assistant_message_id=response['message']['id'])
                    else:
                        context.check_cancelled()
                        response = self.service.send(row['conversation_id'],request['message'],
                            measure_model=request['measure_model'],repeats=request['repeats'],
                            prompt_versions=request['prompt_versions'],on_event=observe)
                if not isinstance(response,dict):
                    raise ValueError('The conversation executor returned no response.')
                compact = self._compact_response(response)
                if self.store.has_unknown(self.workspace_id,job_id):
                    self.store.finish(self.workspace_id,job_id,owner,'unknown',response=compact,
                        error_code='external_delivery_unknown',error='An external request may have completed, but its result is uncertain. It was not automatically repeated.')
                else:
                    message_status = (response.get('message') or {}).get('status')
                    state = 'blocked' if message_status == 'blocked' else 'failed' if message_status == 'failed' else 'succeeded'
                    self.store.finish(self.workspace_id,job_id,owner,state,response=compact)
            except ExecutionDeadline:
                if claimed:
                    unknown=self.store.has_unknown(self.workspace_id,job_id)
                    self.store.finish(self.workspace_id,job_id,owner,'unknown' if unknown else 'blocked',
                        error_code='external_delivery_unknown' if unknown else 'job_deadline_exceeded',
                        error='The job reached its wall-clock deadline. Previously sent calls and completed research records are retained; no request was automatically retried.')
            except ExecutionCancelled:
                if claimed:
                    unknown = self.store.has_unknown(self.workspace_id,job_id)
                    self.store.finish(self.workspace_id,job_id,owner,'unknown' if unknown else 'cancelled',
                        error_code='external_delivery_unknown' if unknown else 'cancelled',
                        error='Cancellation was requested. An already sent external request may still have executed; no refund or rollback is implied.' if unknown else
                              'Execution stopped at a cancellation checkpoint. Already completed external calls are retained.')
            except UnknownExternalAttempt:
                if claimed:
                    self.store.finish(self.workspace_id,job_id,owner,'unknown',error_code='external_delivery_unknown',
                        error='An external request may have executed, but its result is unknown. It was not automatically repeated.')
            except LeaseLost:
                pass
            except Exception as error:
                from .telemetry import log_error
                try:log_error(self.state_root,'job.execution_failed',error,{'job_id':job_id,'workspace_id':self.workspace_id})
                except Exception:pass
                if claimed:
                    try:
                        unknown = self.store.has_unknown(self.workspace_id,job_id)
                        self.store.finish(self.workspace_id,job_id,owner,'unknown' if unknown else 'failed',
                            error_code='external_delivery_unknown' if unknown else 'execution_failed',
                            error='An external request may have executed, but its saved result is uncertain. No request was automatically retried.' if unknown else
                                  'This message could not be completed. Existing records are preserved. Model calls were not automatically retried.')
                    except Exception:
                        pass
            finally:
                stopped.set()
                if pulse is not None and pulse.ident is not None:
                    # This worker owns its heartbeat. Keep it registered until
                    # both threads are quiescent; shutdown's outer timed join
                    # still bounds the caller and reports pending work honestly.
                    pulse.join()
                self._capacity.release()
                with self._lock:
                    self._threads.pop(job_id,None)
        thread = threading.Thread(target=execute,daemon=True,name='nhis-chat-'+job_id[:8])
        with self._lock:
            self._threads[job_id] = thread
            try:
                # Shutdown snapshots this map under the same lock. It must
                # never observe a Thread that cannot yet be joined.
                thread.start()
            except BaseException:
                self._threads.pop(job_id,None)
                if not capacity_reserved:
                    self._capacity.release()
                raise

    @staticmethod
    def _compact_response(response):
        value = {'conversation_id':response.get('conversation_id'),'message':safe_value(response.get('message'))}
        result = response.get('result')
        if isinstance(result,dict) and isinstance(result.get('run_id'),str):
            value['result_run_id'] = result['run_id']
        return value

    def get(self,job_id,*,event_limit=200,before_event=None):
        self.store.reconcile(self.workspace_id)
        row = self.store.get(self.workspace_id,job_id,include_events=False)
        try:
            conversation = getattr(self.service,'get_for_job',self.service.get)(row['conversation_id'])
        except FileNotFoundError:
            try:
                self.store.delete_jobs(self.workspace_id,row['conversation_id'])
            except ActiveJobConflict:
                pass
            raise
        value = self._public(row)
        value.update(self.store.events_page(self.workspace_id,job_id,event_limit,before_event))
        value['external_attempts'] = [{key:item[key] for key in ('attempt_id','attempt_key','kind','state','error_code') if item.get(key) is not None}
                                      for item in row['external_attempts']]
        if 'response' in row:
            response = row['response']
            response['conversation'] = conversation
            response['result'] = None
            reader = getattr(self.service,'_run',None)
            if response.get('result_run_id') and callable(reader):
                response['result'] = reader(response['result_run_id'])
            response.pop('result_run_id',None)
            value['response'] = response
        return value

    def request_cancel(self,job_id):
        row = self.store.get(self.workspace_id,job_id)
        self.service.get(row['conversation_id'])
        self.store.cancel(self.workspace_id,job_id)
        return self.get(job_id)

    def resume(self,job_id):
        self.store.reconcile(self.workspace_id)
        row = self.store.get(self.workspace_id,job_id)
        self.service.get(row['conversation_id'])
        if self._stopping.is_set():
            raise ConversationBusy('The service is shutting down.')
        if not self._capacity.acquire(blocking=False):
            raise ConversationBusy('The service is processing other conversations. Try again shortly.')
        try:
            self.store.prepare_resume(self.workspace_id,job_id)
            self._dispatch(job_id,capacity_reserved=True)
        except ActiveJobConflict as error:
            self._capacity.release()
            raise ConversationBusy(str(error)) from None
        except BaseException:
            self._capacity.release()
            raise
        return self._public(self.store.get(self.workspace_id,job_id))

    def delete_conversation(self,conversation_id):
        # SQLite serializes admission/deletion; never wait for its writer
        # while holding the thread-map lock (public quota already owns it).
        with self.store.transaction() as transaction:
            rows=transaction.execute("SELECT 1 FROM jobs WHERE workspace_id=? AND conversation_id=? AND state IN ('queued','running')",(self.workspace_id,conversation_id)).fetchone()
            if rows:
                raise ConversationBusy('This conversation is processing a message and cannot be deleted yet.')
            result=self.service.delete(conversation_id)
            self.store.delete_jobs(self.workspace_id,conversation_id,connection=transaction)
            return result

    def clear_conversations(self):
        # SQLite serializes admission/deletion; never wait for its writer
        # while holding the thread-map lock (public quota already owns it).
        with self.store.transaction() as transaction:
            if transaction.execute("SELECT 1 FROM jobs WHERE workspace_id=? AND state IN ('queued','running')",(self.workspace_id,)).fetchone():
                raise ConversationBusy('A conversation is processing a message. No conversations were cleared.')
            result=self.service.clear()
            self.store.delete_jobs(self.workspace_id,connection=transaction)
            return result

    def shutdown(self,timeout=5):
        self._stopping.set()
        with self._lock:
            active = list(self._threads.items())
        for identifier,_ in active:
            try:
                self.store.cancel(self.workspace_id,identifier)
            except FileNotFoundError:
                pass
        deadline = time.monotonic()+max(0,float(timeout))
        for _,thread in active:
            thread.join(timeout=max(0,deadline-time.monotonic()))
        return {'drained':all(not thread.is_alive() for _,thread in active),
                'pending_jobs':[identifier for identifier,thread in active if thread.is_alive()]}
