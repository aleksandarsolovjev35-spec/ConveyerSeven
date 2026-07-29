"""Правая панель: решающие правила и компактные сводки."""

import unittest
from pathlib import Path
from types import SimpleNamespace

from core.rule_report import (
    SUMMARY_LINES_LIMIT,
    build_rule_report_row,
    build_rule_report_rows,
)

ROOT = Path(__file__).resolve().parents[1]


def _presence(empty: bool):
    return SimpleNamespace(
        rule_name="part_presence",
        triggered=False,
        details={
            "empty_tray": empty,
            "flatness_left": 1,
            "flatness_right": 0,
            "false_positive_max_count_by_role": {
                "INPUT_LEFT": 2,
                "INPUT_RIGHT": 2,
            },
        },
    )


def _triggered_rule(name="window_sinks", roles=("INPUT_LEFT",)):
    return SimpleNamespace(
        rule_name=name,
        triggered=True,
        details={"per_role": {
            role: {
                "triggered": True,
                "reason": None,
                "overlap_min_px": 5,
                "hits": [{"sink_index": 1, "window_index": 2, "overlap_px": 8}],
            }
            for role in roles
        }},
    )


class RuleReportSummaryTests(unittest.TestCase):
    def test_absent_part_is_the_only_reported_rule(self):
        rows = build_rule_report_rows([
            _presence(True),
            _triggered_rule(),
            _triggered_rule("sinks", ("TOP",)),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "part_presence")
        self.assertTrue(rows[0]["part_absent"])
        self.assertEqual(rows[0]["status_label"], "ДЕТАЛЬ НЕ ОБНАРУЖЕНА")

    def test_present_part_keeps_all_rules(self):
        rows = build_rule_report_rows([_presence(False), _triggered_rule()])
        self.assertEqual([row["name"] for row in rows],
                         ["part_presence", "window_sinks"])
        self.assertFalse(rows[0]["part_absent"])

    def test_presence_summary_is_short_and_informative(self):
        row = build_rule_report_row(_presence(True))
        self.assertEqual(len(row["summary_lines"]), 2)
        self.assertIn("INPUT_LEFT: flatness 1", row["summary_lines"][0])
        self.assertIn("порог ложных 2", row["summary_lines"][0])

    def test_summary_is_not_excessive(self):
        detail = {"per_role": {"SPIDER_LEFT": {
            "triggered": True,
            "reason": None,
            "allowed_thickness_px": 20.0,
            "excess_pixels": 340,
            "largest_component_pixels": 340,
            "excess_component_min_px": 3,
            "max_excess_depth_px": 18.0,
            "top_line_actual_max_residual_px": 1.2,
            "top_line_max_residual_px": 3.0,
            "max_consecutive_columns": 24,
        }}}
        row = build_rule_report_row(SimpleNamespace(
            rule_name="long_omission", triggered=True, details=detail,
        ))
        self.assertTrue(row["summary_lines"])
        self.assertLessEqual(len(row["summary_lines"]), SUMMARY_LINES_LIMIT + 1)
        self.assertTrue(row["decisive"])

    def test_normal_rule_has_no_summary_noise(self):
        row = build_rule_report_row(SimpleNamespace(
            rule_name="sinks", triggered=False,
            details={"per_role": {"TOP": {"triggered": False}}},
        ))
        self.assertEqual(row["summary_lines"], [])
        self.assertFalse(row["decisive"])

    def test_ui_renders_only_decisive_rules(self):
        js = (ROOT / "vision/ui/static/js/diagnostics.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function decisiveRules(", js)
        self.assertIn("rule.part_absent === true", js)
        self.assertIn("ДЕТАЛЬ НЕ ОБНАРУЖЕНА", js)
        self.assertIn("rule.summary_lines", js)


class LineMotionAnimationTests(unittest.TestCase):
    def setUp(self):
        self.js = (ROOT / "vision/ui/static/js/status.js").read_text(
            encoding="utf-8"
        )
        self.motion = (ROOT / "vision/ui/static/css/motion.css").read_text(
            encoding="utf-8"
        )
        self.process = (ROOT / "vision/ui/static/css/process.css").read_text(
            encoding="utf-8"
        )

    def test_belt_scroll_is_synchronized_with_part_movement(self):
        self.assertIn("function lineMoveDuration(", self.js)
        self.assertIn("--move-duration", self.js)
        self.assertIn("conveyor-belt-track", self.js)
        self.assertIn("@keyframes conveyorScroll", self.motion)
        self.assertIn(
            "animation: conveyorScroll var(--move-duration", self.motion
        )

    def test_enter_move_and_exit_animations_exist(self):
        self.assertIn("entering-with-trail", self.js)
        self.assertIn("flying-part", self.js)
        self.assertIn("exiting-flyer", self.js)
        self.assertIn("@keyframes beltTrailEnter", self.motion)
        self.assertIn("@keyframes partWobble", self.motion)

    def test_strict_modules_stay_free_of_animations_and_gradients(self):
        self.assertNotIn("animation:", self.process)
        self.assertNotIn("gradient(", self.process)


if __name__ == "__main__":
    unittest.main()


class LineCellIndexingTests(unittest.TestCase):
    def test_line_cells_are_selected_by_data_pos_not_child_index(self):
        js = (ROOT / "vision/ui/static/js/status.js").read_text(
            encoding="utf-8"
        )
        self.assertIn(".line-cell[data-pos]", js)
        self.assertIn("Number(cell.dataset.pos)", js)
        self.assertNotIn("const cells = els.lineCells.children;", js)
