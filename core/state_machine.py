"""Compatibility façade for the formal line-state reducer.

New control code uses :mod:`core.control_model` and :mod:`core.line_reducer`.
``StateMachine`` remains for the existing ProductionCycle integration, but its
macro states and terminal transitions now match the formal contract.
"""

from __future__ import annotations

import threading
from typing import Callable

from core.control_model import LineState, PauseContinuation, PendingIntent


# Existing imports use ``State``.  It is intentionally the same enum, not a
# second source of truth.
State = LineState


_TRANSITIONS = {
    (State.BOOTING, "RECOVERY_REQUIRED"): State.RECOVERY_REQUIRED,
    (State.RECOVERY_REQUIRED, "CLEANUP_ACKNOWLEDGED"): State.BOOTING,
    (State.BOOTING, "BOOT_COMPLETED"): State.IDLE,
    (State.IDLE, "START"): State.RUNNING,
    (State.STOPPED, "START"): State.RUNNING,
    (State.RUNNING, "STOP"): State.STOPPING,
    (State.RUNNING, "PAUSE"): State.PAUSED,
    (State.PAUSED, "RESUME"): State.RUNNING,
    (State.PAUSED, "STOP"): State.STOPPING,
    (State.STOPPING, "EMPTY"): State.STOPPED,
    (State.IDLE, "EXIT"): State.SHUTTING_DOWN,
    (State.STOPPED, "EXIT"): State.SHUTTING_DOWN,
    (State.BOOTING, "EXIT"): State.SHUTTING_DOWN,
    (State.RECOVERY_REQUIRED, "EXIT"): State.SHUTTING_DOWN,
    (State.SHUTTING_DOWN, "SHUTDOWN_COMPLETED"): State.TERMINATED,
}
for _fault_source in (
    State.BOOTING,
    State.RECOVERY_REQUIRED,
    State.IDLE,
    State.RUNNING,
    State.PAUSED,
    State.STOPPING,
    State.STOPPED,
):
    _TRANSITIONS[(_fault_source, "FAULT")] = State.FAULT


class StateMachine:
    """Thread-safe macro-state adapter used during incremental migration.

    The production application completes BOOT before constructing
    ``ProductionCycle``, hence the compatibility default remains ``IDLE``.
    Formal callers can pass ``initial_state=State.BOOTING``.
    """

    def __init__(
        self,
        on_transition: Callable | None = None,
        *,
        initial_state: State = State.IDLE,
    ):
        self._state = State(initial_state)
        self._exit_requested = False
        self._force_exit = False
        self._on_transition = on_transition
        self._lock = threading.RLock()
        self._state_version = 0
        self._run_id = 0
        self._command_results: dict[str, bool] = {}
        self._last_reason = None
        self._pending_intent = PendingIntent.NONE
        self._pause_continuation: PauseContinuation | None = None

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def state_version(self) -> int:
        with self._lock:
            return self._state_version

    @property
    def run_id(self):
        with self._lock:
            return self._run_id

    @property
    def exit_requested(self) -> bool:
        with self._lock:
            return self._exit_requested

    @property
    def force_exit(self) -> bool:
        with self._lock:
            return self._force_exit

    @property
    def pending_intent(self) -> PendingIntent:
        with self._lock:
            return self._pending_intent

    @property
    def pause_continuation(self) -> PauseContinuation | None:
        with self._lock:
            return self._pause_continuation

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._state in (State.RUNNING, State.STOPPING)

    @property
    def accepts_new_parts(self) -> bool:
        with self._lock:
            return self._state == State.RUNNING

    def request_start(self, command_id: str | None = None, run_id=None) -> bool:
        accepted = self._apply("START", command_id)
        if accepted:
            with self._lock:
                if run_id is None:
                    self._run_id += 1
                else:
                    self._run_id = run_id
                self._pending_intent = PendingIntent.NONE
                self._pause_continuation = None
        return accepted

    def request_stop(self, command_id: str | None = None) -> bool:
        accepted = self._apply("STOP", command_id)
        if accepted:
            with self._lock:
                self._pending_intent = PendingIntent.NONE
                self._pause_continuation = None
        return accepted

    def request_pause(
        self,
        command_id: str | None = None,
        continuation: PauseContinuation = PauseContinuation.NEXT_STEP,
    ) -> bool:
        with self._lock:
            if self._state is State.PAUSED:
                return True
        accepted = self._apply("PAUSE", command_id)
        if accepted:
            with self._lock:
                self._pause_continuation = PauseContinuation(continuation)
        return accepted

    def request_resume(self, command_id: str | None = None) -> bool:
        accepted = self._apply("RESUME", command_id)
        if accepted:
            with self._lock:
                self._pause_continuation = None
                self._pending_intent = PendingIntent.NONE
        return accepted

    def request_exit(self, command_id: str | None = None):
        with self._lock:
            if command_id and command_id in self._command_results:
                return self._command_results[command_id]
            self._exit_requested = True
            state = self._state
        if state in (State.RUNNING, State.PAUSED):
            accepted = self._apply("STOP", command_id)
        elif state is State.STOPPING:
            accepted = True
        elif state is State.SHUTTING_DOWN:
            accepted = True
        elif state in (State.IDLE, State.STOPPED, State.BOOTING, State.RECOVERY_REQUIRED):
            accepted = self._apply("EXIT", command_id)
        else:
            # Graceful EXIT is rejected in FAULT/TERMINATED; FORCE EXIT is the
            # only emergency path from a latched fault.
            accepted = False
        with self._lock:
            if accepted:
                self._pending_intent = PendingIntent.EXIT
            if command_id:
                self._command_results[command_id] = accepted
        return accepted

    def request_force_exit(self, command_id: str | None = None):
        callback_args = None
        with self._lock:
            if command_id and command_id in self._command_results:
                return self._command_results[command_id]
            if self._state is State.TERMINATED:
                if command_id:
                    self._command_results[command_id] = False
                return False
            old = self._state
            self._exit_requested = True
            self._force_exit = True
            self._pending_intent = PendingIntent.NONE
            self._pause_continuation = None
            self._state = State.SHUTTING_DOWN
            self._state_version += 1
            self._last_reason = "FORCE_EXIT"
            if command_id:
                self._command_results[command_id] = True
            if old is not self._state:
                callback_args = (old, self._state, "FORCE_EXIT")
        print("[STATE] FORCE EXIT requested")
        if callback_args and self._on_transition:
            self._on_transition(*callback_args)
        return True

    def notify_line_empty(self, command_id: str | None = None) -> bool:
        accepted = self._apply("EMPTY", command_id)
        if accepted and self.exit_requested:
            self._apply("EXIT")
        return accepted

    def notify_shutdown_complete(self, command_id: str | None = None) -> bool:
        return self._apply("SHUTDOWN_COMPLETED", command_id)

    def notify_recovery_required(self, command_id: str | None = None) -> bool:
        return self._apply("RECOVERY_REQUIRED", command_id)

    def acknowledge_cleanup(self, command_id: str | None = None) -> bool:
        return self._apply("CLEANUP_ACKNOWLEDGED", command_id)

    def notify_boot_completed(self, command_id: str | None = None) -> bool:
        return self._apply("BOOT_COMPLETED", command_id)

    def notify_fault(self, reason: str | None = None, command_id: str | None = None) -> bool:
        result = self._apply("FAULT", command_id)
        if reason:
            with self._lock:
                # StateMachine carries only a display reason. FaultLatch owns
                # immutable root/secondary fault semantics.
                if self._last_reason in (None, "FAULT"):
                    self._last_reason = str(reason)
        return result

    def get_snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "line_state": self._state.value,
                "exit_requested": self._exit_requested,
                "exit_after_drain": self._exit_requested,
                "force_exit": self._force_exit,
                "run_id": self._run_id,
                "pending_intent": self._pending_intent.value,
                "pause_continuation": (
                    self._pause_continuation.value
                    if self._pause_continuation else None
                ),
                "state_version": self._state_version,
                "last_reason": self._last_reason,
            }

    def _apply(self, action: str, command_id: str | None = None) -> bool:
        callback_args = None
        with self._lock:
            if command_id and command_id in self._command_results:
                return self._command_results[command_id]
            if action == "START" and self._exit_requested:
                print(f"[STATE] START ignored after EXIT request in {self._state.value}")
                if command_id:
                    self._command_results[command_id] = False
                return False
            key = (self._state, action)
            new_state = _TRANSITIONS.get(key)
            if new_state is None:
                print(f"[STATE] {action} ignored in {self._state.value}")
                if command_id:
                    self._command_results[command_id] = False
                return False
            old = self._state
            self._state = new_state
            self._state_version += 1
            self._last_reason = action
            if command_id:
                self._command_results[command_id] = True
            callback_args = (old, new_state, action)
        print(f"[STATE] {old.value} --{action}--> {new_state.value}")
        if callback_args and self._on_transition:
            self._on_transition(*callback_args)
        return True
