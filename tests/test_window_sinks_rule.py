import unittest
from pathlib import Path

import numpy as np

from domain.defect_rules.rule_input_window_sinks import InputWindowSinksRule
from vision.overlay.debug_overlay import DebugOverlay


def rect_detection(class_name, x1, y1, x2, y2, *, mask=True, confidence=0.99):
    return {
        "class": class_name,
        "confidence": confidence,
        "bbox": [x1, y1, x2, y2],
        "mask": (
            [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            if mask else None
        ),
    }


def seven_windows():
    return [
        rect_detection("flatness", 100 + index * 40, 100, 130 + index * 40, 140)
        for index in range(7)
    ]


def thresholds(overlap_min_px=5):
    role = "INPUT_LEFT"
    return {
        f"{role}.input_window_sinks_min_confidence": 0.4,
        f"{role}.input_window_sinks_window_min_confidence": 0.15,
        f"{role}.input_window_sinks_overlap_min_px": overlap_min_px,
        f"{role}.input_window_geometry_expected_count": 7,
    }


class WindowSinksRuleTests(unittest.TestCase):
    def test_no_sinks_needs_no_window_references(self):
        result = InputWindowSinksRule(thresholds()).check({"INPUT_LEFT": []})
        details = result.details["per_role"]["INPUT_LEFT"]
        self.assertFalse(result.triggered)
        self.assertEqual(details["sinks_total"], 0)
        self.assertEqual(result.drawings, [])

    def test_sink_overlap_is_measured_per_sink_and_window(self):
        sink = rect_detection("objects", 105, 105, 112, 112)
        result = InputWindowSinksRule(thresholds()).check({
            "INPUT_LEFT": [*seven_windows(), sink],
        })
        details = result.details["per_role"]["INPUT_LEFT"]
        self.assertTrue(result.triggered)
        self.assertEqual(details["confirmed_sinks"], 1)
        self.assertEqual(len(details["hits"]), 1)
        hit = details["hits"][0]
        self.assertEqual(hit["sink_index"], 1)
        self.assertEqual(hit["window_index"], 1)
        self.assertGreaterEqual(hit["overlap_px"], 5)
        self.assertEqual(details["overlap_min_px"], 5)
        self.assertEqual(sum(
            drawing.get("type") == "window_sink_overlap"
            for drawing in result.drawings
        ), 1)

        frame = np.zeros((240, 500, 3), dtype=np.uint8)
        rendered = DebugOverlay.render_frame(
            frame,
            "INPUT_LEFT",
            [result],
        )
        self.assertGreater(int(rendered.sum()), 0)
        source = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/window_sinks.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("cv2.putText", source)
        self.assertNotIn("cv2.fillPoly", source)

    def test_overlap_threshold_is_inclusive_and_not_summed_between_sinks(self):
        sink = rect_detection("objects", 105, 105, 112, 112)
        base = InputWindowSinksRule(thresholds()).check({
            "INPUT_LEFT": [*seven_windows(), sink],
        })
        overlap = base.details["per_role"]["INPUT_LEFT"]["hits"][0][
            "overlap_px"
        ]
        at_limit = InputWindowSinksRule(thresholds(overlap)).check({
            "INPUT_LEFT": [*seven_windows(), sink],
        })
        above_limit = InputWindowSinksRule(thresholds(overlap + 1)).check({
            "INPUT_LEFT": [*seven_windows(), sink],
        })
        self.assertTrue(at_limit.triggered)
        self.assertFalse(above_limit.triggered)

        two_small_sinks = [
            rect_detection("objects", 105, 105, 106, 106),
            rect_detection("objects", 145, 105, 146, 106),
        ]
        not_summed = InputWindowSinksRule(thresholds(5)).check({
            "INPUT_LEFT": [*seven_windows(), *two_small_sinks],
        })
        self.assertFalse(not_summed.triggered)
        self.assertEqual(
            not_summed.details["per_role"]["INPUT_LEFT"]["hits"],
            [],
        )

    def test_sink_outside_windows_is_raw_only(self):
        sink = rect_detection("objects", 10, 10, 20, 20)
        result = InputWindowSinksRule(thresholds()).check({
            "INPUT_LEFT": [*seven_windows(), sink],
        })
        details = result.details["per_role"]["INPUT_LEFT"]
        self.assertFalse(result.triggered)
        self.assertEqual(details["hits"], [])
        self.assertNotIn("sinks_outside", details)
        self.assertEqual(result.drawings, [])

    def test_same_best_seven_are_used_and_extra_window_does_not_trigger(self):
        windows = [
            rect_detection(
                "flatness",
                100 + index * 40,
                100,
                130 + index * 40,
                140,
            )
            for index in range(8)
        ]
        # Пересекается только с восьмой mask, которая при равном шаге лишняя.
        sink = rect_detection("objects", 385, 105, 392, 112)
        result = InputWindowSinksRule(thresholds()).check({
            "INPUT_LEFT": [*windows, sink],
        })
        details = result.details["per_role"]["INPUT_LEFT"]
        self.assertFalse(result.triggered)
        self.assertEqual(details["selected_windows"], 7)
        self.assertEqual(details["ignored_windows"], 1)
        self.assertEqual(details["hits"], [])
        self.assertEqual(result.drawings, [])

    def test_missing_window_count_fails_closed_with_found_contours(self):
        sink = rect_detection("objects", 105, 105, 112, 112)
        result = InputWindowSinksRule(thresholds()).check({
            "INPUT_LEFT": [*seven_windows()[:6], sink],
        })
        details = result.details["per_role"]["INPUT_LEFT"]
        self.assertTrue(result.triggered)
        self.assertIn("invalid_window_reference_count", details["reason"])
        self.assertEqual(details["selected_windows"], 6)
        self.assertEqual(sum(
            drawing.get("type") == "window_sink_reference_count_item"
            for drawing in result.drawings
        ), 6)
        error = next(
            drawing for drawing in result.drawings
            if drawing.get("type") == "construction_error"
        )
        self.assertEqual(error["message"], "WINDOWS REF 6/7")

    def test_segmentation_masks_are_required_without_bbox_fallback(self):
        windows = seven_windows()
        invalid_window = list(windows)
        invalid_window[2] = dict(invalid_window[2], mask=None)
        sink = rect_detection("objects", 105, 105, 112, 112)
        window_result = InputWindowSinksRule(thresholds()).check({
            "INPUT_LEFT": [*invalid_window, sink],
        })
        window_details = window_result.details["per_role"]["INPUT_LEFT"]
        self.assertTrue(window_result.triggered)
        self.assertEqual(window_details["reason"], "invalid_window_masks")
        self.assertEqual(window_details["invalid_window_indices"], [3])
        window_error = next(
            drawing for drawing in window_result.drawings
            if drawing.get("type") == "construction_error"
        )
        self.assertEqual(window_error["message"], "NO WINDOW MASK #3")

        invalid_sink = rect_detection(
            "objects", 105, 105, 112, 112, mask=False,
        )
        sink_result = InputWindowSinksRule(thresholds()).check({
            "INPUT_LEFT": [*windows, invalid_sink],
        })
        sink_details = sink_result.details["per_role"]["INPUT_LEFT"]
        self.assertTrue(sink_result.triggered)
        self.assertEqual(sink_details["reason"], "invalid_sink_masks")
        self.assertEqual(sink_details["invalid_sink_indices"], [1])
        self.assertTrue(any(
            drawing.get("type") == "window_sink_invalid_reference"
            for drawing in sink_result.drawings
        ))
        sink_error = next(
            drawing for drawing in sink_result.drawings
            if drawing.get("type") == "construction_error"
        )
        self.assertEqual(sink_error["message"], "NO SINK MASK #1")


if __name__ == "__main__":
    unittest.main()
