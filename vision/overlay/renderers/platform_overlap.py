import cv2
import numpy as np

from vision.overlay.renderers.primitives import (
    COLOR_FAIL,
    DrawPrimitives,
    LINE_FAIL,
    LINE_THIN,
)

COLOR_PLATFORM_CONTOUR = (180, 180, 180)
# Чистая опорная граница по контактам: тонкий зелёный пунктир.
COLOR_BOUNDARY = (0, 255, 0)
LINE_BOUNDARY = LINE_THIN
BOUNDARY_DASH_LEN = 8


class PlatformOverlapRenderer:
    @staticmethod
    def draw_platform(img, drawing):
        points = PlatformOverlapRenderer._points(
            drawing.get("mask"),
            drawing.get("bbox"),
        )
        valid = bool(drawing.get("valid", True))
        cv2.polylines(
            img,
            [points],
            True,
            COLOR_PLATFORM_CONTOUR if valid else COLOR_FAIL,
            LINE_THIN if valid else LINE_FAIL,
            lineType=cv2.LINE_AA,
        )
        if not valid:
            PlatformOverlapRenderer._draw_cross(img, points)

    @staticmethod
    def draw_boundary(img, drawing):
        points = drawing.get("points") or []
        if len(points) < 4:
            return
        polygon = np.asarray(points, dtype=np.int32).reshape(-1, 2)
        for index, point in enumerate(polygon):
            next_point = polygon[(index + 1) % len(polygon)]
            DrawPrimitives.draw_dashed_line(
                img,
                (int(point[0]), int(point[1])),
                (int(next_point[0]), int(next_point[1])),
                COLOR_BOUNDARY,
                LINE_BOUNDARY,
                dash_len=BOUNDARY_DASH_LEN,
            )

    @staticmethod
    def draw_contact_anchors(img, drawing):
        # Точки контактов используются в details для диагностики, но в оверлее
        # больше не рисуются: так граница rule_top_platform_overlap остаётся
        # чистой и не закрывает сами контакты.
        return

    @staticmethod
    def draw_region(img, drawing):
        raster = drawing.get("raster")
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
        region[active] = COLOR_FAIL
        cv2.addWeighted(overlay, 0.55, img, 0.45, 0, img)
        for raw_contour in drawing.get("contours") or []:
            if not raw_contour:
                continue
            contour = np.asarray(raw_contour, dtype=np.int32).reshape(-1, 1, 2)
            cv2.drawContours(img, [contour], -1, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)

    @staticmethod
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

    @staticmethod
    def _draw_cross(img, points):
        flat = points.reshape(-1, 2)
        x1 = int(flat[:, 0].min())
        x2 = int(flat[:, 0].max())
        y1 = int(flat[:, 1].min())
        y2 = int(flat[:, 1].max())
        cv2.line(img, (x1, y1), (x2, y2), COLOR_FAIL, LINE_FAIL)
        cv2.line(img, (x1, y2), (x2, y1), COLOR_FAIL, LINE_FAIL)
