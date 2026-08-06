"""Интеграционный тест Inspector при одиночном прогоне.

Проверяет, что ``inspect_input_consensus`` работает с ровно одним набором
кадров: part_presence оценивается по этому кадру, defect rules выполняются
один раз, run_frames/run_rule_results содержат по одному элементу,
consensus несёт runs=1.
"""

import unittest
from unittest.mock import Mock

import numpy as np

from domain.defect_rules.base import RuleResult
from domain.threshold_loader import ThresholdLoader
from inspection.inspector import Inspector


def _flatness_detections(count=3):
    return [
        {"class": "flatness", "confidence": 0.95, "bbox": [10, 10, 30, 30]}
        for _ in range(count)
    ]


class FakeVision:
    def __init__(self, detections_by_role):
        self._detections = detections_by_role
        self.last_health = []

    def process_all(self, stage_frames):
        self.last_health = []
        result = {}
        for role, frame in stage_frames.items():
            result[role] = self._detections.get(role, [])
            self.last_health.append({
                "role": role,
                "model": "fake",
                "ok": True,
                "run": 1,
                "elapsed_ms": 5.0,
                "detections": len(self._detections.get(role, [])),
            })
        return result


class FakeDecision:
    def __init__(self, thresholds, rule_results):
        self.thresholds = thresholds
        self._results = rule_results

    def evaluate_all_detailed(self, vision_results, frames=None):
        # Один вызов = один прогон (после отмены тройного голосования)
        self.calls = getattr(self, "calls", 0) + 1
        return [MockRuleResult(r) for r in self._results]


class MockRuleResult(RuleResult):
    def __init__(self, base):
        super().__init__(
            rule_name=base.rule_name,
            triggered=base.triggered,
            details=base.details,
            drawings=base.drawings,
        )


class FakeRecorder:
    def process(self, part_id, step, frames, rule_results):
        return {role: frame.copy() for role, frame in frames.items()}


def _make_inspector(detections, rule_results):
    thresholds = ThresholdLoader("thresholds.json").get_all()
    vision = FakeVision(detections)
    decision = FakeDecision(thresholds, rule_results)
    recorder = FakeRecorder()
    inspector = Inspector(vision, decision, recorder)
    return inspector, decision


class InspectorSingleRunTest(unittest.TestCase):
    def setUp(self):
        self.frame_left = np.zeros((8, 8, 3), dtype=np.uint8)
        self.frame_right = np.ones((8, 8, 3), dtype=np.uint8)

    def _frame_runs(self):
        return [{
            "INPUT_LEFT": self.frame_left,
            "INPUT_RIGHT": self.frame_right,
        }]

    def test_empty_tray_single_run(self):
        inspector, _ = _make_inspector({}, [])
        result = inspector.inspect_input_consensus(
            part_id=1,
            step=1,
            frame_runs=self._frame_runs(),
        )
        self.assertTrue(result.is_empty_tray)
        self.assertEqual(result.defects, [])
        self.assertEqual(result.consensus["runs"], 1)
        self.assertEqual(result.consensus["required_votes"], 1)
        self.assertEqual(
            result.consensus["part_presence"]["decision"], "empty",
        )
        self.assertEqual(len(result.run_frames), 1)
        self.assertEqual(result.run_rule_results, [[]])
        # Пустой лоток: defect-правила не выполняются
        self.assertEqual(len(result.rule_results), 1)  # только part_presence
        self.assertEqual(result.rule_results[0].rule_name, "part_presence")

    def test_present_single_run(self):
        detections = {
            "INPUT_LEFT": _flatness_detections(),
            "INPUT_RIGHT": _flatness_detections(),
        }
        defect = RuleResult(
            rule_name="window_geometry",
            triggered=False,
            details={"per_role": {
                "INPUT_LEFT": {"valid": True, "triggered": False},
                "INPUT_RIGHT": {"valid": True, "triggered": False},
            }},
            drawings=[],
        )
        inspector, decision = _make_inspector(detections, [defect])

        result = inspector.inspect_input_consensus(
            part_id=1,
            step=1,
            frame_runs=self._frame_runs(),
        )

        self.assertFalse(result.is_empty_tray)
        self.assertEqual(decision.calls, 1, "defect rules выполняются один раз")
        self.assertEqual(result.consensus["runs"], 1)
        self.assertEqual(result.consensus["part_presence"]["decision"], "present")
        # presence идёт первым, затем defect rules
        self.assertEqual(
            [r.rule_name for r in result.rule_results],
            ["part_presence", "window_geometry"],
        )
        self.assertEqual(len(result.run_frames), 1)
        self.assertEqual(len(result.run_rule_results), 1)
        self.assertEqual(len(result.run_rule_results[0]), 1)
        self.assertEqual(result.run_rule_results[0][0].rule_name,
                         "window_geometry")

    def test_rejects_two_run_input(self):
        # Старый формат (два набора кадров) должен быть отклонён
        inspector, _ = _make_inspector({}, [])
        frame_runs = self._frame_runs() + [self._frame_runs()[0]]
        with self.assertRaises(Exception):
            inspector.inspect_input_consensus(
                part_id=1, step=1, frame_runs=frame_runs,
            )


if __name__ == "__main__":
    unittest.main()
