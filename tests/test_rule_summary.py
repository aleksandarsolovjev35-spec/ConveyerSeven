"""Тесты core.rule_summary: карточки замеров правил (одиночный прогон)."""

import unittest

from core.rule_summary import build_presence_summary, build_rule_summary


class RuleSummaryTest(unittest.TestCase):
    def test_presence_summary_present(self):
        details = {
            "false_positive_max_count_by_role": {
                "INPUT_LEFT": 2, "INPUT_RIGHT": 2,
            },
            "flatness_left": 5,
            "flatness_right": 6,
            "effective_flatness_left": 5,
            "effective_flatness_right": 6,
        }
        cards = build_presence_summary(details)
        self.assertEqual(len(cards), 2)
        for card in cards:
            self.assertTrue(card["ok"])
            self.assertEqual(card["verdict"], "корпус виден")

    def test_presence_summary_empty(self):
        details = {
            "false_positive_max_count_by_role": {
                "INPUT_LEFT": 2, "INPUT_RIGHT": 2,
            },
            "flatness_left": 0,
            "flatness_right": 0,
            "effective_flatness_left": 0,
            "effective_flatness_right": 0,
        }
        cards = build_presence_summary(details)
        self.assertEqual(len(cards), 2)
        for card in cards:
            self.assertFalse(card["ok"])
            self.assertEqual(card["verdict"], "корпус не виден")

    def test_rule_summary_cards_sorted_bad_first(self):
        details = {
            "per_role": {
                "INPUT_LEFT": {"valid": True, "triggered": False},
                "INPUT_RIGHT": {"valid": True, "triggered": True},
            },
        }
        cards = build_rule_summary("window_geometry", details)
        self.assertEqual(len(cards), 2)
        self.assertFalse(cards[0]["ok"])
        self.assertTrue(cards[1]["ok"])


if __name__ == "__main__":
    unittest.main()
