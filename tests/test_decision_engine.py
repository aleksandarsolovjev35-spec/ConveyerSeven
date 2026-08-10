"""Тесты DecisionEngine: создание из thresholds.json и выполнение правил."""

import unittest

from core.decision_engine import DecisionEngine
from domain.defect_rules import InputPartPresenceRule


class DecisionEngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine()

    def test_engine_created_with_rules(self):
        self.assertTrue(self.engine.rules)
        names = {rule.name for rule in self.engine.rules}
        self.assertIn("window_geometry", names)
        self.assertIn("contacts_long", names)

    def test_empty_vision_returns_empty(self):
        self.assertEqual(self.engine.evaluate_all_detailed({}), [])

    def test_rules_for_role(self):
        input_rules = self.engine.rules_for_role("INPUT_LEFT")
        self.assertTrue(input_rules)
        self.assertTrue(all(
            "INPUT_LEFT" in getattr(rule, "ROLES", ())
            for rule in input_rules
        ))


if __name__ == "__main__":
    unittest.main()
