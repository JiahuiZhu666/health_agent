"""Durable model-call reservations, not a provider invoice or usage estimator.

Token bounds and configurable planning prices reserve capacity before a request.
Only complete observed usage releases a reservation. Unknown delivery or missing
usage keeps it charged until an operator explicitly resolves the reservation.
SQLite BEGIN IMMEDIATE makes admission atomic across threads and processes.
"""
import hashlib
from datetime import datetime, timezone
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path


class BudgetExceeded(RuntimeError):
    def __init__(self, code='budget_exceeded'):
        self.code = code
        super().__init__('New model calls are disabled.' if code == 'kill_switch' else 'The reserved model budget would exceed a configured limit.')


class ModelBudget:
    """One shared state directory provides global limits across visitor workspaces."""
    DEFAULTS = {'turn_calls': 400, 'turn_input_tokens': 2_000_000,
                'turn_output_tokens': 100_000, 'turn_cost_micros': 2_500_000,
                'day_calls': 2_000, 'day_input_tokens': 10_000_000,
                'day_output_tokens': 1_000_000, 'day_cost_micros': 25_000_000}

    def __init__(self, state_dir, *, limits=None, input_usd_per_million=None,
                 output_usd_per_million=None, clock=None):
        self.clock = clock or time.time
        self.limits = {}
        for name, default in self.DEFAULTS.items():
            value = (limits or {}).get(name, os.environ.get('NHIS_BUDGET_' + name.upper(), default))
            if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 10**12:
                raise ValueError('Budget limits must be positive bounded integers.')
            self.limits[name] = int(value)
        self.input_rate = self._rate(input_usd_per_million, 'NHIS_BUDGET_INPUT_USD_PER_MILLION', '4')
        self.output_rate = self._rate(output_usd_per_million, 'NHIS_BUDGET_OUTPUT_USD_PER_MILLION', '20')
        directory = Path(state_dir) / '.cache'
        if directory.is_symlink():
            raise ValueError('The budget directory cannot be a symbolic link.')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / 'model_budget.sqlite3'
        if self.path.is_symlink():
            raise ValueError('The budget database cannot be a symbolic link.')
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO controls VALUES ('enabled',1);
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE,
                    turn_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    cost_micros INTEGER NOT NULL, calls INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX IF NOT EXISTS budget_turn ON reservations(turn_id);
                CREATE INDEX IF NOT EXISTS budget_created ON reservations(created);
            ''')
        self.path.chmod(0o600)

    @staticmethod
    def _rate(value, env, default):
        try:
            result = Decimal(str(value if value is not None else os.environ.get(env, default)))
            if not result.is_finite() or not 0 <= result <= 100_000:
                raise ValueError('Invalid planning price.')
            return result
        except (InvalidOperation, TypeError):
            raise ValueError('Planning prices must be finite nonnegative numbers.') from None

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=5000')
            yield db
        finally:
            db.close()

    def planning_cost(self, input_tokens, output_tokens):
        # USD per million tokens equals microUSD per token.
        return int((Decimal(input_tokens)*self.input_rate + Decimal(output_tokens)*self.output_rate).to_integral_value(rounding=ROUND_CEILING))

    @staticmethod
    def _count(value):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**12:
            raise ValueError('Token reservations must be bounded nonnegative integers.')
        return value

    @staticmethod
    def _identity(value):
        if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid budget scope identifier.')
        return value

    @staticmethod
    def _totals(db, where='', args=()):
        row = db.execute('SELECT COALESCE(SUM(calls),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_micros),0) FROM reservations ' + where, args).fetchone()
        return dict(zip(('calls', 'input_tokens', 'output_tokens', 'cost_micros'), row))

    def reserve(self, *, turn_id, workspace_id='local', input_tokens, output_tokens,
                attempt_id=None, estimated_cost_micros=None, paid=True):
        turn_id, workspace_id = self._identity(turn_id), self._identity(workspace_id)
        attempt_id = self._identity(attempt_id or uuid.uuid4().hex)
        incoming = {'calls': 1, 'input_tokens': self._count(input_tokens),
                    'output_tokens': self._count(output_tokens)}
        computed = self.planning_cost(input_tokens, output_tokens) if paid else 0
        # A caller cannot undercut the configured planning rates.
        incoming['cost_micros'] = max(computed, self._count(estimated_cost_micros)) if estimated_cost_micros is not None else computed
        now = self.clock()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT id FROM reservations WHERE attempt_id=?', (attempt_id,)).fetchone():
                db.rollback()
                raise BudgetExceeded('duplicate_attempt')
            if paid and not self._enabled(db):
                db.rollback()
                raise BudgetExceeded('kill_switch')
            turn = self._totals(db, 'WHERE turn_id=?', (turn_id,))
            day = self._totals(db, "WHERE created>? OR state IN ('reserved','held')", (now-86400,))
            for scope, totals in (('turn', turn), ('day', day)):
                for field, value in incoming.items():
                    if totals[field] + value > self.limits[scope + '_' + field]:
                        db.rollback()
                        raise BudgetExceeded()
            reservation_id = uuid.uuid4().hex
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (reservation_id, attempt_id, turn_id, workspace_id, now, now, 'reserved',
                        incoming['input_tokens'], incoming['output_tokens'], incoming['cost_micros'], 1))
            db.commit()
        return {'id': reservation_id, 'attempt_id': attempt_id, **incoming, 'state': 'reserved'}

    @staticmethod
    def _observed(usage):
        if not isinstance(usage, dict):
            return None
        source = usage.get('input_tokens', usage.get('prompt_tokens', usage.get('prompt_eval_count')))
        output = usage.get('output_tokens', usage.get('completion_tokens', usage.get('eval_count')))
        if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10**12 for v in (source, output)):
            return None
        return source, output

    def reconcile(self, reservation_id, usage=None, *, unknown=False, not_sent=False):
        """Idempotently finalize observed usage or keep unknown capacity reserved."""
        observed = self._observed(usage)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM reservations WHERE id=?', (reservation_id,)).fetchone()
            if row is None:
                db.rollback()
                raise ValueError('Unknown model budget reservation.')
            if row['state'] in ('reconciled', 'released'):
                db.commit()
                return dict(row)
            state = 'released' if not_sent else 'held' if unknown or observed is None else 'reconciled'
            counts = (0, 0, 0, 0) if not_sent else (1, observed[0], observed[1], self.planning_cost(*observed) if row['cost_micros'] else 0) if state == 'reconciled' else (row['calls'], row['input_tokens'], row['output_tokens'], row['cost_micros'])
            db.execute('UPDATE reservations SET state=?,calls=?,input_tokens=?,output_tokens=?,cost_micros=?,updated=? WHERE id=?',
                       (state, *counts, self.clock(), reservation_id))
            result = dict(db.execute('SELECT * FROM reservations WHERE id=?', (reservation_id,)).fetchone())
            db.commit()
        return result

    @staticmethod
    def _enabled(db):
        killed = os.environ.get('NHIS_MODEL_KILL_SWITCH', '').strip().lower() in ('1', 'true', 'yes', 'on')
        return not killed and bool(db.execute("SELECT value FROM controls WHERE key='enabled'").fetchone()[0])

    def set_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be a Boolean.')
        with self._db() as db:
            db.execute("UPDATE controls SET value=? WHERE key='enabled'", (int(enabled),))
        return self.status()

    def status(self, turn_id=None):
        with self._db() as db:
            day = self._totals(db, "WHERE created>? OR state IN ('reserved','held')", (self.clock()-86400,))
            result = {'enabled': self._enabled(db), 'limits': dict(self.limits), 'day': day,
                      'held_reservations': db.execute("SELECT COUNT(*) FROM reservations WHERE state IN ('reserved','held')").fetchone()[0],
                      'planning_rates_usd_per_million': {'input': str(self.input_rate), 'output': str(self.output_rate)},
                      'cost_basis': 'Configured planning rates; not a provider invoice. Unknown usage remains reserved.'}
            if turn_id is not None:
                result['turn'] = self._totals(db, 'WHERE turn_id=?', (self._identity(turn_id),))
        return result

    def reservations(self, state='held', limit=100):
        """Bounded operator view, with no requests, responses or credentials."""
        if state not in ('held', 'reserved', 'reconciled', 'released', 'all'):
            raise ValueError('Invalid reservation state.')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('Invalid reservation read limit.')
        query = 'SELECT * FROM reservations'
        args = []
        if state != 'all':
            query += ' WHERE state=?'
            args.append(state)
        query += ' ORDER BY created DESC,id LIMIT ?'
        args.append(limit)
        with self._db() as db:
            result = []
            for row in db.execute(query, args):
                result.append({key: row[key] for key in ('id', 'attempt_id', 'turn_id', 'state',
                               'calls', 'input_tokens', 'output_tokens', 'cost_micros')})
                result[-1].update(
                    workspace_hash=hashlib.sha256(row['workspace_id'].encode()).hexdigest()[:24],
                    created_at=datetime.fromtimestamp(row['created'], timezone.utc).isoformat(),
                    updated_at=datetime.fromtimestamp(row['updated'], timezone.utc).isoformat(),
                    cost_basis='reserved_bound' if row['state'] in ('reserved', 'held') else 'observed_usage_at_planning_rates' if row['state'] == 'reconciled' else 'released_not_sent')
        return result
