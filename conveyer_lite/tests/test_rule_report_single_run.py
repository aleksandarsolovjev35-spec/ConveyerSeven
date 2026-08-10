"""Тесты строк отчёта правил при одиночном прогоне.

Проверяют форму ``build_rule_report_row``/``build_rule_report_rows``:
run_cards содержит ровно один замер,
vote_details у defect-правил отсутствует (как и раньше), у part_presence —
один прогон с required_votes=1.
"""

import unittest

from domain.defect_rules.base import RuleResult
from core.rule_report import (
    build_rule_report_row,
    build_rule_report_rows,
)


class RuleReportSingleRunTest(unittest.TestCase):
    def test_defect_rule_row_single_run(self):
        consensus = {
            "runs": 1,
            "required_votes": 1,
            "triggered_votes": 0,
            "normal_votes": 1,
            "decision": "normal",
            "states": [False],
            "source_run": 1,
            "evidence_run": 1,
            "run_cards": [[{
                "role": "INPUT_LEFT",
                "ok": True,
                "verdict": "в допуске",
                "found": [],
                "metrics": [{
                    "label": "Смещение, px",
                    "value": "12.0",
                    "limit": "15.0",
                    "ok": True,
                    "value_raw": 12.0,
                    "limit_raw": 15.0,
                    "key": "shift_distance_px",
                }],
            }]],
        }
        result = RuleResult(
            rule_name="window_geometry",
            triggered=False,
            details={
                "per_role": {
                    "INPUT_LEFT": {"valid": True, "triggered": False},
                },
                "consensus": consensus,
            },
            drawings=[],
        )
        row = build_rule_report_row(result)

        self.assertEqual(row["name"], "window_geometry")
        self.assertFalse(row["triggered"])
        # Один замер на порог
        self.assertEqual(len(row["run_cards"]), 1)
        self.assertEqual(row["run_cards"][0][0]["role"], "INPUT_LEFT")
        self.assertEqual(row["run_cards"][0][0]["metrics"][0]["value"], "12.0")
        # У defect-правил vote_details отсутствует (поведение сохранено)
        self.assertIsNone(row["vote_details"])

    def test_part_presence_row_single_run(self):
        consensus = {
            "runs": 1,
            "required_votes": 1,
            "empty_votes": 0,
            "present_votes": 1,
            "decision": "present",
            "states": ["present"],
            "source_run": 1,
            "evidence_run": 1,
            "run_cards": [[{
                "role": "INPUT",
                "ok": True,
                "verdict": "в допуске",
                "found": [],
                "metrics": [],
            }]],
            "run_status": [[{"role": "INPUT", "status": "КОРПУС",
                             "reason": None}]],
            "picture_run": 1,
            "picture_reason": "единственный прогон",
        }
        result = RuleResult(
            rule_name="part_presence",
            triggered=False,
            details={"empty_tray": False, "consensus": consensus},
            drawings=[],
        )
        row = build_rule_report_row(result)

        self.assertEqual(len(row["run_cards"]), 1)
        vote = row["vote_details"]
        self.assertIsNotNone(vote)
        self.assertEqual(vote["decision"], "present")
        self.assertEqual(vote["total_runs"], 1)
        self.assertEqual(vote["required_votes"], 1)
        self.assertEqual(vote["present_votes"], 1)

    def test_rows_with_role_filter(self):
        consensus = {
            "run_cards": [[{
                "role": "INPUT_LEFT",
                "ok": True,
                "metrics": [],
            }]],
        }
        result = RuleResult(
            rule_name="window_geometry",
            triggered=False,
            details={"per_role": {
                "INPUT_LEFT": {"valid": True, "triggered": False},
            }, "consensus": consensus},
            drawings=[],
        )
        rows = build_rule_report_rows([result], role="INPUT_LEFT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_cards"][0][0]["role"], "INPUT_LEFT")

        rows_other = build_rule_report_rows([result], role="SPIDER_LEFT")
        self.assertEqual(rows_other, [])


if __name__ == "__main__":
    unittest.main()
