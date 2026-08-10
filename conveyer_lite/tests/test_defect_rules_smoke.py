"""Smoke-тесты всех defect rules: не падают на пустых и минимальных данных.

Правила дефектов получают детекции моделей. На пустом лотке (или при
отсутствии детекций нужного класса) правило не должно бросать исключение:
оно обязано вернуть RuleResult — обычно fail-closed (сработало/область не
построена) или skip с объяснением. Тест гоняет каждое активное правило
с пустыми vision_results по его ролям и проверяет форму результата.
"""

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

    def test_evaluate_all_detailed_with_empty_vision(self):
        # Правила выполняются по пустым детекциям всех семи камер
        roles = ("INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
                 "SPIDER_IN", "SPIDER_OUT", "TOP")
        results = self.engine.evaluate_all_detailed(_empty_vision(roles))
        self.assertEqual(len(results), len(self.engine.rules))
        for result in results:
            self.assertIsInstance(result, RuleResult)
            self.assertIsInstance(result.triggered, bool)

    def test_structure_rules_are_fail_closed_on_empty(self):
        """Правила построения геометрии обязаны быть fail-closed: при
        отсутствии детекций (пустой лоток) они срабатывают."""
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

    def test_presence_rules_are_normal_without_defect_object(self):
        """Правила проверки НАЛИЧИЯ объекта-дефекта (стекло/раковины) дают
        'норма', если объекта нет: нет стекла -> нет дефекта. Это осознанная
        семантика: пустой лоток на входе ловится part_presence раньше, а
        отсутствие окон/платформы уже отмечено fail-closed родителем."""
        roles = ("INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
                 "SPIDER_IN", "SPIDER_OUT", "TOP")
        results = self.engine.evaluate_all_detailed(_empty_vision(roles))
        by_name = {r.rule_name: r for r in results}

        for name in ("window_sinks", "sinks", "glass", "glass_on_contacts"):
            with self.subTest(rule=name):
                self.assertFalse(by_name[name].triggered, name)

    def test_rule_report_of_empty_detections(self):
        """Строки отчёта для пустых детекций строятся без падений."""
        from core.rule_report import build_rule_report_rows

        roles = ("INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
                 "SPIDER_IN", "SPIDER_OUT", "TOP")
        results = self.engine.evaluate_all_detailed(_empty_vision(roles))
        rows = build_rule_report_rows(results)
        self.assertEqual(len(rows), len(results))
        for row in rows:
            self.assertIn("name", row)
            self.assertIn("triggered", row)
            self.assertIn("detail", row)
            self.assertIn("run_cards", row)

    def test_frames_kwarg_accepted(self):
        """Правила принимают frames=... (используется для построения
        reference-геометрии из кадра)."""
        roles = ("INPUT_LEFT", "INPUT_RIGHT")
        frames = {role: np.zeros((240, 320, 3), dtype=np.uint8)
                  for role in roles}
        results = self.engine.evaluate_all_detailed(
            _empty_vision(roles), frames=frames,
        )
        self.assertEqual(len(results), len(self.engine.rules))
        for result in results:
            self.assertIsInstance(result, RuleResult)


if __name__ == "__main__":
    unittest.main()
