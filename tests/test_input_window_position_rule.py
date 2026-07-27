import unittest
from pathlib import Path

import numpy as np

from domain.defect_rules.rule_input_part_presence import InputPartPresenceRule
from domain.defect_rules.rule_input_window_geometry import InputWindowGeometryRule
from vision.overlay.debug_overlay import DebugOverlay
from vision.overlay.renderers.window_geometry import (
    COLOR_BOTTOM_MEASURE,
    COLOR_TOP_MEASURE,
    WindowGeometryRenderer,
)


def role_thresholds(
    role,
    *,
    top_min=40,
    top_max=60,
    bottom_min=40,
    bottom_max=60,
):
    return {
        f"{role}.input_window_geometry_min_confidence": 0.4,
        f"{role}.input_window_geometry_expected_count": 7,
        f"{role}.input_window_geometry_top_px_min": top_min,
        f"{role}.input_window_geometry_top_px_max": top_max,
        f"{role}.input_window_geometry_bottom_px_min": bottom_min,
        f"{role}.input_window_geometry_bottom_px_max": bottom_max,
        f"{role}.input_window_geometry_center_zone_ratio": 0.5,
    }


def h_mask(x_offset=0, crossbar_top=45, crossbar_bottom=55):
    return [
        [x_offset + 10, 0],
        [x_offset + 20, 0],
        [x_offset + 20, crossbar_top],
        [x_offset + 80, crossbar_top],
        [x_offset + 80, 0],
        [x_offset + 90, 0],
        [x_offset + 90, 100],
        [x_offset + 80, 100],
        [x_offset + 80, crossbar_bottom],
        [x_offset + 20, crossbar_bottom],
        [x_offset + 20, 100],
        [x_offset + 10, 100],
    ]


def detection(index, crossbar_top=45, crossbar_bottom=55, confidence=0.95):
    offset = index * 110
    return {
        "class": "flatness",
        "confidence": confidence,
        "bbox": [offset + 10, 0, offset + 91, 101],
        "mask": h_mask(offset, crossbar_top, crossbar_bottom),
    }


def presence_thresholds(
    *,
    left_confidence=0.4,
    right_confidence=0.4,
    left_false_positive_max=2,
    right_false_positive_max=2,
):
    return {
        "INPUT_LEFT.input_window_geometry_min_confidence": left_confidence,
        "INPUT_RIGHT.input_window_geometry_min_confidence": right_confidence,
        "INPUT_LEFT.input_part_presence_false_positive_max_count": (
            left_false_positive_max
        ),
        "INPUT_RIGHT.input_part_presence_false_positive_max_count": (
            right_false_positive_max
        ),
    }


class InputWindowPositionRuleTests(unittest.TestCase):
    def test_two_measurements_use_mask_bounds_and_lower_crossbar_edge_in_pixels(self):
        measured = InputWindowGeometryRule._measure_crossbar_position(
            detection(0), 0.5,
        )
        self.assertTrue(measured["valid"])
        self.assertAlmostEqual(
            measured["top_px"] + measured["bottom_px"],
            measured["full_height"],
        )
        self.assertAlmostEqual(measured["top_px"], 56.0)
        self.assertAlmostEqual(measured["bottom_px"], 45.0)
        self.assertNotIn("top_ratio", measured)
        self.assertNotIn("bottom_ratio", measured)

    def test_any_single_out_of_pixel_limits_rejects_role(self):
        rule = InputWindowGeometryRule(role_thresholds("INPUT_LEFT"))
        detections = [detection(index) for index in range(7)]
        detections[3] = detection(3, crossbar_top=70, crossbar_bottom=80)
        result = rule.check({"INPUT_LEFT": detections})
        self.assertTrue(result.triggered)
        role = result.details["per_role"]["INPUT_LEFT"]
        self.assertEqual(role["failed_indices"], [4])
        self.assertGreater(role["top_values_px"][3], 60)
        self.assertLess(role["bottom_values_px"][3], 40)
        failed_drawing = next(
            drawing
            for drawing in result.drawings
            if drawing.get("type") == "window_geometry_item"
            and drawing.get("index") == 4
        )
        self.assertTrue(failed_drawing["triggered"])
        self.assertTrue(failed_drawing["top_fail"])
        self.assertTrue(failed_drawing["bottom_fail"])

    def test_all_centered_crossbars_pass_and_draw_pixel_segments(self):
        rule = InputWindowGeometryRule(role_thresholds("INPUT_LEFT"))
        result = rule.check({
            "INPUT_LEFT": [detection(index) for index in range(7)]
        })
        self.assertFalse(result.triggered)
        items = [
            drawing for drawing in result.drawings
            if drawing.get("type") == "window_geometry_item"
        ]
        self.assertEqual(len(items), 7)
        self.assertTrue(all("top_px" in item for item in items))
        self.assertTrue(all("bottom_px" in item for item in items))
        self.assertTrue(all("top_ratio" not in item for item in items))

        image = np.zeros((120, 800, 3), dtype=np.uint8)
        WindowGeometryRenderer.draw_item(image, items[0])
        self.assertGreater(int(image.sum()), 0)
        self.assertTrue(np.any(np.all(image == COLOR_TOP_MEASURE, axis=2)))
        self.assertTrue(np.any(np.all(image == COLOR_BOTTOM_MEASURE, axis=2)))
        self.assertNotEqual(COLOR_TOP_MEASURE, COLOR_BOTTOM_MEASURE)
        renderer_source = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/window_geometry.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("cv2.putText", renderer_source)
        self.assertNotIn("cv2.fillPoly", renderer_source)

    def test_left_and_right_have_independent_pixel_thresholds(self):
        thresholds = {
            **role_thresholds("INPUT_LEFT", top_min=40, top_max=60),
            **role_thresholds("INPUT_RIGHT", top_min=70, top_max=90),
        }
        detections = [detection(index) for index in range(7)]
        result = InputWindowGeometryRule(thresholds).check({
            "INPUT_LEFT": detections,
            "INPUT_RIGHT": detections,
        })
        self.assertFalse(result.details["per_role"]["INPUT_LEFT"]["triggered"])
        self.assertTrue(result.details["per_role"]["INPUT_RIGHT"]["triggered"])

    def test_part_presence_uses_separate_confidence_for_each_input_camera(self):
        thresholds = presence_thresholds(
            left_confidence=0.8,
            right_confidence=0.4,
        )
        result = InputPartPresenceRule(thresholds).check({
            "INPUT_LEFT": [detection(0, confidence=0.5)],
            "INPUT_RIGHT": [detection(0, confidence=0.5)],
        })
        self.assertEqual(result.details["flatness_left"], 0)
        self.assertEqual(result.details["flatness_right"], 1)
        self.assertTrue(result.details["empty_tray"])
        self.assertEqual(result.details["false_positive_ignored_right"], 1)
        self.assertEqual(
            result.details["min_confidence_by_role"],
            {"INPUT_LEFT": 0.8, "INPUT_RIGHT": 0.4},
        )

    def test_part_presence_filters_zero_one_or_two_hits_per_camera(self):
        rule = InputPartPresenceRule(presence_thresholds())
        for left_count, right_count in (
            (0, 0),
            (1, 0),
            (2, 0),
            (0, 2),
            (2, 2),
        ):
            with self.subTest(left=left_count, right=right_count):
                result = rule.check({
                    "INPUT_LEFT": [detection(index) for index in range(left_count)],
                    "INPUT_RIGHT": [detection(index) for index in range(right_count)],
                })
                self.assertTrue(result.details["empty_tray"])
                self.assertEqual(
                    result.details["false_positive_ignored_left"],
                    left_count,
                )
                self.assertEqual(
                    result.details["false_positive_ignored_right"],
                    right_count,
                )
                self.assertFalse(result.triggered)
                self.assertEqual(result.drawings, [])

    def test_part_presence_requires_three_hits_on_both_input_cameras(self):
        rule = InputPartPresenceRule(presence_thresholds())
        for left_count, right_count in ((3, 3), (4, 3), (7, 7)):
            with self.subTest(left=left_count, right=right_count):
                result = rule.check({
                    "INPUT_LEFT": [detection(index) for index in range(left_count)],
                    "INPUT_RIGHT": [detection(index) for index in range(right_count)],
                })
                self.assertFalse(result.details["empty_tray"])
                self.assertTrue(all(result.details["presence_by_role"].values()))
                self.assertFalse(result.triggered)
                self.assertEqual(result.drawings, [])

    def test_part_presence_one_camera_is_not_enough(self):
        rule = InputPartPresenceRule(presence_thresholds())
        for left_count, right_count in ((3, 0), (0, 3), (7, 0), (3, 2), (2, 3)):
            with self.subTest(left=left_count, right=right_count):
                result = rule.check({
                    "INPUT_LEFT": [detection(index) for index in range(left_count)],
                    "INPUT_RIGHT": [detection(index) for index in range(right_count)],
                })
                self.assertTrue(result.details["empty_tray"])
                self.assertFalse(all(result.details["presence_by_role"].values()))
                self.assertEqual(result.drawings, [])

    def test_part_presence_filter_is_independent_and_rules_render_is_empty(self):
        rule = InputPartPresenceRule(presence_thresholds(
            left_false_positive_max=1,
            right_false_positive_max=2,
        ))
        result = rule.check({
            "INPUT_LEFT": [detection(0), detection(1)],
            "INPUT_RIGHT": [detection(0), detection(1)],
        })
        self.assertTrue(result.details["empty_tray"])
        self.assertTrue(result.details["presence_by_role"]["INPUT_LEFT"])
        self.assertFalse(result.details["presence_by_role"]["INPUT_RIGHT"])
        self.assertEqual(result.drawings, [])

        empty_result = InputPartPresenceRule(presence_thresholds()).check({
            "INPUT_LEFT": [detection(0), detection(1)],
            "INPUT_RIGHT": [detection(0)],
        })
        self.assertTrue(empty_result.details["empty_tray"])
        self.assertEqual(empty_result.drawings, [])
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        rendered = DebugOverlay.render_frame(
            frame,
            "INPUT_LEFT",
            [empty_result],
        )
        self.assertTrue(np.array_equal(rendered, frame))
        renderer_path = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/part_presence.py"
        )
        self.assertFalse(renderer_path.exists())

    def test_extra_detection_is_gray_and_best_seven_are_measured(self):
        rule = InputWindowGeometryRule(role_thresholds("INPUT_LEFT"))
        result = rule.check({
            "INPUT_LEFT": [detection(index) for index in range(8)],
        })
        role = result.details["per_role"]["INPUT_LEFT"]
        self.assertEqual(role["found"], 7)
        self.assertEqual(role["found_raw"], 8)
        self.assertEqual(role["ignored"], 1)
        self.assertEqual(sum(
            drawing.get("type") == "window_geometry_item"
            for drawing in result.drawings
        ), 7)
        self.assertEqual(sum(
            drawing.get("type") == "window_geometry_ignored"
            for drawing in result.drawings
        ), 1)

    def test_invalid_mask_has_red_contour_and_cross_without_text(self):
        rule = InputWindowGeometryRule(role_thresholds("INPUT_LEFT"))
        detections = [detection(index) for index in range(7)]
        detections[2] = dict(detections[2], mask=None)
        result = rule.check({"INPUT_LEFT": detections})
        self.assertTrue(result.triggered)
        role = result.details["per_role"]["INPUT_LEFT"]
        self.assertEqual(role["invalid_indices"], [3])
        self.assertEqual(role["items"][2]["reason"], "missing_mask")
        invalid_drawing = next(
            drawing for drawing in result.drawings
            if drawing.get("type") == "window_geometry_item"
            and drawing.get("index") == 3
        )
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        rendered = DebugOverlay.render_frame(
            frame,
            "INPUT_LEFT",
            [result],
        )
        self.assertGreater(int(rendered.sum()), 0)
        self.assertFalse(invalid_drawing["valid"])
        error = next(
            drawing for drawing in result.drawings
            if drawing.get("type") == "construction_error"
        )
        self.assertEqual(error["message"], "NO T/B #3")

    def test_wrong_detection_count_uses_contours_and_short_label(self):
        rule = InputWindowGeometryRule(role_thresholds("INPUT_LEFT"))
        result = rule.check({
            "INPUT_LEFT": [detection(index) for index in range(6)],
        })
        self.assertTrue(result.triggered)
        role = result.details["per_role"]["INPUT_LEFT"]
        self.assertEqual(role["reason"], "too_few: 6/7")
        count_items = [
            drawing for drawing in result.drawings
            if drawing.get("type") == "window_geometry_count_item"
        ]
        self.assertEqual(len(count_items), 6)
        construction = [
            drawing for drawing in result.drawings
            if drawing.get("type") == "construction_error"
        ]
        self.assertEqual(len(construction), 1)
        self.assertEqual(construction[0]["message"], "WINDOWS 6/7")

        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        rendered = DebugOverlay.render_frame(frame, "INPUT_LEFT", [result])
        self.assertGreater(int(rendered.sum()), 0)

        no_windows = rule.check({"INPUT_LEFT": []})
        self.assertTrue(no_windows.triggered)
        self.assertEqual(len(no_windows.drawings), 1)
        self.assertEqual(
            no_windows.drawings[0]["type"], "construction_error",
        )
        self.assertEqual(no_windows.drawings[0]["message"], "WINDOWS 0/7")
        rendered_empty = DebugOverlay.render_frame(
            frame,
            "INPUT_LEFT",
            [no_windows],
        )
        self.assertGreater(int(rendered_empty.sum()), 0)


if __name__ == "__main__":
    unittest.main()
