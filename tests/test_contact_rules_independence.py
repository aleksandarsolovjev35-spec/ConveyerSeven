import unittest
from copy import deepcopy
from pathlib import Path

from domain.defect_rules.rule_spider_contacts_long import SpiderContactsLongRule
from domain.defect_rules.rule_spider_contacts_short import SpiderContactsShortRule


def rectangle_detection(class_name, x, y, width, height, confidence=0.9):
    return {
        "class": class_name,
        "confidence": confidence,
        "bbox": [x, y, x + width, y + height],
        "mask": [
            [x, y],
            [x + width, y],
            [x + width, y + height],
            [x, y + height],
        ],
    }


class ContactRuleIndependenceTests(unittest.TestCase):
    def test_short_contacts_require_valid_omission_reference_fail_closed(self):
        role = "SPIDER_IN"
        thresholds = {
            f"{role}.spider_contacts_short_min_confidence": 0.3,
            f"{role}.spider_contacts_short_expected_count": 2,
            f"{role}.spider_contacts_short_level_deviation_ratio": 0.35,
            f"{role}.spider_contacts_short_omission_tilt_ratio_max": 0.20,
            f"{role}.spider_contacts_short_inscribed_rect_width_px": 25.2,
            f"{role}.spider_contacts_short_inscribed_rect_height_px": 9.6,
            f"{role}.spider_contacts_short_area_absolute_min": 400,
            f"{role}.spider_contacts_short_y_filter_ratio": 3.0,
            f"{role}.spider_short_omission_min_confidence": 0.3,
        }
        contacts = [
            rectangle_detection("flatness_short", 100, 200, 30, 30),
            rectangle_detection("flatness_short", 180, 200, 30, 30),
        ]
        omission = rectangle_detection("omission-short", 80, 100, 160, 60)

        rule = SpiderContactsShortRule(thresholds)
        without_omission = rule.check({role: deepcopy(contacts)})
        with_omission = rule.check({role: deepcopy(contacts + [omission])})

        missing = without_omission.details["per_role"][role]
        present = with_omission.details["per_role"][role]
        self.assertTrue(without_omission.triggered)
        self.assertTrue(missing["omission_reference_fail"])
        self.assertEqual(
            missing["omission_tilt_check"]["reason"],
            "no_valid_omission_top_line",
        )
        self.assertFalse(present["omission_reference_fail"])
        self.assertFalse(present["omission_tilt_fail"])
        self.assertFalse(with_omission.triggered)

    def test_long_contacts_require_valid_omission_reference_fail_closed(self):
        role = "SPIDER_LEFT"
        thresholds = {
            f"{role}.spider_contacts_long_min_confidence": 0.3,
            f"{role}.spider_contacts_long_expected_count": 5,
            f"{role}.spider_contacts_long_line_deviation_ratio": 0.35,
            f"{role}.spider_contacts_long_omission_tilt_ratio_max": 0.20,
            f"{role}.spider_contacts_long_inscribed_rect_width_px": 11.5,
            f"{role}.spider_contacts_long_inscribed_rect_height_px": 8.6,
            f"{role}.spider_contacts_long_y_filter_ratio": 3.0,
            f"{role}.spider_long_omission_min_confidence": 0.3,
        }
        contacts = [
            rectangle_detection("contacts-long", 100 + index * 30, 200, 20, 20)
            for index in range(5)
        ]
        omission = rectangle_detection("omission-long", 80, 100, 180, 60)

        rule = SpiderContactsLongRule(thresholds)
        without_omission = rule.check({role: deepcopy(contacts)})
        with_omission = rule.check({role: deepcopy(contacts + [omission])})

        missing = without_omission.details["per_role"][role]
        present = with_omission.details["per_role"][role]
        self.assertTrue(without_omission.triggered)
        self.assertTrue(missing["omission_reference_fail"])
        self.assertFalse(with_omission.triggered)
        self.assertFalse(present["omission_tilt_fail"])

    def test_both_contact_rules_have_their_own_omission_reference_contract(self):
        root = Path(__file__).resolve().parents[1]
        long_source = (
            root / "domain/defect_rules/rule_spider_contacts_long.py"
        ).read_text(encoding="utf-8").lower()
        short_source = (
            root / "domain/defect_rules/rule_spider_contacts_short.py"
        ).read_text(encoding="utf-8").lower()
        self.assertIn("omission-long", long_source)
        self.assertIn("omission_tilt_ratio_max", long_source)
        self.assertIn("omission-short", short_source)
        self.assertIn("omission_tilt_ratio_max", short_source)


if __name__ == "__main__":
    unittest.main()
