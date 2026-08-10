import cv2
import numpy as np

from vision.overlay.renderers.primitives import (
    COLOR_FAIL,
    COLOR_GLASS,
    COLOR_SKIP,
    LINE_FAIL,
    LINE_THIN,
)

COLOR_CASE = (255, 255, 0)
COLOR_CENTRAL = (120, 120, 120)


class TopGlassRenderer:
    @staticmethod
    def draw_cleanup_references(img, drawing):
        if drawing.get("draw_platform_reference", True):
            cv2.polylines(img, [_points(
                drawing.get("platform_mask"), drawing.get("platform_bbox"),
            )], True, COLOR_SKIP, LINE_THIN,
            lineType=cv2.LINE_AA,)
        cv2.polylines(img, [_points(
            drawing.get("case_mask"), drawing.get("case_bbox"),
        )], True, COLOR_CASE, LINE_THIN,
        lineType=cv2.LINE_AA,)
        if drawing.get("draw_central_reference", True):
            cv2.polylines(img, [_points(
                drawing.get("central_mask"), drawing.get("central_bbox"),
            )], True, COLOR_CENTRAL, LINE_THIN,
            lineType=cv2.LINE_AA,)
        for mask, bbox in zip(
            drawing.get("pin_masks") or [],
            drawing.get("pin_bboxes") or [],
            strict=False,
        ):
            cv2.polylines(
                img, [_points(mask, bbox)], True, COLOR_SKIP, LINE_THIN,
                lineType=cv2.LINE_AA,
            )

    @staticmethod
    def draw_cleanup_region(img, drawing):
        glass = _points(
            drawing.get("glass_mask"), drawing.get("glass_bbox"),
        )
        cv2.polylines(img, [glass], True, COLOR_GLASS, LINE_THIN, lineType=cv2.LINE_AA)
        _draw_raster_region(
            img, drawing.get("cleanup_raster"), COLOR_GLASS, alpha=0.55,
        )
        _draw_contours(
            img, drawing.get("cleanup_contours") or [], COLOR_GLASS,
        )

    @staticmethod
    def draw_bad_references(img, drawing):
        for mask, bbox in zip(
            drawing.get("contact_masks") or [],
            drawing.get("contact_bboxes") or [],
            strict=False,
        ):
            cv2.polylines(
                img, [_points(mask, bbox)], True, COLOR_SKIP, LINE_THIN,
                lineType=cv2.LINE_AA,
            )

    @staticmethod
    def draw_contact_overlap(img, drawing):
        glass = _points(
            drawing.get("glass_mask"), drawing.get("glass_bbox"),
        )
        contact = _points(
            drawing.get("contact_mask"), drawing.get("contact_bbox"),
        )
        cv2.polylines(img, [glass], True, COLOR_GLASS, LINE_THIN, lineType=cv2.LINE_AA)
        cv2.polylines(img, [contact], True, COLOR_SKIP, LINE_THIN, lineType=cv2.LINE_AA)
        _draw_raster_region(
            img, drawing.get("overlap_raster"), COLOR_FAIL, alpha=0.60,
        )
        _draw_contours(
            img, drawing.get("overlap_contours") or [], COLOR_FAIL,
        )

    @staticmethod
    def draw_bad_glass(img, drawing):
        points = _points(drawing.get("mask"), drawing.get("bbox"))
        valid = bool(drawing.get("valid", True))
        cv2.polylines(
            img,
            [points],
            True,
            COLOR_GLASS if valid else COLOR_FAIL,
            LINE_THIN if valid else LINE_FAIL,
            lineType=cv2.LINE_AA,
        )
        if not valid:
            _draw_cross(img, points)


def _draw_raster_region(img, raster, color, alpha):
    if raster is None:
        return
    raster = np.asarray(raster)
    if raster.ndim != 2:
        return
    height = min(img.shape[0], raster.shape[0])
    width = min(img.shape[1], raster.shape[1])
    active = raster[:height, :width] > 0
    if not np.any(active):
        return
    overlay = img.copy()
    region = overlay[:height, :width]
    region[active] = color
    cv2.addWeighted(overlay, alpha, img, 1-alpha, 0, img)


def _draw_contours(img, raw_contours, color):
    for raw_contour in raw_contours:
        if not raw_contour:
            continue
        contour = np.asarray(raw_contour, dtype=np.int32).reshape(-1, 1, 2)
        cv2.drawContours(img, [contour], -1, color, LINE_FAIL, lineType=cv2.LINE_AA)


def _points(mask, bbox):
    mask = mask or []
    if len(mask) >= 3:
        points = np.asarray(mask, dtype=np.int32)
        if points.ndim == 2 and points.shape[1] == 2:
            return points.reshape(-1, 1, 2)
    x1, y1, x2, y2 = map(int, bbox or [0, 0, 0, 0])
    return np.asarray(
        [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        dtype=np.int32,
    ).reshape(-1, 1, 2)


def _draw_cross(img, points):
    flat = points.reshape(-1, 2)
    x1 = int(flat[:, 0].min())
    x2 = int(flat[:, 0].max())
    y1 = int(flat[:, 1].min())
    y2 = int(flat[:, 1].max())
    cv2.line(img, (x1, y1), (x2, y2), COLOR_FAIL, LINE_FAIL)
    cv2.line(img, (x1, y2), (x2, y1), COLOR_FAIL, LINE_FAIL)
