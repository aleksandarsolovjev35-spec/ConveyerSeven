import math

import cv2
import numpy as np

from domain.defect_rules.base import BaseRule, RuleResult


class InputWindowGeometryRule(BaseRule):
    """Проверка геометрии семи областей flatness в абсолютных пикселях.

    Для каждой segmentation mask измеряются:
      T — верхняя граница mask -> нижний край перекладины;
      B — нижний край перекладины -> нижняя граница mask.

    T и B независимо сравниваются со своими диапазонами в px. Для
    INPUT_LEFT и INPUT_RIGHT используются отдельные обязательные параметры.
    """

    name = "window_geometry"
    ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    TARGET_CLASS = "flatness"

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        if not self.enabled:
            return self._make_skip(self.name)

        drawings = []
        triggered = False
        details_per_role = {}

        for role in self.ROLES:
            if role not in vision_results:
                continue

            config = self._read_role_config(role)
            candidates = [
                detection
                for detection in vision_results[role]
                if detection.get("class") == self.TARGET_CLASS
                and float(detection.get("confidence", 0.0))
                >= config["min_confidence"]
            ]
            role_result = self._check_role(
                role=role,
                candidates=candidates,
                expected_count=config["expected_count"],
                top_px_min=config["top_px_min"],
                top_px_max=config["top_px_max"],
                bottom_px_min=config["bottom_px_min"],
                bottom_px_max=config["bottom_px_max"],
                center_zone_ratio=config["center_zone_ratio"],
                drawings=drawings,
            )
            triggered = triggered or role_result["triggered"]
            details_per_role[role] = role_result

        return RuleResult(
            self.name,
            triggered,
            details={"per_role": details_per_role},
            drawings=drawings,
        )

    def _read_role_config(self, role: str) -> dict:
        prefix = f"{role}.input_window_geometry_"
        keys = {
            "min_confidence": prefix + "min_confidence",
            "expected_count": prefix + "expected_count",
            "top_px_min": prefix + "top_px_min",
            "top_px_max": prefix + "top_px_max",
            "bottom_px_min": prefix + "bottom_px_min",
            "bottom_px_max": prefix + "bottom_px_max",
            "center_zone_ratio": prefix + "center_zone_ratio",
        }
        missing = [key for key in keys.values() if key not in self.thresholds]
        if missing:
            raise ValueError(
                "Отсутствуют параметры window_geometry: " + ", ".join(missing)
            )

        min_confidence = self.thresholds[keys["min_confidence"]]
        expected_count = self.thresholds[keys["expected_count"]]
        top_px_min = self.thresholds[keys["top_px_min"]]
        top_px_max = self.thresholds[keys["top_px_max"]]
        bottom_px_min = self.thresholds[keys["bottom_px_min"]]
        bottom_px_max = self.thresholds[keys["bottom_px_max"]]
        center_zone_ratio = self.thresholds[keys["center_zone_ratio"]]

        if not _finite_in_range(min_confidence, 0.0, 1.0):
            raise ValueError(f"{keys['min_confidence']} должен быть числом 0..1")
        if type(expected_count) is not int or expected_count <= 0:
            raise ValueError(f"{keys['expected_count']} должен быть целым числом > 0")
        for name, value in (
            (keys["top_px_min"], top_px_min),
            (keys["top_px_max"], top_px_max),
            (keys["bottom_px_min"], bottom_px_min),
            (keys["bottom_px_max"], bottom_px_max),
        ):
            if not _finite_in_range(value, 0.0, None):
                raise ValueError(f"{name} должен быть конечным числом >= 0")
        if float(top_px_min) > float(top_px_max):
            raise ValueError(
                f"{keys['top_px_min']} не может превышать {keys['top_px_max']}"
            )
        if float(bottom_px_min) > float(bottom_px_max):
            raise ValueError(
                f"{keys['bottom_px_min']} не может превышать "
                f"{keys['bottom_px_max']}"
            )
        if not _finite_in_range(center_zone_ratio, 0.0, 1.0, lower_open=True):
            raise ValueError(f"{keys['center_zone_ratio']} должен быть числом 0..1")

        return {
            "min_confidence": float(min_confidence),
            "expected_count": expected_count,
            "top_px_min": float(top_px_min),
            "top_px_max": float(top_px_max),
            "bottom_px_min": float(bottom_px_min),
            "bottom_px_max": float(bottom_px_max),
            "center_zone_ratio": float(center_zone_ratio),
        }

    def _check_role(
        self,
        *,
        role: str,
        candidates: list,
        expected_count: int,
        top_px_min: float,
        top_px_max: float,
        bottom_px_min: float,
        bottom_px_max: float,
        center_zone_ratio: float,
        drawings: list,
    ) -> dict:
        found_raw = len(candidates)
        limits = {
            "top_min": top_px_min,
            "top_max": top_px_max,
            "bottom_min": bottom_px_min,
            "bottom_max": bottom_px_max,
        }

        if found_raw < expected_count:
            ordered_found = sorted(
                candidates,
                key=lambda detection: self._bbox_center_x(detection["bbox"]),
            )
            for index, detection in enumerate(ordered_found, start=1):
                drawings.append({
                    "type": "window_geometry_count_item",
                    "role": role,
                    "bbox": detection.get("bbox") or [0, 0, 0, 0],
                    "mask": detection.get("mask"),
                    "index": index,
                    "triggered": True,
                })
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": self._combined_bbox(ordered_found),
                "message": f"WINDOWS {found_raw}/{expected_count}",
                "triggered": True,
            })
            return self._count_failure_details(
                found=found_raw,
                found_raw=found_raw,
                expected_count=expected_count,
                reason=f"too_few: {found_raw}/{expected_count}",
                limits=limits,
            )

        picked, ignored, select_note = self._select_row(
            candidates, expected_count,
        )
        for detection in ignored:
            drawings.append({
                "type": "window_geometry_ignored",
                "role": role,
                "bbox": detection.get("bbox") or [0, 0, 0, 0],
                "mask": detection.get("mask"),
                "triggered": False,
            })

        found = len(picked)
        if found != expected_count:
            for index, detection in enumerate(picked, start=1):
                drawings.append({
                    "type": "window_geometry_count_item",
                    "role": role,
                    "bbox": detection.get("bbox") or [0, 0, 0, 0],
                    "mask": detection.get("mask"),
                    "index": index,
                    "triggered": True,
                })
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": self._combined_bbox(picked),
                "message": f"WINDOWS {found}/{expected_count}",
                "triggered": True,
            })
            return self._count_failure_details(
                found=found,
                found_raw=found_raw,
                expected_count=expected_count,
                reason=f"select_failed: {select_note}",
                limits=limits,
            )

        ordered = sorted(
            picked,
            key=lambda detection: self._bbox_center_x(detection["bbox"]),
        )
        items = []
        for index, detection in enumerate(ordered, start=1):
            measurement = self._measure_crossbar_position(
                detection, center_zone_ratio,
            )
            top_px = float(measurement["top_px"])
            bottom_px = float(measurement["bottom_px"])
            top_fail = (
                not measurement["valid"]
                or top_px < top_px_min
                or top_px > top_px_max
            )
            bottom_fail = (
                not measurement["valid"]
                or bottom_px < bottom_px_min
                or bottom_px > bottom_px_max
            )
            item = {
                "det": detection,
                "index": index,
                **measurement,
                "top_fail": top_fail,
                "bottom_fail": bottom_fail,
                "triggered": top_fail or bottom_fail,
            }
            items.append(item)
            self._draw_item(item, role, limits, drawings)
            if not measurement["valid"]:
                drawings.append({
                    "type": "construction_error",
                    "role": role,
                    "bbox": detection.get("bbox") or [0, 0, 0, 0],
                    "message": f"NO T/B #{index}",
                    "triggered": True,
                })

        role_triggered = any(item["triggered"] for item in items)
        top_values = [float(item["top_px"]) for item in items]
        bottom_values = [float(item["bottom_px"]) for item in items]
        failed_indices = [
            item["index"] for item in items if item["triggered"]
        ]
        invalid_indices = [
            item["index"] for item in items if not item["valid"]
        ]

        return {
            "triggered": role_triggered,
            "found": found,
            "found_raw": found_raw,
            "expected_count": expected_count,
            "ignored": len(ignored),
            "selection_note": select_note,
            "top_values_px": [round(value, 3) for value in top_values],
            "bottom_values_px": [round(value, 3) for value in bottom_values],
            "top_limits_px": [top_px_min, top_px_max],
            "bottom_limits_px": [bottom_px_min, bottom_px_max],
            "failed_indices": failed_indices,
            "invalid_indices": invalid_indices,
            "items": [
                {
                    "index": item["index"],
                    "valid": bool(item["valid"]),
                    "reason": item.get("reason"),
                    "top_px": round(float(item["top_px"]), 3),
                    "bottom_px": round(float(item["bottom_px"]), 3),
                    "top_fail": bool(item["top_fail"]),
                    "bottom_fail": bool(item["bottom_fail"]),
                }
                for item in items
            ],
        }

    @staticmethod
    def _count_failure_details(
        *,
        found: int,
        found_raw: int,
        expected_count: int,
        reason: str,
        limits: dict,
    ) -> dict:
        return {
            "triggered": True,
            "reason": reason,
            "found": found,
            "found_raw": found_raw,
            "expected_count": expected_count,
            "top_values_px": [],
            "bottom_values_px": [],
            "top_limits_px": [limits["top_min"], limits["top_max"]],
            "bottom_limits_px": [limits["bottom_min"], limits["bottom_max"]],
            "failed_indices": [],
            "invalid_indices": [],
            "items": [],
        }

    @classmethod
    def _select_row(cls, candidates, expected_count):
        count = len(candidates)
        if count == 0:
            return [], [], "no detections"
        if count == expected_count:
            return list(candidates), [], "exact count"

        ordered = sorted(
            candidates,
            key=lambda detection: cls._bbox_center_x(detection["bbox"]),
        )
        best_score = float("inf")
        best_start = None
        for start in range(count - expected_count + 1):
            window = ordered[start:start + expected_count]
            xs = np.asarray([
                cls._bbox_center_x(detection["bbox"])
                for detection in window
            ], dtype=np.float64)
            spacings = np.diff(xs)
            if len(spacings) < 1:
                continue
            median_step = float(np.median(spacings))
            if median_step <= 0.0:
                continue
            score = float(np.std(spacings)) / median_step
            if score < best_score:
                best_score = score
                best_start = start

        if best_start is None:
            return (
                list(ordered[:expected_count]),
                list(ordered[expected_count:]),
                "fallback: first N",
            )

        picked = ordered[best_start:best_start + expected_count]
        ignored = [detection for detection in ordered if detection not in picked]
        return (
            picked,
            ignored,
            f"picked {expected_count} of {count} "
            f"(evenness={best_score:.3f})",
        )

    @staticmethod
    def _measure_crossbar_position(det, center_zone_ratio):
        bbox = det["bbox"]
        x1, y1, x2, y2 = map(int, bbox)
        bbox_width = max(1, x2 - x1)
        bbox_height = max(1, y2 - y1)
        mask = det.get("mask")
        if mask is None or len(mask) < 3:
            return {
                "valid": False,
                "reason": "missing_mask",
                "mask_top": float(y1),
                "mask_bottom": float(y2),
                "boundary_y": float(y1),
                "top_px": 0.0,
                "bottom_px": float(bbox_height),
                "full_height": float(bbox_height),
            }

        points = np.asarray(mask, dtype=np.float32)
        if (
            points.ndim != 2
            or points.shape[1] != 2
            or len(points) < 3
            or not np.isfinite(points).all()
        ):
            return {
                "valid": False,
                "reason": "invalid_mask",
                "mask_top": float(y1),
                "mask_bottom": float(y2),
                "boundary_y": float(y1),
                "top_px": 0.0,
                "bottom_px": float(bbox_height),
                "full_height": float(bbox_height),
            }

        mask_top = float(points[:, 1].min())
        mask_bottom = float(points[:, 1].max()) + 1.0
        full_height = max(1.0, mask_bottom - mask_top)
        local = points - np.asarray([x1, y1], dtype=np.float32)
        canvas = np.zeros((bbox_height, bbox_width), dtype=np.uint8)
        try:
            cv2.fillPoly(canvas, [local.astype(np.int32)], 255)
        except cv2.error:
            return {
                "valid": False,
                "reason": "mask_raster_error",
                "mask_top": mask_top,
                "mask_bottom": mask_bottom,
                "boundary_y": mask_top,
                "top_px": 0.0,
                "bottom_px": full_height,
                "full_height": full_height,
            }

        zone_width = max(3, int(round(bbox_width * center_zone_ratio)))
        zone_width = min(zone_width, bbox_width)
        zone_start = max(0, (bbox_width - zone_width) // 2)
        zone_end = min(bbox_width, zone_start + zone_width)

        lower_edges = []
        for x in range(zone_start, zone_end):
            ys = np.flatnonzero(canvas[:, x] > 0)
            if len(ys) == 0:
                continue
            run_end = int(ys[0])
            for value in ys[1:]:
                value = int(value)
                if value != run_end + 1:
                    break
                run_end = value
            lower_edges.append(y1 + run_end + 1)

        if not lower_edges:
            return {
                "valid": False,
                "reason": "crossbar_boundary_not_found",
                "mask_top": mask_top,
                "mask_bottom": mask_bottom,
                "boundary_y": mask_top,
                "top_px": 0.0,
                "bottom_px": full_height,
                "full_height": full_height,
            }

        boundary_y = float(np.median(lower_edges))
        boundary_y = min(mask_bottom, max(mask_top, boundary_y))
        top_px = max(0.0, boundary_y - mask_top)
        bottom_px = max(0.0, mask_bottom - boundary_y)
        return {
            "valid": True,
            "reason": None,
            "mask_top": mask_top,
            "mask_bottom": mask_bottom,
            "boundary_y": boundary_y,
            "top_px": top_px,
            "bottom_px": bottom_px,
            "full_height": full_height,
        }

    @staticmethod
    def _draw_item(item, role, limits, drawings):
        detection = item["det"]
        x1, _y1, x2, _y2 = map(int, detection["bbox"])
        width = max(1, x2 - x1)
        drawings.append({
            "type": "window_geometry_item",
            "role": role,
            "bbox": detection["bbox"],
            "mask": detection.get("mask"),
            "index": item["index"],
            "valid": item["valid"],
            "reason": item.get("reason"),
            "mask_top": item["mask_top"],
            "mask_bottom": item["mask_bottom"],
            "boundary_y": item["boundary_y"],
            "top_px": round(float(item["top_px"]), 3),
            "bottom_px": round(float(item["bottom_px"]), 3),
            "full_height": round(float(item["full_height"]), 3),
            "top_limits_px": [limits["top_min"], limits["top_max"]],
            "bottom_limits_px": [limits["bottom_min"], limits["bottom_max"]],
            "top_fail": item["top_fail"],
            "bottom_fail": item["bottom_fail"],
            "top_x": int(x1 + width * 0.35),
            "bottom_x": int(x1 + width * 0.65),
            "x_line_start": x1,
            "x_line_end": x2,
            "triggered": item["triggered"],
        })

    @staticmethod
    def _combined_bbox(detections):
        boxes = [
            detection.get("bbox")
            for detection in detections
            if detection.get("bbox") and len(detection.get("bbox")) == 4
        ]
        if not boxes:
            return [0, 0, 0, 0]
        return [
            min(float(box[0]) for box in boxes),
            min(float(box[1]) for box in boxes),
            max(float(box[2]) for box in boxes),
            max(float(box[3]) for box in boxes),
        ]

    @staticmethod
    def _bbox_center_x(bbox) -> float:
        return (float(bbox[0]) + float(bbox[2])) / 2.0


def _finite_in_range(value, lower, upper, *, lower_open=False) -> bool:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        return False
    number = float(value)
    if lower_open:
        if number <= lower:
            return False
    elif number < lower:
        return False
    return upper is None or number <= upper
