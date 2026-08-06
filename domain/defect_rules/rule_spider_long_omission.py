from domain.defect_rules.base import BaseRule
from domain.defect_rules.omission_boundary import OmissionBoundaryMixin


class SpiderLongOmissionRule(OmissionBoundaryMixin, BaseRule):
    """Выход long omission ниже разрешённой полосы от верхней линии."""

    name = "long_omission"
    ROLES = ("SPIDER_LEFT", "SPIDER_RIGHT")
    TARGET_CLASS = "omission-long"
    FAMILY = "long"
    DRAWING_TYPE = "long_omission_item"
