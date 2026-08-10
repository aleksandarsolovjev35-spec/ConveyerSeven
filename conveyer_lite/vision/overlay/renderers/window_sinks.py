import cv2
import numpy as np

from vision.overlay.renderers.primitives import (
    COLOR_FAIL,
    COLOR_SKIP,
    LINE_FAIL,
    LINE_THIN,
)


class WindowSinksRenderer:
    @staticmethod
    def draw_overlap(img, drawing):
        # Окно — нейтральная reference geometry, sink — красный defect contour.
        window_points = WindowSinksRenderer._points(
            drawing.get("window_mask"),
            drawing.get("window_bbox"),
        )
        sink_points = WindowSinksRenderer._points(
            drawing.get("sink_mask"),
            drawing.get("sink_bbox"),
        )
        if drawing.get("draw_window_reference", True):
            cv2.polylines(
                img, [window_points], True, COLOR_SKIP, LINE_THIN,
                lineType=cv2.LINE_AA,
            )
        cv2.polylines(img, [sink_points], True, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)

        raster = drawing.get("overlap_raster")
        if raster is not None:
            raster = np.asarray(raster)
            if raster.ndim == 2:
                height = min(img.shape[0], raster.shape[0])
                width = min(img.shape[1], raster.shape[1])
                active = raster[:height, :width] > 0
                if np.any(active):
                    overlay = img.copy()
                    overlay[:height, :width][active] = COLOR_FAIL
                    cv2.addWeighted(overlay, 0.60, img, 0.40, 0, img)

        for raw_contour in drawing.get("overlap_contours") or []:
            if not raw_contour:
                continue
            contour = np.asarray(raw_contour, dtype=np.int32).reshape(-1, 1, 2)
            cv2.drawContours(img, [contour], -1, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)

    @staticmethod
    def draw_invalid_reference(img, drawing):
        points = WindowSinksRenderer._points(
            drawing.get("mask"),
            drawing.get("bbox"),
        )
        cv2.polylines(img, [points], True, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)
        flat = points.reshape(-1, 2)
        x1 = int(flat[:, 0].min())
        x2 = int(flat[:, 0].max())
        y1 = int(flat[:, 1].min())
        y2 = int(flat[:, 1].max())
        cv2.line(img, (x1, y1), (x2, y2), COLOR_FAIL, LINE_FAIL)
        cv2.line(img, (x1, y2), (x2, y1), COLOR_FAIL, LINE_FAIL)

    @staticmethod
    def draw_reference_count_item(img, drawing):
        points = WindowSinksRenderer._points(
            drawing.get("mask"),
            drawing.get("bbox"),
        )
        cv2.polylines(img, [points], True, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)

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
