"""Single serialized owner for the pure production-line state."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from core.atomic_publisher import AtomicPublisher
from core.control_model import CommandGuards, LineSnapshot, StepPhase
from core.events import CoreEvent
from core.line_reducer import LineReducer, Reduction


class ControlCore:
    """Serialize commands/events, reduce them and publish complete snapshots.

    Adapters may call :meth:`handle_event` from arbitrary threads; the lock is
    the event-loop serialization boundary.  They never receive a mutable state
    object and cannot directly change current_step or tracked parts.
    """

    def __init__(
        self,
        snapshot: LineSnapshot,
        *,
        publisher: AtomicPublisher | None = None,
        effect_handler: Callable[[str, LineSnapshot], None] | None = None,
    ):
        if not isinstance(snapshot, LineSnapshot):
            raise TypeError("ControlCore requires a LineSnapshot")
        self._lock = threading.RLock()
        self._snapshot = snapshot
        self.publisher = publisher or AtomicPublisher()
        self._effect_handler = effect_handler
        self.publisher.publish(snapshot)

    @property
    def snapshot(self) -> LineSnapshot:
        with self._lock:
            return self._snapshot

    def handle_command(self, command, guards: CommandGuards | None = None) -> Reduction:
        with self._lock:
            return self._apply(LineReducer.command(self._snapshot, command, guards))

    def handle_event(self, event: CoreEvent) -> Reduction:
        if not isinstance(event, CoreEvent):
            raise TypeError("control core accepts only CoreEvent instances")
        with self._lock:
            snapshot = self._snapshot
            if event.is_stale_for(
                run_id=snapshot.run_id,
                transaction_id=snapshot.active_transaction_id,
            ):
                return Reduction(
                    snapshot, False, "stale event ignored", stale=True,
                )
            kind = event.kind
            payload = dict(event.payload)
            transaction_id = event.transaction_id
            handlers = {
                "BootRecoveryRequired": lambda: LineReducer.boot_recovery_required(snapshot),
                "ManualCleanupAcknowledged": lambda: LineReducer.cleanup_acknowledged(snapshot),
                "BootCompleted": lambda: LineReducer.boot_completed(snapshot),
                "JogMoved": lambda: LineReducer.jog_moved(snapshot),
                "LineEmpty": lambda: LineReducer.line_empty(snapshot),
                "ShutdownCompleted": lambda: LineReducer.shutdown_completed(snapshot),
                "IntentCommitted": lambda: LineReducer.intent_committed(snapshot, transaction_id),
                "MotionCommandIssued": lambda: LineReducer.motion_command_issued(snapshot, transaction_id),
                "MotionConfirmed": lambda: LineReducer.commit_step(
                    snapshot,
                    transaction_id,
                    exact_motion_proof=bool(payload.get("exact_motion_proof")),
                    armed_target_seen=bool(payload.get("armed_target_seen")),
                    ready_epoch_changed=bool(payload.get("ready_epoch_changed")),
                    final_reset_seen=bool(payload.get("final_reset_seen")),
                ),
                "StageCommitted": lambda: LineReducer.stage_persisted(snapshot, transaction_id),
                "ArchiveFinalized": lambda: LineReducer.archive_finalized(snapshot, transaction_id),
                "ReviewElapsed": lambda: LineReducer.set_phase(
                    snapshot, StepPhase.CLEAR_REVIEW,
                    transaction_id=transaction_id,
                ),
                "AxisHomed": lambda: LineReducer.distributor_homed(snapshot, transaction_id),
                "FaultRaised": lambda: LineReducer.fault(
                    snapshot,
                    payload.get("fault", payload),
                    positions_known=payload.get("positions_known"),
                ),
            }
            handler = handlers.get(kind)
            if handler is None:
                return Reduction(snapshot, False, f"unsupported event: {kind}")
            return self._apply(handler())

    def reduce(self, operation: Callable[[LineSnapshot], Reduction]) -> Reduction:
        """Apply a reducer operation used by StepExecutor on this same loop."""
        with self._lock:
            return self._apply(operation(self._snapshot))

    def _apply(self, reduction: Reduction) -> Reduction:
        if reduction.snapshot is not self._snapshot:
            self.publisher.publish(reduction.snapshot)
            self._snapshot = reduction.snapshot
        if self._effect_handler is not None:
            for effect in reduction.effects:
                self._effect_handler(effect, self._snapshot)
        return reduction


@dataclass
class StepExecutor:
    """The only public phase-advance API for an active step transaction."""

    core: ControlCore

    def begin(self, **latches) -> Reduction:
        return self.core.reduce(lambda state: LineReducer.begin_step(state, **latches))

    def phase(self, phase: StepPhase | str, *, transaction_id: str, **payload) -> Reduction:
        return self.core.reduce(
            lambda state: LineReducer.set_phase(
                state, phase,
                transaction_id=transaction_id,
                **payload,
            )
        )

    def post_motion_gate(self, transaction_id: str) -> Reduction:
        return self.core.reduce(
            lambda state: LineReducer.post_motion_gate(state, transaction_id)
        )

    def command_gate(self, transaction_id: str) -> Reduction:
        return self.core.reduce(
            lambda state: LineReducer.command_gate(state, transaction_id)
        )
