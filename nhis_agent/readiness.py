"""Cache observed local runtime readiness without unbounded HTTP subprocesses."""
from datetime import datetime, timezone
import threading
import time


class ObservedReadiness:
    def __init__(self, probe, *, ttl=60, clock=None):
        self.probe, self.ttl = probe, ttl
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._value = None
        self._at = float('-inf')
        self._worker = None

    def _refresh(self):
        try:
            value = dict(self.probe())
        except Exception:
            value = {'ready': False, 'status': 'blocked', 'reason': 'The local runtime probe failed.'}
        value['observed_at'] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._value, self._at = value, self.clock()

    def snapshot(self, *, wait=False):
        with self._lock:
            if self.clock()-self._at >= self.ttl and not (self._worker and self._worker.is_alive()):
                self._worker = threading.Thread(target=self._refresh, daemon=True, name='nhis-readiness')
                self._worker.start()
            worker = self._worker
        if wait and worker:
            worker.join(timeout=35)
        with self._lock:
            value = dict(self._value or {'ready': False, 'status': 'checking', 'observed_at': None,
                                        'reason': 'The local statistical runtime has not been observed yet.'})
            value['stale'] = self.clock()-self._at >= self.ttl
            value['refreshing'] = bool(self._worker and self._worker.is_alive())
            return value
