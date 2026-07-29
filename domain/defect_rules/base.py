from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class RuleResult:
    rule_name: str
    triggered: bool
    details:  dict = field(default_factory=dict)
    drawings: list = field(default_factory=list)

    @property
    def defect(self) -> str | None:
        return self.rule_name if self.triggered else None

    def __repr__(self):
        status = "FAIL" if self.triggered else "OK"
        return f"<{self.rule_name}: {status} {self.details}>"


class BaseRule:
    """
    Базовый класс правила.
    Потомок реализует метод check().
    """

    name: str = ""

    def __init__(self, thresholds: dict):
        self.thresholds = thresholds
        self._enabled = self.name not in thresholds.get(
            "disabled_rules", []
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        raise NotImplementedError

    def _get(self, key: str, default=None, role: str | None = None):
        """
        Per-role lookup порога с fallback на общий.

        Порядок поиска:
          1. "{ROLE}.{key}" — если role задана и ключ существует
          2. "{key}" — общий порог
          3. default — если ничего не найдено

        Пример:
          thresholds = {
              "spider_contacts_long_inscribed_rect_width_px": 12.0,
              "SPIDER_LEFT.spider_contacts_long_inscribed_rect_width_px": 11.5,
          }

          _get(
              "spider_contacts_long_inscribed_rect_width_px",
              10.0,
              role="SPIDER_LEFT",
          )
          -> 11.5  (per-role)

          _get(
              "spider_contacts_long_inscribed_rect_width_px",
              10.0,
              role="SPIDER_RIGHT",
          )
          -> 12.0  (общий, per-role не задан)
        """
        if role:
            role_key = f"{role}.{key}"
            if role_key in self.thresholds:
                return self.thresholds[role_key]
        return self.thresholds.get(key, default)

    @staticmethod
    def _make_skip(rule_name: str) -> RuleResult:
        """Результат для отключённого правила."""
        return RuleResult(
            rule_name, False,
            details={"skipped": "rule disabled"},
        )