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

    def test_build_rows_scopes_to_selected_camera_role(self):
        """Анализ кадра показывает только вычисления выбранной камеры."""
        multi = SimpleNamespace(
            rule_name="contacts_long",
            triggered=True,
            details={"per_role": {
                "SPIDER_LEFT": {
                    "triggered": True,
                    "reason": "wrong_count: 3/5",
                    "found": 3,
                },
                "SPIDER_RIGHT": {
                    "triggered": False,
                    "reason": None,
                    "found": 5,
                    "line_tolerance_px": 7.0,
                    "rect_width_px": 11.5,
                    "rect_height_px": 8.6,
                    "omission_tilt_ratio_max": 0.2,
                    "omission_tilt_check": {"status": "ok", "distance_trend_ratio": 0.01},
                    "items": [],
                },
            }},
        )
        top = SimpleNamespace(
            rule_name="top_contacts",
            triggered=False,
            details={"per_role": {"TOP": {"triggered": False}}},
        )
        rows = build_rule_report_rows([multi, top], role="SPIDER_LEFT")
        self.assertEqual([row["name"] for row in rows], ["contacts_long"])
        self.assertTrue(rows[0]["triggered"])
        self.assertTrue(all(
            "SPIDER_RIGHT" not in line
            for line in rows[0].get("detail_lines") or []
        ))
        self.assertTrue(all(
            card.get("role") == "SPIDER_LEFT"
            for card in rows[0].get("summary_cards") or []
        ))

        rows = build_rule_report_rows([multi, top], role="SPIDER_RIGHT")
        self.assertEqual([row["name"] for row in rows], ["contacts_long"])
        self.assertFalse(rows[0]["triggered"])
        self.assertTrue(all(
            "SPIDER_LEFT" not in line
            for line in rows[0].get("detail_lines") or []
        ))

        rows = build_rule_report_rows([multi, top], role="TOP")
        self.assertEqual([row["name"] for row in rows], ["top_contacts"])

    def test_window_geometry_exposes_every_window_measurement(self):
        """Все 7 окон T/B и зональные пороги попадают в summary_cards."""
        items = [
            {
                "index": index,
                "valid": True,
                "top_px": 25.0 + index * 0.1,
                "bottom_px": 30.0 + index * 0.1,
                "top_fail": index == 4,
                "bottom_fail": False,
            }
            for index in range(1, 8)
        ]
        row = build_rule_report_row(SimpleNamespace(
            rule_name="window_geometry",
            triggered=True,
            details={"per_role": {"INPUT_LEFT": {
                "triggered": True,
                "found": 7,
                "expected_count": 7,
                "top_limits_px": [20, 40],
                "bottom_limits_px": [20, 40],
                # без top_values_px/bottom_values_px — только items
                "items": items,
            }}},
        ))
        metrics = {
            m["key"]: m for m in row["summary_cards"][0]["metrics"] if m.get("key")
        }
        self.assertIn("found", metrics)
        self.assertIn("top_px_min", metrics)
        self.assertIn("top_px_max", metrics)
        self.assertIn("bottom_px_min", metrics)
        self.assertIn("bottom_px_max", metrics)
        for index in range(1, 8):
            self.assertIn(f"window_{index}_top_px", metrics)
            self.assertIn(f"window_{index}_bottom_px", metrics)
            self.assertIn(f"window_{index}_ok", metrics)
            self.assertIsNotNone(metrics[f"window_{index}_top_px"]["value_raw"])
            self.assertIsNotNone(metrics[f"window_{index}_top_px"]["limit_raw"])
        self.assertIs(metrics["window_4_ok"]["ok"], False)
        self.assertIs(metrics["window_1_ok"]["ok"], True)

    def test_contacts_long_exposes_per_contact_and_aggregates(self):
        """Длинные контакты: допуск, наклон, 5 контактов с отклонениями."""
        items = [
            {
                "index": index,
                "dev_top_px": float(index),
                "dev_bottom_px": 0.0,
                "rect_fits": index != 3,
                "omission_distance_px": 90 + index,
            }
            for index in range(1, 6)
        ]
        row = build_rule_report_row(SimpleNamespace(
            rule_name="contacts_long",
            triggered=True,
            details={"per_role": {"SPIDER_LEFT": {
                "triggered": True,
                "found": 5,
                "line_tolerance_px": 7.0,
                "max_dev_top": 5.0,
                "level_slope": 0.02,
                "max_level_slope": 0.10,
                "rect_width_px": 11.5,
                "rect_height_px": 8.6,
                "omission_tilt_ratio_max": 0.2,
                "omission_tilt_check": {
                    "status": "ok",
                    "distance_trend_ratio": 0.05,
                },
                "items": items,
            }}},
        ))
        metrics = {
            m["key"]: m for m in row["summary_cards"][0]["metrics"] if m.get("key")
        }
        self.assertIn("found", metrics)
        self.assertIn("line_tolerance_px", metrics)
        self.assertIn("omission_tilt_ratio_max", metrics)
        self.assertIn("max_level_slope", metrics)
        self.assertIn("rect_width_px", metrics)
        for index in range(1, 6):
            self.assertIn(f"contact_{index}_dev_top_px", metrics)
            self.assertIn(f"contact_{index}_rect_fits", metrics)
            self.assertIn(f"contact_{index}_omission_dist_px", metrics)
        self.assertIs(metrics["contact_3_rect_fits"]["ok"], False)

    def test_contacts_long_wrong_count_still_shows_found_metric(self):
        """При wrong_count оператор видит хотя бы «найдено N/5»."""
        row = build_rule_report_row(SimpleNamespace(
            rule_name="contacts_long",
            triggered=True,
            details={"per_role": {"SPIDER_LEFT": {
                "triggered": True,
                "reason": "wrong_count: 3/5",
                "found": 3,
                "line_tolerance_px": 7.0,
                "items": [],
            }}},
        ))
        metrics = {
            m["key"]: m for m in row["summary_cards"][0]["metrics"] if m.get("key")
        }
        self.assertEqual(metrics["found"]["value"], "3")
        self.assertEqual(metrics["found"]["limit"], "5")
        self.assertIs(metrics["found"]["ok"], False)
        self.assertIn("line_tolerance_px", metrics)

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

    def test_run_cards_pass_through_with_operator_labels(self):
        """Три замера по прогонам доходят до отчёта; метрики получают
        понятные названия порогов (как в панели «Пороги правил»)."""
        from core.rule_summary import build_rule_summary

        per_role = {"SPIDER_LEFT": {
            "triggered": False, "reason": None,
            "allowed_thickness_px": 20.0, "excess_pixels": 0,
            "excess_component_min_px": 3, "max_excess_depth_px": 0.0,
            "top_line_actual_max_residual_px": 0.4,
            "top_line_max_residual_px": 3.0,
            "found": 5, "expected_count": 5,
        }}
        run_cards = [
            build_rule_summary("long_omission", {"per_role": per_role}),
            build_rule_summary("long_omission", {"per_role": per_role}),
            build_rule_summary("long_omission", {"per_role": per_role}),
        ]
        row = build_rule_report_row(SimpleNamespace(
            rule_name="long_omission", triggered=False,
            details={
                "per_role": per_role,
                "consensus": {"runs": 3, "run_cards": run_cards},
            },
        ))
        self.assertEqual(len(row["run_cards"]), 3)
        labels = {
            metric["label"]
            for cards in row["run_cards"]
            for card in cards
            for metric in card["metrics"]
        }
        # Название порога из «Порогов правил» вместо внутреннего «избыток».
        self.assertIn("Мин. размер лишнего фрагмента, px", labels)

    def test_run_status_passes_through_report_row(self):
        """Статус прогонов («ОБЛАСТЬ НЕ ПОСТРОЕНА») доходит до отчёта."""
        row = build_rule_report_row(SimpleNamespace(
            rule_name="long_omission", triggered=True,
            details={
                "per_role": {"SPIDER_LEFT": {"triggered": True}},
                "consensus": {
                    "runs": 3,
                    "run_status": [
                        [{"role": "SPIDER_LEFT",
                          "status": "ОБЛАСТЬ НЕ ПОСТРОЕНА",
                          "reason": "no_detections"}],
                        [{"role": "SPIDER_LEFT", "status": "В НОРМЕ",
                          "reason": None}],
                        [{"role": "SPIDER_LEFT", "status": "ОТКЛОНЕНИЕ",
                          "reason": None}],
                    ],
                },
            },
        ))
        self.assertEqual(len(row["run_status"]), 3)
        self.assertEqual(
            row["run_status"][0][0]["status"],
            "ОБЛАСТЬ НЕ ПОСТРОЕНА",
        )

    def test_ui_module_renders_summary_cards(self):
        js = (ROOT / "vision/ui/static/js/rule-summary.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("renderRuleMeasurements", js)
        self.assertIn("fa-threshold", js)
        self.assertIn("buildThresholdBlock", js)
        self.assertIn("fa-threshold-runs", js)
        self.assertIn("formatDeltaSimple", js)
        self.assertIn("is-decisive", js)
        self.assertIn("data-run", js)
        diagnostics = (ROOT / "vision/ui/static/js/diagnostics.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("renderRuleMeasurements(rule, pictureRun)", diagnostics)
        self.assertIn("report.picture_run", diagnostics)
        self.assertIn("setFrameAnalysisRulesFilter", diagnostics)
        self.assertIn("setupFrameAnalysisRunClicks", diagnostics)
        cameras = (ROOT / "vision/ui/static/js/cameras.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function setMainCameraRun", cameras)
        css = (ROOT / "vision/ui/static/css/blocks.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".fa-threshold", css)
        self.assertIn(".fa-threshold.is-decisive", css)
        self.assertIn(".fa-mv-delta", css)
        self.assertIn(".frame-analysis-rules-scroll", css)
        self.assertIn(".frame-analysis-filter-btn", css)
        self.assertIn(".fa-measurement-value.is-ok", css)
        self.assertIn(".fa-measurement-value.is-bad", css)
        self.assertIn(".fa-measurement-value.is-picture-run", css)
