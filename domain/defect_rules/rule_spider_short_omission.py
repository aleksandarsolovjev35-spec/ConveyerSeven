from domain.defect_rules.base import BaseRule
from domain.defect_rules.omission_boundary import OmissionBoundaryMixin


class SpiderShortOmissionRule(OmissionBoundaryMixin, BaseRule):
    """Выход short omission ниже разрешённой полосы от верхней линии."""

    name = "short_omission"
    ROLES = ("SPIDER_IN", "SPIDER_OUT")
    TARGET_CLASS = "omission-short"
    FAMILY = "short"
    DRAWING_TYPE = "short_omission_item"
