"""Independent fail-safe watchdog for production-cycle liveness."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from core.exceptions import SafetyStopError


class ProductionWatchdog:
    """Pings a controller every 500 ms and stops motion on a missed cycle tick.

    The production worker must call :meth:`tick` frequently.  A separate
    watchdog thread owns the liveness deadline, so a blocked inspection path
    cannot silently leave the conveyor moving.  The connected controller must
    additionally be configured with its own hardware watchdog for protection
    against a process-wide GIL stall or host power loss.
    """

    def __init__(
        self,
        ping: Callable[[], None],
        emergency_stop: Callable[[], None],
        *,
        interval_seconds: float = 0.5,
        timeout_seconds: float = 1.0,
    ) -> None:
        """Create a stopped watchdog with validated timing bounds."""
        if interval_seconds <= 0.0 or timeout_seconds < interval_seconds:
            raise ValueError("watchdog timeout must be at least one positive interval")
        self._ping = ping
        self._emergency_stop = emergency_stop
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._last_tick = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._tripped = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: SafetyStopError | None = None

    @property
    def failure(self) -> SafetyStopError | None:
        """Return the watchdog fault once the watchdog has tripped."""
        with self._lock:
            return self._failure

    def start(self) -> None:
        """Start the independent watchdog worker."""
        if self._thread is not None:
            raise RuntimeError("watchdog is already running")
        self._stop.clear()
        self._tripped.clear()
        self.tick()
        self._thread = threading.Thread(target=self._run, name="controller-watchdog", daemon=True)
        self._thread.start()

    def tick(self) -> None:
        """Declare the production main-loop responsive at this instant."""
        with self._lock:
            self._last_tick = time.monotonic()

    def stop(self) -> None:
        """Stop monitoring and join its worker without an unbounded wait."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._timeout + self._interval)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._ping()
            except (OSError, RuntimeError) as error:
                self._trip(f"controller watchdog ping failed: {error}")
                return
            with self._lock:
                tick_age = time.monotonic() - self._last_tick
            if tick_age > self._timeout:
                self._trip(f"production cycle missed watchdog deadline ({tick_age:.3f}s)")
                return

    def _trip(self, message: str) -> None:
        """Issue the one-way stop action and preserve its failure reason."""
        if self._tripped.is_set():
            return
        self._tripped.set()
        try:
            self._emergency_stop()
        finally:
            with self._lock:
                self._failure = SafetyStopError(message)
