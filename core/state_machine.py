"""Thread-safe logical line state machine.

Hardware ownership remains in ControlRuntime/ProductionCycle.  This class only
publishes state transitions and records command idempotency metadata.
"""

from __future__ import annotations

import threading
import uuid
from enum import Enum
from typing import Callable


class State(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULT = "FAULT"


_TRANSITIONS = {
    (State.IDLE, "START"): State.RUNNING,
    (State.STOPPED, "START"): State.RUNNING,
    (State.RUNNING, "STOP"): State.STOPPING,
    (State.STOPPING, "EMPTY"): State.STOPPED,
    (State.RUNNING, "PAUSE"): State.PAUSED,
    (State.PAUSED, "RESUME"): State.RUNNING,
    (State.PAUSED, "STOP"): State.STOPPING,
    (State.IDLE, "FAULT"): State.FAULT,
    (State.RUNNING, "FAULT"): State.FAULT,
    (State.PAUSED, "FAULT"): State.FAULT,
    (State.STOPPING, "FAULT"): State.FAULT,
    (State.STOPPED, "FAULT"): State.FAULT,
}


class StateMachine:
    def __init__(self, on_transition: Callable | None = None):
        self._state = State.IDLE
        self._exit_requested = False
        self._force_exit = False
        self._on_transition = on_transition
        self._lock = threading.RLock()
        self._state_version = 0
        self._run_id = None
        self._command_results: dict[str, bool] = {}
        self._last_reason = None

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
    def is_active(self) -> bool:
        with self._lock:
            return self._state in (State.RUNNING, State.STOPPING)

    @property
    def accepts_new_parts(self) -> bool:
        with self._lock:
            return self._state == State.RUNNING

    def request_start(self, command_id: str | None = None, run_id: str | None = None) -> bool:
        with self._lock:
            if command_id and command_id in self._command_results:
                return self._command_results[command_id]
        accepted = self._apply("START", command_id)
        if accepted:
            with self._lock:
                self._run_id = run_id or f"run-{uuid.uuid4().hex}"
        return accepted

    def request_stop(self, command_id: str | None = None) -> bool:
        return self._apply("STOP", command_id)

    def request_pause(self, command_id: str | None = None) -> bool:
        return self._apply("PAUSE", command_id)

    def request_resume(self, command_id: str | None = None) -> bool:
        return self._apply("RESUME", command_id)

    def request_exit(self, command_id: str | None = None):
        with self._lock:
            if command_id and command_id in self._command_results:
                return self._command_results[command_id]
            self._exit_requested = True
        accepted = True
        # EXIT is a controlled STOP, not a direct shutdown.
        if self.state in (State.RUNNING, State.PAUSED):
            accepted = self._apply("STOP", command_id)
        elif self.state not in (State.STOPPING, State.STOPPED, State.FAULT):
            accepted = False
        if command_id:
            with self._lock:
                self._command_results[command_id] = accepted
        return accepted

    def request_force_exit(self, command_id: str | None = None):
        with self._lock:
            if command_id and command_id in self._command_results:
                return self._command_results[command_id]
            self._exit_requested = True
            self._force_exit = True
            self._state_version += 1
            self._last_reason = "FORCE_EXIT"
            if command_id:
                self._command_results[command_id] = True
        print("[STATE] FORCE EXIT requested")
        return True

    def notify_line_empty(self, command_id: str | None = None) -> bool:
        return self._apply("EMPTY", command_id)

    def notify_fault(self, reason: str | None = None, command_id: str | None = None) -> bool:
        result = self._apply("FAULT", command_id)
        if reason:
            with self._lock:
                self._last_reason = str(reason)
        return result

    def get_snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "exit_requested": self._exit_requested,
                "force_exit": self._force_exit,
                "run_id": self._run_id,
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
