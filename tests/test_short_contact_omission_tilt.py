import unittest
from pathlib import Path

import numpy as np

from domain.defect_rules.rule_spider_contacts_short import SpiderContactsShortRule
from vision.overlay.debug_overlay import DebugOverlay


def rectangle(class_name, x, y, width, height, confidence=0.99):
    return {
        "class": class_name,
        "confidence": confidence,
        "bbox": [x, y, x + width, y + height],
        "mask": [
            [x, y], [x + width, y],
            [x + width, y + height], [x, y + height],
        ],
    }


def polygon(class_name, points, confidence=0.99):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {
        "class": class_name,
        "confidence": confidence,
        "bbox": [min(xs), min(ys), max(xs), max(ys)],
        "mask": points,
    }


def thresholds(role="SPIDER_IN", tilt_limit=0.20):
    return {
        f"{role}.spider_contacts_short_min_confidence": 0.3,
        f"{role}.spider_contacts_short_expected_count": 2,
        f"{role}.spider_contacts_short_level_deviation_ratio": 1.0,
        f"{role}.spider_contacts_short_omission_tilt_ratio_max": tilt_limit,
        f"{role}.spider_contacts_short_inscribed_rect_width_px": 14.5,
        f"{role}.spider_contacts_short_inscribed_rect_height_px": 7.3,
        f"{role}.spider_contacts_short_area_absolute_min": 100,
        f"{role}.spider_contacts_short_y_filter_ratio": 3.0,
        f"{role}.spider_short_omission_min_confidence": 0.3,
    }


class ShortContactOmissionTiltTests(unittest.TestCase):
    def test_equal_distances_to_horizontal_omission_line_pass(self):
        role = "SPIDER_IN"
        contacts = [
            rectangle("flatness_short", 100, 200, 30, 30),
            rectangle("flatness_short", 180, 200, 30, 30),
        ]
        omission = rectangle("omission-short", 80, 100, 160, 60)
        result = SpiderContactsShortRule(thresholds(role)).check({
            role: [*contacts, omission],
        })
        details = result.details["per_role"][role]
        check = details["omission_tilt_check"]
        self.assertEqual(check["status"], "ok")
        self.assertAlmostEqual(check["distance_delta_ratio"], 0.0, places=3)
        self.assertFalse(details["omission_tilt_fail"])

    def test_contact_skew_rejects_by_normalized_distance_delta(self):
        role = "SPIDER_IN"
        contacts = [
            rectangle("flatness_short", 100, 200, 30, 30),
            rectangle("flatness_short", 180, 215, 30, 30),
        ]
        omission = rectangle("omission-short", 80, 100, 160, 60)
        result = SpiderContactsShortRule(thresholds(role)).check({
            role: [*contacts, omission],
        })
        details = result.details["per_role"][role]
        check = details["omission_tilt_check"]
        self.assertTrue(result.triggered)
        self.assertFalse(details["level_fail"])
        self.assertTrue(details["omission_tilt_fail"])
        self.assertGreater(check["distance_delta_ratio"], 0.20)
        self.assertTrue(any(
            drawing.get("type") == "contacts_short_omission_line"
            for drawing in result.drawings
        ))
        self.assertEqual(sum(
            drawing.get("type") == "contacts_short_omission_distance"
            for drawing in result.drawings
        ), 2)
        contact_drawings = [
            drawing for drawing in result.drawings
            if drawing.get("type") == "contacts_short_item"
        ]
        self.assertTrue(contact_drawings)
        self.assertTrue(all(
            not drawing.get("triggered") for drawing in contact_drawings
        ))

        rendered = DebugOverlay.render_frame(
            np.zeros((720, 1280, 3), dtype=np.uint8),
            role,
            [result],
        )
        self.assertGreater(int(rendered.sum()), 0)

    def test_sloped_reference_from_union_of_all_masks_is_supported(self):
        role = "SPIDER_OUT"
        # Общий верх двух mask следует y = 0.2*x + 84.
        omissions = [
            polygon("omission-short", [
                [80, 100], [160, 116], [160, 170], [80, 154],
            ]),
            polygon("omission-short", [
                [160, 116], [260, 136], [260, 190], [160, 170],
            ]),
        ]
        contacts = [
            rectangle("flatness_short", 100, 207, 30, 30),
            rectangle("flatness_short", 200, 227, 30, 30),
        ]
        result = SpiderContactsShortRule(thresholds(role)).check({
            role: [*contacts, *omissions],
        })
        check = result.details["per_role"][role]["omission_tilt_check"]
        self.assertEqual(check["status"], "ok")
        self.assertGreater(check["valid_points"], 12)
        self.assertAlmostEqual(check["angle_deg"], 11.31, delta=1.0)
        self.assertLess(check["distance_delta_ratio"], 0.20)

    def test_area_filter_applies_even_when_raw_count_is_two(self):
        role = "SPIDER_IN"
        contacts = [
            rectangle("flatness_short", 100, 200, 5, 5),
            rectangle("flatness_short", 180, 200, 30, 30),
        ]
        omission = rectangle("omission-short", 80, 100, 160, 60)
        result = SpiderContactsShortRule(thresholds(role)).check({
            role: [*contacts, omission],
        })
        details = result.details["per_role"][role]
        self.assertTrue(result.triggered)
        self.assertEqual(details["found"], 1)
        self.assertEqual(details["area_absolute_min_px2"], 100)
        self.assertEqual(details["reason"], "wrong_count: 1/2")
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "CONTACTS 1/2"
            for drawing in result.drawings
        ))

    def test_best_pair_is_used_and_extra_contact_is_gray(self):
        role = "SPIDER_IN"
        contacts = [
            rectangle("flatness_short", 100, 200, 30, 30),
            rectangle("flatness_short", 180, 200, 30, 30),
            rectangle("flatness_short", 260, 320, 30, 30),
        ]
        omission = rectangle("omission-short", 80, 100, 260, 60)
        result = SpiderContactsShortRule(thresholds(role)).check({
            role: [*contacts, omission],
        })
        details = result.details["per_role"][role]
        self.assertEqual(details["found"], 2)
        self.assertEqual(details["ignored"], 1)
        self.assertEqual(sum(
            drawing.get("type") == "contacts_short_ignored"
            for drawing in result.drawings
        ), 1)

    def test_masks_are_required(self):
        role = "SPIDER_IN"
        omission = rectangle("omission-short", 80, 100, 160, 60)
        invalid_contacts = [
            rectangle("flatness_short", 100, 200, 30, 30),
            rectangle("flatness_short", 180, 200, 30, 30),
        ]
        invalid_contacts[1] = dict(invalid_contacts[1], mask=None)
        invalid = SpiderContactsShortRule(thresholds(role)).check({
            role: [*invalid_contacts, omission],
        })
        self.assertTrue(invalid.triggered)
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "NO CONTACT MASK #2"
            for drawing in invalid.drawings
        ))

    def test_renderer_has_all_geometry_without_text_or_fill(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/contacts_short.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("cv2.putText", source)
        self.assertNotIn("draw_text_with_bg", source)
        self.assertNotIn("cv2.fillPoly", source)
        self.assertNotIn("sample_points", source)

    def test_missing_omission_line_draws_explicit_fail_closed_error(self):
        role = "SPIDER_IN"
        contacts = [
            rectangle("flatness_short", 100, 200, 30, 30),
            rectangle("flatness_short", 180, 200, 30, 30),
        ]
        result = SpiderContactsShortRule(thresholds(role)).check({role: contacts})
        details = result.details["per_role"][role]
        self.assertTrue(result.triggered)
        self.assertTrue(details["omission_reference_fail"])
        self.assertTrue(any(
            drawing.get("type") == "contacts_short_omission_missing"
            for drawing in result.drawings
        ))
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "NO OMISSION"
            for drawing in result.drawings
        ))


if __name__ == "__main__":
    unittest.main()
