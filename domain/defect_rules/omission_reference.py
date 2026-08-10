from __future__ import annotations

import cv2
import numpy as np


DEFAULT_SAMPLE_COUNT = 31
DEFAULT_MIN_POINTS = 12


def fit_omission_top_line(
    omissions,
    *,
    x_start,
    x_end,
    sample_count=DEFAULT_SAMPLE_COUNT,
    min_points=DEFAULT_MIN_POINTS,
):
    """Объединить mask и устойчиво аппроксимировать их верхний контур."""
    polygons = []
    for detection in omissions:
        mask = detection.get("mask")
        if mask is None or len(mask) < 3:
            continue
        points = np.asarray(mask, dtype=np.float32)
        if (
            points.ndim == 2
            and points.shape[1] == 2
            and len(points) >= 3
            and np.isfinite(points).all()
        ):
            polygons.append(points)
    if not polygons:
        return None

    all_points = np.vstack(polygons)
    mask_x_min = int(np.floor(all_points[:, 0].min()))
    mask_x_max = int(np.ceil(all_points[:, 0].max()))
    mask_y_min = int(np.floor(all_points[:, 1].min()))
    mask_y_max = int(np.ceil(all_points[:, 1].max()))
    width = mask_x_max - mask_x_min + 1
    height = mask_y_max - mask_y_min + 1
    if width < 2 or height < 2:
        return None

    canvas = np.zeros((height, width), dtype=np.uint8)
    for points in polygons:
        local = points.copy()
        local[:, 0] -= mask_x_min
        local[:, 1] -= mask_y_min
        cv2.fillPoly(canvas, [local.astype(np.int32)], 255)

    fit_start = max(float(x_start), float(mask_x_min))
    fit_end = min(float(x_end), float(mask_x_max))
    if fit_end - fit_start < 2.0:
        return None

    samples = []
    seen = set()
    for value in np.linspace(fit_start, fit_end, sample_count):
        x_global = int(round(float(value)))
        if x_global in seen:
            continue
        seen.add(x_global)
        ys = np.flatnonzero(canvas[:, x_global - mask_x_min] > 0)
        if len(ys) == 0:
            continue
        samples.append((x_global, int(ys[0]) + mask_y_min))
    if len(samples) < min_points:
        return None

    xs = np.asarray([point[0] for point in samples], dtype=np.float64)
    ys = np.asarray([point[1] for point in samples], dtype=np.float64)
    slope, intercept = fit_theil_sen_line(xs, ys)

    all_sample_points = [
        [int(round(x)), int(round(y))]
        for x, y in zip(xs, ys, strict=True)
    ]
    residuals = np.abs(ys - (slope * xs + intercept))
    median_residual = float(np.median(residuals))
    robust_limit = max(1.0, median_residual * 3.0)
    inliers = residuals <= robust_limit
    if int(np.count_nonzero(inliers)) >= min_points:
        xs = xs[inliers]
        ys = ys[inliers]
        slope, intercept = fit_theil_sen_line(xs, ys)

    return {
        "line": (slope, intercept),
        "x_start": float(x_start),
        "x_end": float(x_end),
        "valid_points": int(len(xs)),
        "sample_points": [
            [int(round(x)), int(round(y))]
            for x, y in zip(xs, ys, strict=True)
        ],
        # Все сэмплы верхней кромки до отбрасывания выбросов: по ним
        # omission_boundary проверяет долю точек у линии (общая картина),
        # а не худшую единичную точку.
        "all_sample_points": all_sample_points,
    }


def fit_theil_sen_line(xs, ys):
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    slopes = []
    for left in range(len(xs) - 1):
        for right in range(left + 1, len(xs)):
            dx = float(xs[right] - xs[left])
            if abs(dx) <= 1e-9:
                continue
            slopes.append(float(ys[right] - ys[left]) / dx)
    slope = float(np.median(slopes)) if slopes else 0.0
    intercept = float(np.median(ys - slope * xs)) if len(xs) else 0.0
    return slope, intercept


def signed_distance_and_projection(point, slope, intercept):
    x, y = point
    denominator = slope * slope + 1.0
    signed_factor = (slope * x - y + intercept) / denominator
    projection_x = x - slope * signed_factor
    projection_y = y + signed_factor
    signed_distance = (
        y - (slope * x + intercept)
    ) / np.sqrt(denominator)
    return float(signed_distance), (
        float(projection_x), float(projection_y),
    )
