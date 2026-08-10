"""Тесты одиночного прогона инспекции."""

import unittest

from domain.defect_rules.base import RuleResult
from inspection.consensus import (
    CONSENSUS_MIN_VOTES,
    INSPECTION_RUNS,
    InspectionConsensusError,
    combine_presence_results,
    combine_rule_results,
    describe_picture_run,
    select_picture_run,
    summarize_model_health,
)


def _rule(name, triggered, per_role=None):
    return RuleResult(
        rule_name=name,
        triggered=triggered,
        details={"per_role": per_role or {}},
        drawings=[],
    )


class ConsensusConstantsTest(unittest.TestCase):
    def test_single_run_constants(self):
        self.assertEqual(INSPECTION_RUNS, 1)
        self.assertEqual(CONSENSUS_MIN_VOTES, 1)


class CombineRuleResultsTest(unittest.TestCase):
    def test_single_run_passthrough(self):
        r1 = _rule("window_geometry", False,
                   per_role={"INPUT_LEFT": {"valid": True, "triggered": False}})
        r2 = _rule("contacts_long", True,
                   per_role={"SPIDER_LEFT": {"valid": True, "triggered": True}})
        final, meta, evidence = combine_rule_results([[r1, r2]])
        self.assertEqual(len(final), 2)
        self.assertFalse(final[0].triggered)
        self.assertTrue(final[1].triggered)
        self.assertEqual(meta["runs"], 1)
        self.assertEqual(meta["required_votes"], 1)

    def test_single_run_empty_set(self):
        final, meta, evidence = combine_rule_results([[]])
        self.assertEqual(final, [])
        self.assertEqual(meta["runs"], 1)

    def test_rejects_multi_run_input(self):
        r1 = _rule("window_geometry", False)
        r2 = _rule("window_geometry", True)
        with self.assertRaises(InspectionConsensusError):
            combine_rule_results([[r1], [r2]])


class CombinePresenceResultsTest(unittest.TestCase):
    def _presence(self, empty):
        return RuleResult(
            rule_name="part_presence",
            triggered=False,
            details={
                "empty_tray": empty,
                "flatness_left": 0 if empty else 12,
                "flatness_right": 0 if empty else 14,
            },
            drawings=[],
        )

    def test_present_single_run(self):
        final, consensus, evidence = combine_presence_results(
            [self._presence(empty=False)])
        self.assertFalse(final.details["empty_tray"])
        self.assertEqual(consensus["decision"], "present")
        self.assertEqual(consensus["present_votes"], 1)

    def test_empty_single_run(self):
        final, consensus, _ = combine_presence_results(
            [self._presence(empty=True)])
        self.assertTrue(final.details["empty_tray"])
        self.assertEqual(consensus["decision"], "empty")


class PictureSelectionTest(unittest.TestCase):
    def test_select_picture_run_without_metrics(self):
        r = _rule("top_contacts", False)
        self.assertIsNone(select_picture_run([r]))
        self.assertEqual(
            describe_picture_run([r], 0),
            "единственный прогон (нет числовых порогов)",
        )


class SummarizeModelHealthTest(unittest.TestCase):
    def test_single_run_summary(self):
        rows = [
            {"role": "INPUT_LEFT", "model": "m", "ok": True, "run": 1,
             "elapsed_ms": 10.0, "detections": 2},
            {"role": "INPUT_RIGHT", "model": "m", "ok": True, "run": 1,
             "elapsed_ms": 12.0, "detections": 3},
        ]
        summary = summarize_model_health(rows)
        self.assertEqual(len(summary), 2)
        for row in summary:
            self.assertTrue(row["ok"])
            self.assertEqual(row["runs"], 1)


if __name__ == "__main__":
    unittest.main()
