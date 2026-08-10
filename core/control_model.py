"""Pure domain types for the production-line control automaton.

The line is deliberately represented by orthogonal state components instead
of one combinatorial enum.  Objects in this module contain no hardware, HMI,
threading, IPC or storage code and can therefore be used by deterministic
reducer tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class LineState(str, Enum):
    BOOTING = "BOOTING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULT = "FAULT"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    TERMINATED = "TERMINATED"


class StepPhase(str, Enum):
    NONE = "NONE"
    HEALTH_GATE = "HEALTH_GATE"
    DISTRIBUTOR_HOME = "DISTRIBUTOR_HOME"
    ROUTE_PREPARE = "ROUTE_PREPARE"
    JOURNAL_INTENT = "JOURNAL_INTENT"
    MOTION_COMMAND = "MOTION_COMMAND"
    MOTION_CONFIRM = "MOTION_CONFIRM"
    STEP_COMMIT = "STEP_COMMIT"
    TRANSFER_COMMIT = "TRANSFER_COMMIT"
    POST_MOTION_GATE = "POST_MOTION_GATE"
    SETTLE = "SETTLE"
    CAPTURE = "CAPTURE"
    ANALYSIS = "ANALYSIS"
    PERSIST = "PERSIST"
    PUBLISH = "PUBLISH"
    REVIEW = "REVIEW"
    CLEAR_REVIEW = "CLEAR_REVIEW"
    COMMAND_GATE = "COMMAND_GATE"


class PendingIntent(str, Enum):
    NONE = "NONE"
    PAUSE = "PAUSE"
    STOP = "STOP"
    EXIT = "EXIT"


class PauseContinuation(str, Enum):
    NEXT_STEP = "NEXT_STEP"
    INSPECT_COMMITTED_STEP = "INSPECT_COMMITTED_STEP"


class HealthState(str, Enum):
    CHECKING = "CHECKING"
    READY = "READY"
    NOT_READY = "NOT_READY"
    FAILED = "FAILED"


class DisplayState(str, Enum):
    LIVE = "LIVE"
    REVIEW = "REVIEW"


class PersistenceState(str, Enum):
    IDLE = "IDLE"
    INTENT_PENDING = "INTENT_PENDING"
    INTENT_COMMITTED = "INTENT_COMMITTED"
    STAGE_PENDING = "STAGE_PENDING"
    STAGE_COMMITTED = "STAGE_COMMITTED"
    ARCHIVE_PENDING = "ARCHIVE_PENDING"
    ARCHIVE_COMMITTED = "ARCHIVE_COMMITTED"
    FAILED = "FAILED"


class OperatorCommand(str, Enum):
    START = "START"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STOP = "STOP"
    EXIT = "EXIT"
    FORCE_EXIT = "FORCE_EXIT"
    JOG_START = "JOG_START"
    JOG_RELEASE = "JOG_RELEASE"

    @classmethod
    def parse(cls, value: "OperatorCommand | str") -> "OperatorCommand":
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().upper().replace(" ", "_")
        aliases = {"JOG": "JOG_START", "E_STOP": "FORCE_EXIT"}
        return cls(aliases.get(normalized, normalized))


@dataclass(frozen=True)
class Counters:
    """Counters with physical meanings; never infer one from another."""

    total: int = 0
    good: int = 0
    bad: int = 0
    cleanup: int = 0
    empty: int = 0

    def __post_init__(self):
        values = (self.total, self.good, self.bad, self.cleanup, self.empty)
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("all production counters must be non-negative integers")
        if self.good + self.bad + self.cleanup > self.total:
            raise ValueError("physical output counters cannot exceed total parts")


@dataclass(frozen=True)
class PartSnapshot:
    """Immutable published view of one tracked production Part."""

    part_id: Any
    birth_step: int
    input_completed: bool = False
    control_completed: bool = False
    defects: tuple[str, ...] = ()
    category: str = "IN_PROGRESS"
    final_decision: str = "none"

    def __post_init__(self):
        if type(self.birth_step) is not int or self.birth_step < 0:
            raise ValueError("PartSnapshot.birth_step must be non-negative")
        object.__setattr__(self, "defects", tuple(self.defects))


@dataclass(frozen=True)
class StepTransaction:
    """Latches fixed before one physical motion command can be issued."""

    transaction_id: str
    run_id: int
    accept_input_for_step: bool
    pending_transfer_part_id: Any | None = None
    route_category: str | None = None
    route_targets: Mapping[str, int] = field(default_factory=dict)
    start_last_ready_ms: int | None = None
    expected_target: int = 38_096
    command_issued: bool = False
    armed_target_seen: bool = False
    ready_epoch_changed: bool = False
    final_reset_seen: bool = False
    step_committed: bool = False
    transfer_committed: bool = False
    required_roles: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.transaction_id, str) or not self.transaction_id:
            raise ValueError("transaction_id is required")
        if type(self.run_id) is not int or self.run_id < 1:
            raise ValueError("an active step requires a positive integer run_id")
        if type(self.expected_target) is not int or self.expected_target <= 0:
            raise ValueError("expected_target must be a positive integer")
        object.__setattr__(self, "route_targets", _frozen_mapping(self.route_targets))
        object.__setattr__(self, "required_roles", tuple(dict.fromkeys(self.required_roles)))


@dataclass(frozen=True)
class CommandGuards:
    """One atomic view of all guards needed by command arbitration."""

    health_ready: bool = True
    line_empty: bool = True
    previous_run_closed: bool = True
    thresholds_valid: bool = True
    storage_ready: bool = True
    service_idle: bool = True
    hardware_idle: bool = True
    hmi_heartbeat: bool = True

    def start_failure(self) -> str | None:
        checks = (
            (self.service_idle, "service operation is active"),
            (self.hardware_idle, "hardware operation is active"),
            (self.health_ready, "health gate is not green"),
            (self.line_empty, "logical line is not empty"),
            (self.previous_run_closed, "previous run is not closed"),
            (self.thresholds_valid, "threshold schema or values are invalid"),
            (self.storage_ready, "storage reserve is insufficient"),
        )
        return next((reason for passed, reason in checks if not passed), None)

    def jog_failure(self) -> str | None:
        checks = (
            (self.service_idle, "another service operation is active"),
            (self.hardware_idle, "another hardware command is active"),
            (self.hmi_heartbeat, "HMI dead-man heartbeat is not active"),
        )
        return next((reason for passed, reason in checks if not passed), None)


@dataclass(frozen=True)
class LineSnapshot:
    """Atomically readable logical truth owned by the control event loop."""

    line_state: LineState = LineState.BOOTING
    step_phase: StepPhase = StepPhase.NONE
    batch_id: str = ""
    run_id: int = 0
    current_step: int = 0
    pending_intent: PendingIntent = PendingIntent.NONE
    exit_after_drain: bool = False
    pause_continuation: PauseContinuation | None = None
    jog_happened: bool = False
    parts: Mapping[Any, Any] = field(default_factory=dict)
    counters: Counters = field(default_factory=Counters)
    root_fault: Any | None = None
    secondary_faults: tuple[Any, ...] = ()
    state_version: int = 0
    display_state: DisplayState = DisplayState.LIVE
    display_roles: Mapping[str, str] = field(default_factory=dict)
    persistence_state: PersistenceState = PersistenceState.IDLE
    health_state: HealthState = HealthState.CHECKING
    transaction: StepTransaction | None = None
    force_exit: bool = False
    positions_known: bool = True

    def __post_init__(self):
        if type(self.run_id) is not int or self.run_id < 0:
            raise ValueError("run_id must be a non-negative integer")
        if type(self.current_step) is not int or self.current_step < 0:
            raise ValueError("current_step must be a non-negative integer")
        if type(self.state_version) is not int or self.state_version < 0:
            raise ValueError("state_version must be a non-negative integer")
        if self.step_phase is StepPhase.NONE and self.transaction is not None:
            raise ValueError("transaction must be absent while step_phase is NONE")
        if self.step_phase is not StepPhase.NONE and self.transaction is None:
            raise ValueError("an active step phase requires a transaction")
        if self.pause_continuation is not None and self.line_state is not LineState.PAUSED:
            raise ValueError("pause_continuation is valid only in PAUSED")
        if self.line_state is LineState.PAUSED and self.pause_continuation is None:
            raise ValueError("PAUSED requires an explicit continuation")
        if self.transaction is not None and self.transaction.run_id != self.run_id:
            raise ValueError("active transaction run_id differs from snapshot run_id")
        if self.display_state is DisplayState.REVIEW and self.step_phase is not StepPhase.REVIEW:
            raise ValueError("frozen REVIEW display requires REVIEW step phase")
        roles = dict(self.display_roles)
        if any(value not in {"live", "frozen"} for value in roles.values()):
            raise ValueError("display role values must be 'live' or 'frozen'")
        if self.display_state is not DisplayState.REVIEW and "frozen" in roles.values():
            raise ValueError("roles may be frozen only during REVIEW")
        if self.display_state is DisplayState.REVIEW and "frozen" not in roles.values():
            raise ValueError("REVIEW requires at least one frozen real-part role")
        object.__setattr__(self, "parts", _frozen_mapping(self.parts))
        object.__setattr__(self, "secondary_faults", tuple(self.secondary_faults))
        object.__setattr__(self, "display_roles", MappingProxyType(roles))

    @property
    def state(self) -> LineState:
        """Compatibility/readability alias for ``line_state``."""
        return self.line_state

    @property
    def accepts_new_parts(self) -> bool:
        return self.line_state is LineState.RUNNING

    @property
    def active_transaction_id(self) -> str | None:
        return self.transaction.transaction_id if self.transaction else None

    def replace(self, **changes) -> "LineSnapshot":
        return replace(self, **changes)

    def published(self, **changes) -> "LineSnapshot":
        """Return the next complete publication with one monotonic version."""
        changes["state_version"] = self.state_version + 1
        return replace(self, **changes)

    def as_dict(self) -> dict[str, Any]:
        transaction = self.transaction
        return {
            "line_state": self.line_state.value,
            "state": self.line_state.value,
            "step_phase": self.step_phase.value,
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "current_step": self.current_step,
            "pending_intent": self.pending_intent.value,
            "exit_after_drain": self.exit_after_drain,
            "pause_continuation": (
                self.pause_continuation.value if self.pause_continuation else None
            ),
            "jog_happened": self.jog_happened,
            "parts": dict(self.parts),
            "counters": {
                "total": self.counters.total,
                "good": self.counters.good,
                "bad": self.counters.bad,
                "cleanup": self.counters.cleanup,
                "empty": self.counters.empty,
            },
            "root_fault": self.root_fault,
            "secondary_faults": list(self.secondary_faults),
            "state_version": self.state_version,
            "display_state": self.display_state.value,
            "display_roles": dict(self.display_roles),
            "persistence_state": self.persistence_state.value,
            "health_state": self.health_state.value,
            "transaction_id": transaction.transaction_id if transaction else None,
            "force_exit": self.force_exit,
            "positions_known": self.positions_known,
        }


def _frozen_mapping(value: Mapping | None) -> MappingProxyType:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError("expected a mapping")
    return MappingProxyType(dict(value))
