import math

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

    def __init__(
        self,
        thresholds: dict | None = None,
        *,
        rule_worker_runner=None,
        rule_timeout: float = 5.0,
    ):
        if thresholds is None:
            thresholds = ThresholdLoader().get_all()
        if rule_timeout <= 0:
            raise ValueError("rule_timeout must be positive")
        self.rule_worker_runner = rule_worker_runner
        self.rule_timeout = float(rule_timeout)

        # The production ruleset is boot-gated by a complete threshold
        # document.  This prevents a direct caller from silently substituting
        # partial/default calibration.
        ThresholdLoader.validate(thresholds)
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

        # The production set is fixed in code.  Operators may edit only
        # numeric thresholds, never rule membership.
        self.rules: list[BaseRule] = list(all_rules)
        if not self.rules:
            raise RuntimeError("No active defect rules; production inspection blocked")
        print(f"[RULES] Active: {[r.name for r in self.rules]}")

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

    def _run_rule(self, rule, vision_results, frames=None):
        kwargs = {}
        if frames:
            kwargs["frames"] = frames
        if self.rule_worker_runner is None:
            result = rule.check(vision_results, **kwargs)
        else:
            result = self.rule_worker_runner(
                rule.check,
                vision_results,
                timeout=self.rule_timeout,
                **kwargs,
            )
        self._validate_result(result, rule.name)
        return result

    @staticmethod
    def _validate_result(result, rule_name: str):
        """Reject malformed rule evidence instead of treating it as a defect."""
        if result is None:
            raise RuntimeError(f"{rule_name}: rule returned no result")
        returned_name = getattr(result, "rule_name", None)
        if not isinstance(returned_name, str) or not returned_name:
            raise RuntimeError(f"{rule_name}: result has no rule_name")
        if returned_name != rule_name:
            raise RuntimeError(f"{rule_name}: result rule_name={returned_name!r}")
        if not isinstance(getattr(result, "triggered", None), bool):
            raise RuntimeError(f"{rule_name}: triggered is not bool")
        details = getattr(result, "details", None)
        drawings = getattr(result, "drawings", None)
        if not isinstance(details, dict) or not isinstance(drawings, list):
            raise RuntimeError(f"{rule_name}: malformed result structure")

        def walk(value, path="details"):
            if value is None or isinstance(value, (bool, str, int)):
                return
            if isinstance(value, float):
                if not math.isfinite(value):
                    raise RuntimeError(f"{rule_name}: non-finite {path}")
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    walk(item, f"{path}.{key}")
                return
            if isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    walk(item, f"{path}[{index}]")
                return
            tolist = getattr(value, "tolist", None)
            if callable(tolist):
                walk(tolist(), path)
                return
            item = getattr(value, "item", None)
            if callable(item):
                walk(item(), path)
                return
            raise RuntimeError(f"{rule_name}: malformed {path}")

        walk(details)
        walk(drawings, "drawings")
