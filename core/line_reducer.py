"""Pure transitions for the formal production-line automaton.

The reducer returns a new :class:`LineSnapshot` and declarative effects.  It
never calls hardware, workers, storage or the HMI.  The control event loop is
responsible for executing effects and feeding their typed completion events
back to this reducer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable
from uuid import uuid4

from core.control_model import (
    CommandGuards,
    Counters,
    DisplayState,
    HealthState,
    LineSnapshot,
    LineState,
    OperatorCommand,
    PauseContinuation,
    PendingIntent,
    PersistenceState,
    StepPhase,
    StepTransaction,
)


@dataclass(frozen=True)
class Reduction:
    snapshot: LineSnapshot
    accepted: bool
    reason: str | None = None
    effects: tuple[str, ...] = ()
    stale: bool = False


_BEFORE_MOTION = frozenset({
    StepPhase.NONE,
    StepPhase.HEALTH_GATE,
    StepPhase.ROUTE_PREPARE,
    StepPhase.JOURNAL_INTENT,
})
_INSPECTION_IN_FLIGHT = frozenset({
    StepPhase.SETTLE,
    StepPhase.CAPTURE,
    StepPhase.ANALYSIS,
    StepPhase.PERSIST,
    StepPhase.PUBLISH,
    StepPhase.REVIEW,
    StepPhase.CLEAR_REVIEW,
})


# Structural phase order.  Branches represent no transfer, no required roles,
# initial inspection and drain homing; they are explicit rather than silently
# skipping a state in adapter code.
_PHASE_TRANSITIONS: dict[StepPhase, frozenset[StepPhase]] = {
    StepPhase.NONE: frozenset({StepPhase.HEALTH_GATE}),
    StepPhase.HEALTH_GATE: frozenset({
        StepPhase.DISTRIBUTOR_HOME,
        StepPhase.ROUTE_PREPARE,
        StepPhase.JOURNAL_INTENT,
        StepPhase.SETTLE,  # initial START inspection has no motion
    }),
    StepPhase.DISTRIBUTOR_HOME: frozenset({StepPhase.NONE}),
    StepPhase.ROUTE_PREPARE: frozenset({StepPhase.JOURNAL_INTENT}),
    StepPhase.JOURNAL_INTENT: frozenset({StepPhase.MOTION_COMMAND}),
    StepPhase.MOTION_COMMAND: frozenset({StepPhase.MOTION_CONFIRM}),
    StepPhase.MOTION_CONFIRM: frozenset({StepPhase.STEP_COMMIT}),
    StepPhase.STEP_COMMIT: frozenset({
        StepPhase.TRANSFER_COMMIT,
        StepPhase.POST_MOTION_GATE,
    }),
    StepPhase.TRANSFER_COMMIT: frozenset({StepPhase.POST_MOTION_GATE}),
    StepPhase.POST_MOTION_GATE: frozenset({
        StepPhase.SETTLE,
        StepPhase.COMMAND_GATE,
    }),
    StepPhase.SETTLE: frozenset({StepPhase.CAPTURE}),
    StepPhase.CAPTURE: frozenset({StepPhase.ANALYSIS}),
    StepPhase.ANALYSIS: frozenset({StepPhase.PERSIST}),
    StepPhase.PERSIST: frozenset({StepPhase.PUBLISH}),
    StepPhase.PUBLISH: frozenset({StepPhase.REVIEW, StepPhase.COMMAND_GATE}),
    StepPhase.REVIEW: frozenset({StepPhase.CLEAR_REVIEW}),
    StepPhase.CLEAR_REVIEW: frozenset({StepPhase.COMMAND_GATE}),
    StepPhase.COMMAND_GATE: frozenset({StepPhase.NONE}),
}


class LineReducer:
    """Deterministic owner of LineState, pending intent and step commits."""

    @staticmethod
    def initial(batch_id: str, *, booting: bool = True) -> LineSnapshot:
        if not isinstance(batch_id, str) or not batch_id:
            raise ValueError("batch_id is required")
        return LineSnapshot(
            line_state=LineState.BOOTING if booting else LineState.IDLE,
            batch_id=batch_id,
            health_state=HealthState.CHECKING if booting else HealthState.READY,
        )

    @staticmethod
    def command(
        snapshot: LineSnapshot,
        command: OperatorCommand | str,
        guards: CommandGuards | None = None,
    ) -> Reduction:
        guards = guards or CommandGuards()
        try:
            command = OperatorCommand.parse(command)
        except ValueError:
            return Reduction(snapshot, False, f"unsupported command: {command}")

        if command is OperatorCommand.FORCE_EXIT:
            return LineReducer._force_exit(snapshot)
        if snapshot.line_state is LineState.TERMINATED:
            return Reduction(snapshot, False, "process is terminated")
        if command is OperatorCommand.START:
            return LineReducer._start(snapshot, guards)
        if command is OperatorCommand.PAUSE:
            return LineReducer._pause(snapshot)
        if command is OperatorCommand.RESUME:
            return LineReducer._resume(snapshot, guards)
        if command is OperatorCommand.STOP:
            return LineReducer._stop(snapshot)
        if command is OperatorCommand.EXIT:
            return LineReducer._exit(snapshot)
        if command in {OperatorCommand.JOG_START, OperatorCommand.JOG_RELEASE}:
            return LineReducer._jog(snapshot, command, guards)
        return Reduction(snapshot, False, f"unsupported command: {command.value}")

    @staticmethod
    def _start(snapshot: LineSnapshot, guards: CommandGuards) -> Reduction:
        if snapshot.line_state not in {LineState.IDLE, LineState.STOPPED}:
            return Reduction(snapshot, False, "START requires IDLE or STOPPED")
        if snapshot.parts:
            return Reduction(snapshot, False, "logical line is not empty")
        failure = guards.start_failure()
        if failure:
            return Reduction(snapshot, False, failure)
        return Reduction(
            snapshot.published(
                line_state=LineState.RUNNING,
                run_id=snapshot.run_id + 1,
                current_step=0,
                pending_intent=PendingIntent.NONE,
                exit_after_drain=False,
                pause_continuation=None,
                jog_happened=False,
                health_state=HealthState.READY,
                persistence_state=PersistenceState.IDLE,
                display_state=DisplayState.LIVE,
                display_roles={},
            ),
            True,
            effects=("START_INITIAL_INSPECTION",),
        )

    @staticmethod
    def _pause(snapshot: LineSnapshot) -> Reduction:
        if snapshot.line_state is LineState.PAUSED:
            return Reduction(snapshot, True, "already paused")
        if snapshot.line_state is not LineState.RUNNING:
            return Reduction(snapshot, False, "PAUSE requires RUNNING")

        transaction = snapshot.transaction
        before_motion = snapshot.step_phase in _BEFORE_MOTION or (
            snapshot.step_phase is StepPhase.MOTION_COMMAND
            and transaction is not None
            and not transaction.command_issued
        )
        if before_motion:
            # A prepared route is not rolled back.  No conveyor command was
            # sent, so this transaction is abandoned at a safe boundary.
            return Reduction(
                snapshot.published(
                    line_state=LineState.PAUSED,
                    step_phase=StepPhase.NONE,
                    transaction=None,
                    pending_intent=PendingIntent.NONE,
                    pause_continuation=PauseContinuation.NEXT_STEP,
                ),
                True,
            )

        if snapshot.step_phase is StepPhase.POST_MOTION_GATE:
            return Reduction(
                snapshot.published(
                    line_state=LineState.PAUSED,
                    pending_intent=PendingIntent.NONE,
                    pause_continuation=PauseContinuation.INSPECT_COMMITTED_STEP,
                ),
                True,
            )

        return Reduction(
            snapshot.published(
                pending_intent=_stronger_intent(
                    snapshot.pending_intent, PendingIntent.PAUSE,
                ),
            ),
            True,
            effects=("DEFER_TO_SAFE_BOUNDARY",),
        )

    @staticmethod
    def _resume(snapshot: LineSnapshot, guards: CommandGuards) -> Reduction:
        if snapshot.line_state is not LineState.PAUSED:
            return Reduction(snapshot, False, "RESUME requires PAUSED")
        if not guards.health_ready:
            return Reduction(snapshot, False, "health gate is not green")
        if not guards.hardware_idle:
            return Reduction(snapshot, False, "hardware operation is active")
        continuation = snapshot.pause_continuation
        if continuation is None:
            return Reduction(snapshot, False, "PAUSED continuation is missing")
        effect = (
            "INSPECT_COMMITTED_STEP"
            if continuation is PauseContinuation.INSPECT_COMMITTED_STEP
            else "NEXT_STEP_HEALTH_GATE"
        )
        return Reduction(
            snapshot.published(
                line_state=LineState.RUNNING,
                pause_continuation=None,
                pending_intent=PendingIntent.NONE,
            ),
            True,
            effects=(effect,),
        )

    @staticmethod
    def _stop(snapshot: LineSnapshot) -> Reduction:
        state = snapshot.line_state
        if state in {LineState.STOPPING, LineState.STOPPED}:
            return Reduction(snapshot, True, "already stopping or stopped")
        if state is LineState.PAUSED:
            # INSPECT_COMMITTED_STEP remains represented by the active
            # transaction.  pause_continuation must be cleared outside PAUSED.
            return Reduction(
                snapshot.published(
                    line_state=LineState.STOPPING,
                    pending_intent=(
                        PendingIntent.STOP if snapshot.transaction else PendingIntent.NONE
                    ),
                    pause_continuation=None,
                ),
                True,
                effects=(
                    ("FINISH_COMMITTED_INSPECTION",)
                    if snapshot.transaction else ("DRAIN_BOUNDARY",)
                ),
            )
        if state is not LineState.RUNNING:
            return Reduction(snapshot, False, "STOP requires RUNNING or PAUSED")

        transaction = snapshot.transaction
        before_motion = snapshot.step_phase in _BEFORE_MOTION or (
            snapshot.step_phase is StepPhase.MOTION_COMMAND
            and transaction is not None
            and not transaction.command_issued
        )
        if before_motion:
            return Reduction(
                snapshot.published(
                    line_state=LineState.STOPPING,
                    step_phase=StepPhase.NONE,
                    transaction=None,
                    pending_intent=PendingIntent.NONE,
                    pause_continuation=None,
                ),
                True,
                effects=("DRAIN_BOUNDARY",),
            )

        return Reduction(
            snapshot.published(
                pending_intent=_stronger_intent(
                    snapshot.pending_intent, PendingIntent.STOP,
                ),
            ),
            True,
            effects=("FINISH_CURRENT_TRANSACTION",),
        )

    @staticmethod
    def _exit(snapshot: LineSnapshot) -> Reduction:
        state = snapshot.line_state
        if state is LineState.SHUTTING_DOWN:
            return Reduction(snapshot, True, "already shutting down")
        if state in {LineState.BOOTING, LineState.RECOVERY_REQUIRED, LineState.IDLE, LineState.STOPPED}:
            return Reduction(
                snapshot.published(
                    line_state=LineState.SHUTTING_DOWN,
                    exit_after_drain=True,
                    pending_intent=PendingIntent.NONE,
                    pause_continuation=None,
                    step_phase=StepPhase.NONE,
                    transaction=None,
                ),
                True,
                effects=("CONTROLLED_SHUTDOWN",),
            )
        if state is LineState.STOPPING:
            if snapshot.exit_after_drain:
                return Reduction(snapshot, True, "exit already set after drain")
            return Reduction(
                snapshot.published(exit_after_drain=True),
                True,
                effects=("EXIT_AFTER_DRAIN",),
            )
        if state is LineState.PAUSED:
            return Reduction(
                snapshot.published(
                    line_state=LineState.STOPPING,
                    pending_intent=(
                        PendingIntent.EXIT if snapshot.transaction else PendingIntent.NONE
                    ),
                    exit_after_drain=True,
                    pause_continuation=None,
                ),
                True,
                effects=(
                    ("FINISH_COMMITTED_INSPECTION",)
                    if snapshot.transaction else ("DRAIN_BOUNDARY",)
                ),
            )
        if state is LineState.RUNNING:
            transaction = snapshot.transaction
            before_motion = snapshot.step_phase in _BEFORE_MOTION or (
                snapshot.step_phase is StepPhase.MOTION_COMMAND
                and transaction is not None
                and not transaction.command_issued
            )
            if before_motion:
                return Reduction(
                    snapshot.published(
                        line_state=LineState.STOPPING,
                        step_phase=StepPhase.NONE,
                        transaction=None,
                        pending_intent=PendingIntent.NONE,
                        exit_after_drain=True,
                    ),
                    True,
                    effects=("DRAIN_BOUNDARY",),
                )
            return Reduction(
                snapshot.published(
                    pending_intent=PendingIntent.EXIT,
                    exit_after_drain=True,
                ),
                True,
                effects=("FINISH_CURRENT_TRANSACTION",),
            )
        # Graceful EXIT is deliberately rejected in a latched FAULT.
        return Reduction(snapshot, False, f"EXIT is not allowed in {state.value}")

    @staticmethod
    def _force_exit(snapshot: LineSnapshot) -> Reduction:
        if snapshot.line_state is LineState.TERMINATED:
            return Reduction(snapshot, False, "process is terminated")
        if snapshot.line_state is LineState.SHUTTING_DOWN and snapshot.force_exit:
            return Reduction(snapshot, True, "force exit already active")
        live_roles = {role: "live" for role in snapshot.display_roles}
        return Reduction(
            snapshot.published(
                line_state=LineState.SHUTTING_DOWN,
                step_phase=StepPhase.NONE,
                transaction=None,
                pending_intent=PendingIntent.NONE,
                exit_after_drain=True,
                pause_continuation=None,
                force_exit=True,
                positions_known=False,
                display_state=DisplayState.LIVE,
                display_roles=live_roles,
            ),
            True,
            effects=("BEST_EFFORT_STOP", "BOUNDED_CLEANUP"),
        )

    @staticmethod
    def _jog(
        snapshot: LineSnapshot,
        command: OperatorCommand,
        guards: CommandGuards,
    ) -> Reduction:
        if snapshot.line_state not in {LineState.IDLE, LineState.STOPPED, LineState.PAUSED}:
            return Reduction(snapshot, False, "JOG requires IDLE, STOPPED or PAUSED")
        # Release is the safety action when heartbeat/another guard has gone
        # false and therefore must not depend on the start guards.
        if command is OperatorCommand.JOG_START:
            failure = guards.jog_failure()
            if failure:
                return Reduction(snapshot, False, failure)
        effect = "JOG_DEADMAN_START" if command is OperatorCommand.JOG_START else "JOG_RELEASE"
        # Pressing JOG is not evidence that movement happened.  JOG_MOVED is a
        # separate hardware event and is the only reducer operation that sets
        # jog_happened.
        return Reduction(snapshot, True, effects=(effect,))

    @staticmethod
    def boot_recovery_required(snapshot: LineSnapshot) -> Reduction:
        if snapshot.line_state is not LineState.BOOTING:
            return Reduction(snapshot, False, "recovery discovery requires BOOTING")
        return Reduction(
            snapshot.published(line_state=LineState.RECOVERY_REQUIRED), True,
            effects=("REQUIRE_MANUAL_CLEANUP",),
        )

    @staticmethod
    def cleanup_acknowledged(snapshot: LineSnapshot) -> Reduction:
        if snapshot.line_state is not LineState.RECOVERY_REQUIRED:
            return Reduction(snapshot, False, "cleanup acknowledgement is not expected")
        return Reduction(
            snapshot.published(line_state=LineState.BOOTING), True,
            effects=("RESTART_BOOT_GATES",),
        )

    @staticmethod
    def boot_completed(snapshot: LineSnapshot) -> Reduction:
        if snapshot.line_state is not LineState.BOOTING:
            return Reduction(snapshot, False, "boot completion requires BOOTING")
        return Reduction(
            snapshot.published(
                line_state=LineState.IDLE,
                health_state=HealthState.READY,
            ),
            True,
        )

    @staticmethod
    def touch_publication(snapshot: LineSnapshot) -> Reduction:
        """Version an otherwise unchanged complete logical publication."""
        return Reduction(snapshot.published(), True)

    @staticmethod
    def set_health(snapshot: LineSnapshot, health_state: HealthState) -> Reduction:
        health_state = HealthState(health_state)
        if snapshot.health_state is health_state:
            return Reduction(snapshot, True, "health state unchanged")
        return Reduction(snapshot.published(health_state=health_state), True)

    @staticmethod
    def commit_tracking_snapshot(
        snapshot: LineSnapshot,
        *,
        parts: dict[Any, Any],
        counters: Counters,
        persistence_state: PersistenceState | None = None,
    ) -> Reduction:
        """Atomically commit Part/counter changes produced on the event loop."""
        changes: dict[str, Any] = {"parts": parts, "counters": counters}
        if persistence_state is not None:
            changes["persistence_state"] = PersistenceState(persistence_state)
        return Reduction(snapshot.published(**changes), True)

    @staticmethod
    def begin_step(
        snapshot: LineSnapshot,
        *,
        transaction_id: str | None = None,
        accept_input_for_step: bool | None = None,
        pending_transfer_part_id: Any | None = None,
        route_category: str | None = None,
        route_targets: dict[str, int] | None = None,
        start_last_ready_ms: int | None = None,
        expected_target: int = 38_096,
    ) -> Reduction:
        if snapshot.line_state not in {LineState.RUNNING, LineState.STOPPING}:
            return Reduction(snapshot, False, "a step requires RUNNING or STOPPING")
        if snapshot.step_phase is not StepPhase.NONE or snapshot.transaction is not None:
            return Reduction(snapshot, False, "another step transaction is active")
        if snapshot.health_state is not HealthState.READY:
            return Reduction(snapshot, False, "health gate is not green")
        accept = snapshot.line_state is LineState.RUNNING
        if accept_input_for_step is not None:
            accept = bool(accept_input_for_step)
        if snapshot.line_state is LineState.STOPPING and accept:
            return Reduction(snapshot, False, "STOPPING may not accept INPUT")
        transaction = StepTransaction(
            transaction_id=transaction_id or uuid4().hex,
            run_id=snapshot.run_id,
            accept_input_for_step=accept,
            pending_transfer_part_id=pending_transfer_part_id,
            route_category=route_category,
            route_targets=route_targets or {},
            start_last_ready_ms=start_last_ready_ms,
            expected_target=expected_target,
        )
        return Reduction(
            snapshot.published(
                step_phase=StepPhase.HEALTH_GATE,
                transaction=transaction,
                persistence_state=PersistenceState.IDLE,
            ),
            True,
        )

    @staticmethod
    def set_phase(
        snapshot: LineSnapshot,
        phase: StepPhase | str,
        *,
        transaction_id: str | None = None,
        frozen_roles: Iterable[str] = (),
    ) -> Reduction:
        try:
            phase = phase if isinstance(phase, StepPhase) else StepPhase(str(phase))
        except ValueError:
            return Reduction(snapshot, False, f"unknown step phase: {phase}")
        stale = LineReducer._stale(snapshot, transaction_id)
        if stale:
            return stale
        current = snapshot.step_phase
        if phase not in _PHASE_TRANSITIONS.get(current, frozenset()):
            return Reduction(snapshot, False, f"invalid phase transition {current.value} -> {phase.value}")
        if phase is StepPhase.NONE:
            return Reduction(
                snapshot.published(
                    step_phase=StepPhase.NONE,
                    transaction=None,
                    display_state=DisplayState.LIVE,
                    display_roles={role: "live" for role in snapshot.display_roles},
                ),
                True,
            )
        changes: dict[str, Any] = {"step_phase": phase}
        if phase is StepPhase.JOURNAL_INTENT:
            changes["persistence_state"] = PersistenceState.INTENT_PENDING
        elif phase is StepPhase.PERSIST:
            changes["persistence_state"] = PersistenceState.STAGE_PENDING
        elif phase is StepPhase.REVIEW:
            roles = tuple(dict.fromkeys(frozen_roles))
            if not roles:
                return Reduction(snapshot, False, "REVIEW requires at least one frozen role")
            changes["display_state"] = DisplayState.REVIEW
            changes["display_roles"] = {role: "frozen" for role in roles}
        elif phase is StepPhase.CLEAR_REVIEW:
            changes["display_state"] = DisplayState.LIVE
            changes["display_roles"] = {role: "live" for role in snapshot.display_roles}
        return Reduction(snapshot.published(**changes), True)

    @staticmethod
    def motion_command_issued(snapshot: LineSnapshot, transaction_id: str) -> Reduction:
        stale = LineReducer._stale(snapshot, transaction_id)
        if stale:
            return stale
        if snapshot.step_phase is not StepPhase.MOTION_COMMAND:
            return Reduction(snapshot, False, "motion command is not expected in this phase")
        transaction = snapshot.transaction
        assert transaction is not None
        if transaction.command_issued:
            # This is an invariant violation, not permission to send again.
            return Reduction(snapshot, False, "motion command already issued")
        return Reduction(
            snapshot.published(
                transaction=replace(transaction, command_issued=True),
            ),
            True,
            effects=("POLL_MOTION_ONLY",),
        )

    @staticmethod
    def intent_committed(snapshot: LineSnapshot, transaction_id: str) -> Reduction:
        stale = LineReducer._stale(snapshot, transaction_id)
        if stale:
            return stale
        if snapshot.step_phase is not StepPhase.JOURNAL_INTENT:
            return Reduction(snapshot, False, "journal intent is not expected")
        return Reduction(
            snapshot.published(persistence_state=PersistenceState.INTENT_COMMITTED),
            True,
        )

    @staticmethod
    def commit_step(
        snapshot: LineSnapshot,
        transaction_id: str,
        *,
        exact_motion_proof: bool,
        armed_target_seen: bool = True,
        ready_epoch_changed: bool = True,
        final_reset_seen: bool = True,
    ) -> Reduction:
        stale = LineReducer._stale(snapshot, transaction_id)
        if stale:
            return stale
        if snapshot.step_phase not in {StepPhase.MOTION_CONFIRM, StepPhase.STEP_COMMIT}:
            return Reduction(snapshot, False, "STEP_COMMIT is not expected")
        transaction = snapshot.transaction
        assert transaction is not None
        if transaction.step_committed:
            return Reduction(snapshot, True, "step already committed")
        complete = bool(
            exact_motion_proof
            and transaction.command_issued
            and armed_target_seen
            and ready_epoch_changed
            and final_reset_seen
        )
        if not complete:
            return Reduction(snapshot, False, "CYCLE_EVIDENCE_MISSING")
        transaction = replace(
            transaction,
            armed_target_seen=True,
            ready_epoch_changed=True,
            final_reset_seen=True,
            step_committed=True,
        )
        return Reduction(
            snapshot.published(
                step_phase=StepPhase.STEP_COMMIT,
                current_step=snapshot.current_step + 1,
                transaction=transaction,
            ),
            True,
        )

    @staticmethod
    def commit_transfer(
        snapshot: LineSnapshot,
        transaction_id: str,
        *,
        category: str,
    ) -> Reduction:
        stale = LineReducer._stale(snapshot, transaction_id)
        if stale:
            return stale
        if snapshot.step_phase not in {StepPhase.STEP_COMMIT, StepPhase.TRANSFER_COMMIT}:
            return Reduction(snapshot, False, "TRANSFER_COMMIT is not expected")
        transaction = snapshot.transaction
        assert transaction is not None
        part_id = transaction.pending_transfer_part_id
        if part_id is None:
            return Reduction(snapshot, False, "no pending transfer part")
        if not transaction.step_committed:
            return Reduction(snapshot, False, "physical step is not committed")
        if transaction.transfer_committed:
            return Reduction(snapshot, True, "transfer already committed")
        normalized = str(category).upper()
        if normalized not in {"GOOD", "BAD", "CLEANUP"}:
            return Reduction(snapshot, False, f"invalid transfer category: {category}")
        if part_id not in snapshot.parts:
            return Reduction(snapshot, False, "pending transfer part is not tracked")
        parts = dict(snapshot.parts)
        del parts[part_id]
        counts = snapshot.counters
        counters = Counters(
            total=counts.total,
            good=counts.good + (normalized == "GOOD"),
            bad=counts.bad + (normalized == "BAD"),
            cleanup=counts.cleanup + (normalized == "CLEANUP"),
            empty=counts.empty,
        )
        return Reduction(
            snapshot.published(
                step_phase=StepPhase.TRANSFER_COMMIT,
                transaction=replace(transaction, transfer_committed=True),
                parts=parts,
                counters=counters,
                persistence_state=PersistenceState.ARCHIVE_PENDING,
            ),
            True,
            effects=("FINALIZE_ARCHIVE",),
        )

    @staticmethod
    def stage_persisted(snapshot: LineSnapshot, transaction_id: str) -> Reduction:
        stale = LineReducer._stale(snapshot, transaction_id)
        if stale:
            return stale
        if snapshot.step_phase is not StepPhase.PERSIST:
            return Reduction(snapshot, False, "stage persistence is not expected")
        return Reduction(
            snapshot.published(persistence_state=PersistenceState.STAGE_COMMITTED), True,
        )

    @staticmethod
    def archive_finalized(snapshot: LineSnapshot, transaction_id: str) -> Reduction:
        stale = LineReducer._stale(snapshot, transaction_id)
        if stale:
            return stale
        transaction = snapshot.transaction
        assert transaction is not None
        if not transaction.transfer_committed:
            return Reduction(snapshot, False, "physical transfer is not committed")
        return Reduction(
            snapshot.published(persistence_state=PersistenceState.ARCHIVE_COMMITTED), True,
        )

    @staticmethod
    def post_motion_gate(snapshot: LineSnapshot, transaction_id: str) -> Reduction:
        stale = LineReducer._stale(snapshot, transaction_id)
        if stale:
            return stale
        if snapshot.step_phase not in {StepPhase.STEP_COMMIT, StepPhase.TRANSFER_COMMIT}:
            return Reduction(snapshot, False, "post-motion gate is not expected")
        transaction = snapshot.transaction
        assert transaction is not None
        if not transaction.step_committed:
            return Reduction(snapshot, False, "step is not committed")
        if transaction.pending_transfer_part_id is not None and not transaction.transfer_committed:
            return Reduction(snapshot, False, "transfer commit is mandatory")
        if (
            transaction.pending_transfer_part_id is not None
            and snapshot.persistence_state is not PersistenceState.ARCHIVE_COMMITTED
        ):
            return Reduction(snapshot, False, "archive finalization is mandatory after transfer")
        changes: dict[str, Any] = {"step_phase": StepPhase.POST_MOTION_GATE}
        effects: tuple[str, ...] = ()
        if snapshot.pending_intent is PendingIntent.PAUSE:
            changes.update(
                line_state=LineState.PAUSED,
                pending_intent=PendingIntent.NONE,
                pause_continuation=PauseContinuation.INSPECT_COMMITTED_STEP,
            )
            effects = ("WAIT_FOR_RESUME_BEFORE_SETTLE",)
        return Reduction(snapshot.published(**changes), True, effects=effects)

    @staticmethod
    def command_gate(snapshot: LineSnapshot, transaction_id: str) -> Reduction:
        stale = LineReducer._stale(snapshot, transaction_id)
        if stale:
            return stale
        if snapshot.step_phase is not StepPhase.COMMAND_GATE:
            return Reduction(snapshot, False, "COMMAND_GATE is not active")
        intent = snapshot.pending_intent
        state = snapshot.line_state
        changes: dict[str, Any] = {
            "step_phase": StepPhase.NONE,
            "transaction": None,
            "pending_intent": PendingIntent.NONE,
            "display_state": DisplayState.LIVE,
            "display_roles": {role: "live" for role in snapshot.display_roles},
        }
        effects: tuple[str, ...] = ()
        if intent in {PendingIntent.STOP, PendingIntent.EXIT}:
            changes["line_state"] = LineState.STOPPING
            changes["pause_continuation"] = None
            if intent is PendingIntent.EXIT:
                changes["exit_after_drain"] = True
            effects = ("DRAIN_BOUNDARY",)
        elif intent is PendingIntent.PAUSE and state is LineState.RUNNING:
            changes["line_state"] = LineState.PAUSED
            changes["pause_continuation"] = PauseContinuation.NEXT_STEP
        elif state is LineState.STOPPING:
            effects = ("DRAIN_BOUNDARY",)
        return Reduction(snapshot.published(**changes), True, effects=effects)

    @staticmethod
    def jog_moved(snapshot: LineSnapshot) -> Reduction:
        if snapshot.line_state not in {LineState.IDLE, LineState.STOPPED, LineState.PAUSED}:
            return Reduction(snapshot, False, "JOG movement is not allowed in this state")
        if snapshot.jog_happened:
            return Reduction(snapshot, True, "JOG movement already recorded")
        return Reduction(snapshot.published(jog_happened=True), True)

    @staticmethod
    def line_empty(snapshot: LineSnapshot) -> Reduction:
        if snapshot.line_state is not LineState.STOPPING:
            return Reduction(snapshot, False, "line-empty event requires STOPPING")
        if snapshot.parts:
            return Reduction(snapshot, False, "tracked parts remain on the line")
        if snapshot.step_phase is not StepPhase.NONE:
            return Reduction(snapshot, False, "a step transaction is still active")
        # STOPPED is not published before both distributor axes are confirmed
        # home.  No conveyor motion effect exists on this path.
        home_tx = StepTransaction(
            transaction_id=f"home-{uuid4().hex}",
            run_id=snapshot.run_id,
            accept_input_for_step=False,
        )
        return Reduction(
            snapshot.published(
                step_phase=StepPhase.DISTRIBUTOR_HOME,
                transaction=home_tx,
            ),
            True,
            effects=("HOME_DISTRIBUTOR_SEQUENTIALLY",),
        )

    @staticmethod
    def distributor_homed(snapshot: LineSnapshot, transaction_id: str) -> Reduction:
        stale = LineReducer._stale(snapshot, transaction_id)
        if stale:
            return stale
        if snapshot.line_state is not LineState.STOPPING or snapshot.step_phase is not StepPhase.DISTRIBUTOR_HOME:
            return Reduction(snapshot, False, "distributor home confirmation is not expected")
        destination = (
            LineState.SHUTTING_DOWN if snapshot.exit_after_drain else LineState.STOPPED
        )
        return Reduction(
            snapshot.published(
                line_state=destination,
                step_phase=StepPhase.NONE,
                transaction=None,
            ),
            True,
            effects=(("CONTROLLED_SHUTDOWN",) if destination is LineState.SHUTTING_DOWN else ()),
        )

    @staticmethod
    def shutdown_completed(snapshot: LineSnapshot) -> Reduction:
        if snapshot.line_state is not LineState.SHUTTING_DOWN:
            return Reduction(snapshot, False, "shutdown completion requires SHUTTING_DOWN")
        return Reduction(snapshot.published(line_state=LineState.TERMINATED), True)

    @staticmethod
    def fault(
        snapshot: LineSnapshot,
        fault: Any,
        *,
        positions_known: bool | None = None,
    ) -> Reduction:
        if snapshot.line_state is LineState.TERMINATED:
            return Reduction(snapshot, False, "terminated process ignores faults")
        if snapshot.root_fault is not None:
            return Reduction(
                snapshot.published(
                    secondary_faults=snapshot.secondary_faults + (fault,),
                ),
                True,
                effects=("PUBLISH_SECONDARY_FAULT",),
            )
        live_roles = {role: "live" for role in snapshot.display_roles}
        return Reduction(
            snapshot.published(
                line_state=LineState.FAULT,
                step_phase=StepPhase.NONE,
                transaction=None,
                pending_intent=PendingIntent.NONE,
                pause_continuation=None,
                root_fault=fault,
                health_state=HealthState.FAILED,
                persistence_state=(
                    PersistenceState.FAILED
                    if snapshot.persistence_state in {
                        PersistenceState.INTENT_PENDING,
                        PersistenceState.STAGE_PENDING,
                        PersistenceState.ARCHIVE_PENDING,
                    }
                    else snapshot.persistence_state
                ),
                display_state=DisplayState.LIVE,
                display_roles=live_roles,
                positions_known=(
                    snapshot.positions_known if positions_known is None else positions_known
                ),
            ),
            True,
            effects=(
                "BLOCK_HARDWARE_COMMANDS",
                "BEST_EFFORT_EMERGENCY_STOP",
                "CANCEL_UNSTARTED_WORKERS",
                "SAVE_DIAGNOSTIC_SNAPSHOT",
            ),
        )

    @staticmethod
    def _stale(snapshot: LineSnapshot, transaction_id: str | None) -> Reduction | None:
        active = snapshot.active_transaction_id
        if not transaction_id or active != transaction_id:
            return Reduction(
                snapshot,
                False,
                f"stale transaction event: {transaction_id!r}; active={active!r}",
                stale=True,
            )
        return None


def _stronger_intent(current: PendingIntent, incoming: PendingIntent) -> PendingIntent:
    """STOP/EXIT dominate PAUSE; once EXIT is set, STOP cannot erase it."""
    rank = {
        PendingIntent.NONE: 0,
        PendingIntent.PAUSE: 1,
        PendingIntent.STOP: 2,
        PendingIntent.EXIT: 3,
    }
    return incoming if rank[incoming] > rank[current] else current
