"""Derived, workspace-local run summaries with bounded legacy import.

Only registration/result files under this workspace are read. Unchanged files,
including invalid/pending files, are not parsed again. The index is rebuildable
and contains no raw provider responses or survey records.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading


SCHEMA_VERSION=1
RUN_ID=re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}')
MAX_REGISTRATION_BYTES=64*1024
MAX_RESULT_BYTES=16*1024*1024
MAX_DIRECTORY_SCAN=100000
MAX_IMPORT_FILES=256
MAX_IMPORT_BYTES=32*1024*1024


def _json(raw):
    def pairs(items):
        answer={}
        for key,value in items:
            if key in answer:raise ValueError('Duplicate saved JSON field.')
            answer[key]=value
        return answer
    def constant(_):raise ValueError('Nonfinite saved JSON value.')
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=constant)


def _fingerprint(info):
    return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]


def _summary(run_id,registration,result):
    if not isinstance(registration,dict) or not isinstance(result,dict):return None
    spec,question,created=registration.get('spec'),registration.get('question'),registration.get('registered_at')
    if (result.get('run_id')!=run_id or result.get('status') not in ('ok','blocked','clarify')
            or not isinstance(spec,dict) or not isinstance(question,str) or not 1<=len(question)<=2000
            or not isinstance(created,str)):
        return None
    dt.datetime.fromisoformat(created.replace('Z','+00:00'))
    measurement=result.get('measurement')
    model=measurement.get('model') if isinstance(measurement,dict) else None
    item={'run_id':run_id,'question':question,'status':result['status'],
          'group_by':spec.get('group_by'),'analysis':spec.get('analysis'),
          'model':model if isinstance(model,str) and len(model)<=256 else None,'created_at':created}
    for name in ('group_by','analysis'):
        if item[name] is not None and (not isinstance(item[name],str) or len(item[name])>128):return None
    if 'outcome' in spec:
        if not isinstance(spec['outcome'],str) or len(spec['outcome'])>128:return None
        item['outcome']=spec['outcome']
    return item


class HistoryIndex:
    def __init__(self,root,*,scan_limit=MAX_DIRECTORY_SCAN,import_limit=MAX_IMPORT_FILES,
                 import_bytes=MAX_IMPORT_BYTES):
        original=Path(root).expanduser()
        if original.is_symlink():raise ValueError('History state must not be a symbolic link.')
        self.root=original.resolve()
        self.runs=self.root/'runs'
        if self.runs.is_symlink():raise ValueError('Run history must not be a symbolic link.')
        directory=self.root/'.cache'
        if directory.is_symlink():raise ValueError('History index state must not be a symbolic link.')
        directory.mkdir(mode=0o700,parents=True,exist_ok=True);directory.chmod(0o700)
        self.db_path=directory/'run-history.sqlite3'
        if self.db_path.is_symlink():raise ValueError('The history index must not be a symbolic link.')
        for value in (scan_limit,import_limit,import_bytes):
            if type(value) is not int or value<1:raise ValueError('History import limits must be positive integers.')
        self.scan_limit,self.import_limit,self.import_bytes=scan_limit,import_limit,import_bytes
        self._lock=threading.RLock()
        connection=self._connect()
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS history_schema (version INTEGER NOT NULL);
                INSERT INTO history_schema(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM history_schema);
                CREATE TABLE IF NOT EXISTS run_summaries (
                    run_id TEXT PRIMARY KEY, source_fingerprint TEXT NOT NULL,
                    valid INTEGER NOT NULL, summary_json TEXT
                );
                CREATE INDEX IF NOT EXISTS history_valid_run ON run_summaries(valid,run_id DESC);
            ''')
            if connection.execute('SELECT version FROM history_schema').fetchone()[0]!=SCHEMA_VERSION:
                raise ValueError('Unsupported run-history index schema version.')
        finally:connection.close()
        self.db_path.chmod(0o600)

    def _connect(self):
        connection=sqlite3.connect(str(self.db_path),timeout=2,isolation_level=None)
        connection.execute('PRAGMA busy_timeout=2000')
        return connection

    def _open_run(self,run_id):
        flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
        parent=os.open(str(self.runs),flags)
        try:return os.open(run_id,flags,dir_fd=parent)
        finally:os.close(parent)

    def _source(self,run_id):
        directory=None;descriptors=[]
        try:
            directory=self._open_run(run_id)
            directory_info=os.fstat(directory)
            identity={'directory':[directory_info.st_dev,directory_info.st_ino]}
            total=0
            for name,maximum in (('registration.json',MAX_REGISTRATION_BYTES),('result.json',MAX_RESULT_BYTES)):
                try:
                    descriptor=os.open(name,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0),dir_fd=directory)
                except FileNotFoundError:
                    identity[name]=None
                    continue
                descriptors.append((name,descriptor,maximum))
                info=os.fstat(descriptor)
                identity[name]=_fingerprint(info)
                if not stat.S_ISREG(info.st_mode) or info.st_size>maximum:
                    return json.dumps(identity,sort_keys=True),None,0
                total+=info.st_size
            if len(descriptors)!=2:return json.dumps(identity,sort_keys=True),None,0
            # Transfer ownership of descriptors to a bounded reader closure.
            held=list(descriptors);descriptors=[]
            def read():
                values={}
                try:
                    for name,descriptor,maximum in held:
                        before=os.fstat(descriptor)
                        with os.fdopen(os.dup(descriptor),'rb') as stream:raw=stream.read(maximum+1)
                        after=os.fstat(descriptor)
                        if len(raw)>maximum or _fingerprint(before)!=_fingerprint(after):
                            raise ValueError('The run artifact changed while being read.')
                        values[name]=_json(raw.decode('utf-8'))
                    return _summary(run_id,values['registration.json'],values['result.json'])
                finally:
                    for _,descriptor,_ in held:os.close(descriptor)
                    held.clear()
            def close():
                for _,descriptor,_ in held:os.close(descriptor)
                held.clear()
            read.close=close
            return json.dumps(identity,sort_keys=True),read,total
        except (OSError,ValueError):
            return 'unsafe-or-unavailable',None,0
        finally:
            for _,descriptor,_ in descriptors:os.close(descriptor)
            if directory is not None:os.close(directory)

    def _write(self,connection,run_id,identity,summary):
        connection.execute('''INSERT INTO run_summaries(run_id,source_fingerprint,valid,summary_json)
            VALUES (?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET source_fingerprint=excluded.source_fingerprint,
            valid=excluded.valid,summary_json=excluded.summary_json''',
            (run_id,identity,int(summary is not None),json.dumps(summary,ensure_ascii=False,allow_nan=False) if summary is not None else None))

    def record(self,run_id,summary=None):
        if not isinstance(run_id,str) or not RUN_ID.fullmatch(run_id):raise ValueError('Invalid run identifier.')
        with self._lock:
            identity,reader,_=self._source(run_id)
            connection=self._connect()
            try:
                if summary is None:
                    old=connection.execute('SELECT source_fingerprint,valid FROM run_summaries WHERE run_id=?',(run_id,)).fetchone()
                    if old and old[0]==identity:
                        if reader:reader.close()
                        return bool(old[1])
                    try:summary=reader() if reader else None
                    except (OSError,ValueError,TypeError,UnicodeError):summary=None
                else:
                    if reader:reader.close()
                    # The write-through hook may provide a compact summary, but
                    # cannot create an owned run that has no regular artifacts.
                    if reader is None:return False
                    if not isinstance(summary,dict) or summary.get('run_id')!=run_id:
                        raise ValueError('The indexed summary must match its owned run.')
                    registration={'spec':{key:summary[key] for key in ('group_by','analysis','outcome') if key in summary},
                                  'question':summary.get('question'),'registered_at':summary.get('created_at')}
                    summary=_summary(run_id,registration,{'run_id':run_id,'status':summary.get('status'),
                                      'measurement':{'model':summary.get('model')}})
                self._write(connection,run_id,identity,summary)
                return summary is not None
            finally:
                if reader:reader.close()
                connection.close()

    def refresh(self):
        """Bound metadata enumeration and changed-file parsing independently."""
        if self.runs.is_symlink():raise ValueError('Run history must not be a symbolic link.')
        names=[];scanned=0
        try:
            with os.scandir(self.runs) as entries:
                for entry in entries:
                    scanned+=1
                    if scanned>self.scan_limit:raise ValueError('The run-history metadata scan exceeded its bounded limit.')
                    if RUN_ID.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):names.append(entry.name)
        except FileNotFoundError:
            connection=self._connect()
            try:connection.execute("UPDATE run_summaries SET valid=0,summary_json=NULL,source_fingerprint='deleted'")
            finally:connection.close()
            return {'indexed':0,'pending':False,'scanned':0}
        names.sort(reverse=True)
        imported=used=0;pending=False;first_pending=None;last_ready=None
        with self._lock:
            connection=self._connect()
            try:
                known=dict(connection.execute('SELECT run_id,source_fingerprint FROM run_summaries LIMIT ?',
                                               (self.scan_limit+1,)))
                if len(known)>self.scan_limit:raise ValueError('The derived history index exceeded its bounded entry limit.')
                for identifier in names:
                    identity,reader,size=self._source(identifier)
                    try:
                        if known.get(identifier)==identity:
                            if first_pending is None:last_ready=identifier
                            continue
                        if reader and (imported>=self.import_limit or (used and used+size>self.import_bytes)):
                            pending=True
                            if first_pending is None:first_pending=identifier
                            continue
                        summary=None
                        if reader:
                            try:summary=reader()
                            except (OSError,ValueError,TypeError,UnicodeError):pass
                            imported+=1;used+=size
                        self._write(connection,identifier,identity,summary)
                        if first_pending is None:last_ready=identifier
                    finally:
                        if reader:reader.close()
                for missing in set(known)-set(names):
                    self._write(connection,missing,'deleted',None)
            finally:connection.close()
        return {'indexed':imported,'pending':pending,'scanned':scanned,
                'first_pending':first_pending,'last_ready':last_ready}

    def page(self,limit=30,before=None):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('The history limit must be an integer from 1 to 100.')
        if before is not None and (not isinstance(before,str) or not RUN_ID.fullmatch(before)):
            raise ValueError('The history cursor is invalid for this workspace.')
        progress=self.refresh()
        connection=self._connect()
        try:
            if before is not None and not connection.execute('SELECT 1 FROM run_summaries WHERE run_id=?',(before,)).fetchone():
                raise ValueError('The history cursor is invalid for this workspace.')
            sql='SELECT summary_json FROM run_summaries WHERE valid=1'
            parameters=[]
            if before is not None:sql+=' AND run_id<?';parameters.append(before)
            floor=progress.get('first_pending')
            if floor is not None and (before is None or before>floor):
                sql+=' AND run_id>?';parameters.append(floor)
            parameters.append(limit+1)
            rows=[json.loads(row[0]) for row in connection.execute(sql+' ORDER BY run_id DESC LIMIT ?',parameters)]
        finally:connection.close()
        next_cursor=rows[limit-1]['run_id'] if len(rows)>limit else rows[-1]['run_id'] if rows and progress['pending'] else None
        if not rows and progress['pending']:
            next_cursor=progress.get('last_ready')
        return {'runs':rows[:limit],'next_cursor':next_cursor,
                'schema_version':SCHEMA_VERSION,'indexing_pending':progress['pending']}
