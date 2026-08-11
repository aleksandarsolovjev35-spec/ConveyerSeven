"""Fail-closed, thread-safe finite-state machine for the production line."""

from __future__ import annotations

import threading
from collections.abc import Callable
from enum import Enum
from typing import TypeAlias, TypedDict

from core.app_logging import get_logger

log = get_logger("state")


class State(str, Enum):
    """Observable line states."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULT = "FAULT"
    E_STOP = "E_STOP"


class Action(str, Enum):
    """The only events accepted by the state transition table."""

    START = "START"
    STOP = "STOP"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    EMPTY = "EMPTY"
    FAULT = "FAULT"
    E_STOP = "E_STOP"


class StateSnapshot(TypedDict):
    """Atomic state payload exposed to presentation adapters."""

    state: str
    exit_requested: bool
    force_exit: bool


TransitionCallback: TypeAlias = Callable[[State, State, Action], None]
TransitionTable: TypeAlias = dict[tuple[State, Action], State]

_TRANSITIONS: TransitionTable = {
    (State.IDLE, Action.START): State.RUNNING,
    (State.STOPPED, Action.START): State.RUNNING,
    (State.RUNNING, Action.STOP): State.STOPPING,
    (State.STOPPING, Action.EMPTY): State.STOPPED,
    (State.RUNNING, Action.PAUSE): State.PAUSED,
    (State.PAUSED, Action.RESUME): State.RUNNING,
    (State.PAUSED, Action.STOP): State.STOPPING,
    (State.IDLE, Action.FAULT): State.FAULT,
    (State.RUNNING, Action.FAULT): State.FAULT,
    (State.PAUSED, Action.FAULT): State.FAULT,
    (State.STOPPING, Action.FAULT): State.FAULT,
    (State.STOPPED, Action.FAULT): State.FAULT,
}
# An E-stop is always accepted and never has an automatic exit transition.
for _state in State:
    if _state is not State.E_STOP:
        _TRANSITIONS[(_state, Action.E_STOP)] = State.E_STOP


class StateMachine:
    """Thread-safe fail-closed state machine for the production line."""

    def __init__(self, on_transition: TransitionCallback | None = None) -> None:
        """Create a machine in ``IDLE`` and optionally register an observer."""
        self._state = State.IDLE
        self._exit_requested = False
        self._force_exit = False
        self._on_transition = on_transition
        self._lock = threading.Lock()

    @property
    def state(self) -> State:
        """Return the current state atomically."""
        with self._lock:
            return self._state

    @property
    def exit_requested(self) -> bool:
        """Whether a graceful application exit was requested."""
        with self._lock:
            return self._exit_requested

    @property
    def force_exit(self) -> bool:
        """Whether an immediate application exit was requested."""
        with self._lock:
            return self._force_exit

    @property
    def is_active(self) -> bool:
        """Return whether the line is still draining or processing parts."""
        with self._lock:
            return self._state in (State.RUNNING, State.STOPPING)

    @property
    def accepts_new_parts(self) -> bool:
        """Return whether new parts may enter the line."""
        with self._lock:
            return self._state is State.RUNNING

    def request_start(self) -> bool:
        """Request the safe start transition."""
        return self._apply(Action.START)

    def request_stop(self) -> bool:
        """Request controlled draining and stopping."""
        return self._apply(Action.STOP)

    def request_pause(self) -> bool:
        """Request a temporary pause."""
        return self._apply(Action.PAUSE)

    def request_resume(self) -> bool:
        """Resume a paused line."""
        return self._apply(Action.RESUME)

    def request_exit(self) -> bool:
        """Request process exit, draining a running line before shutdown."""
        with self._lock:
            self._exit_requested = True
        return self._apply(Action.STOP) if self.state is State.RUNNING else True

    def request_force_exit(self) -> bool:
        """Mark application shutdown as immediate."""
        with self._lock:
            self._exit_requested = True
            self._force_exit = True
        log.info("FORCE EXIT requested")
        return True

    def notify_line_empty(self) -> bool:
        """Notify that controlled draining completed."""
        return self._apply(Action.EMPTY)

    def notify_fault(self) -> bool:
        """Move to a terminal recoverable fault state."""
        return self._apply(Action.FAULT)

    def request_emergency_stop(self) -> bool:
        """Enter terminal emergency-stop state from every non-E-stop state."""
        return self._apply(Action.E_STOP)

    def get_snapshot(self) -> StateSnapshot:
        """Return an atomic UI-safe snapshot."""
        with self._lock:
            return StateSnapshot(
                state=self._state.value,
                exit_requested=self._exit_requested,
                force_exit=self._force_exit,
            )

    def _apply(self, action: Action) -> bool:
        callback_args: tuple[State, State, Action] | None = None
        with self._lock:
            new_state = _TRANSITIONS.get((self._state, action))
            if new_state is None:
                log.debug("%s ignored in %s", action.value, self._state.value)
                return False
            old_state = self._state
            self._state = new_state
            log.info("%s --%s--> %s", old_state.value, action.value, new_state.value)
            callback_args = (old_state, new_state, action)
        if callback_args is not None and self._on_transition is not None:
            self._on_transition(*callback_args)
        return True
