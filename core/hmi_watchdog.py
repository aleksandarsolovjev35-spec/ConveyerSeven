"""Local HMI reconnect grace and safe pending-pause watchdog."""

from __future__ import annotations

import threading
import time


class HMIWatchdog:
    def __init__(self, on_lost=None, grace_seconds: float = 3.0, poll_seconds: float = 0.1, clock=time.monotonic):
        if grace_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("HMI watchdog timings must be positive")
        self.on_lost = on_lost
        self.grace_seconds = float(grace_seconds)
        self.poll_seconds = float(poll_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._last_seen = None
        self._lost_notified = False
        self._stop = threading.Event()
        self._thread = None

    def touch(self, now: float | None = None):
        with self._lock:
            self._last_seen = self._clock() if now is None else float(now)
            self._lost_notified = False

    @property
    def connected(self) -> bool:
        with self._lock:
            if self._last_seen is None:
                return True
            return self._clock() - self._last_seen <= self.grace_seconds

    def check(self, now: float | None = None) -> bool:
        timestamp = self._clock() if now is None else float(now)
        with self._lock:
            if self._last_seen is None or timestamp - self._last_seen <= self.grace_seconds:
                return False
            if self._lost_notified:
                return False
            self._lost_notified = True
        if callable(self.on_lost):
            self.on_lost()
        return True

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="hmi-watchdog", daemon=True)
            self._thread.start()
        return True

    def stop(self, timeout: float = 1.0):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def _loop(self):
        while not self._stop.wait(self.poll_seconds):
            self.check()
