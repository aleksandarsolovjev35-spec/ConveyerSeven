from __future__ import annotations

import math

import cv2
import numpy as np

from domain.defect_rules.base import RuleResult
from domain.defect_rules.omission_reference import fit_omission_top_line


BOUNDARY_NUMERIC_EPSILON_PX = 0.01


class OmissionBoundaryMixin:
    """Общая реализация для long_omission и short_omission, не отдельное правило."""

    ROLES: tuple[str, ...] | list[str] = ()
    TARGET_CLASS = ""
    FAMILY = ""
    DRAWING_TYPE = ""

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
                config=config,
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
        prefix = f"{role}.spider_{self.FAMILY}_omission_"
        names = {
            "min_confidence": prefix + "min_confidence",
            "allowed_thickness_px": prefix + "allowed_thickness_px",
            "excess_component_min_px": (
                prefix + "excess_component_min_px"
            ),
            "top_line_max_residual_px": (
                prefix + "top_line_max_residual_px"
            ),
            "top_line_min_inlier_ratio": (
                prefix + "top_line_min_inlier_ratio"
            ),
        }
        missing = [key for key in names.values() if key not in self.thresholds]
        if missing:
            raise ValueError(
                "Отсутствуют параметры omission boundary: "
                + ", ".join(missing)
            )
        values = {name: self.thresholds[key] for name, key in names.items()}
        if not _finite_in_range(values["min_confidence"], 0.0, 1.0):
            raise ValueError(f"{names['min_confidence']} должен быть числом 0..1")
        for name in (
            "allowed_thickness_px",
            "top_line_max_residual_px",
        ):
            if not _finite_in_range(values[name], 0.0, None):
                raise ValueError(f"{names[name]} должен быть числом >= 0")
        component_min = values["excess_component_min_px"]
        if type(component_min) is not int or component_min < 1:
            raise ValueError(
                f"{names['excess_component_min_px']} должен быть целым >= 1"
            )
        ratio_min = values["top_line_min_inlier_ratio"]
        if type(ratio_min) not in (int, float) or not math.isfinite(
            float(ratio_min)
        ) or not 0.0 < float(ratio_min) <= 1.0:
            raise ValueError(
                f"{names['top_line_min_inlier_ratio']} должен быть числом > 0 и <= 1"
            )
        return {
            "min_confidence": float(values["min_confidence"]),
            "allowed_thickness_px": float(values["allowed_thickness_px"]),
            "excess_component_min_px": int(component_min),
            "top_line_max_residual_px": float(
                values["top_line_max_residual_px"]
            ),
            "top_line_min_inlier_ratio": float(ratio_min),
        }

    def _check_role(self, *, role, candidates, config, drawings):
        measurement = measure_omission_boundary(
            candidates,
            allowed_thickness_px=config["allowed_thickness_px"],
            excess_component_min_px=config["excess_component_min_px"],
            top_line_max_residual_px=config["top_line_max_residual_px"],
            top_line_min_inlier_ratio=config["top_line_min_inlier_ratio"],
        )
        if not measurement["valid"]:
            drawings.append({
                "type": "construction_error",
                "role": role,
                "message": "NO VALID OMISSION",
                "triggered": True,
            })
            return {
                "triggered": True,
                "class": self.TARGET_CLASS,
                "found": len(candidates),
                "ignored": max(0, len(candidates) - 1),
                "valid": False,
                "reason": measurement["reason"],
                "allowed_thickness_px": config["allowed_thickness_px"],
                "excess_component_min_px": config[
                    "excess_component_min_px"
                ],
                "top_line_max_residual_px": config[
                    "top_line_max_residual_px"
                ],
                "top_line_min_inlier_ratio": config[
                    "top_line_min_inlier_ratio"
                ],
                "excess_pixels": None,
                "max_excess_depth_px": None,
                "max_consecutive_columns": None,
            }

        triggered = measurement["confirmed_components"] > 0
        drawings.append({
            "type": self.DRAWING_TYPE,
            "role": role,
            "mask": measurement["mask"],
            "bbox": measurement["bbox"],
            "top_line": measurement["top_line"],
            "limit_line": measurement["limit_line"],
            "excess_contours": measurement["excess_contours"],
            "excess_mask": measurement["excess_mask"],
            "excess_origin": measurement["excess_origin"],
            "triggered": triggered,
        })
        return {
            "triggered": triggered,
            "class": self.TARGET_CLASS,
            "found": len(candidates),
            "ignored": max(0, len(candidates) - 1),
            "valid": True,
            "reason": None,
            "mask_area_px2": round(measurement["mask_area_px2"], 3),
            "allowed_thickness_px": config["allowed_thickness_px"],
            "excess_component_min_px": config[
                "excess_component_min_px"
            ],
            "top_line_angle_deg": measurement["top_line_angle_deg"],
            "top_line_max_residual_px": config["top_line_max_residual_px"],
            "top_line_actual_max_residual_px": measurement[
                "top_line_actual_max_residual_px"
            ],
            "top_line_min_inlier_ratio": config[
                "top_line_min_inlier_ratio"
            ],
            "top_line_actual_inlier_ratio": measurement[
                "top_line_actual_inlier_ratio"
            ],
            "raw_excess_pixels": measurement["raw_excess_pixels"],
            "excess_pixels": measurement["excess_pixels"],
            "largest_component_pixels": measurement[
                "largest_component_pixels"
            ],
            "confirmed_components": measurement["confirmed_components"],
            "ignored_noise_components": measurement[
                "ignored_noise_components"
            ],
            "ignored_noise_pixels": measurement["ignored_noise_pixels"],
            "max_excess_depth_px": measurement["max_excess_depth_px"],
            "max_consecutive_columns": measurement[
                "max_consecutive_columns"
            ],
            "excess_x_start": measurement["excess_x_start"],
            "excess_x_end": measurement["excess_x_end"],
        }


def measure_omission_boundary(
    detections,
    *,
    allowed_thickness_px,
    excess_component_min_px,
    top_line_max_residual_px,
    top_line_min_inlier_ratio,
):
    if not detections:
        return _invalid("no_detections")

    valid = []
    invalid_count = 0
    for detection in detections:
        mask = detection.get("mask")
        if mask is None or len(mask) < 3:
            invalid_count += 1
            continue
        points = np.asarray(mask, dtype=np.float32)
        if (
            points.ndim != 2
            or points.shape[1] != 2
            or len(points) < 3
            or not np.isfinite(points).all()
        ):
            invalid_count += 1
            continue
        area = float(abs(cv2.contourArea(points)))
        if area <= 0.0:
            invalid_count += 1
            continue
        valid.append((area, detection, points))
    if not valid:
        return _invalid("missing_or_invalid_mask")

    _area, main_detection, points = max(valid, key=lambda item: item[0])
    x_min = int(math.floor(float(points[:, 0].min())))
    x_max = int(math.ceil(float(points[:, 0].max())))
    y_min = int(math.floor(float(points[:, 1].min())))
    y_max = int(math.ceil(float(points[:, 1].max())))
    width = x_max - x_min + 1
    height = y_max - y_min + 1
    if width < 4 or height < 1:
        return _invalid("mask_too_small")

    reference = fit_omission_top_line(
        [main_detection],
        x_start=x_min,
        x_end=x_max,
    )
    if reference is None:
        return _invalid("no_valid_top_line")
    slope, intercept = reference["line"]
    # Валидность верхней линии проверяется по ОБЩЕЙ КАРТИНЕ кромки:
    # берём все сэмплы до отбрасывания выбросов и требуем, чтобы доля
    # точек в пределах top_line_max_residual_px от линии была не ниже
    # top_line_min_inlier_ratio. Единичные зубцы маски (шум сегментации)
    # больше не заваливают весь замер, как при проверке худшей точки.
    all_points = (
        reference.get("all_sample_points") or reference["sample_points"]
    )
    residuals = [
        abs(float(y) - (slope * float(x) + intercept))
        for x, y in all_points
    ]
    actual_max_residual = max(residuals, default=float("inf"))
    if not residuals:
        return _invalid("top_line_residual_too_large")
    inlier_count = sum(
        residual <= (
            top_line_max_residual_px + BOUNDARY_NUMERIC_EPSILON_PX
        )
        for residual in residuals
    )
    actual_inlier_ratio = inlier_count / len(residuals)
    if actual_inlier_ratio < top_line_min_inlier_ratio:
        return _invalid("top_line_residual_too_large")

    local = points.copy()
    local[:, 0] -= x_min
    local[:, 1] -= y_min
    canvas = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(canvas, [local.astype(np.int32)], 255)
    mask_area = float(np.count_nonzero(canvas))

    ys, xs = np.nonzero(canvas)
    global_x = xs.astype(np.float64) + x_min
    global_y = ys.astype(np.float64) + y_min
    norm = math.sqrt(1.0 + slope * slope)
    distances = (
        global_y - (slope * global_x + intercept)
    ) / norm
    final_distance = allowed_thickness_px
    # Только защита от float-ошибки вида 20.004 при математической границе
    # ровно 20 px. Это не дополнительная физическая safety-зона.
    excess_selector = distances > (
        final_distance + BOUNDARY_NUMERIC_EPSILON_PX
    )

    excess_local = np.zeros_like(canvas)
    if np.any(excess_selector):
        excess_local[ys[excess_selector], xs[excess_selector]] = 255
    raw_excess_pixels = int(np.count_nonzero(excess_local))
    confirmed_local = np.zeros_like(excess_local)
    largest_component_pixels = 0
    confirmed_components = 0
    ignored_noise_components = 0
    ignored_noise_pixels = 0
    if raw_excess_pixels:
        labels_count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            (excess_local > 0).astype(np.uint8),
            connectivity=8,
        )
        for label in range(1, labels_count):
            component_pixels = int(stats[label, cv2.CC_STAT_AREA])
            largest_component_pixels = max(
                largest_component_pixels, component_pixels,
            )
            if component_pixels >= excess_component_min_px:
                confirmed_local[labels == label] = 255
                confirmed_components += 1
            else:
                ignored_noise_components += 1
                ignored_noise_pixels += component_pixels

    excess_pixels = int(np.count_nonzero(confirmed_local))
    excess_contours = []
    max_depth = 0.0
    max_columns = 0
    excess_x_start = None
    excess_x_end = None
    if excess_pixels:
        contours, _ = cv2.findContours(
            confirmed_local,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        for contour in contours:
            contour = contour.reshape(-1, 2).astype(np.int32)
            contour[:, 0] += x_min
            contour[:, 1] += y_min
            excess_contours.append(contour.tolist())

        depth_map = np.zeros_like(canvas, dtype=np.float64)
        depth_map[ys[excess_selector], xs[excess_selector]] = (
            distances[excess_selector] - final_distance
        )
        max_depth = float(np.max(depth_map[confirmed_local > 0]))
        active_columns = np.any(confirmed_local > 0, axis=0)
        max_columns = _max_true_run(active_columns)
        active_xs = np.flatnonzero(active_columns)
        if len(active_xs):
            excess_x_start = int(active_xs[0] + x_min)
            excess_x_end = int(active_xs[-1] + x_min)

    render_x_start = float(x_min)
    render_x_end = float(x_max)
    limit_intercept = intercept + allowed_thickness_px * norm

    return {
        "valid": True,
        "reason": None,
        "mask": points.tolist(),
        "bbox": [x_min, y_min, x_max, y_max],
        "mask_area_px2": mask_area,
        "top_line": _line_drawing(
            slope, intercept, render_x_start, render_x_end,
            reference["sample_points"],
        ),
        "limit_line": _line_drawing(
            slope, limit_intercept, render_x_start, render_x_end, [],
        ),
        "top_line_angle_deg": round(
            float(np.degrees(np.arctan(slope))), 3,
        ),
        "top_line_actual_max_residual_px": round(
            float(actual_max_residual), 3,
        ),
        "top_line_actual_inlier_ratio": round(
            float(actual_inlier_ratio), 3,
        ),
        "excess_contours": excess_contours,
        "excess_mask": confirmed_local,
        "excess_origin": [x_min, y_min],
        "raw_excess_pixels": raw_excess_pixels,
        "excess_pixels": excess_pixels,
        "largest_component_pixels": largest_component_pixels,
        "confirmed_components": confirmed_components,
        "ignored_noise_components": ignored_noise_components,
        "ignored_noise_pixels": ignored_noise_pixels,
        "max_excess_depth_px": round(max_depth, 3),
        "max_consecutive_columns": int(max_columns),
        "excess_x_start": excess_x_start,
        "excess_x_end": excess_x_end,
        "ignored_invalid_masks": invalid_count,
    }


def _line_drawing(slope, intercept, x_start, x_end, sample_points):
    return {
        "x_start": int(round(x_start)),
        "y_start": int(round(slope * x_start + intercept)),
        "x_end": int(round(x_end)),
        "y_end": int(round(slope * x_end + intercept)),
        "sample_points": sample_points,
    }


def _max_true_run(values):
    best = 0
    current = 0
    for value in values:
        if value:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _invalid(reason):
    return {"valid": False, "reason": reason}


def _finite_in_range(value, lower, upper):
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        return False
    value = float(value)
    return value >= lower and (upper is None or value <= upper)
