"""Durable, workspace-scoped execution records in the shared admission database.

Schema creation is additive. This module does not read legacy conversations,
credentials or provider configuration, and never dispatches work on its own.
"""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


SCHEMA_VERSION = 1
TERMINAL = frozenset(('succeeded', 'failed', 'blocked', 'cancelled', 'unknown'))
ACTIVE = ('queued', 'running')
MAX_JSON_BYTES = 24 * 1024 * 1024


class SubmissionConflict(ValueError):
    """One client id was reused for a different submission (HTTP 409)."""


class ActiveJobConflict(ValueError):
    pass


class LeaseLost(BaseException):
    pass


def encode(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(text.encode('utf-8')) > MAX_JSON_BYTES:
        raise ValueError('Execution record exceeds its storage limit.')
    return text


def fingerprint(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def timestamp(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def workspace_identity(path):
    return hashlib.sha256(str(Path(path).resolve()).encode('utf-8')).hexdigest()


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class JobStore:
    def __init__(self, state_dir, *, clock=None):
        self.state_root = Path(state_dir).expanduser().resolve()
        directory = self.state_root / '.cache'
        if directory.is_symlink():
            raise ValueError('Execution state must not be a symbolic link.')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.db_path = directory / 'public-access.sqlite3'
        if self.db_path.is_symlink():
            raise ValueError('The execution database must not be a symbolic link.')
        self.clock = clock or time.time
        connection = self.connect()
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS execution_schema (
                    component TEXT PRIMARY KEY, version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO execution_schema VALUES ('durable_jobs', 1);
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, client_message_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, request_json TEXT NOT NULL,
                    user_message_id TEXT, assistant_message_id TEXT, created_at REAL NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(workspace_id, conversation_id, client_message_id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE REFERENCES turns(turn_id) ON DELETE CASCADE,
                    workspace_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','blocked','cancelled','unknown')),
                    trace_id TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    started_at REAL, completed_at REAL, lease_owner TEXT, owner_pid INTEGER,
                    lease_expires_at REAL, heartbeat_at REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    cancel_requested_at REAL, recovery_required INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT, error_message TEXT, response_json TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_conversation
                    ON jobs(workspace_id, conversation_id) WHERE state IN ('queued','running');
                CREATE INDEX IF NOT EXISTS jobs_workspace_state ON jobs(workspace_id,state);
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL, event_json TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(job_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS step_results (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    step_key TEXT NOT NULL, input_hash TEXT, kind TEXT NOT NULL,
                    result_json TEXT NOT NULL, saved_at REAL NOT NULL,
                    PRIMARY KEY(job_id,step_key)
                );
                CREATE TABLE IF NOT EXISTS external_attempts (
                    attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    attempt_key TEXT NOT NULL, kind TEXT NOT NULL, request_hash TEXT NOT NULL,
                    request_meta_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('started','completed','unknown')),
                    result_json TEXT, error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(job_id,attempt_key)
                );
            ''')
            version = connection.execute("SELECT version FROM execution_schema WHERE component='durable_jobs'").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise ValueError('Unsupported durable execution schema version.')
            if 'deleted' not in {row[1] for row in connection.execute('PRAGMA table_info(turns)')}:
                connection.execute('ALTER TABLE turns ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0')
        finally:
            connection.close()
        self.db_path.chmod(0o600)

    def connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=5000')
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    @contextmanager
    def transaction(self, connection=None):
        if connection is not None:
            database = connection.execute('PRAGMA database_list').fetchone()[2]
            if not database or Path(database).resolve() != self.db_path.resolve():
                raise ValueError('Quota and job admission must use the same database.')
            if not connection.in_transaction:
                raise ValueError('An externally supplied admission connection must have an active transaction.')
            connection.row_factory = sqlite3.Row
            yield connection
            return
        connection = self.connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _scope(connection, workspace_id, job_id):
        row = connection.execute('SELECT jobs.* FROM jobs JOIN turns USING(turn_id) WHERE jobs.workspace_id=? AND job_id=? AND turns.deleted=0',
                                 (workspace_id, job_id)).fetchone()
        if row is None:
            raise FileNotFoundError('This job does not exist in the current workspace.')
        return dict(row)

    def lookup(self, workspace_id, conversation_id, client_message_id, payload_hash, *, connection=None):
        with self.transaction(connection) as connection:
            row = connection.execute('''SELECT jobs.* FROM turns JOIN jobs USING(turn_id)
                WHERE turns.workspace_id=? AND turns.conversation_id=? AND turns.client_message_id=?''',
                (workspace_id, conversation_id, client_message_id)).fetchone()
            if row is None:
                return None
            turn = connection.execute('SELECT payload_hash,deleted FROM turns WHERE turn_id=?', (row['turn_id'],)).fetchone()
            if turn['deleted']:
                raise FileNotFoundError('The conversation for this job was deleted.')
            original = turn['payload_hash']
            if original != payload_hash:
                raise SubmissionConflict('This client message id was already used for different message content or options.')
            result = dict(row)
            result['has_unknown_attempts'] = bool(connection.execute(
                "SELECT 1 FROM external_attempts WHERE job_id=? AND state IN ('started','unknown')",(row['job_id'],)).fetchone())
            return result

    def create(self, workspace_id, conversation_id, client_message_id, payload_hash, request, *, connection=None, maximum_active=4):
        with self.transaction(connection) as connection:
            existing = self.lookup(workspace_id, conversation_id, client_message_id, payload_hash, connection=connection)
            if existing is not None:
                return existing, False
            if connection.execute("SELECT 1 FROM jobs WHERE workspace_id=? AND conversation_id=? AND state IN ('queued','running')",
                                  (workspace_id, conversation_id)).fetchone():
                raise ActiveJobConflict('The previous message in this conversation is still processing. Wait before continuing.')
            if connection.execute("SELECT COUNT(*) FROM jobs WHERE state='running' OR (state='queued' AND recovery_required=0)").fetchone()[0] >= maximum_active:
                raise ActiveJobConflict('The service is processing other conversations. Try again shortly.')
            now = self.clock()
            turn_id, job_id, trace_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
            connection.execute('''INSERT INTO turns(turn_id,workspace_id,conversation_id,client_message_id,payload_hash,request_json,created_at)
                VALUES (?,?,?,?,?,?,?)''', (turn_id,workspace_id,conversation_id,client_message_id,payload_hash,encode(request),now))
            connection.execute('''INSERT INTO jobs(job_id,turn_id,workspace_id,conversation_id,state,trace_id,created_at,updated_at,owner_pid,lease_expires_at)
                VALUES (?,?,?,?,'queued',?,?,?,?,?)''', (job_id,turn_id,workspace_id,conversation_id,trace_id,now,now,os.getpid(),now+30))
            return self._scope(connection, workspace_id, job_id), True

    def lease_status(self, workspace_id, job_id):
        # Cancellation checkpoints do not decode history or acquire the global
        # admission write lock. Ownership remains fenced on every mutation.
        connection=self.connect()
        try:
            row=connection.execute('SELECT state,cancel_requested,lease_owner,lease_expires_at FROM jobs JOIN turns USING(turn_id) WHERE jobs.workspace_id=? AND job_id=? AND turns.deleted=0',(workspace_id,job_id)).fetchone()
            if row is None:raise FileNotFoundError('This job does not exist.')
            return dict(row)
        finally:connection.close()

    def get(self, workspace_id, job_id, *, include_events=True):
        with self.transaction() as connection:
            row = self._scope(connection,workspace_id,job_id)
            turn = connection.execute('SELECT client_message_id,user_message_id,assistant_message_id FROM turns WHERE turn_id=?',
                                      (row['turn_id'],)).fetchone()
            row.update(dict(turn))
            row['events'] = [json.loads(item[0]) for item in connection.execute(
                'SELECT event_json FROM job_events WHERE job_id=? ORDER BY sequence', (job_id,))] if include_events else []
            row['external_attempts'] = [dict(item) for item in connection.execute(
                'SELECT attempt_id,attempt_key,kind,state,error_code,created_at,updated_at FROM external_attempts WHERE job_id=? ORDER BY created_at', (job_id,))]
            if row['response_json'] is not None:
                row['response'] = json.loads(row.pop('response_json'))
            else:
                row.pop('response_json')
            return row

    def events_page(self,workspace_id,job_id,limit=200,before=None):
        if type(limit) is not int or not 1<=limit<=200:
            raise ValueError('The event page limit must be an integer from 1 to 200.')
        if before is not None and (type(before) is not int or not 1<=before<=10**9):
            raise ValueError('Invalid event page cursor.')
        connection=self.connect()
        try:
            self._scope(connection,workspace_id,job_id)
            rows=connection.execute('SELECT sequence,event_json FROM job_events WHERE job_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?',
                (job_id,before if before is not None else 10**9,limit+1)).fetchall()
            more=len(rows)>limit;rows=rows[:limit]
            return {'events':[json.loads(row[1]) for row in reversed(rows)],
                'event_pagination':{'has_more':more,'before':rows[-1][0] if more else None}}
        finally:connection.close()

    def request(self, workspace_id, job_id):
        with self.transaction() as connection:
            row = self._scope(connection,workspace_id,job_id)
            return json.loads(connection.execute('SELECT request_json FROM turns WHERE turn_id=?', (row['turn_id'],)).fetchone()[0])

    def active(self, workspace_id, conversation_id=None, *, connection=None):
        own=connection is None
        if own:connection=self.connect()
        try:
            sql = "SELECT * FROM jobs WHERE workspace_id=? AND state IN ('queued','running')"
            values = [workspace_id]
            if conversation_id is not None:
                sql += ' AND conversation_id=?'; values.append(conversation_id)
            return [dict(row) for row in connection.execute(sql+' ORDER BY created_at', values)]
        finally:
            if own:connection.close()

    def latest(self, workspace_id, conversation_id):
        with self.transaction() as connection:
            row = connection.execute('SELECT job_id FROM jobs WHERE workspace_id=? AND conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                                     (workspace_id,conversation_id)).fetchone()
            return row[0] if row else None

    def claim(self, workspace_id, job_id, owner, *, lease_seconds=30):
        with self.transaction() as connection:
            row = self._scope(connection,workspace_id,job_id)
            if row['state'] != 'queued' or row['recovery_required'] or row['cancel_requested']:
                return False
            now = self.clock()
            connection.execute("UPDATE jobs SET state='running',lease_owner=?,owner_pid=?,lease_expires_at=?,heartbeat_at=?,started_at=COALESCE(started_at,?),updated_at=? WHERE job_id=?",
                               (owner,os.getpid(),now+lease_seconds,now,now,now,job_id))
            return True

    def heartbeat(self, workspace_id, job_id, owner, *, lease_seconds=30):
        with self.transaction() as connection:
            now = self.clock()
            changed = connection.execute("UPDATE jobs SET heartbeat_at=?,lease_expires_at=?,updated_at=? WHERE workspace_id=? AND job_id=? AND state='running' AND lease_owner=?",
                (now,now+lease_seconds,now,workspace_id,job_id,owner)).rowcount
            return bool(changed)

    def assert_owner(self, connection, workspace_id, job_id, owner):
        row = self._scope(connection,workspace_id,job_id)
        if row['state'] != 'running' or row['lease_owner'] != owner or row['lease_expires_at'] <= self.clock():
            raise LeaseLost('The worker no longer owns this execution lease.')
        return row

    def event(self, workspace_id, job_id, event):
        with self.transaction() as connection:
            self._scope(connection,workspace_id,job_id)
            number = connection.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM job_events WHERE job_id=?', (job_id,)).fetchone()[0]
            connection.execute('INSERT INTO job_events VALUES (?,?,?,?)', (job_id,number,encode(event),self.clock()))

    def link_messages(self, workspace_id, job_id, *, user_message_id=None, assistant_message_id=None):
        with self.transaction() as connection:
            row = self._scope(connection,workspace_id,job_id)
            for name,value in (('user_message_id',user_message_id),('assistant_message_id',assistant_message_id)):
                if value is None:
                    continue
                if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{32}',value):
                    raise ValueError('Invalid persisted message identifier.')
                old = connection.execute('SELECT '+name+' FROM turns WHERE turn_id=?',(row['turn_id'],)).fetchone()[0]
                if old is not None and old != value:
                    raise SubmissionConflict('The saved turn already refers to a different message.')
                connection.execute('UPDATE turns SET '+name+'=? WHERE turn_id=?',(value,row['turn_id']))

    def finish(self, workspace_id, job_id, owner, state, *, response=None, error_code=None, error=None):
        if state not in TERMINAL:
            raise ValueError('Invalid final execution state.')
        with self.transaction() as connection:
            row = self._scope(connection,workspace_id,job_id)
            if row['state'] != 'running' or row['lease_owner'] != owner:
                return False
            now = self.clock()
            connection.execute('''UPDATE jobs SET state=?,response_json=?,error_code=?,error_message=?,completed_at=?,updated_at=?,recovery_required=?,
                lease_owner=NULL,owner_pid=NULL,lease_expires_at=NULL,heartbeat_at=NULL WHERE job_id=?''',
                (state,encode(response) if response is not None else None,error_code,error,now,now,int(state=='unknown'),job_id))
            return True

    def fail_queued(self, workspace_id, job_id, *, code='admission_failed', error='This message was not admitted. No model request was started.'):
        with self.transaction() as connection:
            self._scope(connection,workspace_id,job_id)
            now = self.clock()
            connection.execute("UPDATE jobs SET state='failed',error_code=?,error_message=?,updated_at=?,completed_at=? WHERE job_id=? AND state='queued'",
                               (code,error,now,now,job_id))

    def cancel(self, workspace_id, job_id):
        with self.transaction() as connection:
            row = self._scope(connection,workspace_id,job_id)
            now = self.clock()
            connection.execute('UPDATE jobs SET cancel_requested=1,cancel_requested_at=COALESCE(cancel_requested_at,?),updated_at=? WHERE job_id=?', (now,now,job_id))
            if row['state'] == 'queued':
                connection.execute("UPDATE jobs SET state='cancelled',completed_at=?,recovery_required=0 WHERE job_id=?",(now,job_id))

    def has_unknown(self, workspace_id, job_id):
        with self.transaction() as connection:
            self._scope(connection,workspace_id,job_id)
            return bool(connection.execute("SELECT 1 FROM external_attempts WHERE job_id=? AND state IN ('started','unknown')",(job_id,)).fetchone())

    def reconcile(self, workspace_id):
        """Explain orphaned work; never dispatch or resend an external request."""
        changed = []
        with self.transaction() as connection:
            now = self.clock()
            rows = connection.execute("SELECT * FROM jobs WHERE workspace_id=? AND (state='running' OR (state='queued' AND recovery_required=0))",(workspace_id,)).fetchall()
            for row in rows:
                if row['lease_expires_at'] is not None and row['lease_expires_at'] > now and _alive(row['owner_pid']):
                    continue
                uncertain = connection.execute("SELECT 1 FROM external_attempts WHERE job_id=? AND state IN ('started','unknown')",(row['job_id'],)).fetchone()
                state = 'unknown' if uncertain else 'queued'
                connection.execute("UPDATE external_attempts SET state='unknown',error_code=COALESCE(error_code,'worker_interrupted'),updated_at=? WHERE job_id=? AND state='started'",(now,row['job_id']))
                connection.execute('''UPDATE jobs SET state=?,recovery_required=1,error_code='worker_interrupted',
                    error_message=?,updated_at=?,lease_owner=NULL,owner_pid=NULL,lease_expires_at=NULL,heartbeat_at=NULL WHERE job_id=?''',
                    (state,'Execution was interrupted. External delivery may be unknown; no request was automatically repeated.' if uncertain else
                     'Execution was interrupted. Saved steps are retained; explicit recovery is required.',now,row['job_id']))
                changed.append(row['job_id'])
        return changed

    def reconcile_all(self):
        """Run once before admission starts, so dead visitors cannot hold capacity."""
        with self.transaction() as connection:
            workspaces = [row[0] for row in connection.execute(
                "SELECT DISTINCT workspace_id FROM jobs WHERE state IN ('queued','running')")]
        changed = []
        for workspace in workspaces:
            changed.extend(self.reconcile(workspace))
        return changed

    def prepare_resume(self, workspace_id, job_id):
        with self.transaction() as connection:
            row = self._scope(connection,workspace_id,job_id)
            unknown = connection.execute("SELECT 1 FROM external_attempts WHERE job_id=? AND state IN ('started','unknown')",(job_id,)).fetchone()
            if unknown:
                raise ActiveJobConflict('An external request has an unknown result. It must not be automatically resubmitted.')
            if not row['recovery_required'] or row['state'] not in ('queued','unknown'):
                raise ActiveJobConflict('This job is not awaiting safe explicit recovery.')
            if row['cancel_requested']:
                raise ActiveJobConflict('This job has a cancellation request and cannot be resumed.')
            now = self.clock()
            connection.execute("UPDATE jobs SET state='queued',recovery_required=0,error_code=NULL,error_message=NULL,owner_pid=?,lease_expires_at=?,updated_at=? WHERE job_id=?",(os.getpid(),now+30,now,job_id))

    def load_step(self, workspace_id, job_id, key, input_hash=None):
        with self.transaction() as connection:
            self._scope(connection,workspace_id,job_id)
            row = connection.execute('SELECT * FROM step_results WHERE job_id=? AND step_key=?',(job_id,key)).fetchone()
            if row is None:
                return None
            if row['input_hash'] != input_hash:
                raise SubmissionConflict('A saved step has different registered inputs.')
            return json.loads(row['result_json'])

    def save_step(self, workspace_id, job_id, owner, key, result, input_hash=None, kind='local'):
        with self.transaction() as connection:
            self.assert_owner(connection,workspace_id,job_id,owner)
            text = encode(result)
            old = connection.execute('SELECT * FROM step_results WHERE job_id=? AND step_key=?',(job_id,key)).fetchone()
            if old:
                if old['input_hash'] != input_hash or old['result_json'] != text or old['kind'] != kind:
                    raise SubmissionConflict('A completed step cannot be overwritten with different evidence.')
                return
            connection.execute('INSERT INTO step_results VALUES (?,?,?,?,?,?)',(job_id,key,input_hash,kind,text,self.clock()))

    def external(self, workspace_id, job_id, key):
        with self.transaction() as connection:
            self._scope(connection,workspace_id,job_id)
            row = connection.execute('SELECT * FROM external_attempts WHERE job_id=? AND attempt_key=?',(job_id,key)).fetchone()
            if row is None:
                return None
            value = dict(row)
            if value['result_json'] is not None:
                value['result'] = json.loads(value['result_json'])
            return value

    def start_external(self, workspace_id, job_id, owner, key, kind, metadata, *, request_hash=None):
        with self.transaction() as connection:
            self.assert_owner(connection,workspace_id,job_id,owner)
            old = connection.execute('SELECT attempt_id FROM external_attempts WHERE job_id=? AND attempt_key=?',(job_id,key)).fetchone()
            if old:
                raise SubmissionConflict('This planned external attempt already has a durable record.')
            now = self.clock(); identifier = uuid.uuid4().hex
            connection.execute('''INSERT INTO external_attempts(attempt_id,job_id,attempt_key,kind,request_hash,request_meta_json,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'started',?,?)''',(identifier,job_id,key,kind,request_hash or fingerprint(metadata),encode(metadata),now,now))
            return identifier

    def finish_external(self, workspace_id, job_id, attempt_id, result=None, *, unknown=False, error_code=None):
        with self.transaction() as connection:
            self._scope(connection,workspace_id,job_id)
            row = connection.execute('SELECT * FROM external_attempts WHERE job_id=? AND attempt_id=?',(job_id,attempt_id)).fetchone()
            if row is None:
                raise FileNotFoundError('This external attempt does not belong to the job.')
            text = encode(result) if not unknown else None
            if row['state'] == 'completed':
                if not unknown and row['result_json'] == text:
                    return
                raise SubmissionConflict('A completed external attempt cannot be replaced.')
            connection.execute('UPDATE external_attempts SET state=?,result_json=?,error_code=?,updated_at=? WHERE attempt_id=?',
                ('unknown' if unknown else 'completed',text,error_code,self.clock(),attempt_id))

    def delete_jobs(self, workspace_id, conversation_id=None, *, connection=None):
        with self.transaction(connection) as connection:
            sql = 'SELECT job_id,state,turn_id FROM jobs WHERE workspace_id=?'; args = [workspace_id]
            if conversation_id is not None:
                sql += ' AND conversation_id=?'; args.append(conversation_id)
            rows = connection.execute(sql,args).fetchall()
            if any(row['state'] in ACTIVE for row in rows):
                raise ActiveJobConflict('A conversation is processing a message. No conversations were cleared.')
            # Retain only idempotency/audit tombstones. Conversation prose and
            # checkpoint copies disappear; registered run files are separate.
            keep = {'status','provider','model','response_id','request_id','usage','elapsed_ms',
                    'http_status','error_code','delivery_state','reservation_id','attempt_id','accounting_status'}
            request_keep = {'request_hash','purpose','provider','model','input_tokens','output_tokens',
                            'reservation_id','trace_id','attempt_id'}
            for row in rows:
                for attempt in connection.execute('SELECT * FROM external_attempts WHERE job_id=?',(row['job_id'],)).fetchall():
                    result = json.loads(attempt['result_json']) if attempt['result_json'] is not None else None
                    metadata = json.loads(attempt['request_meta_json'])
                    result = {key:value for key,value in result.items() if key in keep} if isinstance(result,dict) else None
                    metadata = {key:value for key,value in metadata.items() if key in request_keep} if isinstance(metadata,dict) else {}
                    connection.execute('UPDATE external_attempts SET result_json=?,request_meta_json=? WHERE attempt_id=?',
                        (encode(result) if result is not None else None,encode(metadata),attempt['attempt_id']))
                connection.execute('DELETE FROM step_results WHERE job_id=?',(row['job_id'],))
                connection.execute('DELETE FROM job_events WHERE job_id=?',(row['job_id'],))
                connection.execute('UPDATE jobs SET response_json=NULL WHERE job_id=?',(row['job_id'],))
                connection.execute("UPDATE turns SET request_json='{}',user_message_id=NULL,assistant_message_id=NULL,deleted=1 WHERE turn_id=?",(row['turn_id'],))
            return [row['job_id'] for row in rows]
