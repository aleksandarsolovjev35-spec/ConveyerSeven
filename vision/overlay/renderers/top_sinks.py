import cv2
import numpy as np

from vision.overlay.renderers.primitives import (
    COLOR_FAIL,
    COLOR_SKIP,
    LINE_FAIL,
    LINE_THIN,
)

COLOR_CASE_CENTRAL = (255, 255, 0)
COLOR_SHELL = (0, 190, 255)


class TopSinksRenderer:
    @staticmethod
    def draw_references(img, drawing):
        central = TopSinksRenderer._points(
            drawing.get("case_central_mask"),
            drawing.get("case_central_bbox"),
        )
        platform = TopSinksRenderer._points(
            drawing.get("platform_mask"),
            drawing.get("platform_bbox"),
        )
        cv2.polylines(img, [central], True, COLOR_CASE_CENTRAL, LINE_THIN, lineType=cv2.LINE_AA)
        if drawing.get("draw_platform_reference", True):
            cv2.polylines(img, [platform], True, COLOR_SKIP, LINE_THIN, lineType=cv2.LINE_AA)
        if drawing.get("draw_contact_references", True):
            masks = drawing.get("contact_masks") or []
            boxes = drawing.get("contact_bboxes") or []
            for mask, bbox in zip(masks, boxes, strict=False):
                points = TopSinksRenderer._points(mask, bbox)
                cv2.polylines(
                    img, [points], True, COLOR_SKIP, LINE_THIN,
                    lineType=cv2.LINE_AA,
                )

    @staticmethod
    def draw_forbidden_region(img, drawing):
        sink_points = TopSinksRenderer._points(
            drawing.get("sink_mask"),
            drawing.get("sink_bbox"),
        )
        cv2.polylines(img, [sink_points], True, COLOR_SHELL, LINE_THIN, lineType=cv2.LINE_AA)
        raster = drawing.get("forbidden_raster")
        if raster is not None:
            raster = np.asarray(raster)
            if raster.ndim == 2:
                height = min(img.shape[0], raster.shape[0])
                width = min(img.shape[1], raster.shape[1])
                active = raster[:height, :width] > 0
                if np.any(active):
                    overlay = img.copy()
                    region = overlay[:height, :width]
                    region[active] = COLOR_FAIL
                    cv2.addWeighted(overlay, 0.60, img, 0.40, 0, img)
        for raw_contour in drawing.get("forbidden_contours") or []:
            if not raw_contour:
                continue
            contour = np.asarray(raw_contour, dtype=np.int32).reshape(-1, 1, 2)
            cv2.drawContours(img, [contour], -1, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)

    @staticmethod
    def draw_invalid_reference(img, drawing):
        points = TopSinksRenderer._points(
            drawing.get("mask"),
            drawing.get("bbox"),
        )
        cv2.polylines(img, [points], True, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)
        TopSinksRenderer._draw_cross(img, points)

    @staticmethod
    def draw_reference_contact(img, drawing):
        points = TopSinksRenderer._points(
            drawing.get("mask"),
            drawing.get("bbox"),
        )
        cv2.polylines(
            img,
            [points],
            True,
            COLOR_FAIL if drawing.get("invalid") else COLOR_SKIP,
            LINE_FAIL if drawing.get("invalid") else LINE_THIN,
            lineType=cv2.LINE_AA,
        )

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
