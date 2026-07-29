import unittest
from unittest.mock import patch

from domain.defect_rules.rule_spider_contacts_long import SpiderContactsLongRule
from domain.defect_rules.rule_spider_contacts_short import SpiderContactsShortRule
from domain.defect_rules.rule_top_contacts import TopContactsRule
from domain.defect_rules.rule_top_platform import TopPlatformRule
from domain.defect_rules.rule_top_platform_overlap import TopPlatformOverlapRule


def rect_detection(class_name, x, y, width=20, height=20):
    return {
        "class": class_name,
        "confidence": 0.99,
        "bbox": [x, y, x + width, y + height],
        "mask": [
            [x, y], [x + width, y],
            [x + width, y + height], [x, y + height],
        ],
    }


class InscribedRectangleDimensionTests(unittest.TestCase):
    def test_long_contact_width_and_height_are_used_directly_in_pixels(self):
        role = "SPIDER_LEFT"
        thresholds = {
            f"{role}.spider_contacts_long_min_confidence": 0.3,
            f"{role}.spider_contacts_long_expected_count": 5,
            f"{role}.spider_contacts_long_line_deviation_ratio": 0.35,
            f"{role}.spider_contacts_long_inscribed_rect_width_px": 12.0,
            f"{role}.spider_contacts_long_inscribed_rect_height_px": 6.0,
            f"{role}.spider_contacts_long_y_filter_ratio": 3.0,
        }
        contacts = [
            rect_detection("contacts-long", 100 + index * 30, 200)
            for index in range(5)
        ]
        result = SpiderContactsLongRule(thresholds).check({role: contacts})
        check = result.details["per_role"][role]["inscribe_check"]
        self.assertAlmostEqual(check["rect_width_px"], 12.0)
        self.assertAlmostEqual(check["rect_height_px"], 6.0)
        self.assertEqual(check["status"], "ok")
        self.assertEqual(check["fails"], 0)
        self.assertNotIn("scale_px_per_mm", check)

    def test_short_contact_width_and_height_are_used_directly_in_pixels(self):
        role = "SPIDER_IN"
        thresholds = {
            f"{role}.spider_contacts_short_min_confidence": 0.3,
            f"{role}.spider_contacts_short_expected_count": 2,
            f"{role}.spider_contacts_short_level_deviation_ratio": 0.35,
            f"{role}.spider_contacts_short_inscribed_rect_width_px": 29.0,
            f"{role}.spider_contacts_short_inscribed_rect_height_px": 14.5,
            f"{role}.spider_contacts_short_area_absolute_min": 100,
            f"{role}.spider_contacts_short_y_filter_ratio": 3.0,
        }
        contacts = [
            rect_detection("flatness_short", 100, 200, 30, 30),
            rect_detection("flatness_short", 180, 200, 30, 30),
        ]
        result = SpiderContactsShortRule(thresholds).check({role: contacts})
        check = result.details["per_role"][role]["inscribe_check"]
        self.assertAlmostEqual(check["rect_width_px"], 29.0)
        self.assertAlmostEqual(check["rect_height_px"], 14.5)
        self.assertEqual(check["status"], "ok")
        self.assertEqual(check["fails"], 0)
        self.assertNotIn("scale_px_per_mm", check)

    def test_top_contacts_passes_side_and_edge_pixel_rectangles(self):
        rule = TopContactsRule({
            "TOP.top_contacts_min_confidence": 0.3,
            "TOP.top_contacts_expected_count": 14,
            "TOP.top_contacts_platform_min_confidence": 0.3,
            "TOP.top_contacts_edge_distance_deviation_ratio": 0.4,
            "TOP.top_contacts_side_rect_width_px": 30,
            "TOP.top_contacts_side_rect_height_px": 20,
            "TOP.top_contacts_edge_rect_width_px": 22,
            "TOP.top_contacts_edge_rect_height_px": 32,
        })
        with patch.object(
            rule, "_check_role", return_value={"triggered": False},
        ) as check_role:
            rule.check({"TOP": []})
        kwargs = check_role.call_args.kwargs
        self.assertEqual(kwargs["side_rect"], (30.0, 20.0))
        self.assertEqual(kwargs["edge_rect"], (22.0, 32.0))

    def test_top_platform_passes_editable_pixel_width_and_height(self):
        rule = TopPlatformRule({
            "TOP.top_platform_min_confidence": 0.3,
            "TOP.top_platform_inscribed_rect_width_px": 180,
            "TOP.top_platform_inscribed_rect_height_px": 70,
        })
        with patch.object(
            rule, "_check_role", return_value={"triggered": False},
        ) as check_role:
            rule.check({"TOP": []})
        kwargs = check_role.call_args.kwargs
        self.assertEqual(kwargs["rect_width"], 180.0)
        self.assertEqual(kwargs["rect_height"], 70.0)

    def test_top_platform_overlap_passes_editable_contact_area_settings(self):
        rule = TopPlatformOverlapRule({
            "TOP.top_platform_overlap_platform_min_confidence": 0.3,
            "TOP.top_platform_overlap_excess_component_min_px": 5,
            "TOP.top_platform_overlap_contact_min_confidence": 0.4,
            "TOP.top_platform_overlap_contact_inner_ratio": 0.5,
            "TOP.top_platform_overlap_margin_px": 4,
            "TOP.top_platform_overlap_expand_x_ratio": 1.1,
            "TOP.top_platform_overlap_expand_y_ratio": 0.9,
        })
        with patch.object(
            rule, "_check_role", return_value={"triggered": False},
        ) as check_role:
            rule.check({"TOP": []})
        kwargs = check_role.call_args.kwargs
        self.assertEqual(kwargs["component_min"], 5)
        self.assertEqual(kwargs["contact_inner_ratio"], 0.5)
        self.assertEqual(kwargs["margin_px"], 4.0)
        self.assertEqual(kwargs["expand_x_ratio"], 1.1)
        self.assertEqual(kwargs["expand_y_ratio"], 0.9)
        self.assertNotIn("boundary_width", kwargs)
        self.assertNotIn("inner_width", kwargs)


if __name__ == "__main__":
    unittest.main()
