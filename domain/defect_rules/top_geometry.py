from __future__ import annotations

import cv2
import numpy as np


def mask_points(detection):
    mask = detection.get("mask")
    if mask is None or len(mask) < 3:
        return None
    points = np.asarray(mask, dtype=np.float32)
    if (
        points.ndim != 2
        or points.shape[1] != 2
        or len(points) < 3
        or not np.isfinite(points).all()
    ):
        return None
    if abs(float(cv2.contourArea(points))) <= 0.0:
        return None
    return points


def mask_area(detection):
    points = mask_points(detection)
    return 0.0 if points is None else abs(float(cv2.contourArea(points)))


def largest_valid_mask(detections):
    valid = [detection for detection in detections if mask_points(detection) is not None]
    if not valid:
        return None
    return max(valid, key=mask_area)


def infer_shape(*detection_lists):
    max_x = 1
    max_y = 1
    for detections in detection_lists:
        for detection in detections:
            points = mask_points(detection)
            if points is not None:
                max_x = max(max_x, int(np.ceil(points[:, 0].max())) + 2)
                max_y = max(max_y, int(np.ceil(points[:, 1].max())) + 2)
            bbox = detection.get("bbox")
            if bbox and len(bbox) == 4:
                max_x = max(max_x, int(np.ceil(float(bbox[2]))) + 2)
                max_y = max(max_y, int(np.ceil(float(bbox[3]))) + 2)
    return max_y, max_x


def rasterize_mask(detection, shape):
    points = mask_points(detection)
    if points is None:
        return None
    canvas = np.zeros(shape, dtype=np.uint8)
    cv2.fillPoly(canvas, [points.astype(np.int32)], 255)
    return canvas


def mask_orientation(detection):
    points = mask_points(detection)
    if points is None:
        return None
    _center, (width, height), angle = cv2.minAreaRect(points)
    if width < 1.0 or height < 1.0:
        return None
    return float(angle + 90.0 if width < height else angle)


def oriented_rectangle_points(*, center, width_px, height_px, angle_deg):
    """Углы прямоугольника в той же системе угла, что и inscribe helper."""
    center_x, center_y = map(float, center)
    angle = np.deg2rad(float(angle_deg))
    width_axis = np.asarray([np.cos(angle), np.sin(angle)], dtype=np.float32)
    height_axis = np.asarray([-np.sin(angle), np.cos(angle)], dtype=np.float32)
    half_width = float(width_px) / 2.0
    half_height = float(height_px) / 2.0
    center_point = np.asarray([center_x, center_y], dtype=np.float32)
    points = np.asarray([
        center_point - half_width * width_axis - half_height * height_axis,
        center_point + half_width * width_axis - half_height * height_axis,
        center_point + half_width * width_axis + half_height * height_axis,
        center_point - half_width * width_axis + half_height * height_axis,
    ], dtype=np.float32)
    return points


def try_inscribe_center_then_nearest(
    detection,
    *,
    width_px,
    height_px,
    angle_deg=0.0,
):
    """Сохранённый алгоритм: центр, затем ближайшее допустимое положение."""
    points = mask_points(detection)
    if points is None:
        return {"fits": False, "points": None, "reason": "missing_mask"}
    x_min = float(points[:, 0].min())
    x_max = float(points[:, 0].max())
    y_min = float(points[:, 1].min())
    y_max = float(points[:, 1].max())
    center_x = (x_min + x_max) / 2.0
    center_y = (y_min + y_max) / 2.0
    max_dimension = max(x_max - x_min, y_max - y_min, width_px, height_px)
    padding = int(max_dimension * 0.8) + 20
    canvas_size = int(max_dimension) + 2 * padding

    local = points - np.asarray([center_x, center_y], dtype=np.float32)
    local[:, 0] += canvas_size / 2.0
    local[:, 1] += canvas_size / 2.0
    canvas = np.zeros((canvas_size, canvas_size), dtype=np.uint8)
    cv2.fillPoly(canvas, [local.astype(np.int32)], 255)

    matrix = cv2.getRotationMatrix2D(
        (canvas_size / 2.0, canvas_size / 2.0),
        angle_deg,
        1.0,
    )
    rotated = cv2.warpAffine(
        canvas,
        matrix,
        (canvas_size, canvas_size),
        flags=cv2.INTER_NEAREST,
        borderValue=0,
    )

    kernel_width = max(1, int(round(width_px)))
    kernel_height = max(1, int(round(height_px)))
    target_x = canvas_size / 2.0
    target_y = canvas_size / 2.0

    def centered_bounds(cx, cy):
        left = kernel_width // 2
        top = kernel_height // 2
        x0 = int(round(cx - left))
        y0 = int(round(cy - top))
        return x0, y0, x0 + kernel_width, y0 + kernel_height

    x0, y0, x1, y1 = centered_bounds(target_x, target_y)
    fits_center = (
        x0 >= 0
        and y0 >= 0
        and x1 <= rotated.shape[1]
        and y1 <= rotated.shape[0]
        and bool(np.all(rotated[y0:y1, x0:x1] == 255))
    )

    if fits_center:
        fits = True
        fit_x = target_x
        fit_y = target_y
    elif (
        kernel_width > rotated.shape[1]
        or kernel_height > rotated.shape[0]
    ):
        fits = False
        fit_x = target_x
        fit_y = target_y
    else:
        kernel = np.ones((kernel_height, kernel_width), dtype=np.uint8)
        eroded = cv2.erode(rotated, kernel, iterations=1)
        valid_y, valid_x = np.where(eroded > 0)
        if len(valid_x):
            distances = (
                (valid_x.astype(np.float32) - target_x) ** 2
                + (valid_y.astype(np.float32) - target_y) ** 2
            )
            best = int(np.argmin(distances))
            fits = True
            fit_x = float(valid_x[best])
            fit_y = float(valid_y[best])
        else:
            fits = False
            fit_x = target_x
            fit_y = target_y

    half_width = width_px / 2.0
    half_height = height_px / 2.0
    rectangle = np.asarray([
        [fit_x - half_width, fit_y - half_height],
        [fit_x + half_width, fit_y - half_height],
        [fit_x + half_width, fit_y + half_height],
        [fit_x - half_width, fit_y + half_height],
    ], dtype=np.float32)
    inverse = cv2.getRotationMatrix2D(
        (canvas_size / 2.0, canvas_size / 2.0),
        -angle_deg,
        1.0,
    )
    restored = (
        inverse
        @ np.hstack([
            rectangle,
            np.ones((4, 1), dtype=np.float32),
        ]).T
    ).T
    restored[:, 0] -= canvas_size / 2.0
    restored[:, 1] -= canvas_size / 2.0
    restored[:, 0] += center_x
    restored[:, 1] += center_y
    placed_center = restored.mean(axis=0)
    return {
        "fits": bool(fits),
        "points": np.rint(restored).astype(np.int32).tolist(),
        "centered": bool(fits_center),
        "center": [round(center_x, 3), round(center_y, 3)],
        "placed_center": [
            round(float(placed_center[0]), 3),
            round(float(placed_center[1]), 3),
        ],
        "angle_deg": round(float(angle_deg), 6),
    }


def overlap_mask(raster_a, raster_b):
    if raster_a is None or raster_b is None:
        return None
    return cv2.bitwise_and(raster_a, raster_b)
