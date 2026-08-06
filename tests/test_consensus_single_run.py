"""Тесты одиночного прогона инспекции (тройное голосование 2 из 3 убрано).

Проверяют, что после отмены тройного голосования вспомогательные функции
``inspection.consensus`` работают с ровно одним прогоном:
INSPECTION_RUNS == 1, CONSENSUS_MIN_VOTES == 1, evidence — единственный
прогон, run_cards — один элемент, а старые многопрогоновые входы
отклоняются.
"""

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
        self.assertEqual(evidence, 0)
        self.assertEqual(meta["runs"], 1)
        self.assertEqual(meta["required_votes"], 1)
        self.assertEqual(meta["evidence_run"], 1)

        rules_meta = meta["rules"]
        self.assertEqual(rules_meta["window_geometry"]["decision"], "normal")
        self.assertEqual(rules_meta["window_geometry"]["triggered_votes"], 0)
        self.assertEqual(rules_meta["window_geometry"]["normal_votes"], 1)
        self.assertEqual(rules_meta["contacts_long"]["decision"], "triggered")
        self.assertEqual(rules_meta["contacts_long"]["triggered_votes"], 1)

        # run_cards — ровно один замер на правило
        self.assertEqual(len(rules_meta["window_geometry"]["run_cards"]), 1)
        self.assertEqual(len(rules_meta["contacts_long"]["run_cards"]), 1)

    def test_single_run_empty_set(self):
        final, meta, evidence = combine_rule_results([[]])
        self.assertEqual(final, [])
        self.assertEqual(evidence, 0)
        self.assertEqual(meta["runs"], 1)
        self.assertEqual(meta["rules"], {})

    def test_rejects_multi_run_input(self):
        # Защитный тест: данные от старого «тройного голосования» должны
        # отклоняться, а не молча обрабатываться.
        r1 = _rule("window_geometry", False)
        r2 = _rule("window_geometry", True)
        with self.assertRaises(InspectionConsensusError):
            combine_rule_results([[r1], [r2]])

    def test_rejects_empty_runs_list(self):
        with self.assertRaises(InspectionConsensusError):
            combine_rule_results([])

    def test_rejects_mismatched_rule_sets(self):
        r1 = _rule("window_geometry", False)
        with self.assertRaises(InspectionConsensusError):
            combine_rule_results([[r1], []])


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
            [self._presence(empty=False)]
        )
        self.assertFalse(final.details["empty_tray"])
        self.assertEqual(consensus["decision"], "present")
        self.assertEqual(consensus["present_votes"], 1)
        self.assertEqual(consensus["empty_votes"], 0)
        self.assertEqual(consensus["runs"], 1)
        self.assertEqual(consensus["evidence_run"], 1)
        self.assertEqual(len(consensus["run_cards"]), 1)
        self.assertEqual(evidence, 0)

    def test_empty_single_run(self):
        final, consensus, _ = combine_presence_results(
            [self._presence(empty=True)]
        )
        self.assertTrue(final.details["empty_tray"])
        self.assertEqual(consensus["decision"], "empty")
        self.assertEqual(consensus["empty_votes"], 1)

    def test_rejects_multi_run(self):
        with self.assertRaises(InspectionConsensusError):
            combine_presence_results(
                [self._presence(empty=False), self._presence(empty=True)]
            )


class PictureSelectionTest(unittest.TestCase):
    def test_select_picture_run_with_numeric_metrics(self):
        rules = []
        for name, triggered, role in (
            ("window_geometry", False, "INPUT_LEFT"),
            ("contacts_long", True, "SPIDER_LEFT"),
        ):
            rules.append(RuleResult(
                rule_name=name,
                triggered=triggered,
                details={
                    "per_role": {role: {"valid": True, "triggered": triggered}},
                    "consensus": {
                        "run_cards": [[{
                            "role": role,
                            "ok": not triggered,
                            "verdict": "в допуске" if not triggered else "отклонение",
                            "found": [],
                            "metrics": [{
                                "label": "Смещение, px",
                                "value": "12.0",
                                "limit": "15.0",
                                "ok": not triggered,
                                "value_raw": 12.0,
                                "limit_raw": 15.0,
                                "key": "shift_distance_px",
                            }],
                        }]],
                    },
                },
                drawings=[],
            ))
        self.assertEqual(select_picture_run(rules), 0)
        reason = describe_picture_run(rules, 0)
        self.assertIn("ближе всего к порогу", reason)

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
            self.assertEqual(row["detections_by_run"], [row["detections"]])

    def test_failed_run_marks_not_ok(self):
        rows = [
            {"role": "INPUT_LEFT", "model": "m", "ok": False, "run": 1,
             "elapsed_ms": 10.0, "detections": 2, "error": "boom"},
        ]
        summary = summarize_model_health(rows)
        self.assertEqual(len(summary), 1)
        self.assertFalse(summary[0]["ok"])
        self.assertEqual(summary[0]["runs"], 1)
        self.assertEqual(summary[0]["error"], "boom")


if __name__ == "__main__":
    unittest.main()
