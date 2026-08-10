"""State-machine compatibility API backed by the formal ControlCore.

ProductionCycle historically consumed ``StateMachine`` methods.  This facade
keeps that narrow API while making the immutable LineSnapshot in ControlCore
the sole macro-state, pending-intent and current-step authority.
"""

from __future__ import annotations

from typing import Any, Callable

from core.control_core import ControlCore
from core.control_model import (
    CommandGuards,
    LineSnapshot,
    LineState,
    PauseContinuation,
    PendingIntent,
    StepPhase,
)
from core.events import CoreEvent
from core.line_reducer import LineReducer, Reduction


class CoreStateMachine:
    def __init__(
        self,
        core: ControlCore,
        on_transition: Callable | None = None,
        guard_provider: Callable[[], CommandGuards] | None = None,
    ):
        self.core = core
        self._on_transition = on_transition
        self._guard_provider = guard_provider or CommandGuards
        self._last_reason: str | None = None

    @property
    def state(self) -> LineState:
        return self.core.snapshot.line_state

    @property
    def state_version(self) -> int:
        return self.core.snapshot.state_version

    @property
    def run_id(self) -> int:
        return self.core.snapshot.run_id

    @property
    def exit_requested(self) -> bool:
        return self.core.snapshot.exit_after_drain

    @property
    def force_exit(self) -> bool:
        return self.core.snapshot.force_exit

    @property
    def pending_intent(self) -> PendingIntent:
        return self.core.snapshot.pending_intent

    @property
    def pause_continuation(self) -> PauseContinuation | None:
        return self.core.snapshot.pause_continuation

    @property
    def is_active(self) -> bool:
        return self.state in {LineState.RUNNING, LineState.STOPPING}

    @property
    def accepts_new_parts(self) -> bool:
        return self.state is LineState.RUNNING

    def request_start(self, command_id=None, run_id=None) -> bool:
        return self._command("START").accepted

    def request_stop(self, command_id=None) -> bool:
        return self._command("STOP").accepted

    def request_pause(self, command_id=None, continuation=None) -> bool:
        # Continuation is derived from the formal StepPhase, never supplied by
        # HMI or legacy adapter state.
        return self._command("PAUSE").accepted

    def request_resume(self, command_id=None) -> bool:
        return self._command("RESUME").accepted

    def request_jog_start(self) -> bool:
        return self._command("JOG_START").accepted

    def request_jog_release(self) -> bool:
        return self._command("JOG_RELEASE").accepted

    def request_exit(self, command_id=None) -> bool:
        return self._command("EXIT").accepted

    def request_force_exit(self, command_id=None) -> bool:
        return self._command("FORCE_EXIT").accepted

    def notify_line_empty(self, command_id=None) -> bool:
        first = self._reduce(LineReducer.line_empty, "LINE_EMPTY")
        if not first.accepted:
            return False
        transaction_id = self.core.snapshot.active_transaction_id
        second = self._reduce(
            lambda state: LineReducer.distributor_homed(state, transaction_id),
            "DISTRIBUTOR_HOMED",
        )
        return second.accepted

    def notify_shutdown_complete(self, command_id=None) -> bool:
        return self._reduce(LineReducer.shutdown_completed, "SHUTDOWN_COMPLETED").accepted

    def notify_recovery_required(self, command_id=None) -> bool:
        return self._reduce(LineReducer.boot_recovery_required, "RECOVERY_REQUIRED").accepted

    def acknowledge_cleanup(self, command_id=None) -> bool:
        return self._reduce(LineReducer.cleanup_acknowledged, "CLEANUP_ACKNOWLEDGED").accepted

    def notify_boot_completed(self, command_id=None) -> bool:
        return self._reduce(LineReducer.boot_completed, "BOOT_COMPLETED").accepted

    def notify_fault(
        self,
        reason: Any = None,
        command_id=None,
        *,
        positions_known: bool | None = None,
    ) -> bool:
        return self._reduce(
            lambda state: LineReducer.fault(
                state,
                reason or "production fault",
                positions_known=positions_known,
            ),
            "FAULT",
        ).accepted

    def begin_step(self, **latches) -> Reduction:
        return self._reduce(lambda state: LineReducer.begin_step(state, **latches), "BEGIN_STEP")

    def set_phase(self, phase: StepPhase, *, transaction_id: str, **payload) -> Reduction:
        return self._reduce(
            lambda state: LineReducer.set_phase(
                state, phase, transaction_id=transaction_id, **payload,
            ),
            phase.value,
        )

    def post_motion_gate(self, transaction_id: str) -> Reduction:
        return self._reduce(
            lambda state: LineReducer.post_motion_gate(state, transaction_id),
            "POST_MOTION_GATE",
        )

    def command_gate(self, transaction_id: str) -> Reduction:
        return self._reduce(
            lambda state: LineReducer.command_gate(state, transaction_id),
            "COMMAND_GATE",
        )

    def handle_event(self, event: CoreEvent) -> Reduction:
        old = self.state
        reduction = self.core.handle_event(event)
        self._after(old, reduction, event.kind)
        return reduction

    def mutate(self, operation: Callable[[LineSnapshot], Reduction], reason: str) -> Reduction:
        return self._reduce(operation, reason)

    def get_snapshot(self) -> dict:
        snapshot = self.core.snapshot
        result = snapshot.as_dict()
        result.update({
            "exit_requested": snapshot.exit_after_drain,
            "force_exit": snapshot.force_exit,
            "last_reason": self._last_reason,
        })
        return result

    def _command(self, command: str) -> Reduction:
        old = self.state
        reduction = self.core.handle_command(command, self._guard_provider())
        self._after(old, reduction, command)
        return reduction

    def _reduce(self, operation, reason: str) -> Reduction:
        old = self.state
        reduction = self.core.reduce(operation)
        self._after(old, reduction, reason)
        return reduction

    def _after(self, old: LineState, reduction: Reduction, reason: str):
        if reduction.accepted:
            self._last_reason = reason
        new = self.state
        if old is not new and self._on_transition is not None:
            self._on_transition(old, new, reason)
