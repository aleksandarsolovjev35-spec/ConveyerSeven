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
    cycle.parts = []
    cycle.current_step = 1
    cycle.OFFSET_INPUT = 0
    cycle.OFFSET_SPIDER = 4
    cycle.OFFSET_REJECT = 7
    return cycle


def _frame(value=0):
    return np.full((8, 8, 3), value, dtype=np.uint8)


class StageCaptureTest(unittest.TestCase):
    def test_captures_once(self):
        frame = _frame()
        cycle = _make_cycle()
        cycle.cameras = SimpleNamespace(
            drain_buffers=Mock(),
            capture_all=Mock(return_value={"INPUT_LEFT": frame}),
        )
        cycle.sm = SimpleNamespace(accepts_new_parts=False)

        frame_runs = cycle._stage_capture()

        cycle.cameras.capture_all.assert_called_once_with()
        cycle.cameras.drain_buffers.assert_called_once_with()
        self.assertEqual(len(frame_runs), 1)
        self.assertIs(frame_runs[0]["INPUT_LEFT"], frame)


class StageAnalysisTest(unittest.TestCase):
    def _result(self, name="window_geometry"):
        rule = RuleResult(
            rule_name=name,
            triggered=False,
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
        display = cycle._stage_analysis([{"INPUT_LEFT": frame}], True)

        # display подменяется evidence-кадрами стадии: сверяем ключи и объекты
        self.assertEqual(set(display), {"INPUT_LEFT"})
        self.assertIs(display["INPUT_LEFT"], input_result.raw_frames["INPUT_LEFT"])

        # Единый снимок: один вызов _refresh_monitor с кадрами, run_frames
        # и run_rule_results (numpy-массивы не сравниваются assertEqual)
        cycle._refresh_monitor.assert_called_once()
        args, kwargs = cycle._refresh_monitor.call_args
        self.assertEqual(set(args[0]), {"INPUT_LEFT"})
        self.assertIs(args[0]["INPUT_LEFT"], input_result.raw_frames["INPUT_LEFT"])
        self.assertEqual(len(kwargs["run_frames"]), 1)
        self.assertIs(kwargs["run_frames"][0]["INPUT_LEFT"],
                      input_result.raw_frames["INPUT_LEFT"])
        self.assertEqual(
            kwargs["run_rule_results"],
            [[input_result.run_rule_results[0][0]]],
        )

    def test_publishes_input_and_spider_merged(self):
        cycle = _make_cycle()
        cycle.sm = SimpleNamespace(accepts_new_parts=True)
        cycle._last_vision_results = {}
        cycle._last_rule_results = []
        cycle._refresh_monitor = Mock()
        # Корпус на позиции +4
        cycle.parts = [SimpleNamespace(step_created=-3)]

        input_result = self._result("window_geometry")
        spider_rule = RuleResult(
            rule_name="contacts_long",
            triggered=True,
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

        frame = _frame()
        cycle._stage_analysis([{"INPUT_LEFT": frame}], True)

        args, kwargs = cycle._refresh_monitor.call_args
        self.assertEqual(len(kwargs["run_frames"]), 1)
        self.assertEqual(set(kwargs["run_frames"][0]),
                         {"INPUT_LEFT", "SPIDER_LEFT"})
        merged_rules = kwargs["run_rule_results"][0]
        self.assertEqual(
            [r.rule_name for r in merged_rules],
            ["window_geometry", "contacts_long"],
        )


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
