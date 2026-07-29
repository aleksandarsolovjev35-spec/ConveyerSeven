import math

import cv2
import numpy as np

from domain.defect_rules.base import BaseRule, RuleResult
from domain.defect_rules.top_geometry import (
    infer_shape,
    largest_valid_mask,
    mask_orientation,
    mask_points,
    oriented_rectangle_points,
    rasterize_mask,
)


CONTACT_CLASS = "contacts"
SIDES = ("L", "R", "T", "B")


class TopPlatformOverlapRule(BaseRule):
    """Контроль заплыва platform mask за область, построенную по контактам.

    Область строится только по контактам TOP: контакты группируются по
    сторонам платформы (L/R/T/B) в системе координат, повёрнутой на угол
    платформы. Для каждой стороны берётся медиана опорных точек контактов
    (по умолчанию ``top_platform_overlap_contact_inner_ratio = 0.5`` —
    центры контактов), и по этим четырём линиям строится ориентированный
    прямоугольник. Размер дополнительно правится через ``margin_px``
    (расширение наружу) и ``expand_x/y_ratio``.

    Если контактов не хватает хотя бы по одному на каждую сторону,
    построить область невозможно: правило считает это браком построения
    (``NO CONTACT RECT``). Концентрический fallback вокруг inscribed rect
    больше не используется.

    Если платформа пересекает границы этого прямоугольника, срабатывает
    правило пересечения, а вышедшие за границу пиксели маски платформы
    выделяются как дефектная область.
    """

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
            component_min = self._get(
                "top_platform_overlap_excess_component_min_px", 3,
                role=role,
            )
            contact_min_conf = self._get(
                "top_platform_overlap_contact_min_confidence", 0.3,
                role=role,
            )
            contact_inner_ratio = self._get(
                "top_platform_overlap_contact_inner_ratio", 0.5,
                role=role,
            )
            margin_px = self._get(
                "top_platform_overlap_margin_px", 0.0,
                role=role,
            )
            expand_x = self._get(
                "top_platform_overlap_expand_x_ratio", 1.0,
                role=role,
            )
            expand_y = self._get(
                "top_platform_overlap_expand_y_ratio", 1.0,
                role=role,
            )
            platforms = [
                detection for detection in vision_results[role]
                if detection.get("class") == self.PLATFORM_CLASS
                and float(detection.get("confidence", 0.0))
                >= min_confidence
            ]
            contacts = [
                detection for detection in vision_results[role]
                if detection.get("class") == CONTACT_CLASS
                and float(detection.get("confidence", 0.0))
                >= contact_min_conf
            ]
            result = self._check_role(
                role=role,
                platforms=platforms,
                contacts=contacts,
                component_min=int(component_min),
                contact_inner_ratio=float(contact_inner_ratio),
                margin_px=float(margin_px),
                expand_x_ratio=float(expand_x),
                expand_y_ratio=float(expand_y),
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
        contacts,
        component_min,
        contact_inner_ratio,
        margin_px,
        expand_x_ratio,
        expand_y_ratio,
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
                "contacts_found": len(contacts),
            }

        angle = cls._upright_angle(platform)
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
                "contacts_found": len(contacts),
            }

        drawings.append({
            "type": "platform_overlap_platform",
            "role": role,
            "bbox": platform.get("bbox") or [0, 0, 0, 0],
            "mask": platform.get("mask"),
            "valid": True,
            "triggered": False,
        })

        contact_boundary = cls._build_boundary_from_contacts(
            platform=platform,
            contacts=contacts,
            angle_deg=angle,
            inner_ratio=contact_inner_ratio,
            margin_px=margin_px,
            expand_x_ratio=expand_x_ratio,
            expand_y_ratio=expand_y_ratio,
        )

        if contact_boundary is None:
            group_counts = cls._empty_groups_summary(
                platform, contacts, angle,
            )
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": platform.get("bbox") or [0, 0, 0, 0],
                "message": "NO CONTACT RECT",
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": "contact_boundary_not_built",
                "found": len(platforms),
                "ignored": max(0, len(platforms) - 1),
                "anchor": "contacts_rectangle",
                "angle_deg": round(float(angle), 3),
                "contacts_found": len(contacts),
                "used_contacts": 0,
                "contact_groups": group_counts,
                "contact_inner_ratio": round(float(contact_inner_ratio), 4),
                "margin_px": round(float(margin_px), 3),
                "expand_x_ratio": round(float(expand_x_ratio), 4),
                "expand_y_ratio": round(float(expand_y_ratio), 4),
            }

        center = contact_boundary["center"]
        b_width = contact_boundary["width"]
        b_height = contact_boundary["height"]
        boundary = contact_boundary["points"]
        used_contacts = contact_boundary["used_contacts"]
        group_counts = contact_boundary["group_counts"]
        anchor_points = contact_boundary["anchor_points"]

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
            "anchor": "contacts_rectangle",
            "triggered": is_triggered,
        })
        drawings.append({
            "type": "platform_overlap_contact_anchors",
            "role": role,
            "points": [
                [int(round(point[0])), int(round(point[1]))]
                for point, _group in anchor_points
            ],
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
            "anchor": "contacts_rectangle",
            "boundary_center": [round(float(v), 3) for v in center],
            "angle_deg": round(float(angle), 3),
            "boundary_width_px": round(float(b_width), 3),
            "boundary_height_px": round(float(b_height), 3),
            "excess_component_min_px": component_min,
            "contact_inner_ratio": round(float(contact_inner_ratio), 4),
            "margin_px": round(float(margin_px), 3),
            "expand_x_ratio": round(float(expand_x_ratio), 4),
            "expand_y_ratio": round(float(expand_y_ratio), 4),
            "contacts_found": len(contacts),
            "used_contacts": int(used_contacts),
            "contact_groups": dict(group_counts),
            **measurement,
        }

    # ------------------------------------------------------------------
    # Построение области по контактам
    # ------------------------------------------------------------------

    @staticmethod
    def _upright_angle(platform):
        """Угол платформы, приведённый к вертикальной ориентации.

        ``mask_orientation`` возвращает угол *длинной* оси mask, поэтому у
        вертикально стоящей детали он равен ~90°, и рабочая система
        координат ложится набок: стороны L/R и T/B меняются местами, а
        ``expand_x/y_ratio`` начинают растягивать область поперёк
        ожидаемого направления.

        Платформа всегда стоит вертикально, поэтому угол нормализуется в
        диапазон ``[-45, 45]``: ось X рабочей системы совпадает с
        горизонталью детали (стороны L/R), ось Y — с вертикалью (T/B).
        Небольшой физический наклон детали при этом сохраняется.
        """
        angle = mask_orientation(platform)
        if angle is None:
            return None
        angle = float(angle) % 180.0
        if angle >= 90.0:
            angle -= 180.0
        if angle > 45.0:
            angle -= 90.0
        elif angle < -45.0:
            angle += 90.0
        return angle

    @staticmethod
    def _rotate_point(point, center, angle_deg):
        """Повернуть point вокруг center на angle_deg (CCW)."""
        if point is None or center is None:
            return None
        rad = math.radians(float(angle_deg))
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        x, y = float(point[0]), float(point[1])
        cx, cy = float(center[0]), float(center[1])
        dx = x - cx
        dy = y - cy
        rx = dx * cos_a - dy * sin_a
        ry = dx * sin_a + dy * cos_a
        return (cx + rx, cy + ry)

    @staticmethod
    def _median(values):
        if not values:
            return None
        return float(np.median(np.asarray(values, dtype=float)))

    @classmethod
    def _platform_frame(cls, platform, angle_deg):
        """Центр платформы и её габариты в повёрнутой системе координат."""
        points = mask_points(platform)
        if points is None:
            return None
        center, _size, _angle = cv2.minAreaRect(points)
        center = (float(center[0]), float(center[1]))
        rotated = [
            cls._rotate_point(point, center, -float(angle_deg))
            for point in points
        ]
        xs = [point[0] for point in rotated]
        ys = [point[1] for point in rotated]
        return {
            "center": center,
            "x_min": min(xs),
            "x_max": max(xs),
            "y_min": min(ys),
            "y_max": max(ys),
        }

    @classmethod
    def _contact_frame(cls, detection, center, angle_deg):
        """Габариты bbox контакта в повёрнутой системе координат."""
        bbox = detection.get("bbox")
        if not bbox or len(bbox) != 4:
            return None
        try:
            x1, y1, x2, y2 = map(float, bbox)
        except (TypeError, ValueError):
            return None
        if x2 <= x1 or y2 <= y1:
            return None
        corners = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
        rotated = [
            cls._rotate_point(corner, center, -float(angle_deg))
            for corner in corners
        ]
        xs = [point[0] for point in rotated]
        ys = [point[1] for point in rotated]
        return {
            "x_min": min(xs),
            "x_max": max(xs),
            "y_min": min(ys),
            "y_max": max(ys),
            "cx": (min(xs) + max(xs)) * 0.5,
            "cy": (min(ys) + max(ys)) * 0.5,
        }

    @staticmethod
    def _side_for(contact_frame, platform_frame):
        """Определить сторону платформы, с которой стоит контакт."""
        cx = contact_frame["cx"]
        cy = contact_frame["cy"]
        candidates = []
        if cx < platform_frame["x_min"]:
            candidates.append((platform_frame["x_min"] - cx, "L"))
        if cx > platform_frame["x_max"]:
            candidates.append((cx - platform_frame["x_max"], "R"))
        if cy < platform_frame["y_min"]:
            candidates.append((platform_frame["y_min"] - cy, "T"))
        if cy > platform_frame["y_max"]:
            candidates.append((cy - platform_frame["y_max"], "B"))
        if candidates:
            # Сторона, от которой контакт отстоит дальше всего наружу.
            return max(candidates, key=lambda item: item[0])[1]
        # Контакт внутри габарита платформы — решаем по дальней оси.
        center_x = (platform_frame["x_min"] + platform_frame["x_max"]) * 0.5
        center_y = (platform_frame["y_min"] + platform_frame["y_max"]) * 0.5
        half_width = max(1e-6, (platform_frame["x_max"] - platform_frame["x_min"]) * 0.5)
        half_height = max(1e-6, (platform_frame["y_max"] - platform_frame["y_min"]) * 0.5)
        if abs(cx - center_x) / half_width >= abs(cy - center_y) / half_height:
            return "L" if cx < center_x else "R"
        return "T" if cy < center_y else "B"

    @classmethod
    def _group_contacts_by_side(cls, contacts, platform_frame, angle_deg):
        """Сгруппировать контакты по сторонам платформы."""
        groups = {side: [] for side in SIDES}
        if platform_frame is None:
            return groups
        for detection in contacts:
            frame = cls._contact_frame(
                detection, platform_frame["center"], angle_deg,
            )
            if frame is None:
                continue
            groups[cls._side_for(frame, platform_frame)].append(frame)
        return groups

    @classmethod
    def _empty_groups_summary(cls, platform, contacts, angle_deg):
        platform_frame = cls._platform_frame(platform, angle_deg)
        groups = cls._group_contacts_by_side(
            contacts, platform_frame, angle_deg,
        )
        return {side: len(items) for side, items in groups.items()}

    @staticmethod
    def _anchor_value(frame, side, inner_ratio):
        """Опорная координата контакта по его стороне.

        ``inner_ratio = 0.5`` даёт центр контакта, ``0`` — внутреннюю
        (обращённую к платформе) кромку, ``1`` — внешнюю кромку.
        """
        ratio = float(inner_ratio)
        if side == "L":
            return frame["x_max"] - (frame["x_max"] - frame["x_min"]) * ratio
        if side == "R":
            return frame["x_min"] + (frame["x_max"] - frame["x_min"]) * ratio
        if side == "T":
            return frame["y_max"] - (frame["y_max"] - frame["y_min"]) * ratio
        return frame["y_min"] + (frame["y_max"] - frame["y_min"]) * ratio

    @classmethod
    def _build_boundary_from_contacts(
        cls,
        *,
        platform,
        contacts,
        angle_deg,
        inner_ratio,
        margin_px,
        expand_x_ratio,
        expand_y_ratio,
    ):
        """Построить ориентированный прямоугольник по контактам."""
        if not contacts:
            return None
        platform_frame = cls._platform_frame(platform, angle_deg)
        if platform_frame is None:
            return None
        p_center = platform_frame["center"]

        groups = cls._group_contacts_by_side(
            contacts, platform_frame, angle_deg,
        )
        group_counts = {side: len(items) for side, items in groups.items()}
        if not all(group_counts[side] > 0 for side in SIDES):
            return None

        values = {side: [] for side in SIDES}
        anchor_points = []
        for side in SIDES:
            for frame in groups[side]:
                value = cls._anchor_value(frame, side, inner_ratio)
                values[side].append(value)
                rotated_point = (
                    (value, frame["cy"]) if side in ("L", "R")
                    else (frame["cx"], value)
                )
                anchor_points.append((
                    cls._rotate_point(
                        rotated_point, p_center, float(angle_deg),
                    ),
                    side,
                ))

        left = cls._median(values["L"])
        right = cls._median(values["R"])
        top = cls._median(values["T"])
        bottom = cls._median(values["B"])
        if None in (left, right, top, bottom):
            return None
        if left >= right or top >= bottom:
            return None

        # margin расширяет область наружу по всем сторонам
        left -= float(margin_px)
        right += float(margin_px)
        top -= float(margin_px)
        bottom += float(margin_px)
        if right - left <= 0 or bottom - top <= 0:
            return None

        center_x = (left + right) * 0.5
        center_y = (top + bottom) * 0.5
        width = (right - left) * float(expand_x_ratio)
        height = (bottom - top) * float(expand_y_ratio)
        if width <= 0 or height <= 0:
            return None

        center_orig = cls._rotate_point(
            (center_x, center_y), p_center, float(angle_deg),
        )
        if center_orig is None:
            return None

        points = oriented_rectangle_points(
            center=center_orig,
            width_px=width,
            height_px=height,
            angle_deg=float(angle_deg),
        )
        return {
            "center": center_orig,
            "width": width,
            "height": height,
            "points": points,
            "used_contacts": len(anchor_points),
            "group_counts": group_counts,
            "anchor_points": anchor_points,
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
