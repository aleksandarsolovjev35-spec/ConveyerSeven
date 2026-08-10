"""Executable invariants for the orthogonal production automaton."""

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from core.atomic_publisher import AtomicPublisher, InspectionExecutionResult
from core.command_arbiter import CommandArbiter
from core.control_core import ControlCore
from core.control_runtime import ControlRuntime
from core.control_model import (
    CommandGuards, Counters, DisplayState, HealthState, LineSnapshot, LineState,
    PauseContinuation, PendingIntent, StepPhase,
)
from core.events import CoreEvent, EventGroup
from core.line_reducer import LineReducer
from core.motion_transaction import CycleEvidenceMissing, MotionTransaction
from core.part_tracker import PartTracker, TrackingInvariantError


def running():
    state = LineReducer.initial("batch")
    state = LineReducer.boot_completed(state).snapshot
    return LineReducer.command(state, "START").snapshot


def at_motion_confirm(state, transaction_id="tx"):
    state = LineReducer.begin_step(state, transaction_id=transaction_id).snapshot
    state = LineReducer.set_phase(state, StepPhase.JOURNAL_INTENT,
                                  transaction_id=transaction_id).snapshot
    state = LineReducer.intent_committed(state, transaction_id).snapshot
    state = LineReducer.set_phase(state, StepPhase.MOTION_COMMAND,
                                  transaction_id=transaction_id).snapshot
    state = LineReducer.motion_command_issued(state, transaction_id).snapshot
    return LineReducer.set_phase(state, StepPhase.MOTION_CONFIRM,
                                 transaction_id=transaction_id).snapshot


class FormalAutomatonInvariantTest(unittest.TestCase):
    def test_one_motion_command_per_transaction(self):
        calls = []
        tx = MotionTransaction("tx", 1, start_last_ready_ms=10)
        tx.commit_intent(lambda *args, **kwargs: None)
        tx.issue_once(lambda: calls.append("G3"))
        with self.assertRaises(Exception):
            tx.issue_once(lambda: calls.append("G3"))
        self.assertEqual(calls, ["G3"])

    def test_current_step_only_changes_after_exact_confirmation(self):
        state = at_motion_confirm(running())
        rejected = LineReducer.commit_step(
            state, "tx", exact_motion_proof=False,
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.snapshot.current_step, 0)
        committed = LineReducer.commit_step(
            state, "tx", exact_motion_proof=True,
        )
        self.assertTrue(committed.accepted)
        self.assertEqual(committed.snapshot.current_step, 1)
        repeated = LineReducer.commit_step(
            committed.snapshot, "tx", exact_motion_proof=True,
        )
        self.assertEqual(repeated.snapshot.current_step, 1)

    def test_duplicate_command_id_does_not_call_handler_twice(self):
        calls = []
        arbiter = CommandArbiter(
            lambda command: calls.append(command) or True,
            lambda: {"state": "IDLE", "state_version": 1},
        )
        first = arbiter.submit("same", "START")
        duplicate = arbiter.submit("same", "START")
        self.assertTrue(first.accepted)
        self.assertIs(duplicate, first)
        self.assertEqual(calls, ["START"])

    def test_runtime_routes_positional_jog_through_arbiter(self):
        cycle = _JogCycle()
        runtime = ControlRuntime(cycle)
        result = runtime.dispatch("jog-1", "JOG", "+")
        self.assertTrue(result.accepted)
        self.assertEqual(cycle.jog_calls, ["+"])
        self.assertIs(runtime.dispatch("jog-1", "JOG", "+"), result)
        self.assertEqual(cycle.jog_calls, ["+"])

    def test_part_is_created_only_after_presence_and_not_for_empty(self):
        empty = PartTracker.commit_input_presence(
            {}, presence_by_role={"INPUT_LEFT": False, "INPUT_RIGHT": False},
            next_part_id=1, birth_step=0, batch_id="b", part_factory=_Part,
        )
        self.assertIsNone(empty.created_part)
        self.assertEqual(empty.empty_increment, 1)
        present = PartTracker.commit_input_presence(
            {}, presence_by_role={"INPUT_LEFT": True, "INPUT_RIGHT": False},
            next_part_id=1, birth_step=0, batch_id="b", part_factory=_Part,
        )
        self.assertEqual(present.created_part.id, 1)
        self.assertTrue(present.mismatch)

    def test_stop_during_motion_keeps_input_latch(self):
        state = at_motion_confirm(running())
        self.assertTrue(state.transaction.accept_input_for_step)
        stopped = LineReducer.command(state, "STOP").snapshot
        self.assertEqual(stopped.line_state, LineState.RUNNING)
        self.assertEqual(stopped.pending_intent, PendingIntent.STOP)
        self.assertTrue(stopped.transaction.accept_input_for_step)

    def test_pause_during_motion_waits_before_snapshot(self):
        state = at_motion_confirm(running())
        state = LineReducer.command(state, "PAUSE").snapshot
        self.assertEqual(state.line_state, LineState.RUNNING)
        self.assertEqual(state.pending_intent, PendingIntent.PAUSE)
        state = LineReducer.commit_step(state, "tx", exact_motion_proof=True).snapshot
        state = LineReducer.post_motion_gate(state, "tx").snapshot
        self.assertEqual(state.line_state, LineState.PAUSED)
        self.assertEqual(
            state.pause_continuation,
            PauseContinuation.INSPECT_COMMITTED_STEP,
        )
        self.assertEqual(state.step_phase, StepPhase.POST_MOTION_GATE)

    def test_stop_and_pause_do_not_clear_review(self):
        state = self._review_state()
        for command, expected in (("PAUSE", PendingIntent.PAUSE),
                                  ("STOP", PendingIntent.STOP)):
            changed = LineReducer.command(state, command).snapshot
            self.assertEqual(changed.step_phase, StepPhase.REVIEW)
            self.assertEqual(changed.display_state, DisplayState.REVIEW)
            self.assertEqual(changed.pending_intent, expected)

    def test_stopping_never_accepts_input(self):
        state = LineReducer.command(running(), "STOP").snapshot
        begun = LineReducer.begin_step(state, transaction_id="drain")
        self.assertTrue(begun.accepted)
        self.assertFalse(begun.snapshot.transaction.accept_input_for_step)
        rejected = LineReducer.begin_step(
            LineReducer.command(running(), "STOP").snapshot,
            transaction_id="bad", accept_input_for_step=True,
        )
        self.assertFalse(rejected.accepted)

    def test_frozen_roles_exist_only_during_review(self):
        state = self._review_state()
        self.assertEqual(dict(state.display_roles), {"INPUT_LEFT": "frozen"})
        cleared = LineReducer.set_phase(
            state, StepPhase.CLEAR_REVIEW, transaction_id="tx",
        ).snapshot
        self.assertEqual(cleared.display_state, DisplayState.LIVE)
        self.assertNotIn("frozen", cleared.display_roles.values())
        with self.assertRaises(ValueError):
            LineSnapshot(
                line_state=LineState.IDLE,
                display_roles={"INPUT_LEFT": "frozen"},
            )

    def test_atomic_result_has_one_exact_state_version(self):
        state = running()
        published = []
        publisher = AtomicPublisher(lambda snap: published.append(snap) or snap.state_version)
        version = publisher.publish(state)
        result = InspectionExecutionResult(state, version, aggregate={"complete": True})
        self.assertEqual(result.publication_version, result.snapshot.state_version)
        self.assertEqual(published, [state])

    def test_route_latches_are_immutable_and_retained_to_commit(self):
        state = LineReducer.begin_step(
            running(), transaction_id="tx", pending_transfer_part_id=7,
            route_category="BAD", route_targets={"DIST1": 340, "DIST2": 0},
        ).snapshot
        with self.assertRaises(TypeError):
            state.transaction.route_targets["DIST1"] = 0
        state = LineReducer.set_phase(state, StepPhase.ROUTE_PREPARE,
                                      transaction_id="tx").snapshot
        state = LineReducer.set_phase(state, StepPhase.JOURNAL_INTENT,
                                      transaction_id="tx").snapshot
        self.assertEqual(state.transaction.route_category, "BAD")

    def test_unconfirmed_motion_does_not_change_step(self):
        state = at_motion_confirm(running())
        with self.assertRaises(CycleEvidenceMissing):
            tx = MotionTransaction("m", 1, start_last_ready_ms=10)
            tx.commit_intent(lambda *args, **kwargs: None)
            tx.issue_once(lambda: None)
            tx.commit()
        self.assertEqual(state.current_step, 0)

    def test_part_removed_at_transfer_cannot_be_sorted_again(self):
        state = running().replace(
            parts={1: _Part(1, birth_step=0, batch_id="b")},
            counters=Counters(total=1),
        )
        state = at_motion_confirm(state)
        # Replace the transaction with a pre-latched transfer identity as it
        # would have been before command issue.
        tx = state.transaction
        state = state.replace(transaction=tx.__class__(
            transaction_id=tx.transaction_id, run_id=tx.run_id,
            accept_input_for_step=tx.accept_input_for_step,
            pending_transfer_part_id=1, route_category="GOOD",
            command_issued=True,
        ))
        state = LineReducer.commit_step(state, "tx", exact_motion_proof=True).snapshot
        transferred = LineReducer.commit_transfer(state, "tx", category="GOOD")
        self.assertTrue(transferred.accepted)
        self.assertNotIn(1, transferred.snapshot.parts)
        repeated = LineReducer.commit_transfer(transferred.snapshot, "tx", category="GOOD")
        self.assertEqual(repeated.snapshot.counters.good, 1)

    def test_root_fault_is_not_replaced(self):
        state = LineReducer.fault(running(), {"code": "ROOT"}).snapshot
        state = LineReducer.fault(state, {"code": "SECONDARY"}).snapshot
        self.assertEqual(state.root_fault["code"], "ROOT")
        self.assertEqual(state.secondary_faults[0]["code"], "SECONDARY")

    def test_fault_has_no_return_to_production(self):
        state = LineReducer.fault(running(), {"code": "ROOT"}).snapshot
        self.assertFalse(LineReducer.command(state, "START").accepted)
        self.assertFalse(LineReducer.command(state, "RESUME").accepted)

    def test_jog_does_not_change_logical_position(self):
        state = running()
        state = LineReducer.command(state, "PAUSE").snapshot
        before = state.current_step
        self.assertTrue(LineReducer.command(state, "JOG_START").accepted)
        moved = LineReducer.jog_moved(state).snapshot
        self.assertTrue(moved.jog_happened)
        self.assertEqual(moved.current_step, before)

    def test_stale_transaction_event_is_ignored(self):
        state = at_motion_confirm(running())
        core = ControlCore(state)
        event = CoreEvent(
            EventGroup.HARDWARE, "MotionConfirmed", run_id=state.run_id,
            transaction_id="old",
            payload={"exact_motion_proof": True, "armed_target_seen": True,
                     "ready_epoch_changed": True, "final_reset_seen": True},
        )
        result = core.handle_event(event)
        self.assertTrue(result.stale)
        self.assertEqual(core.snapshot.current_step, 0)

    def test_empty_drain_homes_axes_without_conveyor_motion(self):
        state = LineReducer.command(running(), "STOP").snapshot
        result = LineReducer.line_empty(state)
        self.assertTrue(result.accepted)
        self.assertEqual(result.snapshot.step_phase, StepPhase.DISTRIBUTOR_HOME)
        self.assertEqual(result.effects, ("HOME_DISTRIBUTOR_SEQUENTIALLY",))
        self.assertNotIn("MOTION", " ".join(result.effects))

    @staticmethod
    def _review_state():
        state = at_motion_confirm(running())
        state = LineReducer.commit_step(state, "tx", exact_motion_proof=True).snapshot
        state = LineReducer.post_motion_gate(state, "tx").snapshot
        for phase in (StepPhase.SETTLE, StepPhase.CAPTURE, StepPhase.ANALYSIS,
                      StepPhase.PERSIST, StepPhase.PUBLISH):
            state = LineReducer.set_phase(state, phase, transaction_id="tx").snapshot
        return LineReducer.set_phase(
            state, StepPhase.REVIEW, transaction_id="tx",
            frozen_roles=("INPUT_LEFT",),
        ).snapshot


class _JogState:
    @staticmethod
    def get_snapshot():
        return {"state": "PAUSED", "state_version": 1}


class _JogCycle:
    def __init__(self):
        self.sm = _JogState()
        self.jog_calls = []
        self.state = "PAUSED"

    def jog_hold_start(self, direction):
        self.jog_calls.append(direction)
        return True


class _Part:
    def __init__(self, part_id, *, birth_step, batch_id):
        self.id = part_id
        self.birth_step = birth_step
        self.batch_id = batch_id
        self.defects = []

    def add_input_defect(self, defect):
        self.defects.append(defect)


if __name__ == "__main__":
    unittest.main()
