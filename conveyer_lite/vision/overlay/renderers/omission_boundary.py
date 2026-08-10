import cv2
import numpy as np

from vision.overlay.renderers.primitives import (
    COLOR_FAIL,
    LINE_FAIL,
    LINE_THIN,
)


COLOR_MASK = (180, 180, 180)
COLOR_TOP = (255, 255, 0)
COLOR_LIMIT = (0, 200, 0)


def draw_omission_item(img, drawing):
    mask = drawing.get("mask") or []
    bbox = drawing.get("bbox") or [0, 0, 0, 0]
    if len(mask) >= 3:
        points = np.asarray(mask, dtype=np.int32)
        cv2.polylines(img, [points], True, COLOR_MASK, LINE_THIN, lineType=cv2.LINE_AA)
    else:
        x1, y1, x2, y2 = map(int, bbox)
        cv2.rectangle(img, (x1, y1), (x2, y2), COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)

    _draw_reference_line(img, drawing.get("top_line") or {}, COLOR_TOP)
    _draw_reference_line(img, drawing.get("limit_line") or {}, COLOR_LIMIT)
    _draw_excess_mask(
        img,
        drawing.get("excess_mask"),
        drawing.get("excess_origin") or [0, 0],
    )

    for raw_contour in drawing.get("excess_contours") or []:
        if not raw_contour:
            continue
        contour = np.asarray(raw_contour, dtype=np.int32).reshape(-1, 1, 2)
        cv2.drawContours(img, [contour], -1, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)


def _draw_reference_line(img, line, color):
    start = (int(line.get("x_start", 0)), int(line.get("y_start", 0)))
    end = (int(line.get("x_end", 0)), int(line.get("y_end", 0)))
    cv2.line(img, start, end, color, 2)


def _draw_excess_mask(img, raster, origin):
    if raster is None:
        return
    raster = np.asarray(raster)
    if raster.ndim != 2 or not np.any(raster > 0):
        return
    origin_x, origin_y = map(int, origin)
    src_x0 = max(0, -origin_x)
    src_y0 = max(0, -origin_y)
    dst_x0 = max(0, origin_x)
    dst_y0 = max(0, origin_y)
    width = min(raster.shape[1] - src_x0, img.shape[1] - dst_x0)
    height = min(raster.shape[0] - src_y0, img.shape[0] - dst_y0)
    if width <= 0 or height <= 0:
        return
    active = raster[
        src_y0:src_y0 + height,
        src_x0:src_x0 + width,
    ] > 0
    overlay = img.copy()
    region = overlay[dst_y0:dst_y0 + height, dst_x0:dst_x0 + width]
    region[active] = COLOR_FAIL
    cv2.addWeighted(overlay, 0.55, img, 0.45, 0, img)
