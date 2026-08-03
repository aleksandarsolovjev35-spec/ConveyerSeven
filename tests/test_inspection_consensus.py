import unittest

import numpy as np

from core.production_cycle import ProductionCycle
from domain.defect_rules.base import RuleResult
from inspection.consensus import (
    InspectionConsensusError,
    combine_presence_results,
    combine_rule_results,
)
from inspection.inspector import Inspector


class Recorder:
    def __init__(self):
        self.calls = []

    def process(self, **kwargs):
        self.calls.append(kwargs)
        return {role: frame.copy() for role, frame in kwargs["frames"].items()}


class ScriptedVision:
    def __init__(self, counts_by_run):
        self.counts_by_run = counts_by_run
        self.last_health = []
        self.calls = 0

    @staticmethod
    def _detection(name, run_id, index=0):
        x1 = 1 + index
        return {
            "class": name,
            "confidence": 0.9,
            "bbox": [x1, 1, x1 + 4, 5],
            "mask": [
                [x1, 1], [x1 + 4, 1],
                [x1 + 4, 5], [x1, 5],
            ],
            "run_id": run_id,
        }

    def process_all(self, frames):
        self.calls += 1
        run_id = int(next(iter(frames.values()))[0, 0, 0])
        left_count, right_count = self.counts_by_run[run_id]
        self.last_health = [
            {
                "role": role,
                "model": f"weights/{role}.pt",
                "ok": True,
                "elapsed_ms": 1.0,
                "detections": 1,
                "error": None,
            }
            for role in frames
        ]
        result = {}
        for role, count in (
            ("INPUT_LEFT", left_count),
            ("INPUT_RIGHT", right_count),
        ):
            if role not in frames:
                continue
            detections = [self._detection("marker", run_id)]
            detections.extend(
                self._detection("flatness", run_id, index + 1)
                for index in range(count)
            )
            result[role] = detections
        return result


class ScriptedDecision:
    thresholds = {
        "INPUT_LEFT.input_window_geometry_min_confidence": 0.4,
        "INPUT_RIGHT.input_window_geometry_min_confidence": 0.4,
        "INPUT_LEFT.input_part_presence_false_positive_max_count": 2,
        "INPUT_RIGHT.input_part_presence_false_positive_max_count": 2,
    }

    def __init__(self, trigger_by_run=None):
        self.trigger_by_run = trigger_by_run or {1: True, 2: False, 3: True}
        self.calls = 0

    def evaluate_all_detailed(self, vision_results, frames=None):
        self.calls += 1
        marker = next(
            detection
            for detections in vision_results.values()
            for detection in detections
            if detection.get("class") == "marker"
        )
        run_id = int(marker["run_id"])
        triggered = bool(self.trigger_by_run[run_id])
        return [RuleResult(
            "window_geometry",
            triggered,
            details={"run_id": run_id},
            drawings=[],
        )]


def input_frame_runs():
    return [
        {
            "INPUT_LEFT": np.full((20, 20, 3), run_id, dtype=np.uint8),
            "INPUT_RIGHT": np.full((20, 20, 3), run_id, dtype=np.uint8),
        }
        for run_id in (1, 2, 3)
    ]


class InspectionConsensusTests(unittest.TestCase):
    def test_each_rule_uses_independent_majority_two_of_three(self):
        runs = [
            [
                RuleResult("rule_a", True, details={"run": 1}),
                RuleResult("rule_b", False, details={"run": 1}),
            ],
            [
                RuleResult("rule_a", False, details={"run": 2}),
                RuleResult("rule_b", False, details={"run": 2}),
            ],
            [
                RuleResult("rule_a", True, details={"run": 3}),
                RuleResult("rule_b", True, details={"run": 3}),
            ],
        ]
        final, metadata, evidence_index = combine_rule_results(runs)
        self.assertEqual([result.triggered for result in final], [True, False])
        self.assertEqual(
            final[0].details["consensus"]["triggered_votes"],
            2,
        )
        self.assertEqual(final[1].details["consensus"]["normal_votes"], 2)
        self.assertEqual(metadata["rules"]["rule_a"]["states"], [True, False, True])
        self.assertIn(evidence_index, (0, 1, 2))

    def test_mismatched_rule_set_fails_instead_of_returning_good(self):
        runs = [
            [RuleResult("rule_a", False)],
            [RuleResult("rule_b", False)],
            [RuleResult("rule_a", False)],
        ]
        with self.assertRaisesRegex(
            InspectionConsensusError,
            "порядок или набор правил",
        ):
            combine_rule_results(runs)

    def test_presence_majority_is_neutral_and_tracks_all_votes(self):
        results = [
            RuleResult("part_presence", False, {"empty_tray": False}),
            RuleResult("part_presence", False, {"empty_tray": True}),
            RuleResult("part_presence", False, {"empty_tray": True}),
        ]
        final, metadata, evidence_index = combine_presence_results(results)
        self.assertTrue(final.details["empty_tray"])
        self.assertFalse(final.triggered)
        self.assertEqual(metadata["empty_votes"], 2)
        self.assertEqual(metadata["present_votes"], 1)
        self.assertEqual(evidence_index, 2)

    def test_input_runs_rules_three_times_after_presence_majority_present(self):
        vision = ScriptedVision({1: (3, 3), 2: (0, 0), 3: (3, 3)})
        decision = ScriptedDecision({1: True, 2: False, 3: True})
        recorder = Recorder()
        result = Inspector(vision, decision, recorder).inspect_input_consensus(
            part_id=1,
            step=1,
            frame_runs=input_frame_runs(),
        )
        self.assertFalse(result.is_empty_tray)
        self.assertEqual(vision.calls, 3)
        self.assertEqual(decision.calls, 3)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(result.defects, ["window_geometry"])
        self.assertEqual(
            result.consensus["part_presence"]["present_votes"],
            2,
        )
        self.assertEqual(
            result.consensus["rules"]["window_geometry"]["triggered_votes"],
            2,
        )
        self.assertEqual(len(result.model_health), 2)
        self.assertTrue(all(row["runs"] == 3 for row in result.model_health))
        self.assertTrue(all(
            len(row["detections_by_run"]) == 3
            for row in result.model_health
        ))

    def test_input_majority_empty_skips_all_defect_rules(self):
        vision = ScriptedVision({1: (3, 3), 2: (0, 0), 3: (1, 1)})
        decision = ScriptedDecision()
        result = Inspector(vision, decision, Recorder()).inspect_input_consensus(
            part_id=1,
            step=1,
            frame_runs=input_frame_runs(),
        )
        self.assertTrue(result.is_empty_tray)
        self.assertEqual(vision.calls, 3)
        self.assertEqual(decision.calls, 0)
        self.assertEqual(len(result.rule_results), 1)
        self.assertEqual(result.rule_results[0].rule_name, "part_presence")
        row = ProductionCycle._rule_report_row(result.rule_results[0])
        self.assertEqual(row["status_label"], "КОРПУС НЕ ОБНАРУЖЕН · 2/3")
        self.assertTrue(row["neutral"])

    def test_rule_report_exposes_vote_without_overlay_text(self):
        result = RuleResult(
            "window_sinks",
            True,
            details={
                "consensus": {
                    "runs": 3,
                    "required_votes": 2,
                    "triggered_votes": 2,
                    "normal_votes": 1,
                    "decision": "triggered",
                }
            },
        )
        row = ProductionCycle._rule_report_row(result)
        self.assertEqual(row["status_label"], "СРАБОТАЛО · 2/3")
        self.assertEqual(row["consensus"]["triggered_votes"], 2)


if __name__ == "__main__":
    unittest.main()
