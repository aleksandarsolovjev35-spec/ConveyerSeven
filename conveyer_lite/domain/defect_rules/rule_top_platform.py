from domain.defect_rules.base import BaseRule, RuleResult
from domain.defect_rules.top_geometry import (
    largest_valid_mask,
    mask_orientation,
    try_inscribe_center_then_nearest,
)


class TopPlatformRule(BaseRule):
    name = "top_platform"
    ROLES = ("TOP",)
    PLATFORM_CLASS = "platform"

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        if not self.enabled:
            return self._make_skip(self.name)
        drawings = []
        per_role = {}
        triggered = False
        for role in self.ROLES:
            if role not in vision_results:
                continue
            min_confidence = self._get(
                "top_platform_min_confidence", 0.3, role=role,
            )
            rect_width = self._get(
                "top_platform_inscribed_rect_width_px", 260, role=role,
            )
            rect_height = self._get(
                "top_platform_inscribed_rect_height_px", 120, role=role,
            )
            platforms = [
                detection for detection in vision_results[role]
                if detection.get("class") == self.PLATFORM_CLASS
                and float(detection.get("confidence", 0.0))
                >= min_confidence
            ]
            result = self._check_role(
                role=role,
                platforms=platforms,
                rect_width=float(rect_width),
                rect_height=float(rect_height),
                drawings=drawings,
            )
            per_role[role] = result
            triggered = triggered or result["triggered"]
        return RuleResult(
            self.name,
            triggered,
            details={"per_role": per_role},
            drawings=drawings,
        )

    @staticmethod
    def _check_role(*, role, platforms, rect_width, rect_height, drawings):
        platform = largest_valid_mask(platforms)
        if platform is None:
            drawings.append({
                "type": "construction_error",
                "role": role,
                "message": "NO PLATFORM",
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": "no_valid_platform",
                "found": len(platforms),
                "ignored": 0,
            }
        angle = mask_orientation(platform)
        if angle is None:
            drawings.append({
                "type": "top_platform_actual",
                "role": role,
                "bbox": platform.get("bbox") or [0, 0, 0, 0],
                "mask": platform.get("mask"),
                "valid": False,
                "triggered": True,
            })
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": platform.get("bbox") or [0, 0, 0, 0],
                "message": "NO ORIENTATION",
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": "invalid_platform_orientation",
                "found": len(platforms),
                "ignored": max(0, len(platforms) - 1),
            }
        fit = try_inscribe_center_then_nearest(
            platform,
            width_px=rect_width,
            height_px=rect_height,
            angle_deg=angle,
        )
        triggered = not fit["fits"]
        target_center = fit.get("center") or [0.0, 0.0]
        placed_center = fit.get("placed_center") or target_center
        shift_distance = (
            (float(placed_center[0]) - float(target_center[0])) ** 2
            + (float(placed_center[1]) - float(target_center[1])) ** 2
        ) ** 0.5
        placement = (
            "not_fitted"
            if triggered
            else ("centered" if fit.get("centered") else "shifted")
        )
        drawings.append({
            "type": "top_platform_actual",
            "role": role,
            "bbox": platform.get("bbox") or [0, 0, 0, 0],
            "mask": platform.get("mask"),
            "valid": True,
            "triggered": triggered,
        })
        if fit.get("points") is not None:
            drawings.append({
                "type": "top_platform_inscribed_rect",
                "role": role,
                "points": fit["points"],
                "fits": fit["fits"],
                "triggered": triggered,
            })
        drawings.append({
            "type": "top_platform_centers",
            "role": role,
            "target_center": target_center,
            "placed_center": placed_center,
            "shifted": placement == "shifted",
            "triggered": triggered,
        })
        return {
            "triggered": triggered,
            "reason": None,
            "found": len(platforms),
            "ignored": max(0, len(platforms) - 1),
            "rect_width_px": rect_width,
            "rect_height_px": rect_height,
            "angle_deg": round(angle, 3),
            "fits": fit["fits"],
            "centered": fit.get("centered", False),
            "placement": placement,
            "target_center": [round(float(value), 3) for value in target_center],
            "placed_center": [round(float(value), 3) for value in placed_center],
            "shift_distance_px": round(float(shift_distance), 3),
            "inscribe_fail": triggered,
            "inscribe_check": {
                "status": "fail" if triggered else "ok",
                "rect_width_px": rect_width,
                "rect_height_px": rect_height,
                "expected_width_px": rect_width,
                "expected_height_px": rect_height,
                "fits": fit["fits"],
            },
        }
