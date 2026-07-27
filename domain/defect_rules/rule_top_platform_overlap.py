# domain/defect_rules/rule_top_platform_overlap.py

import cv2
import numpy as np

from domain.defect_rules.base import BaseRule, RuleResult
from domain.defect_rules.top_geometry import (
    infer_shape,
    largest_valid_mask,
    mask_orientation,
    oriented_rectangle_points,
    rasterize_mask,
    try_inscribe_center_then_nearest,
)


class TopPlatformOverlapRule(BaseRule):
    """Контроль заплыва platform mask за настраиваемую внешнюю границу."""

    name = "platform_contacts_overlap"
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
                "top_platform_overlap_platform_min_confidence", 0.3,
                role=role,
            )
            inner_width = self._get(
                "top_platform_inscribed_rect_width_px", 260,
                role=role,
            )
            inner_height = self._get(
                "top_platform_inscribed_rect_height_px", 120,
                role=role,
            )
            boundary_width = self._get(
                "top_platform_overlap_boundary_width_px", 305,
                role=role,
            )
            boundary_height = self._get(
                "top_platform_overlap_boundary_height_px", 140,
                role=role,
            )
            component_min = self._get(
                "top_platform_overlap_excess_component_min_px", 3,
                role=role,
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
                inner_width=float(inner_width),
                inner_height=float(inner_height),
                boundary_width=float(boundary_width),
                boundary_height=float(boundary_height),
                component_min=int(component_min),
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

    @classmethod
    def _check_role(
        cls,
        *,
        role,
        platforms,
        inner_width,
        inner_height,
        boundary_width,
        boundary_height,
        component_min,
        drawings,
    ):
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
                "type": "platform_overlap_platform",
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

        drawings.append({
            "type": "platform_overlap_platform",
            "role": role,
            "bbox": platform.get("bbox") or [0, 0, 0, 0],
            "mask": platform.get("mask"),
            "valid": True,
            "triggered": False,
        })

        # Якорь полностью повторяет production-проверку top_platform:
        # центр mask, затем ближайшее место, куда входит внутренний rectangle.
        inner_fit = try_inscribe_center_then_nearest(
            platform,
            width_px=inner_width,
            height_px=inner_height,
            angle_deg=angle,
        )
        if not inner_fit.get("fits") or inner_fit.get("placed_center") is None:
            if inner_fit.get("points") is not None:
                drawings.append({
                    "type": "platform_overlap_inner_attempt",
                    "role": role,
                    "points": inner_fit["points"],
                    "triggered": True,
                })
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": platform.get("bbox") or [0, 0, 0, 0],
                "message": "NO INNER RECT",
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": "inner_platform_reference_not_fitted",
                "found": len(platforms),
                "ignored": max(0, len(platforms) - 1),
                "inner_rect_width_px": inner_width,
                "inner_rect_height_px": inner_height,
            }

        center = inner_fit["placed_center"]
        boundary = oriented_rectangle_points(
            center=center,
            width_px=boundary_width,
            height_px=boundary_height,
            angle_deg=angle,
        )
        shape = infer_shape([platform])
        platform_raster = rasterize_mask(platform, shape)
        boundary_raster = np.zeros(shape, dtype=np.uint8)
        cv2.fillPoly(
            boundary_raster,
            [np.rint(boundary).astype(np.int32)],
            255,
        )
        outside = cv2.bitwise_and(
            platform_raster,
            cv2.bitwise_not(boundary_raster),
        )
        measurement = cls._measure_components(outside, component_min)
        is_triggered = measurement["confirmed_components"] > 0
        boundary_points = np.rint(boundary).astype(np.int32).tolist()

        drawings.append({
            "type": "platform_overlap_boundary",
            "role": role,
            "points": boundary_points,
            "triggered": is_triggered,
        })
        if is_triggered:
            drawings.append({
                "type": "platform_overlap_region",
                "role": role,
                "raster": measurement.pop("confirmed_raster"),
                "contours": measurement.pop("confirmed_contours"),
                "triggered": True,
            })
        else:
            measurement.pop("confirmed_raster")
            measurement.pop("confirmed_contours")

        return {
            "triggered": is_triggered,
            "reason": None,
            "found": len(platforms),
            "ignored": max(0, len(platforms) - 1),
            "anchor": "top_platform_inscribed_rect",
            "boundary_center": [round(float(value), 3) for value in center],
            "angle_deg": round(float(angle), 3),
            "inner_rect_width_px": inner_width,
            "inner_rect_height_px": inner_height,
            "boundary_width_px": boundary_width,
            "boundary_height_px": boundary_height,
            "excess_component_min_px": component_min,
            **measurement,
        }

    @staticmethod
    def _measure_components(outside, component_min):
        binary = np.where(np.asarray(outside) > 0, 255, 0).astype(np.uint8)
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            binary,
            connectivity=8,
        )
        areas = [int(stats[index, cv2.CC_STAT_AREA]) for index in range(1, count)]
        confirmed_labels = [
            index
            for index in range(1, count)
            if int(stats[index, cv2.CC_STAT_AREA]) >= component_min
        ]
        ignored_labels = [
            index
            for index in range(1, count)
            if int(stats[index, cv2.CC_STAT_AREA]) < component_min
        ]
        confirmed = np.zeros_like(binary)
        for label in confirmed_labels:
            confirmed[labels == label] = 255
        contours, _hierarchy = cv2.findContours(
            confirmed,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        return {
            "raw_excess_pixels": int(np.count_nonzero(binary)),
            "excess_pixels": int(np.count_nonzero(confirmed)),
            "largest_component_pixels": max(areas, default=0),
            "confirmed_components": len(confirmed_labels),
            "ignored_noise_components": len(ignored_labels),
            "ignored_noise_pixels": sum(areas[index - 1] for index in ignored_labels),
            "confirmed_raster": confirmed,
            "confirmed_contours": [
                contour.reshape(-1, 2).astype(np.int32).tolist()
                for contour in contours
                if len(contour) >= 1
            ],
        }
