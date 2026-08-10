"""Single-owner, idempotent command arbitration for the control core."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable


PRIORITY = {
    "FORCE_EXIT": 0,
    "E_STOP": 0,
    "STOP": 1,
    "EXIT": 1,
    "PAUSE": 2,
}


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    command: str
    accepted: bool
    state: str
    reason: str | None = None
    duplicate: bool = False
    state_version: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "command": self.command,
            "accepted": self.accepted,
            "state": self.state,
            "reason": self.reason,
            "duplicate": self.duplicate,
            "state_version": self.state_version,
            "data": dict(self.data),
        }


class CommandArbiter:
    """Serialize all mutating commands and memoize command IDs.

    ``handler`` is the only callback allowed to touch runtime state/hardware.
    A duplicate ID returns the original response and never calls the handler.
    The class also exposes a priority helper used by adapters that receive
    several requests at the same boundary.
    """

    def __init__(
        self,
        handler: Callable[..., Any],
        state_provider: Callable[[], Any] | None = None,
    ):
        self._handler = handler
        self._state_provider = state_provider or (lambda: "UNKNOWN")
        self._lock = threading.RLock()
        self._responses: dict[str, CommandResult] = {}

    def submit(self, command_id: str, command: str, **payload) -> CommandResult:
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("command_id is required")
        command = str(command).upper()
        with self._lock:
            previous = self._responses.get(command_id)
            if previous is not None:
                # Return the saved result itself.  No fresh state is sampled
                # and the reducer/hardware handler is not entered again.
                return previous
            try:
                result = self._handler(command, **payload)
                if isinstance(result, bool):
                    accepted = result
                    reason = None if accepted else "command rejected in current state"
                    data = {}
                elif hasattr(result, "accepted"):
                    # Formal LineReducer result.  Preserve its rejection reason
                    # rather than accidentally treating every object as true.
                    accepted = bool(result.accepted)
                    reason = getattr(result, "reason", None)
                    data = {
                        "effects": list(getattr(result, "effects", ()) or ()),
                        "stale": bool(getattr(result, "stale", False)),
                    }
                else:
                    accepted = True
                    reason = None
                    data = dict(result) if isinstance(result, dict) else {"result": result}
            except Exception as exc:
                accepted = False
                reason = f"{type(exc).__name__}: {exc}"
                data = {}
            state_snapshot = self._state_provider()
            state_version = None
            if isinstance(state_snapshot, dict):
                state_version = state_snapshot.get("state_version")
                state = state_snapshot.get("state", state_snapshot)
            else:
                state_version = getattr(state_snapshot, "state_version", None)
                state = getattr(
                    state_snapshot,
                    "line_state",
                    getattr(state_snapshot, "state", state_snapshot),
                )
                if hasattr(state, "value"):
                    state = state.value
            response = CommandResult(
                command_id=command_id,
                command=command,
                accepted=bool(accepted),
                state=str(state),
                reason=reason,
                state_version=state_version,
                data=data,
            )
            self._responses[command_id] = response
            return response

    def clear(self):
        with self._lock:
            self._responses.clear()

    def response(self, command_id: str) -> CommandResult | None:
        with self._lock:
            return self._responses.get(command_id)

    @staticmethod
    def order(commands: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
        """Return pending commands by safety priority while retaining FIFO."""
        return sorted(
            commands,
            key=lambda item: PRIORITY.get(str(item[0]).upper(), 3),
        )
