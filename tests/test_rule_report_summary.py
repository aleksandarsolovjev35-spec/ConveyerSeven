"""Правая панель: решающие правила и компактные сводки."""

import unittest
from pathlib import Path
from types import SimpleNamespace

from core.rule_summary import METRICS_PER_ROLE_LIMIT
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
        self.assertEqual(rows[0]["status_label"], "КОРПУС НЕ ОБНАРУЖЕН")

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

    def test_triggered_rule_exposes_failed_value_threshold_and_conclusion(self):
        row = build_rule_report_row(SimpleNamespace(
            rule_name="long_omission", triggered=True,
            details={"per_role": {"SPIDER_LEFT": {
                "triggered": True,
                "excess_pixels": 12,
                "excess_component_min_px": 3,
            }}},
        ))
        breach = row["threshold_breaches"][0]
        self.assertEqual(breach["label"], "избыток")
        self.assertEqual(breach["value"], "12 px")
        self.assertEqual(breach["threshold"], "3 px")
        self.assertIn("ИЗБЫТОЧНАЯ ТОЛЩИНА", row["threshold_conclusion"])

    def test_normal_rule_has_no_summary_noise(self):
        row = build_rule_report_row(SimpleNamespace(
            rule_name="sinks", triggered=False,
            details={"per_role": {"TOP": {"triggered": False}}},
        ))
        self.assertEqual(row["summary_lines"], [])
        self.assertFalse(row["decisive"])

    def test_glass_on_contacts_uses_pairs_when_hits_is_a_count(self):
        """The rule's ``hits`` field is numeric; overlap details are pairs."""
        row = build_rule_report_row(SimpleNamespace(
            rule_name="glass_on_contacts",
            triggered=True,
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": None,
                "glasses_total": 1,
                "hits": 1,
                "pairs": [{
                    "glass_index": 1,
                    "contact_index": 4,
                    "overlap_pixels": 9,
                }],
            }}},
        ))
        self.assertIn("contact #4", row["detail"])
        self.assertEqual(
            row["summary_cards"][0]["metrics"][0]["value"], "1",
        )
        self.assertEqual(row["human_cause"], "СТЕКЛО НА КОНТАКТАХ")

    def test_ui_renders_only_decisive_rules(self):
        js = (ROOT / "vision/ui/static/js/diagnostics.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function decisiveRules(", js)
        self.assertIn("rule.part_absent === true", js)
        self.assertIn("КОРПУС НЕ ОБНАРУЖЕН", js)
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

    def test_tokens_slide_in_sync_with_conveyor_step_duration(self):
        # Одна длительность шага линии управляет и подсветкой ленты, и
        # скольжением маркеров деталей: визуал повторяет движение конвейера.
        self.assertIn("function lineMoveDuration(", self.js)
        self.assertIn("--move-duration", self.js)
        self.assertIn("conveyor-belt", self.js)
        self.assertIn(".line-token", self.process)
        self.assertIn("left var(--move-duration", self.process)

    def test_parts_are_absolute_tokens_moving_between_cells(self):
        self.assertIn("line-token", self.js)
        self.assertIn("_lineTokens", self.js)
        self.assertIn("els.lineCells.appendChild(el)", self.js)
        self.assertIn("rects[meta.position]", self.js)
        # Ушедшая с линии деталь съезжает за последнюю ячейку и исчезает.
        self.assertIn("token.el.style.opacity = '0'", self.js)
        self.assertIn("setTimeout(", self.js)

    def test_cells_show_only_part_number_no_strange_animations(self):
        # В маркере только короткий ID детали вида «#1»; старые
        # «летящие» элементы и циклические keyframes удалены как визуальный баг.
        self.assertIn("`#${id}`", self.js)
        self.assertNotIn("flying-part", self.js)
        self.assertNotIn("entering-with-trail", self.js)
        self.assertNotIn("exiting-flyer", self.js)
        self.assertNotIn("conveyor-belt-track", self.js)
        self.assertNotIn("conveyor-notch", self.js)
        self.assertNotIn("wobbling", self.js)
        self.assertNotIn("@keyframes conveyorScroll", self.motion)
        self.assertNotIn("@keyframes partWobble", self.motion)
        self.assertNotIn("@keyframes beltTrailEnter", self.motion)
        self.assertNotIn("flying-part", self.process)
        self.assertNotIn("moving-right", self.process)
        self.assertNotIn("entering-with-trail", self.process)
        self.assertNotIn("exiting-flyer", self.process)

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


class RuleSummaryCardTests(unittest.TestCase):
    """Сводка показывает и норму, и отклонение с числовыми показателями."""

    def _omission_row(self):
        return build_rule_report_row(SimpleNamespace(
            rule_name="long_omission",
            triggered=True,
            details={"per_role": {
                "SPIDER_LEFT": {
                    "triggered": True, "reason": None,
                    "allowed_thickness_px": 20.0, "excess_pixels": 340,
                    "excess_component_min_px": 3, "max_excess_depth_px": 18.0,
                    "top_line_actual_max_residual_px": 1.2,
                    "top_line_max_residual_px": 3.0,
                    "found": 5, "expected_count": 5,
                },
                "SPIDER_RIGHT": {
                    "triggered": False, "reason": None,
                    "allowed_thickness_px": 20.0, "excess_pixels": 0,
                    "excess_component_min_px": 3, "max_excess_depth_px": 0.0,
                    "top_line_actual_max_residual_px": 0.4,
                    "top_line_max_residual_px": 3.0,
                    "found": 5, "expected_count": 5,
                },
            }},
        ))

    def test_cards_cover_every_camera_including_good_ones(self):
        cards = self._omission_row()["summary_cards"]
        self.assertEqual(len(cards), 2)
        by_role = {card["role"]: card for card in cards}
        self.assertIs(by_role["SPIDER_LEFT"]["ok"], False)
        self.assertIs(by_role["SPIDER_RIGHT"]["ok"], True)
        self.assertEqual(by_role["SPIDER_RIGHT"]["verdict"], "в допуске")

    def test_failing_camera_is_listed_first(self):
        cards = self._omission_row()["summary_cards"]
        self.assertEqual(cards[0]["role"], "SPIDER_LEFT")

    def test_cards_report_what_was_detected(self):
        cards = self._omission_row()["summary_cards"]
        self.assertIn("объекты: 5/5", cards[0]["found"])

    def test_metrics_pair_value_with_limit_and_state(self):
        card = self._omission_row()["summary_cards"][0]
        metrics = {metric["label"]: metric for metric in card["metrics"]}
        self.assertEqual(metrics["избыток"]["value"], "340 px")
        self.assertEqual(metrics["избыток"]["limit"], "3 px")
        self.assertIs(metrics["избыток"]["ok"], False)
        self.assertIs(metrics["отклонение линии"]["ok"], True)
        self.assertLessEqual(len(card["metrics"]), METRICS_PER_ROLE_LIMIT)

    def test_presence_cards_show_counts_per_input_camera(self):
        row = build_rule_report_row(_presence(False))
        cards = {card["role"]: card for card in row["summary_cards"]}
        self.assertEqual(set(cards), {"INPUT_LEFT", "INPUT_RIGHT"})
        self.assertEqual(cards["INPUT_LEFT"]["metrics"][0]["label"], "flatness")

    def test_platform_placement_is_human_readable(self):
        row = build_rule_report_row(SimpleNamespace(
            rule_name="top_platform", triggered=False,
            details={"per_role": {"TOP": {
                "triggered": False, "reason": None, "placement": "centered",
                "shift_distance_px": 1.8, "angle_deg": 0.4,
            }}},
        ))
        metrics = {m["label"]: m for m in row["summary_cards"][0]["metrics"]}
        self.assertEqual(metrics["положение"]["value"], "по центру")
        self.assertIs(metrics["положение"]["ok"], True)

    def test_skipped_camera_is_marked_without_measurement(self):
        row = build_rule_report_row(SimpleNamespace(
            rule_name="top_contacts", triggered=False,
            details={"per_role": {"TOP": {
                "skipped": True, "reason": "no_valid_platform",
            }}},
        ))
        card = row["summary_cards"][0]
        self.assertIsNone(card["ok"])
        self.assertIn("не найдена платформа", card["verdict"])

    def test_ui_module_renders_summary_cards(self):
        js = (ROOT / "vision/ui/static/js/rule-summary.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("renderRuleSummaryCards", js)
        self.assertIn("rule-summary-metric", js)
        self.assertIn("Обнаружено", js)
        diagnostics = (ROOT / "vision/ui/static/js/diagnostics.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("rule.summary_cards", diagnostics)
        css = (ROOT / "vision/ui/static/css/stats.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".rule-summary-metric.is-ok", css)
        self.assertIn(".rule-summary-metric.is-bad", css)
