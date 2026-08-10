import cv2
import numpy as np

from vision.overlay.renderers.primitives import (
    COLOR_FAIL,
    LINE_FAIL,
    LINE_THIN,
)

COLOR_PLATFORM_CONTOUR = (180, 180, 180)
COLOR_REFERENCE_RECT = (255, 200, 100)
COLOR_TARGET_CENTER = (150, 150, 150)
COLOR_PLACED_CENTER = (255, 210, 80)
COLOR_SHIFT = (0, 190, 255)


class TopPlatformRenderer:
    @staticmethod
    def draw_actual(img, drawing):
        points = TopPlatformRenderer._points(drawing)
        valid = bool(drawing.get("valid", True))
        color = COLOR_PLATFORM_CONTOUR if valid else COLOR_FAIL
        width = LINE_THIN if valid else LINE_FAIL
        cv2.polylines(img, [points], True, color, width, lineType=cv2.LINE_AA)
        if not valid:
            flat = points.reshape(-1, 2)
            x1 = int(flat[:, 0].min())
            x2 = int(flat[:, 0].max())
            y1 = int(flat[:, 1].min())
            y2 = int(flat[:, 1].max())
            cv2.line(img, (x1, y1), (x2, y2), COLOR_FAIL, LINE_FAIL)
            cv2.line(img, (x1, y2), (x2, y1), COLOR_FAIL, LINE_FAIL)

    @staticmethod
    def draw_inscribed_rect(img, drawing):
        points = drawing.get("points") or []
        if len(points) < 4:
            return
        fits = bool(drawing.get("fits"))
        cv2.polylines(
            img,
            [np.asarray(points, dtype=np.int32)],
            True,
            COLOR_REFERENCE_RECT if fits else COLOR_FAIL,
            LINE_THIN if fits else LINE_FAIL,
            lineType=cv2.LINE_AA,
        )

    @staticmethod
    def draw_centers(img, drawing):
        target = tuple(
            int(round(value))
            for value in drawing.get("target_center") or [0, 0]
        )
        placed = tuple(
            int(round(value))
            for value in drawing.get("placed_center") or target
        )
        if drawing.get("shifted"):
            cv2.line(img, target, placed, COLOR_SHIFT, LINE_THIN)
        target_color = COLOR_FAIL if drawing.get("triggered") else COLOR_TARGET_CENTER
        cv2.drawMarker(
            img,
            target,
            target_color,
            markerType=cv2.MARKER_CROSS,
            markerSize=9,
            thickness=LINE_THIN,
        )
        cv2.circle(
            img,
            placed,
            4,
            COLOR_FAIL if drawing.get("triggered") else COLOR_PLACED_CENTER,
            LINE_FAIL if drawing.get("triggered") else LINE_THIN,
        )

    @staticmethod
    def _points(drawing):
        mask = drawing.get("mask") or []
        if len(mask) >= 3:
            points = np.asarray(mask, dtype=np.int32)
            if points.ndim == 2 and points.shape[1] == 2:
                return points.reshape(-1, 1, 2)
        x1, y1, x2, y2 = map(
            int,
            drawing.get("bbox") or [0, 0, 0, 0],
        )
        return np.asarray(
            [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
            dtype=np.int32,
        ).reshape(-1, 1, 2)
