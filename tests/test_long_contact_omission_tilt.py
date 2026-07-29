import unittest
from pathlib import Path

import numpy as np

from domain.defect_rules.rule_spider_contacts_long import SpiderContactsLongRule
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


def thresholds(role="SPIDER_LEFT", tilt_limit=0.20):
    return {
        f"{role}.spider_contacts_long_min_confidence": 0.3,
        f"{role}.spider_contacts_long_expected_count": 5,
        f"{role}.spider_contacts_long_line_deviation_ratio": 0.35,
        f"{role}.spider_contacts_long_omission_tilt_ratio_max": tilt_limit,
        f"{role}.spider_contacts_long_inscribed_rect_width_px": 9.6,
        f"{role}.spider_contacts_long_inscribed_rect_height_px": 7.2,
        f"{role}.spider_contacts_long_y_filter_ratio": 3.0,
        f"{role}.spider_long_omission_min_confidence": 0.3,
    }


def contact_row(y_values):
    return [
        rectangle("contacts-long", 100 + index * 30, y, 20, 20)
        for index, y in enumerate(y_values)
    ]


class LongContactOmissionTiltTests(unittest.TestCase):
    def test_parallel_row_has_constant_distances_and_passes(self):
        role = "SPIDER_LEFT"
        contacts = contact_row([200, 200, 200, 200, 200])
        omission = rectangle("omission-long", 80, 100, 180, 60)
        result = SpiderContactsLongRule(thresholds(role)).check({
            role: [*contacts, omission],
        })
        details = result.details["per_role"][role]
        check = details["omission_tilt_check"]
        self.assertEqual(check["status"], "ok")
        self.assertAlmostEqual(check["distance_trend_ratio"], 0.0, places=3)
        self.assertFalse(result.triggered)

    def test_collective_linear_tilt_is_caught_when_old_line_check_passes(self):
        role = "SPIDER_LEFT"
        # Все пять контактов лежат на идеальной прямой: старая проверка
        # collinearity проходит, но ряд наклонён к горизонтальному omission.
        contacts = contact_row([200, 205, 210, 215, 220])
        omission = rectangle("omission-long", 80, 100, 180, 60)
        result = SpiderContactsLongRule(thresholds(role)).check({
            role: [*contacts, omission],
        })
        details = result.details["per_role"][role]
        check = details["omission_tilt_check"]
        self.assertFalse(details["line_fail"])
        self.assertTrue(details["omission_tilt_fail"])
        self.assertTrue(result.triggered)
        self.assertGreater(check["distance_trend_ratio"], 0.20)
        self.assertAlmostEqual(check["distance_trend_delta_px"], 20.0, delta=1.0)
        self.assertEqual(len(check["contacts"]), 5)

        self.assertTrue(any(
            drawing.get("type") == "contacts_long_omission_line"
            for drawing in result.drawings
        ))
        self.assertEqual(sum(
            drawing.get("type") == "contacts_long_omission_distance"
            for drawing in result.drawings
        ), 5)
        contact_drawings = [
            drawing for drawing in result.drawings
            if drawing.get("type") == "contacts_long_item"
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

    def test_sloped_union_reference_and_parallel_contact_row_pass(self):
        role = "SPIDER_RIGHT"
        omissions = [
            polygon("omission-long", [
                [80, 100], [170, 118], [170, 170], [80, 152],
            ]),
            polygon("omission-long", [
                [170, 118], [270, 138], [270, 190], [170, 170],
            ]),
        ]
        # Верх контактов параллелен y = 0.2*x + 84 с постоянным отступом.
        contacts = [
            rectangle(
                "contacts-long",
                100 + index * 30,
                int(round(0.2 * (110 + index * 30) + 184)),
                20,
                20,
            )
            for index in range(5)
        ]
        result = SpiderContactsLongRule(thresholds(role)).check({
            role: [*contacts, *omissions],
        })
        details = result.details["per_role"][role]
        check = details["omission_tilt_check"]
        self.assertEqual(check["status"], "ok")
        self.assertGreater(check["valid_points"], 12)
        self.assertAlmostEqual(check["angle_deg"], 11.31, delta=1.0)
        self.assertLess(check["distance_trend_ratio"], 0.20)

    def test_best_five_masks_are_used_and_extra_is_gray(self):
        role = "SPIDER_LEFT"
        contacts = [
            rectangle("contacts-long", 100 + index * 30, 200, 20, 20)
            for index in range(6)
        ]
        omission = rectangle("omission-long", 80, 100, 220, 60)
        result = SpiderContactsLongRule(thresholds(role)).check({
            role: [*contacts, omission],
        })
        details = result.details["per_role"][role]
        self.assertEqual(details["found"], 5)
        self.assertEqual(details["ignored"], 1)
        self.assertEqual(sum(
            drawing.get("type") == "contacts_long_ignored"
            for drawing in result.drawings
        ), 1)

    def test_contact_masks_are_required_and_count_has_no_text_banner(self):
        role = "SPIDER_LEFT"
        contacts = contact_row([200, 200, 200, 200, 200])
        contacts[2] = dict(contacts[2], mask=None)
        omission = rectangle("omission-long", 80, 100, 180, 60)
        invalid = SpiderContactsLongRule(thresholds(role)).check({
            role: [*contacts, omission],
        })
        details = invalid.details["per_role"][role]
        self.assertTrue(invalid.triggered)
        self.assertEqual(details["reason"], "invalid_contact_masks")
        self.assertEqual(details["invalid_mask_indices"], [3])
        self.assertTrue(any(
            drawing.get("type") == "contacts_long_invalid_mask"
            for drawing in invalid.drawings
        ))
        invalid_error = next(
            drawing for drawing in invalid.drawings
            if drawing.get("type") == "construction_error"
        )
        self.assertEqual(invalid_error["message"], "NO CONTACT MASK #3")

        wrong_count = SpiderContactsLongRule(thresholds(role)).check({
            role: [*contact_row([200, 200, 200, 200]), omission],
        })
        self.assertTrue(wrong_count.triggered)
        self.assertEqual(sum(
            drawing.get("type") == "contacts_long_count_item"
            for drawing in wrong_count.drawings
        ), 4)
        self.assertFalse(any(
            drawing.get("type") == "stats_panel_entry"
            for drawing in wrong_count.drawings
        ))
        count_error = next(
            drawing for drawing in wrong_count.drawings
            if drawing.get("type") == "construction_error"
        )
        self.assertEqual(count_error["message"], "CONTACTS 4/5")

    def test_renderer_has_all_geometry_without_text_or_mask_fill(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/contacts_long.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("cv2.putText", source)
        self.assertNotIn("draw_text_with_bg", source)
        self.assertNotIn("cv2.fillPoly", source)

    def test_missing_reference_is_fail_closed(self):
        role = "SPIDER_LEFT"
        result = SpiderContactsLongRule(thresholds(role)).check({
            role: contact_row([200, 200, 200, 200, 200]),
        })
        details = result.details["per_role"][role]
        self.assertTrue(result.triggered)
        self.assertTrue(details["omission_reference_fail"])
        self.assertTrue(any(
            drawing.get("type") == "contacts_long_omission_missing"
            for drawing in result.drawings
        ))
        error = next(
            drawing for drawing in result.drawings
            if drawing.get("type") == "construction_error"
            and drawing.get("message") == "NO OMISSION"
        )
        self.assertEqual(error["message"], "NO OMISSION")


if __name__ == "__main__":
    unittest.main()
