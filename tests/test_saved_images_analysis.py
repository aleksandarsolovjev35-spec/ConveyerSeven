import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from analyze_saved_images import (
    EXPECTED_SIZE,
    INPUT_ROLES,
    ROLES,
    SPIDER_ROLES,
    InteractiveAnalyzer,
    SampleSpec,
    analyze_sample,
    collect_omission_calibration,
    compute_category,
    discover_folder_as_role,
    discover_in_directory,
    inspect_frame_health,
    load_manifest,
    partition_rules,
    preferred_analysis_mode,
    rule_report_row,
    rule_status_for_role,
    safe_name,
    summarize_detections,
    write_image,
    write_index,
)


class SavedImagesAnalysisTests(unittest.TestCase):
    def test_camera_rules_overlay_has_no_embedded_statistics_panels(self):
        from vision.overlay.debug_overlay import DebugOverlay

        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        result = SimpleNamespace(
            rule_name="demo_rule",
            triggered=True,
            details={},
            drawings=[{
                "type": "stats_panel_entry",
                "role": "TOP",
                "rule": "demo_rule",
                "triggered": True,
                "lines": [("value", "123", "must stay in UI")],
            }],
        )
        rendered = DebugOverlay.render_frame(frame, "TOP", [result])
        self.assertTrue(np.array_equal(rendered, frame))
        root = Path(__file__).resolve().parents[1]
        source = (root / "vision/overlay/debug_overlay.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("PanelRenderer", source)
        self.assertNotIn("draw_stats_panel", source)
        self.assertNotIn("draw_rules_panel", source)
        self.assertFalse((
            root / "vision/overlay/renderers/panels.py"
        ).exists())

    def test_interactive_mode_has_camera_classes_enter_tab_and_full_statistics(self):
        source = (
            Path(__file__).resolve().parents[1] / "analyze_saved_images.py"
        ).read_text(encoding="utf-8")
        for token in (
            "class InteractiveAnalyzer",
            "КЛАСС 1/6",
            "КЛАСС 2/7",
            "КЛАСС 3/5",
            "КЛАСС 4",
            'bind_class(priority_tag, "<Return>"',
            'bind_class(priority_tag, "<Tab>"',
            "prepend_bindtag(self.root)",
            "КЛАССЫ ОБЪЕКТОВ",
            "ВСЕ ОБЪЕКТЫ",
            "ПРАВИЛА",
            "ГЕОМЕТРИЯ ОКОН · ПИКСЕЛИ",
            "нет измерения T/B",
            "СОХРАНИТЬ ОТЧЁТ",
        ):
            self.assertIn(token, source)

    def test_part_presence_empty_status_is_concise_in_offline_report(self):
        result = SimpleNamespace(
            rule_name="part_presence",
            triggered=False,
            defect=None,
            details={
                "empty_tray": True,
                "flatness_left": 2,
                "flatness_right": 1,
                "false_positive_ignored_left": 2,
                "false_positive_ignored_right": 1,
                "false_positive_max_count_by_role": {
                    "INPUT_LEFT": 2,
                    "INPUT_RIGHT": 2,
                },
            },
        )
        row = rule_report_row(result, INPUT_ROLES)
        self.assertEqual(row["detail"], "ДЕТАЛЬ НЕ ОБНАРУЖЕНА")
        self.assertEqual(row["details"], {})

    def test_long_contact_omission_tilt_is_visible_in_report(self):
        result = SimpleNamespace(
            rule_name="contacts_long",
            triggered=True,
            defect="contacts_long",
            details={"per_role": {"SPIDER_LEFT": {
                "triggered": True,
                "reason": None,
                "ignored": 0,
                "line_tolerance_px": 7.0,
                "rect_width_mm": 0.48,
                "rect_height_mm": 0.36,
                "omission_tilt_ratio_max": 0.2,
                "omission_tilt_check": {
                    "status": "fail",
                    "distance_trend_ratio": 0.7,
                    "contacts": [
                        {"distance_px": 100.0},
                        {"distance_px": 105.0},
                        {"distance_px": 110.0},
                        {"distance_px": 115.0},
                        {"distance_px": 120.0},
                    ],
                },
                "inscribe_check": {
                    "status": "ok",
                    "scale_px_per_mm": 24.0,
                    "rect_width_mm": 0.48,
                    "rect_height_mm": 0.36,
                    "expected_width_px": 11.5,
                    "expected_height_px": 8.6,
                    "fails": 0,
                },
                "items": [
                    {
                        "index": index,
                        "dev_top_px": float(index),
                        "dev_bottom_px": float(index) / 2,
                        "rect_fits": True,
                        "omission_distance_px": 95.0 + index * 5,
                    }
                    for index in range(1, 6)
                ],
            }}}, 
        )
        row = rule_report_row(result, ("SPIDER_LEFT", "SPIDER_RIGHT"))
        self.assertIn("omission tilt 0.700/limit 0.200", row["detail"])
        for index in range(1, 6):
            self.assertIn(f"SPIDER_LEFT #{index}", row["detail"])
        self.assertIn("rect OK", row["detail"])
        self.assertIn("d=100.0px", row["detail"])

    def test_short_contact_omission_tilt_is_visible_in_report(self):
        result = SimpleNamespace(
            rule_name="contacts_short",
            triggered=True,
            defect="contacts_short",
            details={"per_role": {"SPIDER_IN": {
                "triggered": True,
                "reason": None,
                "ignored": 0,
                "area_absolute_min_px2": 400,
                "tolerance": 10.0,
                "delta_top": 5.0,
                "delta_bottom": 4.0,
                "delta_height": 1.0,
                "rect_width_mm": 1.74,
                "rect_height_mm": 0.66,
                "omission_tilt_ratio_max": 0.2,
                "omission_tilt_check": {
                    "status": "fail",
                    "distance_delta_ratio": 0.5,
                    "contact_a": {"distance_px": 100.0},
                    "contact_b": {"distance_px": 115.0},
                },
                "inscribe_check": {
                    "status": "ok",
                    "scale_px_per_mm": 14.5,
                    "rect_width_mm": 1.74,
                    "rect_height_mm": 0.66,
                    "expected_width_px": 25.3,
                    "expected_height_px": 9.6,
                    "fails": 0,
                },
                "items": [
                    {
                        "index": 1, "top_y": 100, "bottom_y": 130,
                        "height_px": 30, "rect_fits": True,
                        "omission_distance_px": 100.0,
                    },
                    {
                        "index": 2, "top_y": 105, "bottom_y": 134,
                        "height_px": 29, "rect_fits": True,
                        "omission_distance_px": 115.0,
                    },
                ],
            }}}, 
        )
        row = rule_report_row(result, ("SPIDER_IN", "SPIDER_OUT"))
        self.assertIn("omission tilt 0.500/limit 0.200", row["detail"])
        self.assertIn("SPIDER_IN #1", row["detail"])
        self.assertIn("SPIDER_IN #2", row["detail"])
        self.assertIn("d=100.0px", row["detail"])
        self.assertIn("d=115.0px", row["detail"])

    def test_top_contacts_report_contains_groups_and_all_fourteen_contacts(self):
        group_checks = {
            group: {
                "median_distance_px": 50.0,
                "max_deviation_px": 3.0,
                "allowed_deviation_px": 16.0,
            }
            for group in ("L", "R", "T", "B")
        }
        groups = ["L"]*5 + ["R"]*5 + ["T"]*2 + ["B"]*2
        items = [
            {
                "index": index,
                "group": group,
                "distance_px": 50.0,
                "deviation_px": 3.0,
                "allowed_deviation_px": 16.0,
                "rect_width_px": 28 if group in ("L", "R") else 30,
                "rect_height_px": 35 if group in ("L", "R") else 28,
                "rect_fits": True,
            }
            for index, group in enumerate(groups, start=1)
        ]
        result = SimpleNamespace(
            rule_name="top_contacts",
            triggered=False,
            defect=None,
            details={"per_role": {"TOP": {
                "triggered": False,
                "reason": None,
                "ignored": 1,
                "group_checks": group_checks,
                "items": items,
            }}},
        )
        row = rule_report_row(result, ("TOP",))
        for group in ("L", "R", "T", "B"):
            self.assertIn(f"TOP {group}: distance median", row["detail"])
        for index in range(1, 15):
            self.assertIn(f"TOP #{index}", row["detail"])
        self.assertIn("лишних contacts показано серым: 1", row["detail"])

    def test_top_platform_geometry_is_visible_without_extra_detections(self):
        result = SimpleNamespace(
            rule_name="top_platform",
            triggered=False,
            defect=None,
            details={"per_role": {"TOP": {
                "triggered": False,
                "reason": None,
                "found": 3,
                "ignored": 2,
                "rect_width_px": 260.0,
                "rect_height_px": 120.0,
                "angle_deg": 4.5,
                "fits": True,
                "placement": "shifted",
                "shift_distance_px": 12.5,
                "target_center": [500, 350],
                "placed_center": [500, 362.5],
            }}},
        )
        row = rule_report_row(result, ("TOP",))
        self.assertIn("rect 260x120px", row["detail"])
        self.assertIn("angle 4.5deg", row["detail"])
        self.assertIn("сдвинут", row["detail"])
        self.assertIn("shift 12.5px", row["detail"])
        public = row["details"]["per_role"]["TOP"]
        self.assertNotIn("found", public)
        self.assertNotIn("ignored", public)
        self.assertNotIn("target_center", public)
        self.assertNotIn("placed_center", public)

    def test_platform_overlap_boundary_measurements_are_visible_in_report(self):
        result = SimpleNamespace(
            rule_name="platform_contacts_overlap",
            triggered=True,
            defect="platform_contacts_overlap",
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": None,
                "boundary_width_px": 275,
                "boundary_height_px": 135,
                "raw_excess_pixels": 23,
                "excess_pixels": 21,
                "largest_component_pixels": 17,
                "excess_component_min_px": 3,
            }}},
        )
        row = rule_report_row(result, ("TOP",))
        self.assertEqual(row["status"], "TRIGGERED")
        self.assertIn("boundary 275x135px", row["detail"])
        self.assertIn("component min 3px", row["detail"])
        self.assertIn("largest component 17px", row["detail"])
        self.assertIn("confirmed 21px", row["detail"])
        public = row["details"]["per_role"]["TOP"]
        self.assertNotIn("raw_excess_pixels", public)
        self.assertNotIn("boundary_center", public)
        self.assertNotIn("angle_deg", public)

    def test_paired_rule_status_is_local_to_selected_camera(self):
        row = {
            "name": "window_geometry",
            "status": "TRIGGERED",
            "triggered": True,
            "details": {"per_role": {
                "INPUT_LEFT": {"triggered": False},
                "INPUT_RIGHT": {"triggered": True},
            }},
        }
        self.assertEqual(rule_status_for_role(row, "INPUT_LEFT"), "OK")
        self.assertEqual(rule_status_for_role(row, "INPUT_RIGHT"), "TRIGGERED")

    def test_triggered_rule_opens_rules_view_automatically(self):
        self.assertEqual(
            preferred_analysis_mode({
                "rules": [{"name": "window_geometry", "triggered": True}],
            }),
            "RULES",
        )
        self.assertEqual(
            preferred_analysis_mode({
                "rules": [{"name": "window_geometry", "triggered": False}],
            }),
            "RAW",
        )

    def test_window_geometry_report_explains_invisible_count_failure(self):
        result = SimpleNamespace(
            rule_name="window_geometry",
            triggered=True,
            defect="window_geometry",
            details={"per_role": {"INPUT_LEFT": {
                "triggered": True,
                "reason": "too_few: 0/7",
                "found": 0,
                "expected_count": 7,
                "top_limits_px": [20, 40],
                "bottom_limits_px": [20, 40],
                "top_values_px": [],
                "bottom_values_px": [],
                "failed_indices": [],
                "invalid_indices": [],
                "items": [],
            }}},
        )
        row = rule_report_row(result, ("INPUT_LEFT", "INPUT_RIGHT"))
        self.assertEqual(row["status"], "TRIGGERED")
        self.assertIn("найдено 0/7", row["detail"])
        self.assertNotIn("too_few", row["detail"])
        self.assertIn("INPUT_LEFT", row["details"]["per_role"])

    def test_window_geometry_report_contains_all_seven_measurements(self):
        top_values = [55, 56, 80, 54, 53, 52, 51]
        bottom_values = [46, 45, 20, 47, 48, 49, 50]
        items = [
            {
                "index": index,
                "valid": True,
                "top_px": top,
                "bottom_px": bottom,
                "top_fail": index == 3,
                "bottom_fail": False,
            }
            for index, (top, bottom) in enumerate(
                zip(top_values, bottom_values, strict=True),
                start=1,
            )
        ]
        result = SimpleNamespace(
            rule_name="window_geometry",
            triggered=True,
            defect="window_geometry",
            details={"per_role": {"INPUT_RIGHT": {
                "triggered": True,
                "found": 7,
                "expected_count": 7,
                "top_limits_px": [20, 40],
                "bottom_limits_px": [20, 40],
                "top_values_px": top_values,
                "bottom_values_px": bottom_values,
                "failed_indices": [3],
                "invalid_indices": [],
                "ignored": 0,
                "items": items,
            }}},
        )
        row = rule_report_row(result, ("INPUT_LEFT", "INPUT_RIGHT"))
        for index in range(1, 8):
            self.assertIn(f"INPUT_RIGHT #{index}", row["detail"])
        self.assertIn("#3: T=80.0px; B=20.0px", row["detail"])
        self.assertIn("T вне допуска", row["detail"])

    def test_window_sinks_report_lists_each_confirmed_overlap(self):
        result = SimpleNamespace(
            rule_name="window_sinks",
            triggered=True,
            defect="window_sinks",
            details={"per_role": {"INPUT_LEFT": {
                "triggered": True,
                "reason": None,
                "overlap_min_px": 5,
                "hits": [
                    {"sink_index": 1, "window_index": 3, "overlap_px": 9},
                    {"sink_index": 2, "window_index": 6, "overlap_px": 12},
                ],
            }}},
        )
        row = rule_report_row(result, ("INPUT_LEFT", "INPUT_RIGHT"))
        self.assertIn("раковина #1 -> окно #3", row["detail"])
        self.assertIn("overlap 9px >= 5px", row["detail"])
        self.assertIn("раковина #2 -> окно #6", row["detail"])
        self.assertNotIn("снаружи", row["detail"])

    def test_glass_on_contacts_report_lists_each_pair(self):
        result = SimpleNamespace(
            rule_name="glass_on_contacts",
            triggered=True,
            defect="glass_on_contacts",
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": None,
                "reference_fail": False,
                "hits": 1,
                "glasses_total": 2,
                "pairs": [
                    {
                        "glass_index": 1,
                        "contact_index": 3,
                        "overlap_pixels": 7,
                        "route": "BAD",
                    },
                    {
                        "glass_index": 1,
                        "contact_index": 4,
                        "overlap_pixels": 5,
                        "route": "BAD",
                    },
                ],
            }}},
        )
        row = rule_report_row(result, ("TOP",))
        self.assertIn("glass #1 -> contact #3", row["detail"])
        self.assertIn("overlap 7px -> BAD", row["detail"])
        self.assertIn("glass #1 -> contact #4", row["detail"])
        public = row["details"]["per_role"]["TOP"]
        self.assertIn("pairs", public)
        self.assertNotIn("glasses_total", public)

    def test_glass_cleanup_report_lists_overlap_zones_per_glass(self):
        result = SimpleNamespace(
            rule_name="glass",
            triggered=True,
            defect="glass",
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": None,
                "cleanup_hits": 1,
                "glasses_total": 3,
                "hits": [{
                    "glass_index": 2,
                    "platform_overlap_px": 8,
                    "pin_overlap_px": 2,
                    "ring_overlap_px": 5,
                    "cleanup_overlap_px": 15,
                    "route": "CLEANUP",
                }],
            }}},
        )
        row = rule_report_row(result, ("TOP",))
        self.assertIn("glass #2 -> CLEANUP", row["detail"])
        self.assertIn("platform 8px", row["detail"])
        self.assertIn("pin 2px", row["detail"])
        self.assertIn("ring 5px", row["detail"])
        public = row["details"]["per_role"]["TOP"]
        self.assertIn("hits", public)
        self.assertNotIn("glasses_total", public)

    def test_top_sinks_report_lists_forbidden_pixels_per_shell(self):
        result = SimpleNamespace(
            rule_name="sinks",
            triggered=True,
            defect="sinks",
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": None,
                "defect_sinks": 1,
                "sinks_total": 3,
                "contacts_used": 14,
                "hits": [{
                    "sink_index": 2,
                    "forbidden_pixels": 11,
                    "central_overlap_px": 18,
                    "platform_overlap_px": 4,
                    "contacts_overlap_px": 2,
                }],
            }}},
        )
        row = rule_report_row(result, ("TOP",))
        self.assertIn("shell #2", row["detail"])
        self.assertIn("forbidden 11px", row["detail"])
        public = row["details"]["per_role"]["TOP"]
        self.assertIn("hits", public)
        self.assertNotIn("sinks_total", public)
        self.assertNotIn("contacts_used", public)

    def test_tab_switches_between_raw_and_rules_after_analysis(self):
        app = InteractiveAnalyzer.__new__(InteractiveAnalyzer)
        app.report = {"ok": True}
        app.mode = "RAW"
        app.mode_var = SimpleNamespace(set=lambda value: None)
        app.status_var = SimpleNamespace(set=lambda value: None)
        app._is_current_analyzed = lambda: True
        app._update_mode_buttons = lambda: None
        app._render_image = lambda: None
        self.assertEqual(app._on_tab(), "break")
        self.assertEqual(app.mode, "RULES")
        self.assertEqual(app._on_tab(), "break")
        self.assertEqual(app.mode, "RAW")

    def test_safe_name_and_directory_discovery_support_single_and_prefixed_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TOP.jpg").write_bytes(b"x")
            (root / "part_002_INPUT_LEFT.png").write_bytes(b"x")
            (root / "part_002_INPUT_RIGHT.png").write_bytes(b"x")
            samples = discover_in_directory(root)
            by_name = {sample.name: sample for sample in samples}
            self.assertIn(safe_name(root.name), by_name)
            self.assertEqual(set(by_name[safe_name(root.name)].images), {"TOP"})
            self.assertEqual(
                set(by_name["part_002"].images),
                {"INPUT_LEFT", "INPUT_RIGHT"},
            )

    def test_manifest_paths_are_relative_to_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "left.jpg").write_bytes(b"x")
            manifest = root / "samples.json"
            manifest.write_text(json.dumps({
                "samples": [{
                    "name": "sample one",
                    "images": {"INPUT_LEFT": "left.jpg"},
                }]
            }), encoding="utf-8")
            samples = load_manifest(manifest)
            self.assertEqual(samples[0].name, "sample_one")
            self.assertEqual(samples[0].images["INPUT_LEFT"], root / "left.jpg")

    def test_partition_rules_allows_single_camera_partial_rule_diagnostics(self):
        rules = [
            SimpleNamespace(name="input", ROLES=INPUT_ROLES),
            SimpleNamespace(name="top", ROLES=("TOP",)),
            SimpleNamespace(name="spider", ROLES=("SPIDER_LEFT", "SPIDER_RIGHT")),
        ]
        runnable, skipped = partition_rules(rules, {"INPUT_LEFT", "TOP"})
        self.assertEqual([rule.name for rule in runnable], ["input", "top"])
        self.assertEqual(skipped, [])

    def test_frame_health_matches_production_resolution_and_black_guard(self):
        width, height = EXPECTED_SIZE
        good = np.full((height, width, 3), 100, dtype=np.uint8)
        report = inspect_frame_health(
            good,
            allow_size_mismatch=False,
            allow_near_black=False,
        )
        self.assertEqual((report["width"], report["height"]), EXPECTED_SIZE)
        black = np.zeros((height, width, 3), dtype=np.uint8)
        with self.assertRaisesRegex(RuntimeError, "Почти чёрное"):
            inspect_frame_health(
                black,
                allow_size_mismatch=False,
                allow_near_black=False,
            )

    def test_analysis_writes_json_html_and_three_image_variants(self):
        class Vision:
            def __init__(self):
                self.last_health = []

            def process_all(self, frames):
                self.last_health = [{
                    "role": "TOP",
                    "model": "weights/top.pt",
                    "ok": True,
                    "elapsed_ms": 5.0,
                    "detections": 0,
                    "error": None,
                }]
                return {role: [] for role in frames}

        class Decision:
            thresholds = {}

            def __init__(self):
                self.rules = [SimpleNamespace(name="top_rule", ROLES=("TOP",))]

            def evaluate_rules_detailed(self, rules, vision_results, frames=None):
                return [SimpleNamespace(
                    rule_name=rules[0].name,
                    triggered=False,
                    defect=None,
                    details={},
                    drawings=[],
                )]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "TOP.jpg"
            write_image(
                image_path,
                np.full((720, 1280, 3), 100, dtype=np.uint8),
            )
            report = analyze_sample(
                SampleSpec("sample", {"TOP": image_path}),
                root / "results",
                Vision(),
                Decision(),
                allow_size_mismatch=False,
                allow_near_black=False,
            )
            sample_dir = root / "results" / "sample"
            self.assertTrue(report["ok"])
            self.assertTrue((sample_dir / "report.json").is_file())
            self.assertTrue((sample_dir / "report.html").is_file())
            for kind in ("original", "models", "rules"):
                self.assertTrue((sample_dir / kind / "TOP.jpg").is_file())

    def test_folder_role_treats_every_arbitrarily_named_image_as_separate_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad one.jpg").write_bytes(b"x")
            nested = root / "nested"
            nested.mkdir()
            (nested / "bad two.png").write_bytes(b"x")
            samples = discover_folder_as_role(root, "SPIDER_IN")
            self.assertEqual(len(samples), 2)
            self.assertTrue(all(set(sample.images) == {"SPIDER_IN"} for sample in samples))
            self.assertEqual(len({sample.name for sample in samples}), 2)
            input_samples = discover_folder_as_role(root, "INPUT_LEFT")
            self.assertEqual(len(input_samples), 2)
            self.assertTrue(
                all(set(sample.images) == {"INPUT_LEFT"} for sample in input_samples)
            )

    def test_detection_statistics_include_actual_mask_area(self):
        summary = summarize_detections({
            "SPIDER_IN": [{
                "class": "omission-short",
                "confidence": 0.9,
                "bbox": [0, 0, 20, 10],
                "mask": [[0, 0], [10, 0], [10, 10], [0, 10]],
                "model_path": "short.pt",
            }],
        })
        item = summary["SPIDER_IN"]["items"][0]
        self.assertEqual(item["mask_area_px2"], 100.0)
        self.assertEqual(item["bbox_area_px2"], 200.0)
        self.assertIsNone(item["mask_area_error"])

    def test_omission_public_details_hide_raw_and_ignored_metrics(self):
        result = SimpleNamespace(
            rule_name="long_omission",
            triggered=True,
            defect="long_omission",
            details={"per_role": {"SPIDER_LEFT": {
                "triggered": True,
                "valid": True,
                "reason": None,
                "found": 3,
                "ignored": 2,
                "allowed_thickness_px": 20.0,
                "excess_component_min_px": 3,
                "top_line_max_residual_px": 3.0,
                "top_line_actual_max_residual_px": 0.8,
                "largest_component_pixels": 12,
                "excess_pixels": 12,
                "max_excess_depth_px": 4.0,
                "raw_excess_pixels": 17,
                "ignored_noise_components": 2,
                "max_consecutive_columns": 5,
                "excess_x_start": 10,
            }}},
        )
        row = rule_report_row(result, ("SPIDER_LEFT", "SPIDER_RIGHT"))
        public = row["details"]["per_role"]["SPIDER_LEFT"]
        self.assertIn("largest_component_pixels", public)
        self.assertIn("top_line_actual_max_residual_px", public)
        self.assertNotIn("found", public)
        self.assertNotIn("ignored", public)
        self.assertNotIn("raw_excess_pixels", public)
        self.assertNotIn("ignored_noise_components", public)
        self.assertNotIn("max_consecutive_columns", public)
        self.assertNotIn("excess_x_start", public)

    def test_batch_omission_calibration_reports_boundary_measurements(self):
        def report(name, excess, depth, columns):
            return {
                "sample": name,
                "images": {"SPIDER_IN": {"path": f"{name}.jpg"}},
                "rules": [{
                    "name": "short_omission",
                    "details": {"per_role": {"SPIDER_IN": {
                        "triggered": excess > 0,
                        "found": 1,
                        "ignored": 0,
                        "valid": True,
                        "reason": None,
                        "mask_area_px2": 5000,
                        "allowed_thickness_px": 20,
                        "excess_component_min_px": 3,
                        "top_line_angle_deg": 0.0,
                        "top_line_max_residual_px": 3.0,
                        "top_line_actual_max_residual_px": 0.5,
                        "raw_excess_pixels": excess,
                        "excess_pixels": excess,
                        "largest_component_pixels": excess,
                        "confirmed_components": 1 if excess else 0,
                        "ignored_noise_components": 0,
                        "ignored_noise_pixels": 0,
                        "max_excess_depth_px": depth,
                        "max_consecutive_columns": columns,
                        "excess_x_start": 100 if excess else None,
                        "excess_x_end": 100 + columns if excess else None,
                    }}},
                }],
            }

        reports = [
            report("bad_1", 340, 18.0, 24),
            report("bad_2", 120, 8.0, 10),
        ]
        calibration = collect_omission_calibration(reports)
        summary = calibration["summaries"][0]
        self.assertEqual(summary["largest_component_max_px"], 340)
        self.assertEqual(summary["confirmed_median_px"], 230.0)
        self.assertEqual(summary["confirmed_max_px"], 340)
        self.assertEqual(summary["max_actual_residual_px"], 0.5)
        self.assertEqual(summary["residual_limit_px"], 3.0)
        self.assertEqual(summary["max_depth_px"], 18.0)
        self.assertNotIn("ignored", calibration["rows"][0])
        self.assertNotIn("raw_excess_pixels", calibration["rows"][0])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for item in reports:
                sample_dir = root / item["sample"]
                sample_dir.mkdir()
                (sample_dir / "report.html").write_text("ok", encoding="utf-8")
            write_index(root, reports)
            self.assertTrue((root / "omission_areas.csv").is_file())
            self.assertTrue((root / "omission_areas.json").is_file())
            self.assertTrue((root / "omission_areas.html").is_file())
            csv_text = (root / "omission_areas.csv").read_text(
                encoding="utf-8-sig"
            )
            self.assertIn("allowed_thickness_px", csv_text)
            self.assertIn("excess_pixels", csv_text)
            self.assertIn("top_line_actual_max_residual_px", csv_text)
            self.assertNotIn("raw_excess_pixels", csv_text)
            self.assertNotIn("ignored_noise_components", csv_text)
            html_text = (root / "omission_areas.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("КАЛИБРОВКА ГРАНИЦЫ OMISSION", html_text)
            self.assertIn("Residual max/limit", html_text)

    def test_missing_omission_measurement_is_reported_as_invalid(self):
        calibration = collect_omission_calibration([{
            "sample": "missed",
            "images": {"SPIDER_LEFT": {"path": "missed.jpg"}},
            "rules": [{
                "name": "long_omission",
                "details": {"per_role": {"SPIDER_LEFT": {
                    "triggered": True,
                    "found": 0,
                    "ignored": 0,
                    "valid": False,
                    "reason": "no_detections",
                    "allowed_thickness_px": 20,
                    "excess_component_min_px": 3,
                    "excess_pixels": None,
                    "max_excess_depth_px": None,
                    "max_consecutive_columns": None,
                }}},
            }],
        }])
        summary = calibration["summaries"][0]
        self.assertEqual(summary["invalid_samples"], 1)
        self.assertEqual(summary["valid_samples"], 0)
        self.assertIsNone(summary["confirmed_median_px"])
        self.assertIsNone(summary["max_actual_residual_px"])

    def test_category_is_final_only_for_complete_seven_camera_set(self):
        rule = SimpleNamespace(name="top_glass", ROLES=("TOP",))
        decision = SimpleNamespace(rules=[rule])
        result = SimpleNamespace(
            rule_name="top_glass",
            triggered=True,
            defect="glass",
        )
        category, defects = compute_category(
            [result], set(ROLES), False, decision,
        )
        self.assertEqual(category, "CLEANUP")
        self.assertEqual(defects, ["glass"])
        category, _ = compute_category(
            [result], set(SPIDER_ROLES), False, decision,
        )
        self.assertEqual(category, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
