"""Bounded metadata-only operational events; never prompts or HTTP bodies."""
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import traceback
from pathlib import Path


_TEXT = {'trace_id', 'turn_id', 'job_id', 'run_id', 'attempt_id', 'reservation_id',
         'purpose', 'provider', 'model', 'response_id', 'request_id', 'status',
         'error_code', 'delivery_state', 'exception_type', 'stage'}
_COUNTS = {'elapsed_ms', 'http_status', 'input_tokens', 'output_tokens', 'total_tokens',
           'prompt_tokens', 'completion_tokens', 'prompt_eval_count', 'eval_count',
           'reserved_input_tokens', 'reserved_output_tokens', 'reserved_cost_micros'}
_SECRET = re.compile(r'(?i)(?:sk-[a-z0-9_-]{8,}|bearer\s+\S+|api[_-]?key\s*[=:]\s*\S+)')
_FRAME_NAME = re.compile(r'[^A-Za-z0-9_.<>-]')


def safe_metadata(source):
    """A positive allowlist avoids logging new caller fields by accident."""
    result = {}
    if not isinstance(source, dict):
        return result
    for name in _TEXT:
        value = source.get(name)
        if isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value):
            result[name] = _SECRET.sub('[REDACTED]', value)
    for name in _COUNTS:
        value = source.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if math.isfinite(value) and 0 <= value <= 10**15:
                    result[name] = value
            except OverflowError:
                pass
    # Workspace IDs may be names/paths; telemetry only receives an opaque hash.
    workspace = source.get('workspace_id')
    if isinstance(workspace, str) and workspace:
        result['workspace_hash'] = hashlib.sha256(workspace.encode()).hexdigest()[:24]
    usage = source.get('usage')
    if isinstance(usage, dict):
        result.update({name: value for name, value in safe_metadata(usage).items() if name in _COUNTS})
    frames = source.get('stack')
    if isinstance(frames, list):
        safe_frames = []
        for frame in frames[-12:]:
            if not isinstance(frame, dict):
                continue
            filename, function, line = frame.get('file'), frame.get('function'), frame.get('line')
            if (isinstance(filename, str) and isinstance(function, str) and isinstance(line, int)
                    and not isinstance(line, bool) and 0 < line < 10**8):
                safe_frames.append({'file': _FRAME_NAME.sub('_', _SECRET.sub('[REDACTED]', os.path.basename(filename)))[:128],
                                    'function': _FRAME_NAME.sub('_', _SECRET.sub('[REDACTED]', function))[:128], 'line': line})
        if safe_frames:
            result['stack'] = safe_frames
    return result


class Telemetry:
    def __init__(self, state_dir, *, max_events=20_000, ttl_days=30):
        if isinstance(max_events, bool) or not isinstance(max_events, int) or not 10 <= max_events <= 1_000_000:
            raise ValueError('Invalid telemetry retention limit.')
        if isinstance(ttl_days, bool) or not isinstance(ttl_days, int) or not 1 <= ttl_days <= 365:
            raise ValueError('Invalid telemetry retention duration.')
        directory = Path(state_dir) / '.cache'
        if directory.is_symlink():
            raise ValueError('The telemetry directory cannot be a symbolic link.')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / 'model_telemetry.sqlite3'
        if self.path.is_symlink():
            raise ValueError('The telemetry database cannot be a symbolic link.')
        self.max_events = max_events
        self.ttl_seconds = ttl_days * 86400
        db = self._db()
        try:
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, event TEXT NOT NULL, metadata TEXT NOT NULL)')
            db.commit()
        finally:
            db.close()
        self.path.chmod(0o600)

    def _db(self):
        return sqlite3.connect(str(self.path), timeout=5)

    def emit(self, event, metadata=None):
        if not isinstance(event, str) or not re.fullmatch(r'[a-z][a-z0-9_.]{0,63}', event):
            raise ValueError('Invalid telemetry event name.')
        safe = safe_metadata(metadata)
        db = self._db()
        try:
            with db:
                db.execute('INSERT INTO events(created,event,metadata) VALUES (?,?,?)',
                           (time.time(), event, json.dumps(safe, sort_keys=True, allow_nan=False)))
                db.execute('DELETE FROM events WHERE created<?', (time.time()-self.ttl_seconds,))
                db.execute('DELETE FROM events WHERE id <= COALESCE((SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?),-1)', (self.max_events,))
        finally:
            db.close()
        return safe

    def emit_error(self, event, exc, identifiers=None):
        return self.emit(event, {**(identifiers or {}), 'exception_type': type(exc).__name__,
                          'stack': [{'file': frame.filename, 'function': frame.name, 'line': frame.lineno}
                                    for frame in traceback.extract_tb(exc.__traceback__)[-12:]]})

    def recent(self, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('Invalid telemetry read limit.')
        db = self._db()
        try:
            return [{'created_at': row[0], 'event': row[1], **json.loads(row[2])}
                    for row in db.execute('SELECT created,event,metadata FROM events ORDER BY id DESC LIMIT ?', (limit,))]
        finally:
            db.close()


def log_error(state_dir, event, exc, identifiers=None):
    """Log exception location and identity only, never its message or locals."""
    return Telemetry(state_dir).emit_error(event, exc, identifiers)
