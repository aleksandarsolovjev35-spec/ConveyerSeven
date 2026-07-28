import cv2
import numpy as np

from domain.defect_rules.base import BaseRule, RuleResult
from domain.defect_rules.top_geometry import mask_points
from domain.defect_rules.top_glass_context import build_top_glass_context


class TopGlassOnContactsRule(BaseRule):
    """Glass на selected contacts или invalid общий context -> BAD."""

    name = "glass_on_contacts"
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

    @classmethod
    def _check_role(cls, role, context, drawings):
        if not context["has_glass"]:
            return {
                "triggered": False,
                "reason": None,
                "glasses_total": 0,
                "hits": 0,
                "pairs": [],
            }
        if not context["valid"]:
            cls._draw_context_error(role, context, drawings)
            return {
                "triggered": True,
                "reason": context["reason"],
                "glasses_total": len(context["glasses"]),
                "hits": 0,
                "pairs": [],
                "reference_fail": True,
                **{
                    key: value
                    for key, value in context.items()
                    if key in (
                        "invalid_glass_indices", "valid_contacts",
                        "contact_group_counts", "pins_found",
                        "invalid_pin_indices", "case_found",
                        "case_central_found",
                    )
                },
            }

        pairs = []
        hit_glasses = set()
        for glass_index, (glass, glass_raster) in enumerate(
            zip(context["glasses"], context["glass_rasters"], strict=True),
            start=1,
        ):
            for contact_index, (contact, contact_raster) in enumerate(
                zip(
                    context["contacts"],
                    context["contact_rasters"],
                    strict=True,
                ),
                start=1,
            ):
                overlap = cv2.bitwise_and(glass_raster, contact_raster)
                overlap_px = int(np.count_nonzero(overlap))
                if overlap_px <= 0:
                    continue
                hit_glasses.add(glass_index)
                pairs.append({
                    "glass_index": glass_index,
                    "contact_index": contact_index,
                    "overlap_pixels": overlap_px,
                    "route": "BAD",
                })
                contours, _hierarchy = cv2.findContours(
                    overlap,
                    cv2.RETR_EXTERNAL,
                    cv2.CHAIN_APPROX_SIMPLE,
                )
                drawings.append({
                    "type": "top_glass_contact_overlap",
                    "role": role,
                    "glass_index": glass_index,
                    "contact_index": contact_index,
                    "glass_mask": glass.get("mask"),
                    "glass_bbox": glass.get("bbox") or [0, 0, 0, 0],
                    "contact_mask": contact.get("mask"),
                    "contact_bbox": contact.get("bbox") or [0, 0, 0, 0],
                    "overlap_raster": overlap,
                    "overlap_contours": [
                        contour.reshape(-1, 2).astype(np.int32).tolist()
                        for contour in contours
                        if len(contour) >= 1
                    ],
                    "triggered": True,
                })

        if pairs:
            drawings.insert(0, {
                "type": "top_glass_bad_references",
                "role": role,
                "contact_masks": [
                    contact.get("mask") for contact in context["contacts"]
                ],
                "contact_bboxes": [
                    contact.get("bbox") or [0, 0, 0, 0]
                    for contact in context["contacts"]
                ],
                "triggered": True,
            })

        return {
            "triggered": bool(pairs),
            "reason": None,
            "reference_fail": False,
            "glasses_total": len(context["glasses"]),
            "hits": len(hit_glasses),
            "hit_glass_indices": sorted(hit_glasses),
            "pairs": pairs,
        }

    @staticmethod
    def _draw_context_error(role, context, drawings):
        invalid_indices = set(context.get("invalid_glass_indices") or [])
        for index, glass in enumerate(context.get("glasses") or [], start=1):
            valid = mask_points(glass) is not None
            drawings.append({
                "type": "top_glass_bad_glass",
                "role": role,
                "bbox": glass.get("bbox") or [0, 0, 0, 0],
                "mask": glass.get("mask"),
                "valid": valid,
                "triggered": True,
            })
            if index in invalid_indices:
                drawings.append({
                    "type": "construction_error",
                    "role": role,
                    "bbox": glass.get("bbox") or [0, 0, 0, 0],
                    "message": f"NO GLASS MASK #{index}",
                    "triggered": True,
                })
        message = _context_error_message(context)
        if not invalid_indices or context.get("reason") != "missing_glass_mask":
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": _combined_bbox(context.get("glasses") or []),
                "message": message,
                "slot": 1 if invalid_indices else 0,
                "triggered": True,
            })


def _context_error_message(context):
    reason = str(context.get("reason") or "")
    if reason == "missing_glass_mask":
        return "NO GLASS MASK"
    if reason == "no_valid_platform":
        return "NO PLATFORM"
    if reason == "invalid_platform_bbox":
        return "NO PLATFORM BBOX"
    if reason == "insufficient_valid_contacts":
        return f"CONTACTS REF {int(context.get('valid_contacts') or 0)}/14"
    if reason == "invalid_contact_layout":
        counts = context.get("contact_group_counts") or {}
        text = " ".join(
            f"{group}{int(counts.get(group) or 0)}/{expected}"
            for group, expected in (("L", 5), ("R", 5), ("T", 2), ("B", 2))
        )
        return f"CONTACTS REF {text}"
    if reason.startswith("wrong_pin_count"):
        return f"PINS {int(context.get('pins_found') or 0)}/14"
    if reason == "missing_pin_mask":
        indices = context.get("invalid_pin_indices") or []
        return "NO PIN MASK " + ",".join(f"#{index}" for index in indices)
    if reason.startswith("invalid_case_count"):
        return f"CASE {int(context.get('case_found') or 0)}/1"
    if reason.startswith("invalid_case_central_count"):
        return f"CASE CENTRAL {int(context.get('case_central_found') or 0)}/1"
    if reason == "case_central_not_inside_case":
        return "INVALID CASE RING"
    if reason == "empty_case_ring":
        return "EMPTY CASE RING"
    return "GLASS CONTEXT INVALID"


def _combined_bbox(detections):
    boxes = [detection.get("bbox") for detection in detections]
    boxes = [box for box in boxes if box and len(box) == 4]
    if not boxes:
        return [0, 0, 0, 0]
    return [
        min(float(box[0]) for box in boxes),
        min(float(box[1]) for box in boxes),
        max(float(box[2]) for box in boxes),
        max(float(box[3]) for box in boxes),
    ]


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
