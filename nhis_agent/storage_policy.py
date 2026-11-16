"""Local state quotas, a conversation-summary index, and explicit operator tools.

Only managed conversations, run artifacts and cache metadata are inspected.
Official survey inputs, credentials and runtime environments are never backed up
or read here. Cleanup is a dry run unless the operator explicitly applies it.
"""
import base64
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import threading
import time
import uuid
import weakref


_CID = re.compile(r'[a-f0-9]{32}')
_RUN = re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}')
_MANAGED = ('conversations', 'runs', '.cache')
_EXCLUDED = {'data', '.venv', '.r-env', 'renv', 'backups', 'upgrades', 'previews', '__pycache__', '.git'}
_LOCKS, _LOCKS_GUARD = weakref.WeakValueDictionary(), threading.Lock()
_MAX_SCAN = 100_000


class StorageLimit(ValueError):
    def __init__(self, message='The workspace storage limit has been reached.', code='storage_limit'):
        self.code = code
        super().__init__(message)


def _positive(name, default):
    value = os.environ.get(name, default)
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 10**12:
        raise ValueError(name + ' must be a positive bounded integer.')
    return int(value)


def _root(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Managed state cannot be a symbolic link.')
    return path.resolve()


def _safe_path(root, path):
    path = Path(path)
    if '..' in path.parts:
        raise ValueError('Parent traversal is not allowed in managed state.')
    if not path.is_absolute():
        path = root / path
    # macOS exposes temporary directories through both /var and /private/var.
    # Normalize aliases above the workspace, then inspect every managed part.
    relative = None
    for ancestor in path.parents:
        if ancestor == root or ancestor.resolve() == root:
            relative = path.relative_to(ancestor)
            break
    if relative is None:
        raise ValueError('The state path is outside this workspace.') from None
    path = root / relative
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('Symbolic links are not allowed in managed state.')
    return path


def _files(directory, *, maximum=_MAX_SCAN):
    """Bounded metadata walk; never follow a file or directory symbolic link."""
    if directory.is_symlink():
        raise ValueError('Symbolic links are not allowed in managed state.')
    count = 0
    if not directory.exists():
        return
    for base, directories, files in os.walk(str(directory), followlinks=False):
        for name in list(directories):
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError('Symbolic links are not allowed in managed state.')
            if name in _EXCLUDED:
                directories.remove(name)
        for name in files:
            path = Path(base) / name
            try:info = path.lstat()
            except FileNotFoundError:continue  # SQLite may remove WAL/SHM sidecars.
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError('Only regular files are allowed in managed state.')
            count += 1
            if count > maximum:
                raise StorageLimit('The managed state scan exceeded its file limit.', 'storage_scan_limit')
            yield path, info


def _read_json(path, maximum):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError('The saved JSON file exceeds its limit or is not regular.')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            raw = source.read(maximum+1)
        if len(raw) > maximum:
            raise ValueError('The saved JSON file exceeds its limit.')
        return json.loads(raw.decode('utf-8'))
    finally:
        os.close(descriptor)


class StoragePolicy:
    def __init__(self, root):
        self.root = _root(root)
        self.conversation_bytes = _positive('NHIS_STORAGE_CONVERSATION_BYTES', 8*1024*1024)
        self.workspace_bytes = _positive('NHIS_STORAGE_WORKSPACE_BYTES', 64*1024*1024)
        self.run_bytes = _positive('NHIS_STORAGE_RUN_BYTES', 16*1024*1024)

    @contextmanager
    def write_guard(self):
        """Hold across quota check and atomic replace for race-free admission."""
        directory = _safe_path(self.root, self.root / '.cache')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        key = str(self.root)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        with lock:
            descriptor = os.open(str(directory / 'storage.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

    def _usage(self):
        counts = {name: {'bytes': 0, 'files': 0} for name in _MANAGED}
        for name in _MANAGED:
            for _, info in _files(self.root / name):
                counts[name]['bytes'] += info.st_size
                counts[name]['files'] += 1
        return counts

    def status(self):
        counts = self._usage()
        total = sum(value['bytes'] for value in counts.values())
        return {'bytes': total, 'files': sum(value['files'] for value in counts.values()),
                'limits': {'conversation_bytes': self.conversation_bytes,
                           'workspace_bytes': self.workspace_bytes, 'run_bytes': self.run_bytes},
                'remaining_bytes': max(0, self.workspace_bytes-total), 'within_limit': total <= self.workspace_bytes,
                'managed': counts}

    def check_conversation_write(self, path, payload_bytes):
        path = _safe_path(self.root, path)
        if path.parent != self.root / 'conversations' or path.suffix != '.json' or not _CID.fullmatch(path.stem):
            raise ValueError('The conversation path must be a registered ID in this workspace.')
        if isinstance(payload_bytes, (bytes, bytearray)):
            payload_bytes = len(payload_bytes)
        if isinstance(payload_bytes, bool) or not isinstance(payload_bytes, int) or payload_bytes < 0:
            raise ValueError('The payload size must be a nonnegative byte count.')
        if payload_bytes > self.conversation_bytes:
            raise StorageLimit('This conversation has reached its storage limit. Start a new conversation.', 'conversation_storage_limit')
        current = path.stat().st_size if path.exists() else 0
        if sum(item['bytes'] for item in self._usage().values()) - current + payload_bytes > self.workspace_bytes:
            raise StorageLimit()

    def check_run_admission(self):
        if sum(item['bytes'] for item in self._usage().values()) + self.run_bytes > self.workspace_bytes:
            raise StorageLimit('There is not enough reserved workspace capacity for a new run.', 'run_storage_limit')

    def check_run_size(self, directory):
        directory = _safe_path(self.root, directory)
        if directory.parent != self.root/'runs' or not _RUN.fullmatch(directory.name):
            raise ValueError('Invalid run directory.')
        if sum(info.st_size for _, info in _files(directory)) > self.run_bytes:
            raise StorageLimit('This run exceeded its artifact storage limit.', 'run_storage_limit')
        if self.status()['bytes'] > self.workspace_bytes:
            raise StorageLimit()


class ConversationIndex:
    """Additive metadata index; unchanged conversations are never JSON-decoded."""
    def __init__(self, root):
        self.root = _root(root)
        self.directory = _safe_path(self.root, self.root/'conversations')
        cache = _safe_path(self.root, self.root/'.cache')
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache.chmod(0o700)
        self.path = _safe_path(self.root, cache/'conversation-index.sqlite3')
        self.maximum = _positive('NHIS_STORAGE_INDEX_MAX_FILES', 50_000)
        self.json_limit = _positive('NHIS_STORAGE_CONVERSATION_BYTES', 8*1024*1024)
        with self._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS conversation_index_schema(version INTEGER NOT NULL);
                INSERT INTO conversation_index_schema SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM conversation_index_schema);
                CREATE TABLE IF NOT EXISTS conversation_summaries(
                    id TEXT PRIMARY KEY,title TEXT,created_at TEXT,updated_at TEXT,last_run_id TEXT,
                    message_count INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,size INTEGER NOT NULL,inode INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS conversation_summary_order ON conversation_summaries(updated_at DESC,id DESC);''')
            if db.execute('SELECT version FROM conversation_index_schema').fetchone()[0] != 1:
                raise ValueError('Unsupported conversation index schema version.')
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _summary(conversation):
        if (not isinstance(conversation, dict) or not isinstance(conversation.get('id'), str)
                or not _CID.fullmatch(conversation['id']) or not isinstance(conversation.get('messages'), list)):
            raise ValueError('Invalid saved conversation format.')
        fields = {name: conversation.get(name) if isinstance(conversation.get(name), str) else ''
                  for name in ('id', 'title', 'created_at', 'updated_at')}
        fields['title'] = fields['title'][:200]
        fields['created_at'], fields['updated_at'] = fields['created_at'][:64], fields['updated_at'][:64]
        fields['last_run_id'] = conversation.get('last_run_id') if isinstance(conversation.get('last_run_id'), str) and _RUN.fullmatch(conversation['last_run_id']) else None
        fields['message_count'] = len(conversation['messages'])
        return fields

    @staticmethod
    def _put(db, summary, info):
        db.execute('''INSERT OR REPLACE INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?)''',
                   tuple(summary[name] for name in ('id','title','created_at','updated_at','last_run_id','message_count')) + (info.st_mtime_ns,info.st_size,info.st_ino))

    def record(self, conversation, path):
        summary = self._summary(conversation)
        path = _safe_path(self.root, path)
        if path != self.directory/(summary['id']+'.json'):
            raise ValueError('Conversation index paths cannot leave their workspace.')
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('The saved conversation must be a regular file.')
        with self._db() as db:
            self._put(db, summary, info)

    def remove(self, ids):
        if not isinstance(ids, (list, tuple)) or len(ids) > self.maximum or any(not isinstance(item,str) or not _CID.fullmatch(item) for item in ids):
            raise ValueError('Invalid conversation index IDs.')
        with self._db() as db:
            db.executemany('DELETE FROM conversation_summaries WHERE id=?', [(item,) for item in ids])

    def ensure(self):
        inspected = changed = skipped = 0
        seen = set()
        if self.directory.is_symlink():
            raise ValueError('The conversation directory cannot be a symbolic link.')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = {row['id']: row for row in db.execute('SELECT id,mtime_ns,size,inode FROM conversation_summaries')}
            if self.directory.exists():
                with os.scandir(self.directory) as entries:
                    for entry in entries:
                        if not entry.name.endswith('.json') or not _CID.fullmatch(entry.name[:-5]):
                            continue
                        inspected += 1
                        if inspected > self.maximum:
                            raise StorageLimit('Conversation indexing exceeded its bounded file scan.', 'storage_scan_limit')
                        if not entry.is_file(follow_symlinks=False):
                            skipped += 1
                            continue
                        try:info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:continue
                        cid = entry.name[:-5]
                        seen.add(cid)
                        old = prior.get(cid)
                        if old and (old['mtime_ns'],old['size'],old['inode']) == (info.st_mtime_ns,info.st_size,info.st_ino):
                            continue
                        try:
                            summary = self._summary(_read_json(Path(entry.path), self.json_limit))
                            if summary['id'] != cid:
                                raise ValueError('Conversation filename and ID differ.')
                            self._put(db, summary, info)
                            changed += 1
                        except (OSError, ValueError, UnicodeError, RecursionError):
                            db.execute('DELETE FROM conversation_summaries WHERE id=?', (cid,))
                            skipped += 1
            db.executemany('DELETE FROM conversation_summaries WHERE id=?', [(cid,) for cid in prior if cid not in seen])
        return {'inspected': inspected, 'updated': changed, 'skipped': skipped}

    def page(self, limit=30, cursor=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError('The conversation page size must be 1–200.')
        self.ensure()
        where, args = '', []
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
                    raise ValueError()
                value = json.loads(base64.b64decode(cursor + '='*((-len(cursor))%4), altchars=b'-_', validate=True))
                if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0],str) or len(value[0])>64 or not isinstance(value[1],str) or not _CID.fullmatch(value[1]):
                    raise ValueError()
                where, args = ' WHERE updated_at<? OR (updated_at=? AND id<?)', [value[0],value[0],value[1]]
            except (ValueError, UnicodeError, TypeError):
                raise ValueError('Invalid conversation page cursor.') from None
        with self._db() as db:
            rows = [dict(row) for row in db.execute('SELECT id,title,created_at,updated_at,last_run_id,message_count FROM conversation_summaries'+where+' ORDER BY updated_at DESC,id DESC LIMIT ?', (*args,limit+1))]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last['updated_at'],last['id']],separators=(',',':')).encode()).decode().rstrip('=')
        return {'conversations': rows[:limit], 'next_cursor': next_cursor}

    def all_summaries(self):
        self.ensure()
        with self._db() as db:
            return [dict(row) for row in db.execute('SELECT id,title,created_at,updated_at,last_run_id,message_count FROM conversation_summaries ORDER BY updated_at DESC,id DESC LIMIT ?', (self.maximum,))]


def _backup_name(name):
    path = PurePosixPath(name)
    parts = path.parts
    if path.is_absolute() or not parts or '..' in parts or str(path) != name or '\\' in name:
        return False
    if any(part in _EXCLUDED or part.startswith('.env') for part in parts):
        return False
    if any(part.lower() in ('adult24.csv','adultinc24.csv','adult24.zip','adultinc24.zip') for part in parts):
        return False
    if parts[0] == 'visitors':
        if len(parts) < 4 or not _CID.fullmatch(parts[1]):
            return False
        parts = parts[2:]
    if parts[0] not in _MANAGED or len(parts) < 2:
        return False
    if parts[0] == 'conversations':
        return len(parts) == 2 and bool(re.fullmatch(r'[a-f0-9]{32}\.json', parts[1]))
    if parts[0] == 'runs':
        return len(parts) >= 3 and bool(_RUN.fullmatch(parts[1])) and not parts[-1].endswith(('.tmp','.lock'))
    return not parts[-1].endswith(('.lock','.tmp','-wal','-shm','-journal'))


def _state_files(root):
    roots = [root]
    visitors = root/'visitors'
    if visitors.is_symlink():
        raise ValueError('The visitor state directory cannot be a symbolic link.')
    if visitors.exists():
        with os.scandir(visitors) as entries:
            for entry in entries:
                if _CID.fullmatch(entry.name):
                    if not entry.is_dir(follow_symlinks=False):
                        raise ValueError('Visitor workspaces must be regular directories.')
                    roots.append(Path(entry.path))
                    if len(roots) > _MAX_SCAN:
                        raise StorageLimit('The state backup exceeded its workspace limit.', 'storage_scan_limit')
    count = 0
    for workspace in roots:
        for name in _MANAGED:
            for path, info in _files(workspace/name):
                relative = path.relative_to(root).as_posix()
                if _backup_name(relative):
                    count += 1
                    if count > _MAX_SCAN:
                        raise StorageLimit('The state backup exceeded its file limit.', 'storage_scan_limit')
                    yield path, relative, info


def _sqlite_snapshot(source, destination):
    source_db = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=5)
    target_db = sqlite3.connect(str(destination), timeout=5)
    deadline = time.monotonic()+30
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('SQLite snapshot exceeded its time limit.')
    try:
        source_db.backup(target_db, pages=128, progress=progress, sleep=.01)
        # Each archive member is self-contained; do not depend on new sidecars.
        target_db.execute('PRAGMA journal_mode=DELETE')
        if target_db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('A SQLite backup did not pass its integrity check.')
    finally:
        target_db.close()
        source_db.close()
    destination.chmod(0o600)


def create_backup(root, destination):
    """Publish a new archive atomically, using SQLite backup rather than WAL copies."""
    root, destination = _root(root), Path(destination).absolute()
    if not root.is_dir():raise FileNotFoundError('The source state directory does not exist.')
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('The backup destination must be a new file.')
    destination=destination.parent.resolve()/destination.name
    try:
        relative = destination.relative_to(root)
    except ValueError:
        relative = None
    if relative and relative.parts[0] in (*_MANAGED,'visitors'):
        raise ValueError('Store backups outside the managed state directories.')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    maximum = _positive('NHIS_STORAGE_BACKUP_BYTES', 1024*1024*1024)
    with tempfile.TemporaryDirectory(prefix='.state-backup-', dir=str(destination.parent)) as temporary:
        temporary = Path(temporary)
        temporary.chmod(0o700)
        metadata, total = [], 0
        for number, (source, relative, info) in enumerate(_state_files(root)):
            copied = temporary/('file-'+str(number))
            is_database = source.suffix in ('.sqlite3','.sqlite','.db')
            if is_database:
                _sqlite_snapshot(source, copied)
            else:
                descriptor = os.open(str(source), os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
                try:
                    before = os.fstat(descriptor)
                    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum-total:
                        raise ValueError('A backup source is not regular or exceeds the archive limit.')
                    with os.fdopen(descriptor,'rb',closefd=False) as incoming, copied.open('xb') as outgoing:
                        copied_bytes=0
                        while True:
                            chunk=incoming.read(min(1024*1024,maximum-total-copied_bytes+1))
                            if not chunk:break
                            copied_bytes+=len(chunk)
                            if copied_bytes>maximum-total:raise StorageLimit('The backup source exceeded its byte limit.','backup_storage_limit')
                            outgoing.write(chunk)
                    after = os.fstat(descriptor)
                    if (before.st_mtime_ns,before.st_size) != (after.st_mtime_ns,after.st_size):
                        raise ValueError('State changed during backup. Pause writes and try again.')
                finally:
                    os.close(descriptor)
                copied.chmod(0o600)
            size = copied.stat().st_size
            total += size
            if total > maximum:
                raise StorageLimit('The backup exceeds its configured byte limit.', 'backup_storage_limit')
            checksum = hashlib.sha256()
            with copied.open('rb') as content:
                for chunk in iter(lambda:content.read(1024*1024),b''):checksum.update(chunk)
            metadata.append({'path':relative,'bytes':size,'sha256':checksum.hexdigest(),'sqlite':is_database})
        manifest = {'format':'nhis-agent-state','schema_version':1,
                    'created_at':datetime.now(timezone.utc).isoformat(), 'files':metadata,
                    'warnings':['Official survey data, credentials, application code and runtime environments are excluded.',
                                'SQLite databases are consistent online snapshots, including committed WAL data.',
                                'Pause application writes for a coordinated filesystem and database recovery point.',
                                'Restore to a separate new directory and verify it before changing the live state path.']}
        manifest_bytes = json.dumps(manifest,sort_keys=True,allow_nan=False).encode()
        if len(manifest_bytes) > 16*1024*1024:
            raise ValueError('The backup manifest exceeds its limit.')
        archive = temporary/'archive.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:
            header = tarfile.TarInfo('manifest.json');header.size=len(manifest_bytes);header.mode=0o600
            tar.addfile(header,io.BytesIO(manifest_bytes))
            for number, item in enumerate(metadata):
                header = tarfile.TarInfo(item['path']);header.size=item['bytes'];header.mode=0o600
                with (temporary/('file-'+str(number))).open('rb') as content:tar.addfile(header,content)
        archive.chmod(0o600)
        # Hard-link publication fails rather than overwriting a concurrent file.
        os.link(str(archive),str(destination))
    return {'archive':str(destination),'file_count':len(metadata),'bytes':total,'schema_version':1,'warnings':manifest['warnings']}


def validate_state(root):
    """Check an independent copy without loading survey rows or credentials."""
    root = _root(root)
    if not root.is_dir():raise FileNotFoundError('The recovery state directory does not exist.')
    files = databases = conversations = 0
    warnings = []
    for path, relative, info in _state_files(root):
        files += 1
        if path.suffix in ('.sqlite3','.sqlite','.db'):
            db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)
            try:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('A restored SQLite database failed integrity checks.')
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'execution_schema' in tables:
                    versions = db.execute('SELECT version FROM execution_schema').fetchall()
                    if any(row[0] != 1 for row in versions):
                        raise ValueError('The restored durable job schema requires an unsupported migration.')
                elif path.name == 'public-access.sqlite3':
                    warnings.append('Legacy admission state has no durable job schema; startup must apply the additive migration.')
                if 'conversation_index_schema' in tables and db.execute('SELECT version FROM conversation_index_schema').fetchone()[0] != 1:
                    raise ValueError('The conversation index schema requires an unsupported migration.')
                if 'history_schema' in tables and db.execute('SELECT version FROM history_schema').fetchone()[0] != 1:
                    raise ValueError('The run-history index schema requires an unsupported migration.')
                databases += 1
            finally:db.close()
        elif path.parent.name == 'conversations':
            value = _read_json(path,_positive('NHIS_STORAGE_CONVERSATION_BYTES',8*1024*1024))
            summary = ConversationIndex._summary(value)
            if summary['id'] != path.stem:raise ValueError('A restored conversation ID does not match its filename.')
            conversations += 1
        elif path.name in ('result.json','registration.json'):
            value = _read_json(path,_positive('NHIS_STORAGE_RUN_BYTES',16*1024*1024))
            if not isinstance(value,dict):raise ValueError('A restored run record must be an object.')
            if path.name == 'result.json' and value.get('run_id') != path.parent.name:
                raise ValueError('A restored run ID does not match its directory.')
    return {'valid':True,'files':files,'databases':databases,'conversations':conversations,
            'schema_version':1,'warnings':list(dict.fromkeys(warnings))}


def restore_backup(archive, destination):
    """Validate and atomically publish a separate copy; never overwrite live state."""
    archive, destination = Path(archive), Path(destination).absolute()
    if archive.is_symlink() or not archive.is_file():raise ValueError('The backup must be a regular archive file.')
    if destination.exists() or destination.is_symlink():raise FileExistsError('Restore requires a new, absent destination directory.')
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    maximum = _positive('NHIS_STORAGE_BACKUP_BYTES',1024*1024*1024)
    staging = Path(tempfile.mkdtemp(prefix='.'+destination.name+'.restore-',dir=str(destination.parent)))
    staging.chmod(0o700)
    try:
        descriptor = os.open(str(archive),os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(descriptor,'rb') as source, tarfile.open(fileobj=source,mode='r|gz') as tar:
            members=iter(tar)
            first = next(members,None)
            if first is None or first.name!='manifest.json' or not first.isfile() or first.size>16*1024*1024:
                raise ValueError('The archive must begin with a bounded state manifest.')
            manifest = json.loads(tar.extractfile(first).read().decode('utf-8'))
            if not isinstance(manifest,dict) or manifest.get('format')!='nhis-agent-state' or type(manifest.get('schema_version')) is not int or manifest['schema_version']!=1 or not isinstance(manifest.get('files'),list) or len(manifest['files'])>_MAX_SCAN:
                raise ValueError('The backup manifest or schema version is unsupported.')
            expected, total = {}, 0
            for item in manifest['files']:
                if (not isinstance(item,dict) or not isinstance(item.get('path'),str) or not _backup_name(item['path'])
                        or item['path'] in expected or type(item.get('bytes')) is not int or item['bytes']<0
                        or not isinstance(item.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',item['sha256'])):
                    raise ValueError('The manifest contains an unsafe or duplicate state path.')
                total += item['bytes']
                if total>maximum:raise StorageLimit('The restored archive exceeds its byte limit.','backup_storage_limit')
                expected[item['path']]=item
            seen=set()
            for member in members:
                if not member.isfile() or member.name not in expected or member.name in seen or member.size!=expected[member.name]['bytes']:
                    raise ValueError('The archive contains an unexpected, duplicate or nonregular member.')
                seen.add(member.name)
                target=_safe_path(staging,staging/member.name)
                target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                for directory in (target.parent,*target.parent.parents):
                    if directory==staging:break
                    directory.chmod(0o700)
                checksum=hashlib.sha256(); remaining=member.size
                with tar.extractfile(member) as incoming,target.open('xb') as outgoing:
                    while remaining:
                        chunk=incoming.read(min(1024*1024,remaining))
                        if not chunk:raise ValueError('The archive member is truncated.')
                        remaining-=len(chunk);checksum.update(chunk);outgoing.write(chunk)
                target.chmod(0o600)
                if checksum.hexdigest()!=expected[member.name]['sha256']:raise ValueError('The restored file checksum does not match the manifest.')
            if seen!=set(expected):raise ValueError('The archive is missing a required state file.')
        validated=validate_state(staging)
        if destination.exists() or destination.is_symlink():raise FileExistsError('Restore destination appeared while validation was running.')
        os.rename(str(staging),str(destination))
        return {**validated,'destination':str(destination),'warnings':validated['warnings']+[
            'This is an independent recovery copy. It has not replaced or started the live application.',
            'Official survey data, credentials and R/Python environments must be configured separately.']}
    finally:
        if staging.exists():shutil.rmtree(staging)


def _references(value):
    found, stack, inspected = set(), [value], 0
    while stack:
        item=stack.pop();inspected+=1
        if inspected>100_000:raise ValueError('A state reference scan exceeded its bound.')
        if isinstance(item,str) and _RUN.fullmatch(item):found.add(item)
        elif isinstance(item,dict):stack.extend(item.values())
        elif isinstance(item,list):stack.extend(item)
    return found


def _workspace_signature(path):
    """Metadata fingerprint, rejecting credentials and inputs before tree removal."""
    digest=hashlib.sha256(); count=total=0; newest=path.stat().st_mtime
    for base,directories,files in os.walk(path,followlinks=False):
        for name in directories+files:
            child=Path(base)/name
            if child.is_symlink() or name in _EXCLUDED or name.startswith('.env') or name.lower() in ('adult24.csv','adultinc24.csv','adult24.zip','adultinc24.zip'):
                raise ValueError('Protected inputs or symbolic links are present in this workspace.')
        if Path(base)==path and any(name not in _MANAGED for name in directories+files):
            raise ValueError('Unrecognized files are present in this workspace.')
        for name in sorted(files):
            if name.endswith('.lock'):continue
            child=Path(base)/name;info=child.lstat()
            if not stat.S_ISREG(info.st_mode):raise ValueError('Nonregular state cannot be removed automatically.')
            count+=1;total+=info.st_size;newest=max(newest,info.st_mtime)
            if count>_MAX_SCAN:raise StorageLimit('Retention exceeded its scan bound.','storage_scan_limit')
            digest.update((child.relative_to(path).as_posix()+':'+str(info.st_ino)+':'+str(info.st_size)+':'+str(info.st_mtime_ns)+'\n').encode())
    return {'signature':digest.hexdigest(),'files':count,'bytes':total,'newest':newest}


def _job_rows(root):
    path=_safe_path(root,root/'.cache'/'public-access.sqlite3')
    if not path.exists():return [],set()
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5);db.row_factory=sqlite3.Row
    try:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'jobs' not in tables:return [],set()
        rows=[dict(row) for row in db.execute('SELECT job_id,workspace_id,conversation_id,state,updated_at,completed_at FROM jobs LIMIT ?',(_MAX_SCAN+1,))]
        if len(rows)>_MAX_SCAN:raise StorageLimit('Retention job scan exceeded its bound.','storage_scan_limit')
        external={row[0] for row in db.execute('SELECT DISTINCT job_id FROM external_attempts')} if 'external_attempts' in tables else set()
        return rows,external
    finally:db.close()


def plan_retention(root, *, now=None):
    """Return metadata-only candidates. Audit evidence is archive-only, never deleted."""
    root=_root(root);now=time.time() if now is None else now
    ttl={'anonymous_days':_positive('NHIS_RETENTION_ANONYMOUS_DAYS',90),
         'completed_jobs_days':_positive('NHIS_RETENTION_COMPLETED_JOBS_DAYS',30),
         'cache_days':_positive('NHIS_RETENTION_CACHE_DAYS',30),
         'audit_days':_positive('NHIS_RETENTION_AUDIT_DAYS',365)}
    jobs,external=_job_rows(root)
    active={row['workspace_id'] for row in jobs if row['state'] in ('queued','running')}
    workspaces=[root];visitors=root/'visitors'
    if visitors.is_symlink():raise ValueError('Visitor state cannot be a symbolic link.')
    if visitors.exists():
        for entry in visitors.iterdir():
            if _CID.fullmatch(entry.name):
                if entry.is_symlink() or not entry.is_dir():raise ValueError('Visitor state must be a regular directory.')
                workspaces.append(entry)
                if len(workspaces)>_MAX_SCAN:raise StorageLimit('Retention workspace scan exceeded its bound.','storage_scan_limit')
    actions=[];protected=[];archive=[]
    for workspace in workspaces:
        wid=hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
        relative=workspace.relative_to(root).as_posix()
        if wid in active:
            protected.append({'path':relative,'reason':'active_jobs'});continue
        refs=set();audit=False;invalid=False
        conversations=workspace/'conversations'
        if conversations.is_symlink():raise ValueError('Conversation state cannot be a symbolic link.')
        if conversations.exists():
            scanned=0
            for path in conversations.glob('*.json'):
                if not _CID.fullmatch(path.stem):continue
                scanned+=1
                if scanned>_MAX_SCAN:raise StorageLimit('Retention conversation scan exceeded its bound.','storage_scan_limit')
                try:refs.update(_references(_read_json(path,8*1024*1024)))
                except (OSError,ValueError,RecursionError):invalid=True
        runs=workspace/'runs'
        if runs.is_symlink():raise ValueError('Run state cannot be a symbolic link.')
        if runs.exists():
            scanned=0
            for directory in runs.iterdir():
                if not _RUN.fullmatch(directory.name):continue
                scanned+=1
                if scanned>_MAX_SCAN:raise StorageLimit('Retention run scan exceeded its bound.','storage_scan_limit')
                if directory.is_symlink() or not directory.is_dir():raise ValueError('Run state must be a regular directory.')
                try:
                    registration=_read_json(directory/'registration.json',16*1024*1024)
                    result=_read_json(directory/'result.json',16*1024*1024)
                    if not isinstance(registration,dict) or not isinstance(result,dict):raise ValueError('Invalid run metadata.')
                    refs.update(_references(result)-{directory.name})
                    measured=registration.get('measure_model') is True or bool((result.get('measurement') or {}).get('planned_calls')) or bool((result.get('measurement') or {}).get('calls'))
                    audit=audit or measured
                    if measured and max(path.stat().st_mtime for path,_ in _files(directory))<now-ttl['audit_days']*86400:
                        archive.append({'path':directory.relative_to(root).as_posix(),'reason':'expired_measurement_audit_archive_only'})
                except (OSError,ValueError,TypeError,AttributeError,RecursionError):invalid=True
        expired_workspace=False
        if workspace!=root:
            try:snapshot=_workspace_signature(workspace)
            except ValueError:
                protected.append({'path':relative,'reason':'unrecognized_or_protected_state'});continue
            if snapshot['newest']<now-ttl['anonymous_days']*86400:
                if audit or refs or invalid:
                    reason='measurement_audit' if audit else 'referenced_results' if refs else 'unreadable_state'
                    protected.append({'path':relative,'reason':reason})
                else:
                    actions.append({'kind':'delete_anonymous_workspace','path':relative,**snapshot})
                    expired_workspace=True
        if not expired_workspace:
            cache=workspace/'.cache'/'survey'
            for path,info in _files(cache):
                if path.parent==cache and re.fullmatch(r'[a-f0-9]{64}\.json',path.name) and info.st_mtime<now-ttl['cache_days']*86400:
                    actions.append({'kind':'delete_cache','path':path.relative_to(root).as_posix(),'bytes':info.st_size,'mtime_ns':info.st_mtime_ns})
    for row in jobs:
        if row['workspace_id'] in active:continue
        age=row['completed_at'] if row['completed_at'] is not None else row['updated_at']
        if row['state'] in ('succeeded','failed','blocked','cancelled') and age<now-ttl['completed_jobs_days']*86400:
            if row['job_id'] in external:
                protected.append({'job_id':row['job_id'],'reason':'external_attempt_audit_archive_only'})
            else:
                actions.append({'kind':'prune_job_payload','job_id':row['job_id'],'workspace_id':row['workspace_id'],'updated_at':row['updated_at']})
    return {'dry_run':True,'generated_at':datetime.fromtimestamp(now,timezone.utc).isoformat(),
            'ttl':ttl,'actions':actions,'protected':protected[:1000],
            'protected_count':len(protected),'archive_candidates':archive[:1000],
            'archive_candidate_count':len(archive),
            'warnings':['No deletion occurs without an explicit apply operation.',
                        'Pause application admission before applying retention. Changed or active state is skipped.',
                        'Measurement and external-attempt audit evidence requires an explicit archive workflow; it is never automatically deleted.',
                        'Completed job payloads may be pruned; idempotency tombstones remain.']}


def _delete_tree(path):
    descriptor=os.open(str(path),os.O_RDONLY | getattr(os,'O_DIRECTORY',0) | getattr(os,'O_NOFOLLOW',0))
    def remove(directory_fd):
        for name in os.listdir(directory_fd):
            if name in _EXCLUDED or name.startswith('.env') or name.lower() in ('adult24.csv','adultinc24.csv','adult24.zip','adultinc24.zip'):
                raise ValueError('Protected inputs cannot be removed automatically.')
            info=os.stat(name,dir_fd=directory_fd,follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child=os.open(name,os.O_RDONLY | getattr(os,'O_DIRECTORY',0) | getattr(os,'O_NOFOLLOW',0),dir_fd=directory_fd)
                try:remove(child)
                finally:os.close(child)
                os.rmdir(name,dir_fd=directory_fd)
            elif stat.S_ISREG(info.st_mode):os.unlink(name,dir_fd=directory_fd)
            else:raise ValueError('Nonregular state cannot be removed automatically.')
    try:remove(descriptor)
    finally:os.close(descriptor)
    path.rmdir()


@contextmanager
def _pause_admission(root):
    """Share the public/job admission write lock while rechecking a deletion."""
    path=_safe_path(root,root/'.cache'/'public-access.sqlite3')
    if not path.exists():
        yield
        return
    db=sqlite3.connect(str(path),timeout=5,isolation_level=None)
    try:
        db.execute('BEGIN IMMEDIATE')
        yield
    finally:
        db.rollback();db.close()


def apply_retention(root, plan):
    """Explicit operator mutation. Recheck every candidate, preserving changed state."""
    root=_root(root)
    if not isinstance(plan,dict) or not isinstance(plan.get('actions'),list):raise ValueError('A retention plan is required.')
    fresh=plan_retention(root)
    allowed={json.dumps(item,sort_keys=True) for item in fresh['actions']}
    applied=[];skipped=[]
    with StoragePolicy(root).write_guard():
        for action in plan['actions']:
            if json.dumps(action,sort_keys=True) not in allowed:
                skipped.append({'kind':action.get('kind'),'reason':'state_changed_or_protected'});continue
            kind=action['kind']
            if kind=='prune_job_payload':
                path=_safe_path(root,root/'.cache'/'public-access.sqlite3')
                db=sqlite3.connect(str(path),timeout=5);db.execute('PRAGMA foreign_keys=ON')
                try:
                    with db:
                        db.execute('BEGIN IMMEDIATE')
                        row=db.execute('SELECT state,updated_at,turn_id FROM jobs WHERE job_id=? AND workspace_id=?',(action['job_id'],action['workspace_id'])).fetchone()
                        if not row or row[0] in ('queued','running','unknown') or row[1]!=action['updated_at'] or db.execute('SELECT 1 FROM external_attempts WHERE job_id=?',(action['job_id'],)).fetchone():
                            skipped.append({'kind':kind,'reason':'state_changed_or_audit'});continue
                        db.execute('DELETE FROM job_events WHERE job_id=?',(action['job_id'],))
                        db.execute('DELETE FROM step_results WHERE job_id=?',(action['job_id'],))
                        db.execute('UPDATE jobs SET response_json=NULL WHERE job_id=?',(action['job_id'],))
                        db.execute("UPDATE turns SET request_json='{}' WHERE turn_id=?",(row[2],))
                finally:db.close()
            else:
                path=_safe_path(root,root/action['path'])
                workspace=root/'visitors'/path.relative_to(root).parts[1] if path.relative_to(root).parts[0]=='visitors' else root
                wid=hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
                with _pause_admission(root), StoragePolicy(workspace).write_guard() if workspace!=root else nullcontext():
                    jobs,_=_job_rows(root)
                    if any(row['workspace_id']==wid and row['state'] in ('queued','running') for row in jobs):
                        skipped.append({'kind':kind,'reason':'active_jobs'});continue
                    if kind=='delete_cache':
                        if path.exists() and path.lstat().st_mtime_ns==action['mtime_ns'] and path.stat().st_size==action['bytes']:path.unlink()
                        else:
                            skipped.append({'kind':kind,'reason':'state_changed'});continue
                    elif kind=='delete_anonymous_workspace':
                        if _workspace_signature(path)['signature']!=action['signature']:
                            skipped.append({'kind':kind,'reason':'state_changed'});continue
                        _delete_tree(path)
                    else:raise ValueError('Unknown retention action.')
            applied.append(action)
    return {'dry_run':False,'applied':applied,'applied_count':len(applied),'skipped':skipped,
            'skipped_count':len(skipped),'archive_candidates':fresh['archive_candidates']}
