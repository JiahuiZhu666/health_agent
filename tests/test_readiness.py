"""Observed runtime readiness tests use gated fake probes, never subprocesses."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading
import time
import unittest

from nhis_agent.readiness import ObservedReadiness


class ReadinessTests(unittest.TestCase):
    def test_first_snapshot_is_nonblocking_and_honestly_unobserved(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def probe():
            started.set(); release.wait(2)
            return {'ready': True, 'source': 'synthetic probe'}
        cache = ObservedReadiness(probe, ttl=60)
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                future = pool.submit(cache.snapshot)
                snapshot = future.result(timeout=.5)
                self.assertTrue(started.wait(.5))
                self.assertFalse(snapshot['ready'])
                self.assertEqual(snapshot['status'], 'checking')
                self.assertIsNone(snapshot['observed_at'])
                self.assertTrue(snapshot['stale'])
                self.assertTrue(snapshot['refreshing'])
            finally:
                release.set()
        completed = cache.snapshot(wait=True)
        self.assertTrue(completed['ready'])
        self.assertFalse(completed['refreshing'])

    def test_concurrent_snapshots_share_one_inflight_probe(self):
        release, started = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls, lock = [], threading.Lock()
        def probe():
            with lock: calls.append(1)
            started.set(); release.wait(2)
            return {'ready': True}
        cache = ObservedReadiness(probe)
        try:
            with ThreadPoolExecutor(max_workers=12) as pool:
                snapshots = list(pool.map(lambda _: cache.snapshot(), range(36)))
            self.assertTrue(started.wait(.5))
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(item['refreshing'] and item['stale'] for item in snapshots))
        finally:
            release.set()
        cache.snapshot(wait=True)
        self.assertEqual(len(calls), 1)

    def test_ttl_returns_stale_observation_while_one_refresh_runs(self):
        now, calls = [0.0], []
        release, second_started = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def probe():
            calls.append(1)
            if len(calls) == 2:
                second_started.set(); release.wait(2)
            return {'ready': len(calls) == 1, 'marker': len(calls)}
        cache = ObservedReadiness(probe, ttl=10, clock=lambda: now[0])
        first = cache.snapshot(wait=True)
        self.assertTrue(first['ready']); self.assertFalse(first['stale'])
        datetime.fromisoformat(first['observed_at'])
        first['ready'] = False
        now[0] = 9
        self.assertTrue(cache.snapshot()['ready'])
        self.assertEqual(len(calls), 1)
        now[0] = 11
        try:
            stale = cache.snapshot()
            self.assertTrue(second_started.wait(.5))
            self.assertTrue(stale['ready']); self.assertTrue(stale['stale']); self.assertTrue(stale['refreshing'])
            self.assertEqual(stale['marker'], 1)
            for _ in range(10): cache.snapshot()
            self.assertEqual(len(calls), 2)
        finally:
            release.set()
        fresh = cache.snapshot(wait=True)
        self.assertFalse(fresh['ready']); self.assertFalse(fresh['stale']); self.assertFalse(fresh['refreshing'])
        self.assertEqual(fresh['marker'], 2)

    def test_probe_failure_is_sanitized_and_can_be_refreshed_after_ttl(self):
        now, calls = [0.0], []
        def probe():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('SYNTHETIC_PRIVATE_PATH_AND_CREDENTIAL')
            return {'ready': True, 'status': 'observed'}
        cache = ObservedReadiness(probe, ttl=3, clock=lambda: now[0])
        failed = cache.snapshot(wait=True)
        self.assertFalse(failed['ready']); self.assertEqual(failed['status'], 'blocked')
        self.assertNotIn('SYNTHETIC_PRIVATE', str(failed))
        self.assertIsNotNone(failed['observed_at']); self.assertFalse(failed['stale'])
        now[0] = 4
        recovered = cache.snapshot(wait=True)
        self.assertTrue(recovered['ready']); self.assertFalse(recovered['refreshing']); self.assertFalse(recovered['stale'])
        self.assertEqual(len(calls), 2)

    def test_fresh_snapshots_do_not_spawn_extra_probes_or_claim_api_verification(self):
        calls = []
        def probe(): calls.append(1); return {'ready': True, 'capability': 'local statistics only'}
        cache = ObservedReadiness(probe, ttl=100)
        cache.snapshot(wait=True)
        for _ in range(100):
            result = cache.snapshot()
            self.assertEqual(result['capability'], 'local statistics only')
            self.assertNotIn('dialogue', result)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
