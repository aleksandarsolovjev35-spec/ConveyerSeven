"""Typed events accepted by the single-owner control event loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class EventGroup(str, Enum):
    OPERATOR_COMMAND = "OperatorCommand"
    HARDWARE = "HardwareEvent"
    CAMERA = "CameraEvent"
    WORKER = "WorkerEvent"
    PERSISTENCE = "PersistenceEvent"
    TIMER = "TimerEvent"
    INTERNAL = "InternalEvent"


@dataclass(frozen=True)
class CoreEvent:
    """An immutable event carrying production correlation identities.

    Events for an active run must have ``run_id``.  Transaction-scoped events
    must additionally have ``transaction_id``.  Camera frame events also carry
    role, monotonic timestamp and frame version.  Validation happens at the
    boundary so malformed adapter callbacks never reach the reducer.
    """

    group: EventGroup
    kind: str
    run_id: int | None = None
    transaction_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("event kind is required")
        if self.run_id is not None and (type(self.run_id) is not int or self.run_id < 1):
            raise ValueError("run_id must be a positive integer")
        if self.transaction_id is not None and (
            not isinstance(self.transaction_id, str) or not self.transaction_id
        ):
            raise ValueError("transaction_id must be a non-empty string")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        if self.group is EventGroup.CAMERA and self.kind == "FramePublished":
            self._validate_frame()

    def _validate_frame(self):
        role = self.payload.get("role")
        timestamp = self.payload.get("monotonic_timestamp")
        frame_version = self.payload.get("frame_version")
        if not isinstance(role, str) or not role:
            raise ValueError("FramePublished requires role")
        if not isinstance(timestamp, (int, float)) or timestamp < 0:
            raise ValueError("FramePublished requires a monotonic timestamp")
        if type(frame_version) is not int or frame_version < 0:
            raise ValueError("FramePublished requires a non-negative frame_version")

    def is_stale_for(self, *, run_id: int, transaction_id: str | None) -> bool:
        """Return true when the event cannot belong to the active transaction."""
        if self.run_id is not None and self.run_id != run_id:
            return True
        if self.transaction_id is not None and self.transaction_id != transaction_id:
            return True
        return False


@dataclass(frozen=True)
class OperatorCommandEvent:
    command_id: str
    command: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.command_id, str) or not self.command_id.strip():
            raise ValueError("command_id is required")
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("command is required")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
