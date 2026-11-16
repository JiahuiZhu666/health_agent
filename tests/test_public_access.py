"""Signed-cookie and rolling-limit checks in temporary state only.

Test signing values are inert fixtures. No model calls, network requests, real
visitor workspaces, or deployment secrets are read or created by these tests.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.cookies import SimpleCookie
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from nhis_agent.access import PublicAccess


SIGNING_FIXTURE = 'UNIT_SIGNING_VALUE_NOT_REAL_12345678901234567890'


class Clock:
    def __init__(self):self.value=1_800_000_000.0
    def __call__(self):return self.value


class PublicAccessTests(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock=Clock()

    def access(self,**kwargs):
        return PublicAccess(SIGNING_FIXTURE,state_dir=self.folder.name,clock=self.clock,**kwargs)

    @staticmethod
    def cookie_header(set_cookie):
        return set_cookie.split(';',1)[0]

    def test_new_cookie_is_random_signed_private_and_secure_when_hosted(self):
        access=self.access()
        first,cookie=access.visitor(None)
        second,_=access.visitor(None)
        self.assertRegex(first,r'^[a-f0-9]{32}$')
        self.assertNotEqual(first,second)
        parsed=SimpleCookie();parsed.load(cookie)
        value=parsed[access.cookie_name]
        self.assertEqual(access.cookie_name,'__Host-nhis_visitor')
        self.assertTrue(value['secure']);self.assertTrue(value['httponly'])
        self.assertEqual(value['samesite'],'Lax')
        self.assertEqual(value['path'],'/')
        self.assertEqual(value['max-age'],str(30*86400))
        self.assertEqual(value['domain'],'')
        self.assertNotIn(SIGNING_FIXTURE,cookie)
        self.assertEqual(access.visitor(self.cookie_header(cookie)),(first,None))

    def test_local_cookie_explicitly_omits_secure_but_retains_other_attributes(self):
        access=self.access(secure=False)
        _,cookie=access.visitor(None)
        parsed=SimpleCookie();parsed.load(cookie)
        value=parsed[access.cookie_name]
        self.assertEqual(access.cookie_name,'nhis_visitor')
        self.assertFalse(value['secure']);self.assertTrue(value['httponly'])
        self.assertEqual(value['samesite'],'Lax')
        self.assertEqual(value['path'],'/')

    def test_unsigned_tampered_and_duplicate_visitor_cookies_are_replaced(self):
        access=self.access()
        original,cookie=access.visitor(None)
        header=self.cookie_header(cookie)
        payload=header.split('=',1)[1]
        parts=payload.split('.')
        altered=list(parts);altered[1]='a'*32 if original!='a'*32 else 'b'*32
        cases=[access.cookie_name+'='+original,
               access.cookie_name+'='+'.'.join(altered),
               access.cookie_name+'='+payload[:-1]+('0' if payload[-1]!='0' else '1'),
               header+'; '+header,
               access.cookie_name+'=unsigned; '+header,
               header+'; '+access.cookie_name+'=unsigned']
        for supplied in cases:
            with self.subTest(supplied_kind=len(supplied)):
                visitor,new_cookie=access.visitor(supplied)
                self.assertNotEqual(visitor,original)
                self.assertIsNotNone(new_cookie)

    def test_expired_future_or_other_secret_cookies_do_not_restore_a_workspace(self):
        access=self.access()
        original,cookie=access.visitor(None)
        self.clock.value += 30*86400
        visitor,replacement=access.visitor(self.cookie_header(cookie))
        self.assertNotEqual(visitor,original);self.assertIsNotNone(replacement)
        self.clock.value -= 30*86400+1
        visitor,replacement=access.visitor(self.cookie_header(cookie))
        self.assertNotEqual(visitor,original);self.assertIsNotNone(replacement)
        self.clock.value += 1
        other=PublicAccess('OTHER_UNIT_SIGNING_VALUE_12345678901234567890',
                           state_dir=self.folder.name,clock=self.clock)
        visitor,replacement=other.visitor(self.cookie_header(cookie))
        self.assertNotEqual(visitor,original);self.assertIsNotNone(replacement)

    def test_malformed_cookie_input_does_not_crash_or_allocate_rate_history(self):
        access=self.access()
        for supplied in (None,{},'x'*9000,'nhis_visitor="unterminated', 'not a cookie'):
            visitor,cookie=access.visitor(supplied)
            self.assertRegex(visitor,r'^[a-f0-9]{32}$')
            self.assertIsNotNone(cookie)
        with closing(sqlite3.connect(access.db_path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM accepted_events').fetchone()[0],0)

    def test_per_visitor_hourly_message_limit_and_exact_retry_after(self):
        access=self.access(messages_per_hour=2,messages_per_day=20)
        visitor,_=access.visitor(None)
        self.assertTrue(access.reserve(visitor)['allowed'])
        self.clock.value += 10
        self.assertTrue(access.reserve(visitor)['allowed'])
        self.clock.value += 20
        denied=access.reserve(visitor)
        self.assertFalse(denied['allowed']);self.assertEqual(denied['retry_after'],3570)
        other,_=access.visitor(None)
        self.assertTrue(access.reserve(other)['allowed'])
        self.clock.value += 3570
        self.assertTrue(access.reserve(visitor)['allowed'])

    def test_global_daily_message_cap_survives_cookie_churn_and_instance_restart(self):
        first=self.access(messages_per_hour=20,messages_per_day=2)
        for _ in range(2):
            visitor,_=first.visitor(None)
            self.assertTrue(first.reserve(visitor)['allowed'])
        restarted=self.access(messages_per_hour=20,messages_per_day=2)
        visitor,_=restarted.visitor(None)
        denied=restarted.reserve(visitor)
        self.assertFalse(denied['allowed'])
        self.assertEqual(denied['retry_after'],86400)
        self.clock.value += 86400
        self.assertTrue(restarted.reserve(visitor)['allowed'])

    def test_conversation_creation_has_independent_visitor_and_global_caps(self):
        access=self.access(chats_per_hour=1,chats_per_day=2,messages_per_hour=20,messages_per_day=20)
        first,_=access.visitor(None);second,_=access.visitor(None);third,_=access.visitor(None)
        self.assertTrue(access.reserve(first,'create')['allowed'])
        self.assertFalse(access.reserve(first,'create')['allowed'])
        self.assertTrue(access.reserve(second,'create')['allowed'])
        denied=access.reserve(third,'create')
        self.assertFalse(denied['allowed']);self.assertEqual(denied['retry_after'],86400)
        self.assertTrue(access.reserve(first,'message')['allowed'])

    def test_callback_rejection_does_not_charge_or_store_callback_data(self):
        access=self.access(messages_per_hour=1)
        visitor,_=access.visitor(None)
        def reject():raise RuntimeError('Admission rejected by the job coordinator')
        with self.assertRaises(RuntimeError):access.reserve(visitor,accept=reject)
        actual=access.reserve(visitor,accept=lambda:{'job_id':'UNIT_JOB'})
        self.assertTrue(actual['allowed'])
        self.assertEqual(actual['value'],{'job_id':'UNIT_JOB'})
        with closing(sqlite3.connect(access.db_path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM accepted_events').fetchone()[0],1)
            fields=[row[1] for row in connection.execute('PRAGMA table_info(accepted_events)')]
            self.assertEqual(fields,['id','visitor','kind','accepted_at'])

    def test_rate_limited_request_does_not_call_admission_callback(self):
        access=self.access(messages_per_hour=1)
        visitor,_=access.visitor(None)
        access.reserve(visitor)
        with patch('builtins.print') as callback:
            denied=access.reserve(visitor,accept=callback)
        self.assertFalse(denied['allowed']);callback.assert_not_called()

    def test_concurrent_instances_atomically_enforce_the_global_cap(self):
        first=self.access(messages_per_hour=20,messages_per_day=5)
        second=self.access(messages_per_hour=20,messages_per_day=5)
        barrier=threading.Barrier(12)
        admitted=[]
        def attempt(number):
            instance=first if number%2 else second
            visitor,_=instance.visitor(None)
            barrier.wait(timeout=2)
            return instance.reserve(visitor,accept=lambda:admitted.append(number))['allowed']
        with ThreadPoolExecutor(max_workers=12) as executor:
            results=list(executor.map(attempt,range(12)))
        self.assertEqual(sum(results),5)
        self.assertEqual(len(admitted),5)
        with closing(sqlite3.connect(first.db_path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM accepted_events').fetchone()[0],5)

    def test_expired_history_is_cleaned_and_database_contains_no_cookie_or_secret(self):
        access=self.access()
        visitor,cookie=access.visitor(None)
        for _ in range(3):self.assertTrue(access.reserve(visitor)['allowed'])
        self.clock.value += 86401
        access.reserve(visitor,'create')
        with closing(sqlite3.connect(access.db_path)) as connection:
            rows=connection.execute('SELECT visitor,kind,accepted_at FROM accepted_events').fetchall()
        self.assertEqual(rows,[(visitor,'create',self.clock.value)])
        self.assertNotIn(SIGNING_FIXTURE.encode(),Path(access.db_path).read_bytes())
        self.assertNotIn(self.cookie_header(cookie).encode(),Path(access.db_path).read_bytes())
        self.assertEqual(Path(access.db_path).stat().st_mode & 0o777,0o600)

    def test_lowering_limits_returns_delay_until_enough_events_expire(self):
        original=self.access(messages_per_hour=3,messages_per_day=20)
        visitor,_=original.visitor(None)
        for _ in range(3):
            original.reserve(visitor)
            self.clock.value += 10
        lower=self.access(messages_per_hour=1,messages_per_day=20)
        denied=lower.reserve(visitor)
        self.assertEqual(denied['retry_after'],3590)

    def test_invalid_identifiers_kinds_limits_and_short_signing_values_are_rejected(self):
        for value in ('short',' '*40,None):
            with self.subTest(kind='signing'),self.assertRaises(ValueError):
                PublicAccess(value,state_dir=self.folder.name)
        for limit in (0,-1,True,1.5,'01','bad',100001):
            with self.subTest(kind='limit'),self.assertRaises(ValueError):
                self.access(messages_per_hour=limit)
        access=self.access()
        for value in ('../path','A'*32,'',None):
            with self.subTest(kind='visitor'),self.assertRaises(ValueError):access.reserve(value)
        with self.assertRaises(ValueError):access.reserve('a'*32,'unknown')

    def test_limits_can_be_configured_from_explicit_environment_names(self):
        values={'NHIS_PUBLIC_MESSAGES_PER_HOUR':'7','NHIS_PUBLIC_MESSAGES_PER_DAY':'17',
                'NHIS_PUBLIC_CHATS_PER_HOUR':'3','NHIS_PUBLIC_CHATS_PER_DAY':'13'}
        with patch.dict('os.environ',values,clear=True):
            access=self.access()
        self.assertEqual(access.limits,{'message':(7,17),'create':(3,13)})


if __name__=='__main__':
    unittest.main()
