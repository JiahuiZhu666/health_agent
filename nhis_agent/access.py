"""Signed anonymous workspace cookies and durable, atomic public-use limits.

No unsigned request identifier is accepted as a visitor cookie. Only accepted
admissions enter SQLite; model prompts, responses, credentials and raw cookies
are never stored. Admission callbacks must be quick and signal rejection by
raising an exception, rather than performing a model request synchronously.
"""
import hashlib
import hmac
from contextlib import closing
from http.cookies import CookieError, SimpleCookie
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

_VISITOR = re.compile(r'[a-f0-9]{32}')
_SIGNATURE = re.compile(r'[a-f0-9]{64}')
_TTL = 30 * 24 * 60 * 60
_HOUR = 3600
_DAY = 86400
_DOMAIN = b'nhis-public-visitor-v1\x00'


class PublicAccess:
    def __init__(self, secret, state_dir=None, *, secure=True,
                 messages_per_hour=None, messages_per_day=None,
                 chats_per_hour=None, chats_per_day=None, clock=None):
        if not isinstance(secret,(str,bytes)) or len(secret.strip()) < 32:
            raise ValueError('The public access signing secret must contain at least 32 characters.')
        if type(secure) is not bool:
            raise ValueError('The cookie secure option must be Boolean.')
        self._secret = secret.encode('utf-8') if isinstance(secret,str) else secret
        self._clock = clock or time.time
        self.secure = secure
        self.cookie_name = '__Host-nhis_visitor' if secure else 'nhis_visitor'
        self.limits = {
            'message':(
                self._limit(messages_per_hour,'NHIS_PUBLIC_MESSAGES_PER_HOUR',20),
                self._limit(messages_per_day,'NHIS_PUBLIC_MESSAGES_PER_DAY',200)),
            'create':(
                self._limit(chats_per_hour,'NHIS_PUBLIC_CHATS_PER_HOUR',10),
                self._limit(chats_per_day,'NHIS_PUBLIC_CHATS_PER_DAY',200)),
        }
        root = Path(state_dir or os.environ.get('NHIS_STATE_DIR') or Path(__file__).resolve().parents[1]).expanduser().resolve()
        directory = root / '.cache'
        if directory.is_symlink():
            raise ValueError('The public access state directory must not be a symbolic link.')
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.db_path = directory / 'public-access.sqlite3'
        if self.db_path.is_symlink():
            raise ValueError('The public access database must not be a symbolic link.')
        with closing(self._connect()) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS accepted_events (
                    id INTEGER PRIMARY KEY,
                    visitor TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('message','create')),
                    accepted_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS public_kind_time
                    ON accepted_events(kind,accepted_at);
                CREATE INDEX IF NOT EXISTS public_visitor_kind_time
                    ON accepted_events(visitor,kind,accepted_at);
            ''')
        self.db_path.chmod(0o600)

    @staticmethod
    def _limit(value, variable, default):
        if value is None:
            value = os.environ.get(variable,str(default))
        if isinstance(value,bool):
            raise ValueError('Public request limits must be positive integers.')
        try:
            number = int(value)
        except (ValueError,TypeError,OverflowError):
            raise ValueError('Public request limits must be positive integers.') from None
        if str(number) != str(value).strip() or not 1 <= number <= 100_000:
            raise ValueError('Public request limits must be integers from 1 to 100000.')
        return number

    def _connect(self):
        connection = sqlite3.connect(str(self.db_path),timeout=2,isolation_level=None)
        connection.execute('PRAGMA busy_timeout=2000')
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _now(self):
        value = self._clock()
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value < 0:
            raise ValueError('The public access clock returned an invalid timestamp.')
        return float(value)

    def _sign(self, payload):
        return hmac.new(self._secret,_DOMAIN + payload.encode('ascii'),hashlib.sha256).hexdigest()

    def visitor(self, headers_cookie):
        """Return a verified visitor id, issuing a random signed cookie if needed."""
        now = self._now()
        supplied = None
        if (isinstance(headers_cookie,str) and len(headers_cookie) <= 8192
                and len(re.findall(r'(?:^|;)\s*'+re.escape(self.cookie_name)+r'\s*=',headers_cookie)) <= 1):
            try:
                cookies = SimpleCookie()
                cookies.load(headers_cookie)
                if self.cookie_name in cookies:
                    supplied = cookies[self.cookie_name].value
            except CookieError:
                pass
        if isinstance(supplied,str):
            parts = supplied.split('.')
            if (len(parts)==4 and parts[0]=='v1' and _VISITOR.fullmatch(parts[1])
                    and re.fullmatch(r'[0-9]{1,12}',parts[2]) and _SIGNATURE.fullmatch(parts[3])):
                payload = '.'.join(parts[:3])
                issued = int(parts[2])
                if 0 <= now-issued < _TTL and hmac.compare_digest(parts[3],self._sign(payload)):
                    return parts[1],None
        visitor_id = secrets.token_hex(16)
        payload = 'v1.' + visitor_id + '.' + str(int(now))
        cookies = SimpleCookie()
        cookies[self.cookie_name] = payload + '.' + self._sign(payload)
        cookie = cookies[self.cookie_name]
        cookie['path'] = '/'
        cookie['max-age'] = _TTL
        cookie['httponly'] = True
        cookie['samesite'] = 'Lax'
        if self.secure:
            cookie['secure'] = True
        return visitor_id,cookie.OutputString()

    def reserve(self, visitor_id, kind='message', accept=None, *, lookup=None, accept_transaction=None):
        """Atomically check visitor/hour + global/day limits and charge admission.

        ``messages_per_day`` and ``chats_per_day`` are global limits, shared by
        every signed visitor and every PublicAccess instance using this state.
        A successful callback return is exposed as ``value``. An exception rolls
        the transaction back and is re-raised, leaving no quota charge.
        """
        if not isinstance(visitor_id,str) or not _VISITOR.fullmatch(visitor_id):
            raise ValueError('A verified visitor identifier is required.')
        if kind not in self.limits:
            raise ValueError('The public reservation kind must be message or create.')
        if accept is not None and not callable(accept):
            raise ValueError('The admission callback must be callable.')
        if lookup is not None and not callable(lookup):
            raise ValueError('The duplicate lookup must be callable.')
        if accept_transaction is not None and not callable(accept_transaction):
            raise ValueError('The transactional admission callback must be callable.')
        now = self._now()
        visitor_limit,global_limit = self.limits[kind]
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            if lookup is not None:
                original = lookup(connection)
                if original is not None:
                    connection.commit()
                    return {'allowed':True,'retry_after':0,'status':'accepted','idempotent':True,'value':original}
            connection.execute('DELETE FROM accepted_events WHERE accepted_at <= ?', (now-_DAY,))
            visitor_rows = connection.execute(
                'SELECT accepted_at FROM accepted_events WHERE visitor=? AND kind=? AND accepted_at>? ORDER BY accepted_at',
                (visitor_id,kind,now-_HOUR)).fetchall()
            global_rows = connection.execute(
                'SELECT accepted_at FROM accepted_events WHERE kind=? AND accepted_at>? ORDER BY accepted_at',
                (kind,now-_DAY)).fetchall()
            waits = []
            for rows,limit,window in ((visitor_rows,visitor_limit,_HOUR),(global_rows,global_limit,_DAY)):
                if len(rows) >= limit:
                    # Lowered configuration limits may require more than the
                    # oldest event to expire before a request can be admitted.
                    waits.append(max(1,math.ceil(rows[len(rows)-limit][0]+window-now)))
            if waits:
                connection.commit()  # Keep expiry cleanup even when denied.
                return {'allowed':False,'retry_after':max(waits),'status':'rate_limited',
                        'message':'Too many requests. Try again after the indicated delay.'}
            connection.execute('INSERT INTO accepted_events(visitor,kind,accepted_at) VALUES (?,?,?)',
                               (visitor_id,kind,now))
            value = accept_transaction(connection) if accept_transaction is not None else accept() if accept is not None else None
            connection.commit()
            result = {'allowed':True,'retry_after':0,'status':'accepted'}
            if accept is not None or accept_transaction is not None:
                result['value'] = value
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
