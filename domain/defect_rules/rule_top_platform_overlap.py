import math
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


CONTACT_CLASS = "contacts"


class TopPlatformOverlapRule(BaseRule):
    """Контроль заплыва platform mask за настраиваемую внешнюю границу.

    Два режима построения границы:
    1) fallback — концентрический прямоугольник вокруг inscribed_rect
       (старая логика): ``top_platform_overlap_boundary_width/height_px``.
    2) contact-based — прямоугольник, построенный через контакты TOP,
       который задевает одну треть каждого контакта (параметр
       ``top_platform_overlap_contact_inner_ratio``). Размер области
       дополнительно меняется через ``margin_px`` и ``expand_x/y_ratio``.

    Если контакты найдены в достаточном количестве (по одному на каждую
    сторону L/R/T/B), используется contact-based режим, иначе — fallback.
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
            contact_min_conf = self._get(
                "top_platform_overlap_contact_min_confidence", 0.3,
                role=role,
            )
            contact_inner_ratio = self._get(
                "top_platform_overlap_contact_inner_ratio", 0.33,
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
                inner_width=float(inner_width),
                inner_height=float(inner_height),
                boundary_width=float(boundary_width),
                boundary_height=float(boundary_height),
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
        inner_width,
        inner_height,
        boundary_width,
        boundary_height,
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

        # Попробовать построить границу через контакты
        contact_boundary = cls._build_boundary_from_contacts(
            platform=platform,
            contacts=contacts,
            angle_deg=angle,
            inner_ratio=contact_inner_ratio,
            margin_px=margin_px,
            expand_x_ratio=expand_x_ratio,
            expand_y_ratio=expand_y_ratio,
        )

        if contact_boundary is not None:
            center = contact_boundary["center"]
            b_width = contact_boundary["width"]
            b_height = contact_boundary["height"]
            boundary = contact_boundary["points"]
            anchor = "contacts_rectangle"
            # Для отладки можно нарисовать контакты, но сохраняем
            # совместимость типов отрисовки: boundary тот же.
            boundary_center = center
            used_contacts = contact_boundary["used_contacts"]
            group_counts = contact_boundary["group_counts"]
        else:
            # Fallback — старая концентрическая логика через inscribed rect
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
            b_width = boundary_width
            b_height = boundary_height
            anchor = "top_platform_inscribed_rect"
            boundary_center = center
            used_contacts = 0
            group_counts = {}

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

        result = {
            "triggered": is_triggered,
            "reason": None,
            "found": len(platforms),
            "ignored": max(0, len(platforms) - 1),
            "anchor": anchor,
            "boundary_center": [round(float(v), 3) for v in boundary_center],
            "angle_deg": round(float(angle), 3),
            "boundary_width_px": round(float(b_width), 3),
            "boundary_height_px": round(float(b_height), 3),
            "excess_component_min_px": component_min,
            "contact_inner_ratio": round(float(contact_inner_ratio), 4),
            "margin_px": round(float(margin_px), 3),
            "expand_x_ratio": round(float(expand_x_ratio), 4),
            "expand_y_ratio": round(float(expand_y_ratio), 4),
            "used_contacts": int(used_contacts),
            "contact_groups": dict(group_counts),
            **measurement,
        }
        # Для совместимости со старой телеметрией сохраняем старые ключи,
        # если использовался fallback.
        if anchor == "top_platform_inscribed_rect":
            result["inner_rect_width_px"] = inner_width
            result["inner_rect_height_px"] = inner_height
        else:
            result["inner_rect_width_px"] = inner_width
            result["inner_rect_height_px"] = inner_height
        return result

    # ------------------------------------------------------------------
    # Построение границы через контакты
    # ------------------------------------------------------------------

    @staticmethod
    def _group_contacts_by_side(contacts, platform_bbox):
        """Сгруппировать контакты по сторонам относительно platform bbox.

        Контакты слева (L) имеют центр x < x1 платформы, справа (R) x > x2,
        сверху (T) y < y1, снизу (B) y > y2. Если bbox контакта внутри
        платформы (что не должно случаться), отнесение делается по
        ближайшему к центру стороны.
        """
        groups = {"L": [], "R": [], "T": [], "B": []}
        if not platform_bbox or len(platform_bbox) != 4:
            return groups
        px1, py1, px2, py2 = map(float, platform_bbox)
        pcx = (px1 + px2) * 0.5
        pcy = (py1 + py2) * 0.5
        for det in contacts:
            bbox = det.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            try:
                x1, y1, x2, y2 = map(float, bbox)
            except Exception:
                continue
            if x2 <= x1 or y2 <= y1:
                continue
            cx = (x1 + x2) * 0.5
            cy = (y1 + y2) * 0.5
            # Кандидаты по расстоянию до сторон
            candidates = []
            if cx < px1:
                candidates.append((px1 - cx, "L"))
            if cx > px2:
                candidates.append((cx - px2, "R"))
            if cy < py1:
                candidates.append((py1 - cy, "T"))
            if cy > py2:
                candidates.append((cy - py2, "B"))
            if candidates:
                _, g = min(candidates, key=lambda it: it[0])
                groups[g].append(det)
            else:
                # Внутри bbox платформы — определить по более удалённой оси
                dx = abs(cx - pcx)
                dy = abs(cy - pcy)
                if dx > dy:
                    groups["L" if cx < pcx else "R"].append(det)
                else:
                    groups["T" if cy < pcy else "B"].append(det)
        return groups

    @staticmethod
    def _median(values):
        if not values:
            return None
        arr = np.asarray(values, dtype=float)
        return float(np.median(arr))

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
        """Построить ориентированный прямоугольник через контакты.

        Для каждого контакта вычисляется точка на его внутренней трети:
        - L: x = x2 - w*ratio, y = cy
        - R: x = x1 + w*ratio, y = cy
        - T: x = cx, y = y2 - h*ratio
        - B: x = cx, y = y1 + h*ratio

        Затем все точки поворачиваются на -angle вокруг центра платформы,
        и медиана по каждой стороне даёт границы в повёрнутой системе.
        После применения margin и expand прямоугольник возвращается в
        исходную систему координат.
        """
        if not contacts:
            return None
        platform_bbox = platform.get("bbox")
        if not platform_bbox or len(platform_bbox) != 4:
            return None
        try:
            px1, py1, px2, py2 = map(float, platform_bbox)
        except Exception:
            return None
        if px2 <= px1 or py2 <= py1:
            return None
        p_center = ((px1 + px2) * 0.5, (py1 + py2) * 0.5)

        groups = cls._group_contacts_by_side(contacts, platform_bbox)
        # Требуется хотя бы по одному контакту на каждую сторону для
        # стабильного прямоугольника.
        if not all(len(groups[g]) > 0 for g in ("L", "R", "T", "B")):
            return None

        # Сбор inset точек и значений для медианы
        inset_points = []  # list of (point, group)
        l_vals = []
        r_vals = []
        t_vals = []
        b_vals = []

        for det in groups["L"]:
            bbox = det.get("bbox")
            if not bbox:
                continue
            x1, y1, x2, y2 = map(float, bbox)
            w = x2 - x1
            inset_x = x2 - w * float(inner_ratio)
            cy = (y1 + y2) * 0.5
            pt = (inset_x, cy)
            inset_points.append((pt, "L"))
            l_vals.append(inset_x)

        for det in groups["R"]:
            bbox = det.get("bbox")
            if not bbox:
                continue
            x1, y1, x2, y2 = map(float, bbox)
            w = x2 - x1
            inset_x = x1 + w * float(inner_ratio)
            cy = (y1 + y2) * 0.5
            pt = (inset_x, cy)
            inset_points.append((pt, "R"))
            r_vals.append(inset_x)

        for det in groups["T"]:
            bbox = det.get("bbox")
            if not bbox:
                continue
            x1, y1, x2, y2 = map(float, bbox)
            h = y2 - y1
            inset_y = y2 - h * float(inner_ratio)
            cx = (x1 + x2) * 0.5
            pt = (cx, inset_y)
            inset_points.append((pt, "T"))
            t_vals.append(inset_y)

        for det in groups["B"]:
            bbox = det.get("bbox")
            if not bbox:
                continue
            x1, y1, x2, y2 = map(float, bbox)
            h = y2 - y1
            inset_y = y1 + h * float(inner_ratio)
            cx = (x1 + x2) * 0.5
            pt = (cx, inset_y)
            inset_points.append((pt, "B"))
            b_vals.append(inset_y)

        if not (l_vals and r_vals and t_vals and b_vals):
            return None

        # Поворот точек на -angle вокруг центра платформы для вычисления
        # границ в системе координат, выровненной с платформой.
        rotated_l_x = []
        rotated_r_x = []
        rotated_t_y = []
        rotated_b_y = []

        for pt, group in inset_points:
            rpt = cls._rotate_point(pt, p_center, -float(angle_deg))
            if rpt is None:
                continue
            if group == "L":
                rotated_l_x.append(rpt[0])
            elif group == "R":
                rotated_r_x.append(rpt[0])
            elif group == "T":
                rotated_t_y.append(rpt[1])
            elif group == "B":
                rotated_b_y.append(rpt[1])

        left_rot = cls._median(rotated_l_x)
        right_rot = cls._median(rotated_r_x)
        top_rot = cls._median(rotated_t_y)
        bottom_rot = cls._median(rotated_b_y)

        if None in (left_rot, right_rot, top_rot, bottom_rot):
            return None
        if left_rot >= right_rot or top_rot >= bottom_rot:
            return None

        # Применить margin (расширение наружу)
        left_rot -= float(margin_px)
        right_rot += float(margin_px)
        top_rot -= float(margin_px)
        bottom_rot += float(margin_px)

        width0 = right_rot - left_rot
        height0 = bottom_rot - top_rot
        if width0 <= 0 or height0 <= 0:
            return None

        # Применить expand коэффициенты
        center_rot_x = (left_rot + right_rot) * 0.5
        center_rot_y = (top_rot + bottom_rot) * 0.5
        width_exp = width0 * float(expand_x_ratio)
        height_exp = height0 * float(expand_y_ratio)
        if width_exp <= 0 or height_exp <= 0:
            return None

        left_rot = center_rot_x - width_exp * 0.5
        right_rot = center_rot_x + width_exp * 0.5
        top_rot = center_rot_y - height_exp * 0.5
        bottom_rot = center_rot_y + height_exp * 0.5

        # Центр в повёрнутой системе
        center_rot = (center_rot_x, center_rot_y)

        # Вернуть центр в исходную систему
        center_orig = cls._rotate_point(center_rot, p_center, float(angle_deg))
        if center_orig is None:
            return None

        # Построить ориентированный прямоугольник
        points = oriented_rectangle_points(
            center=center_orig,
            width_px=width_exp,
            height_px=height_exp,
            angle_deg=float(angle_deg),
        )

        return {
            "center": center_orig,
            "width": width_exp,
            "height": height_exp,
            "points": points,
            "used_contacts": len(inset_points),
            "group_counts": {k: len(v) for k, v in groups.items()},
            "left_rot": left_rot,
            "right_rot": right_rot,
            "top_rot": top_rot,
            "bottom_rot": bottom_rot,
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
