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
            self.assertIn(card["role"], ("INPUT_LEFT", "INPUT_RIGHT"))

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

    def test_presence_summary_skips_missing_role(self):
        details = {
            "false_positive_max_count_by_role": {"INPUT_LEFT": 2},
            "flatness_left": 5,
            # flatness_right отсутствует — карточка для него не строится
        }
        cards = build_presence_summary(details)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["role"], "INPUT_LEFT")

    def test_rule_summary_empty_details(self):
        self.assertEqual(build_rule_summary("window_geometry", {}), [])
        self.assertEqual(build_rule_summary("window_geometry",
                                            {"per_role": {}}), [])

    def test_rule_summary_cards_sorted_bad_first(self):
        details = {
            "per_role": {
                "INPUT_LEFT": {"valid": True, "triggered": False},
                "INPUT_RIGHT": {"valid": True, "triggered": True},
            },
        }
        cards = build_rule_summary("window_geometry", details)
        self.assertEqual(len(cards), 2)
        # Сначала карточки с ok=False (сработавшие)
        self.assertFalse(cards[0]["ok"])
        self.assertTrue(cards[1]["ok"])

    def test_rule_summary_metric_values(self):
        details = {
            "per_role": {
                "SPIDER_LEFT": {
                    "valid": True,
                    "triggered": False,
                    "excess_pixels": 40,
                    "excess_component_min_px": 30,
                },
            },
        }
        cards = build_rule_summary("long_omission", details)
        self.assertEqual(len(cards), 1)
        metrics = cards[0]["metrics"]
        self.assertTrue(metrics)  # хотя бы одна метрика с числовым значением
        # Основная пара: избыток vs порог
        main = next(
            (m for m in metrics if m.get("key") == "excess_component_min_px"),
            None,
        )
        self.assertIsNotNone(main)
        self.assertEqual(main["value"], "40 px")
        self.assertEqual(main["limit"], "30 px")
        self.assertTrue(main["ok"])


if __name__ == "__main__":
    unittest.main()
