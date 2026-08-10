"""Тесты ProductionCycle: выбор ролей захвата и атомарная публикация."""

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
    cycle.current_step = 1
    cycle.OFFSET_INPUT = 0
    cycle.OFFSET_SPIDER = 4
    cycle.OFFSET_REJECT = 7
    return cycle


def _frame(value=0):
    return np.full((8, 8, 3), value, dtype=np.uint8)


INPUT = ("INPUT_LEFT", "INPUT_RIGHT")
SPIDER = ("SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP")


class StageCaptureTest(unittest.TestCase):
    def make_capture_cycle(self, *, accepts_input, spider_part=False):
        frame = _frame()
        cycle = _make_cycle()
        cycle.inspector = SimpleNamespace(INPUT_ROLES=INPUT, SPIDER_ROLES=SPIDER)
        cycle.parts = [SimpleNamespace(step_created=-3)] if spider_part else []
        cycle.sm = SimpleNamespace(accepts_new_parts=accepts_input)
        cycle.cameras = SimpleNamespace(
            drain_buffers=Mock(),
            capture_roles=Mock(return_value={role: frame for role in (
                INPUT if accepts_input else SPIDER
            )}),
        )
        return cycle, frame

    def test_captures_only_input_roles_when_only_input_is_active(self):
        cycle, frame = self.make_capture_cycle(accepts_input=True)
        frame_runs = cycle._stage_capture()
        cycle.cameras.drain_buffers.assert_called_once_with(roles=INPUT)
        cycle.cameras.capture_roles.assert_called_once_with(INPUT)
        self.assertEqual(set(frame_runs[0]), set(INPUT))

    def test_captures_only_spider_roles_when_input_is_closed(self):
        cycle, frame = self.make_capture_cycle(accepts_input=False, spider_part=True)
        frame_runs = cycle._stage_capture()
        cycle.cameras.drain_buffers.assert_called_once_with(roles=SPIDER)
        cycle.cameras.capture_roles.assert_called_once_with(SPIDER)
        self.assertEqual(set(frame_runs[0]), set(SPIDER))

    def test_does_not_capture_when_no_inspection_position_is_occupied(self):
        cycle, _ = self.make_capture_cycle(accepts_input=False)
        frame_runs = cycle._stage_capture()
        cycle.cameras.drain_buffers.assert_not_called()
        cycle.cameras.capture_roles.assert_not_called()
        self.assertEqual(frame_runs, [{}])


class StageAnalysisTest(unittest.TestCase):
    def _result(self, name="window_geometry"):
        rule = RuleResult(
            rule_name=name, triggered=False,
            details={"per_role": {
                "INPUT_LEFT": {"valid": True, "triggered": False},
            }},
            drawings=[],
        )
        frame = _frame()
        return SimpleNamespace(
            is_empty_tray=False,
            raw_frames={"INPUT_LEFT": frame},
            run_frames=[{"INPUT_LEFT": frame}],
            run_rule_results=[[rule]],
        )

    def test_publishes_atomic_snapshot(self):
        cycle = _make_cycle()
        cycle.sm = SimpleNamespace(accepts_new_parts=True)
        cycle._last_vision_results = {}
        cycle._last_rule_results = []
        cycle._refresh_monitor = Mock()

        input_result = self._result()
        cycle._process_input_stage = Mock(return_value=input_result)
        cycle._run_spider_inspection = Mock(return_value=None)

        frame = _frame()
        cycle._stage_analysis([{"INPUT_LEFT": frame}], True)

        cycle._refresh_monitor.assert_called_once()
        args, kwargs = cycle._refresh_monitor.call_args
        self.assertEqual(set(args[0]), {"INPUT_LEFT"})
        self.assertEqual(len(kwargs["run_frames"]), 1)

    def test_publishes_input_and_spider_merged(self):
        cycle = _make_cycle()
        cycle.sm = SimpleNamespace(accepts_new_parts=True)
        cycle._last_vision_results = {}
        cycle._last_rule_results = []
        cycle._refresh_monitor = Mock()
        cycle.parts = [SimpleNamespace(step_created=-3)]

        input_result = self._result("window_geometry")
        spider_rule = RuleResult(
            rule_name="contacts_long", triggered=True,
            details={"per_role": {
                "SPIDER_LEFT": {"valid": True, "triggered": True},
            }},
            drawings=[],
        )
        spider_result = SimpleNamespace(
            raw_frames={"SPIDER_LEFT": _frame(1)},
            run_frames=[{"SPIDER_LEFT": _frame(1)}],
            run_rule_results=[[spider_rule]],
        )
        cycle._process_input_stage = Mock(return_value=input_result)
        cycle._run_spider_inspection = Mock(return_value=spider_result)

        cycle._stage_analysis([{"INPUT_LEFT": _frame()}], True)

        args, kwargs = cycle._refresh_monitor.call_args
        self.assertEqual(
            set(kwargs["run_frames"][0]),
            {"INPUT_LEFT", "SPIDER_LEFT"},
        )


if __name__ == "__main__":
    unittest.main()
