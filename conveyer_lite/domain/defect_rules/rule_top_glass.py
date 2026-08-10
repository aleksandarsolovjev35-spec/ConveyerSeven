import cv2
import numpy as np

from domain.defect_rules.base import BaseRule, RuleResult
from domain.defect_rules.top_glass_context import build_top_glass_context


class TopGlassRule(BaseRule):
    """Glass на platform/pin/case ring -> CLEANUP; contacts -> BAD rule."""

    name = "glass"
    ROLES = ("TOP",)

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        if not self.enabled:
            return self._make_skip(self.name)
        drawings = []
        per_role = {}
        triggered = False
        for role in self.ROLES:
            if role not in vision_results:
                continue
            context = build_top_glass_context(
                vision_results[role],
                _confidence(self, role),
            )
            result = self._check_role(role, context, drawings)
            per_role[role] = result
            triggered = triggered or result["triggered"]
        return RuleResult(
            self.name,
            triggered,
            details={"per_role": per_role},
            drawings=drawings,
        )

    @staticmethod
    def _check_role(role, context, drawings):
        if not context["has_glass"]:
            return {
                "triggered": False,
                "reason": None,
                "glasses_total": 0,
                "cleanup_hits": 0,
                "hits": [],
            }
        if not context["valid"]:
            # Общий fail closed публикует BAD rule glass_on_contacts.
            return {
                "triggered": False,
                "skipped": True,
                "reason": f"reference_invalid: {context['reason']}",
                "glasses_total": len(context["glasses"]),
                "cleanup_hits": 0,
                "hits": [],
            }

        hits = []
        on_contacts = []
        for index, (glass, glass_raster) in enumerate(
            zip(context["glasses"], context["glass_rasters"], strict=True),
            start=1,
        ):
            contact_overlap = cv2.bitwise_and(
                glass_raster,
                context["contact_union"],
            )
            contact_overlap_px = int(np.count_nonzero(contact_overlap))
            if contact_overlap_px > 0:
                on_contacts.append(index)
                continue

            platform_overlap = cv2.bitwise_and(
                glass_raster,
                context["platform_raster"],
            )
            pin_overlap = cv2.bitwise_and(
                glass_raster,
                context["pin_union"],
            )
            ring_overlap = cv2.bitwise_and(
                glass_raster,
                context["ring_raster"],
            )
            platform_px = int(np.count_nonzero(platform_overlap))
            pin_px = int(np.count_nonzero(pin_overlap))
            ring_px = int(np.count_nonzero(ring_overlap))
            cleanup_union = cv2.bitwise_or(platform_overlap, pin_overlap)
            cleanup_union = cv2.bitwise_or(cleanup_union, ring_overlap)
            cleanup_px = int(np.count_nonzero(cleanup_union))
            if cleanup_px <= 0:
                continue
            hits.append({
                "glass_index": index,
                "platform_overlap_px": platform_px,
                "pin_overlap_px": pin_px,
                "ring_overlap_px": ring_px,
                "cleanup_overlap_px": cleanup_px,
                "route": "CLEANUP",
            })
            contours, _hierarchy = cv2.findContours(
                cleanup_union,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE,
            )
            drawings.append({
                "type": "top_glass_cleanup_region",
                "role": role,
                "glass_index": index,
                "glass_mask": glass.get("mask"),
                "glass_bbox": glass.get("bbox") or [0, 0, 0, 0],
                "cleanup_raster": cleanup_union,
                "cleanup_contours": [
                    contour.reshape(-1, 2).astype(np.int32).tolist()
                    for contour in contours
                    if len(contour) >= 1
                ],
                "triggered": True,
            })

        if hits:
            drawings.insert(0, {
                "type": "top_glass_cleanup_references",
                "role": role,
                "platform_mask": context["platform"].get("mask"),
                "platform_bbox": context["platform"].get("bbox") or [0, 0, 0, 0],
                "pin_masks": [pin.get("mask") for pin in context["pins"]],
                "pin_bboxes": [
                    pin.get("bbox") or [0, 0, 0, 0]
                    for pin in context["pins"]
                ],
                "case_mask": context["case"].get("mask"),
                "case_bbox": context["case"].get("bbox") or [0, 0, 0, 0],
                "central_mask": context["central"].get("mask"),
                "central_bbox": context["central"].get("bbox") or [0, 0, 0, 0],
                "triggered": True,
            })

        return {
            "triggered": bool(hits),
            "reason": None,
            "glasses_total": len(context["glasses"]),
            "cleanup_hits": len(hits),
            "hits": hits,
            "on_contacts_indices": on_contacts,
        }


def _confidence(rule, role):
    return {
        "glass": rule._get("top_glass_min_confidence", 0.1, role=role),
        "platform": rule._get(
            "top_glass_platform_min_confidence", 0.3, role=role,
        ),
        "contacts": rule._get(
            "top_contacts_min_confidence", 0.3, role=role,
        ),
        "case": rule._get("top_glass_case_min_confidence", 0.3, role=role),
        "central": rule._get(
            "top_glass_case_central_min_confidence", 0.3, role=role,
        ),
        "pin": rule._get("top_glass_pin_min_confidence", 0.3, role=role),
    }
