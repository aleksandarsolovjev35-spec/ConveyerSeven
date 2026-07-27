import unittest
from pathlib import Path

import numpy as np

from domain.defect_rules.base import BaseRule
from domain.defect_rules.omission_boundary import (
    OmissionBoundaryMixin,
    measure_omission_boundary,
)
from domain.defect_rules.rule_spider_long_omission import SpiderLongOmissionRule
from domain.defect_rules.rule_spider_short_omission import SpiderShortOmissionRule
from vision.overlay.debug_overlay import DebugOverlay


def omission_thresholds(
    role,
    *,
    allowed=20.0,
    component_min=3,
    residual=3.0,
    confidence=0.3,
):
    family = "long" if role in ("SPIDER_LEFT", "SPIDER_RIGHT") else "short"
    prefix = f"{role}.spider_{family}_omission_"
    return {
        prefix + "min_confidence": confidence,
        prefix + "allowed_thickness_px": allowed,
        prefix + "excess_component_min_px": component_min,
        prefix + "top_line_max_residual_px": residual,
    }


def polygon_detection(class_name, points, confidence=0.9):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {
        "class": class_name,
        "confidence": confidence,
        "bbox": [min(xs), min(ys), max(xs), max(ys)],
        "mask": points,
    }


def rectangle(class_name, x, y, width, height):
    return polygon_detection(class_name, [
        [x, y], [x + width, y],
        [x + width, y + height], [x, y + height],
    ])


class OmissionBoundaryRuleTests(unittest.TestCase):
    def test_common_boundary_code_is_not_a_separate_active_rule(self):
        self.assertFalse(issubclass(OmissionBoundaryMixin, BaseRule))
        self.assertTrue(issubclass(SpiderLongOmissionRule, BaseRule))
        self.assertTrue(issubclass(SpiderShortOmissionRule, BaseRule))
        self.assertEqual(SpiderLongOmissionRule.name, "long_omission")
        self.assertEqual(SpiderShortOmissionRule.name, "short_omission")

    def test_thin_mask_inside_single_limit_line_passes(self):
        role = "SPIDER_LEFT"
        detection = rectangle("omission-long", 100, 100, 200, 20)
        result = SpiderLongOmissionRule(omission_thresholds(role)).check({
            role: [detection],
        })
        details = result.details["per_role"][role]
        self.assertFalse(result.triggered)
        self.assertEqual(details["excess_pixels"], 0)
        self.assertEqual(details["allowed_thickness_px"], 20.0)

        rendered = DebugOverlay.render_frame(
            np.zeros((300, 500, 3), dtype=np.uint8),
            role,
            [result],
        )
        self.assertGreater(int(rendered.sum()), 0)

    def test_single_excess_pixel_is_ignored_as_noise(self):
        role = "SPIDER_LEFT"
        detection = polygon_detection("omission-long", [
            [0, 0], [100, 0], [100, 20],
            [52, 20], [51, 21], [50, 20], [0, 20],
        ])
        result = SpiderLongOmissionRule(omission_thresholds(role)).check({
            role: [detection],
        })
        details = result.details["per_role"][role]
        self.assertFalse(result.triggered)
        self.assertEqual(details["raw_excess_pixels"], 1)
        self.assertEqual(details["excess_pixels"], 0)
        self.assertEqual(details["largest_component_pixels"], 1)
        self.assertEqual(details["ignored_noise_components"], 1)
        rendered = DebugOverlay.render_frame(
            np.zeros((160, 180, 3), dtype=np.uint8),
            role,
            [result],
        )
        red_pixels = int(np.count_nonzero(np.all(
            rendered == np.asarray([0, 0, 255], dtype=np.uint8),
            axis=2,
        )))
        self.assertEqual(red_pixels, 0)

    def test_same_single_pixel_rejects_if_threshold_is_one(self):
        role = "SPIDER_LEFT"
        detection = polygon_detection("omission-long", [
            [0, 0], [100, 0], [100, 20],
            [52, 20], [51, 21], [50, 20], [0, 20],
        ])
        result = SpiderLongOmissionRule(
            omission_thresholds(role, component_min=1)
        ).check({role: [detection]})
        self.assertTrue(result.triggered)

    def test_narrow_connected_tail_at_least_three_pixels_rejects(self):
        role = "SPIDER_LEFT"
        detection = polygon_detection("omission-long", [
            [0, 0], [100, 0], [100, 20],
            [51, 20], [51, 60], [50, 60], [50, 20], [0, 20],
        ])
        result = SpiderLongOmissionRule(omission_thresholds(role)).check({
            role: [detection],
        })
        details = result.details["per_role"][role]
        self.assertTrue(result.triggered)
        self.assertGreater(details["excess_pixels"], 0)
        self.assertGreater(details["max_excess_depth_px"], 0)
        self.assertGreaterEqual(details["max_consecutive_columns"], 1)

    def test_thick_middle_and_thick_edge_both_reject(self):
        role = "SPIDER_IN"
        middle = polygon_detection("omission-short", [
            [0, 0], [120, 0], [120, 20],
            [80, 20], [80, 60], [40, 60], [40, 20], [0, 20],
        ])
        edge = polygon_detection("omission-short", [
            [0, 0], [120, 0], [120, 20],
            [30, 20], [30, 60], [0, 60],
        ])
        rule = SpiderShortOmissionRule(omission_thresholds(role))
        middle_result = rule.check({role: [middle]})
        edge_result = rule.check({role: [edge]})
        self.assertTrue(middle_result.triggered)
        self.assertTrue(edge_result.triggered)
        self.assertEqual(
            edge_result.details["per_role"][role]["excess_x_start"],
            0,
        )

    def test_fully_thick_and_tapered_variations_reject(self):
        role = "SPIDER_OUT"
        full = rectangle("omission-short", 0, 0, 150, 70)
        tapered = polygon_detection("omission-short", [
            [0, 0], [150, 0], [150, 20], [120, 30],
            [90, 40], [60, 50], [30, 60], [0, 70],
        ])
        rule = SpiderShortOmissionRule(omission_thresholds(role))
        self.assertTrue(rule.check({role: [full]}).triggered)
        self.assertTrue(rule.check({role: [tapered]}).triggered)

    def test_only_largest_mask_is_checked(self):
        role = "SPIDER_RIGHT"
        main_thin = rectangle("omission-long", 0, 0, 200, 20)
        small_deep = rectangle("omission-long", 220, 0, 5, 100)
        result = SpiderLongOmissionRule(omission_thresholds(role)).check({
            role: [main_thin, small_deep],
        })
        details = result.details["per_role"][role]
        self.assertFalse(result.triggered)
        self.assertEqual(details["found"], 2)
        self.assertEqual(details["ignored"], 1)
        self.assertEqual(len(result.drawings), 1)
        self.assertFalse(any(
            "ignored" in str(drawing.get("type", ""))
            for drawing in result.drawings
        ))

    def test_lines_end_at_main_mask_width(self):
        detection = rectangle("omission-long", 100, 100, 200, 20)
        measurement = measure_omission_boundary(
            [detection],
            allowed_thickness_px=20,
            excess_component_min_px=3,
            top_line_max_residual_px=3,
        )
        self.assertTrue(measurement["valid"])
        self.assertAlmostEqual(measurement["top_line"]["x_start"], 100, delta=1)
        self.assertAlmostEqual(measurement["top_line"]["x_end"], 300, delta=1)

    def test_sloped_thin_mask_passes_with_perpendicular_offsets(self):
        role = "SPIDER_LEFT"
        detection = polygon_detection("omission-long", [
            [0, 20], [200, 60], [200, 80], [0, 40],
        ])
        result = SpiderLongOmissionRule(omission_thresholds(role)).check({
            role: [detection],
        })
        details = result.details["per_role"][role]
        self.assertAlmostEqual(details["top_line_angle_deg"], 11.31, delta=1.0)
        self.assertFalse(result.triggered)

    def test_red_excess_region_is_rendered_on_failure(self):
        role = "SPIDER_LEFT"
        detection = rectangle("omission-long", 50, 50, 200, 60)
        result = SpiderLongOmissionRule(omission_thresholds(role)).check({
            role: [detection],
        })
        rendered = DebugOverlay.render_frame(
            np.zeros((250, 350, 3), dtype=np.uint8),
            role,
            [result],
        )
        red_pixels = int(np.count_nonzero(np.all(
            rendered == np.asarray([0, 0, 255], dtype=np.uint8),
            axis=2,
        )))
        self.assertGreater(red_pixels, 20)

    def test_shared_renderer_is_minimal_for_long_and_short(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/omission_boundary.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("BELOW LIMIT LINE", source)
        self.assertNotIn("cv2.fillPoly", source)
        self.assertNotIn("sample_points", source)
        self.assertNotIn("(0, 0, 0), 5", source)

    def test_missing_invalid_or_bad_top_reference_is_fail_closed(self):
        role = "SPIDER_LEFT"
        empty = SpiderLongOmissionRule(omission_thresholds(role)).check({role: []})
        self.assertTrue(empty.triggered)
        self.assertEqual(empty.details["per_role"][role]["reason"], "no_detections")

        invalid = SpiderLongOmissionRule(omission_thresholds(role)).check({
            role: [{
                "class": "omission-long",
                "confidence": 0.9,
                "bbox": [0, 0, 100, 20],
                "mask": None,
            }],
        })
        self.assertTrue(invalid.triggered)
        self.assertEqual(
            invalid.details["per_role"][role]["reason"],
            "missing_or_invalid_mask",
        )
        error = next(
            drawing for drawing in invalid.drawings
            if drawing.get("type") == "construction_error"
        )
        self.assertEqual(error["message"], "NO VALID OMISSION")
        self.assertNotIn("reason", error)

    def test_each_camera_has_independent_allowed_thickness(self):
        thresholds = {
            **omission_thresholds("SPIDER_LEFT", allowed=15),
            **omission_thresholds("SPIDER_RIGHT", allowed=40),
        }
        detection = rectangle("omission-long", 0, 0, 150, 30)
        result = SpiderLongOmissionRule(thresholds).check({
            "SPIDER_LEFT": [detection],
            "SPIDER_RIGHT": [detection],
        })
        self.assertTrue(result.details["per_role"]["SPIDER_LEFT"]["triggered"])
        self.assertFalse(result.details["per_role"]["SPIDER_RIGHT"]["triggered"])


if __name__ == "__main__":
    unittest.main()
