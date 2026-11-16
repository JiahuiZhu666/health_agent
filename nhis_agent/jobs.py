"""Small, loopback-local job coordinator for observable conversational execution."""
import copy
import datetime as dt
import threading
import uuid


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class ConversationBusy(ValueError):
    pass


class ChatJobs:
    def __init__(self, service, *, capacity=None):
        self.service = service
        self._lock = threading.Lock()
        self._jobs = {}
        self._active = {}
        self._capacity = capacity

    def has_active(self):
        with self._lock:
            return bool(self._active)

    def active_job(self, conversation_id):
        with self._lock:
            return self._active.get(conversation_id)

    def submit(self, conversation_id, text, measure_model=False, repeats=5, prompt_versions=3, *, admission=None):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise ValueError('Enter a message of 1–2,000 characters.')
        if type(measure_model) is not bool or type(repeats) is not int or not 1 <= repeats <= 5 or type(prompt_versions) is not int or not 1 <= prompt_versions <= 3:
            raise ValueError('The model measurement option must be Boolean, with 1–5 repetitions and 1–3 prompt versions.')
        job_id = uuid.uuid4().hex
        with self._lock:
            # Deletion and admission share this lock: a checked conversation
            # cannot disappear before its running job is registered.
            self.service.get(conversation_id)
            if conversation_id in self._active:
                raise ConversationBusy('The previous message in this conversation is still processing. Wait for the reply before continuing.')
            if len(self._active) >= 4:
                raise ConversationBusy('This computer is processing other conversations. Try again shortly.')
            if self._capacity is not None and not self._capacity.acquire(blocking=False):
                raise ConversationBusy('The service is processing other conversations. Try again shortly.')
            # A disconnected tab can recover an existing job, but never duplicate it.
            self._jobs[job_id] = {'id':job_id, 'job_id':job_id, 'conversation_id':conversation_id,
                                  'status':'running', 'created_at':_now(), 'updated_at':_now(), 'events':[]}
            self._active[conversation_id] = job_id
            finished = [key for key, value in self._jobs.items() if value['status'] != 'running']
            for key in finished[:-64]:
                self._jobs.pop(key, None)
        def observe(event):
            with self._lock:
                self._jobs[job_id]['events'].append(copy.deepcopy(event))
                self._jobs[job_id]['updated_at'] = _now()
        def run():
            try:
                if admission is not None and not admission.result():
                    with self._lock:
                        self._jobs[job_id].update(status='failed', updated_at=_now(),
                            error='This message could not be admitted. No model request was started. Please try again later.')
                    return
                response = self.service.send(conversation_id, text.strip(), measure_model=measure_model,
                    repeats=repeats, prompt_versions=prompt_versions, on_event=observe)
                with self._lock:
                    self._jobs[job_id].update(status='complete', response=response, updated_at=_now())
            except Exception:
                with self._lock:
                    self._jobs[job_id].update(status='failed', updated_at=_now(),
                        error='This message could not be completed. Existing conversations and statistical records are preserved. Try again later; model calls were not automatically retried.')
            finally:
                with self._lock:
                    self._active.pop(conversation_id, None)
                if self._capacity is not None:
                    self._capacity.release()
        try:
            threading.Thread(target=run, daemon=True, name='nhis-chat-' + job_id[:8]).start()
        except Exception:
            with self._lock:
                self._active.pop(conversation_id, None)
                self._jobs.pop(job_id, None)
            if self._capacity is not None:
                self._capacity.release()
            raise
        return {'id':job_id, 'job_id':job_id, 'conversation_id':conversation_id, 'status':'running'}

    def delete_conversation(self, conversation_id):
        with self._lock:
            if conversation_id in self._active:
                raise ConversationBusy('This conversation is processing a message and cannot be deleted yet.')
            result = self.service.delete(conversation_id)
            self._jobs = {key:value for key,value in self._jobs.items()
                          if value['conversation_id'] != conversation_id}
            return result

    def clear_conversations(self):
        with self._lock:
            if self._active:
                raise ConversationBusy('A conversation is processing a message. No conversations were cleared.')
            result = self.service.clear()
            self._jobs.clear()
            return result

    def get(self, job_id):
        with self._lock:
            if job_id not in self._jobs:
                raise FileNotFoundError('This job does not exist or the service has restarted. Restore the conversation before deciding whether to send the message again.')
            job = self._jobs[job_id]
            try:
                self.service.get(job['conversation_id'])
            except FileNotFoundError:
                self._jobs.pop(job_id, None)
                raise FileNotFoundError('The conversation for this job was deleted.') from None
            return copy.deepcopy(job)
