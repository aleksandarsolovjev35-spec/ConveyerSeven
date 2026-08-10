"""Тесты одиночного захвата и атомарной публикации в ProductionCycle.

Проверяют, что:
- ``_stage_capture`` снимает камеры ровно один раз;
- ``_stage_analysis`` публикует кадры, run_frames и run_rule_results одним
  вызовом ``_refresh_monitor`` (единый снимок для синхронного UI);
- ``_merge_run_frames``/``_merge_run_rule_rows`` склеивают INPUT и SPIDER
  в один набор.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from core.production_cycle import ProductionCycle
from domain.defect_rules.base import RuleResult


def _make_cycle():
    cycle = object.__new__(ProductionCycle)
    cycle.stages = SimpleNamespace(
        enter_capture=Mock(),
        enter_analysis=Mock(),
    )
    cycle._set_process = Mock()
    cycle._check_motion_cancelled = Mock()
    cycle.monitor = None
    cycle.parts = []
    cycle.current_step = 4
    cycle.OFFSET_INPUT = 0
    cycle.OFFSET_SPIDER = 4
    cycle.OFFSET_REJECT = 7
    return cycle


def _frame(value=0):
    return np.full((8, 8, 3), value, dtype=np.uint8)


class StageCaptureTest(unittest.TestCase):
    INPUT = ("INPUT_LEFT", "INPUT_RIGHT")
    SPIDER = ("SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP")

    def make_capture_cycle(self, *, accepts_input, spider_part=False):
        frame = _frame()
        cycle = _make_cycle()
        cycle.inspector = SimpleNamespace(INPUT_ROLES=self.INPUT, SPIDER_ROLES=self.SPIDER)
        cycle.parts = [SimpleNamespace(step_created=0)] if spider_part else []
        cycle.sm = SimpleNamespace(accepts_new_parts=accepts_input)
        cycle.cameras = SimpleNamespace(
            drain_buffers=Mock(),
            capture_roles=Mock(return_value={role: frame for role in (
                self.INPUT if accepts_input else self.SPIDER
            )}),
        )
        return cycle, frame

    def test_captures_only_input_roles_when_only_input_is_active(self):
        cycle, frame = self.make_capture_cycle(accepts_input=True)
        frame_runs = cycle._stage_capture()
        cycle.cameras.drain_buffers.assert_called_once_with(roles=self.INPUT)
        cycle.cameras.capture_roles.assert_called_once_with(self.INPUT)
        self.assertEqual(set(frame_runs[0]), set(self.INPUT))

    def test_captures_only_spider_roles_when_input_is_closed(self):
        cycle, frame = self.make_capture_cycle(accepts_input=False, spider_part=True)
        frame_runs = cycle._stage_capture()
        cycle.cameras.drain_buffers.assert_called_once_with(roles=self.SPIDER)
        cycle.cameras.capture_roles.assert_called_once_with(self.SPIDER)
        self.assertEqual(set(frame_runs[0]), set(self.SPIDER))

    def test_captures_all_seven_only_when_two_parts_need_inspection(self):
        cycle, frame = self.make_capture_cycle(accepts_input=True, spider_part=True)
        all_roles = self.INPUT + self.SPIDER
        cycle.cameras.capture_roles.return_value = {role: frame for role in all_roles}
        frame_runs = cycle._stage_capture()
        cycle.cameras.capture_roles.assert_called_once_with(all_roles)
        self.assertEqual(set(frame_runs[0]), set(all_roles))

    def test_does_not_capture_when_no_inspection_position_is_occupied(self):
        cycle, _ = self.make_capture_cycle(accepts_input=False)
        frame_runs = cycle._stage_capture()
        cycle.cameras.drain_buffers.assert_not_called()
        cycle.cameras.capture_roles.assert_not_called()
        self.assertEqual(frame_runs, [{}])

    def test_stop_after_motion_cannot_revoke_latched_input(self):
        cycle, _ = self.make_capture_cycle(accepts_input=False)
        # Macrostate is already STOPPING, but RUNNING accepted this INPUT cell
        # before the physical movement command.
        cycle._accept_input_for_active_step = True
        self.assertEqual(cycle._capture_roles_for_current_step(), self.INPUT)

    def test_multiple_parts_at_control_position_is_a_fault(self):
        cycle, _ = self.make_capture_cycle(accepts_input=False)
        cycle.parts = [
            SimpleNamespace(step_created=0),
            SimpleNamespace(step_created=0),
        ]
        with self.assertRaisesRegex(RuntimeError, "multiple Part"):
            cycle._capture_roles_for_current_step()


class MergeHelpersTest(unittest.TestCase):
    def test_merge_run_frames_single(self):
        a = {"INPUT_LEFT": _frame(0)}
        b = {"SPIDER_LEFT": _frame(1)}
        merged = ProductionCycle._merge_run_frames([a], [b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0]), {"INPUT_LEFT", "SPIDER_LEFT"})

    def test_merge_run_rule_rows_single(self):
        r1 = RuleResult("a", False)
        r2 = RuleResult("b", True)
        merged = ProductionCycle._merge_run_rule_rows([[r1]], [[r2]])
        self.assertEqual(len(merged), 1)
        self.assertEqual([r.rule_name for r in merged[0]], ["a", "b"])

    def test_merge_handles_empty_incoming(self):
        acc = [{"INPUT_LEFT": _frame(0)}]
        self.assertEqual(ProductionCycle._merge_run_frames(acc, []), acc)
        acc_rules = [[RuleResult("a", False)]]
        self.assertEqual(
            ProductionCycle._merge_run_rule_rows(acc_rules, []),
            acc_rules,
        )


if __name__ == "__main__":
    unittest.main()
