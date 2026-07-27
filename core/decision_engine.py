# core/decision_engine.py

from domain.threshold_loader import ThresholdLoader
from domain.defect_rules import (
    BaseRule,
    InputWindowGeometryRule,
    InputWindowSinksRule,
    SpiderContactsLongRule,
    SpiderLongOmissionRule,
    SpiderContactsShortRule,
    SpiderShortOmissionRule,
    TopContactsRule,
    TopPlatformRule,
    TopSinksRule,
    TopGlassRule,
    TopGlassOnContactsRule,
    TopPlatformOverlapRule,
)


class DecisionEngine:
    """
    Оркестратор правил.
    """

    def __init__(self, thresholds: dict = None):
        if thresholds is None:
            thresholds = ThresholdLoader().get_all()

        # Сохраняем thresholds для доступа снаружи
        # (нужно например Inspector'у для создания служебных правил
        # вроде InputPartPresenceRule без пересоздания загрузчика).
        self.thresholds = thresholds

        all_rules: list[BaseRule] = [
            InputWindowGeometryRule(thresholds),
            InputWindowSinksRule(thresholds),

            SpiderContactsLongRule(thresholds),
            SpiderLongOmissionRule(thresholds),

            SpiderContactsShortRule(thresholds),
            SpiderShortOmissionRule(thresholds),

            TopContactsRule(thresholds),
            TopPlatformRule(thresholds),
            TopSinksRule(thresholds),
            TopGlassRule(thresholds),
            TopGlassOnContactsRule(thresholds),
            TopPlatformOverlapRule(thresholds),
        ]

        self.rules: list[BaseRule] = [
            r for r in all_rules if r.enabled
        ]

        disabled = [r.name for r in all_rules if not r.enabled]
        if disabled:
            print(f"[RULES] Disabled: {disabled}")
        if not self.rules:
            raise RuntimeError("No active defect rules; production inspection blocked")
        print(f"[RULES] Active: {[r.name for r in self.rules]}")

    def evaluate_all(self, vision_results, frames=None):
        if not vision_results:
            return []

        defects = []
        for rule in self.rules:
            result = self._run_rule(rule, vision_results, frames)
            if result.triggered:
                defect_name = result.rule_name
                if defect_name not in defects:
                    defects.append(defect_name)

        return defects

    def evaluate_all_detailed(self, vision_results, frames=None):
        return self.evaluate_rules_detailed(
            self.rules,
            vision_results,
            frames=frames,
        )

    def rules_for_role(self, role: str):
        """Активные правила, в которых участвует выбранная камера."""
        return [
            rule
            for rule in self.rules
            if role in tuple(getattr(rule, "ROLES", ()))
        ]

    def evaluate_rules_detailed(self, rules, vision_results, frames=None):
        """Выполнить только явно выбранный набор правил."""
        if not vision_results:
            return []
        return [
            self._run_rule(rule, vision_results, frames)
            for rule in rules
        ]

    @staticmethod
    def _run_rule(rule, vision_results, frames=None):
        kwargs = {}
        if frames:
            kwargs["frames"] = frames
        return rule.check(vision_results, **kwargs)