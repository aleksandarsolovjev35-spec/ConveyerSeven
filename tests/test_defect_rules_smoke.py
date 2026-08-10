"""Smoke-тесты всех defect rules: не падают на пустых и минимальных данных."""

import unittest

import numpy as np

from core.decision_engine import DecisionEngine
from domain.defect_rules.base import RuleResult


def _empty_vision(roles):
    return {role: [] for role in roles}


class DefectRulesSmokeTest(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine()

    def test_all_rules_return_rule_result_on_empty_detections(self):
        for rule in self.engine.rules:
            roles = tuple(getattr(rule, "ROLES", ()))
            if not roles:
                continue
            with self.subTest(rule=rule.name):
                result = rule.check(_empty_vision(roles))
                self.assertIsInstance(result, RuleResult, rule.name)
                self.assertEqual(result.rule_name, rule.name)
                self.assertIsInstance(result.triggered, bool)
                self.assertIsInstance(result.details, dict)

    def test_structure_rules_are_fail_closed_on_empty(self):
        roles = ("INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
                 "SPIDER_IN", "SPIDER_OUT", "TOP")
        results = self.engine.evaluate_all_detailed(_empty_vision(roles))
        by_name = {r.rule_name: r for r in results}
        fail_closed = {
            "window_geometry", "contacts_long", "long_omission",
            "contacts_short", "short_omission", "top_contacts",
            "top_platform", "platform_contacts_overlap",
        }
        for name in fail_closed:
            with self.subTest(rule=name):
                self.assertTrue(
                    by_name[name].triggered,
                    f"{name}: на пустом входе должно сработать (fail-closed)",
                )


if __name__ == "__main__":
    unittest.main()
