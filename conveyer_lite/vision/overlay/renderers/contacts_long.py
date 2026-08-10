import cv2
import numpy as np

from vision.overlay.renderers.primitives import (
    COLOR_FAIL,
    COLOR_PASS,
    COLOR_SKIP,
    DrawPrimitives,
    LINE_FAIL,
    LINE_THIN,
)

COLOR_TOP_LINE = (255, 210, 80)
COLOR_BOTTOM_LINE = (0, 190, 255)
COLOR_OMISSION_REFERENCE = (180, 100, 255)
COLOR_REFERENCE_RECT = (255, 200, 100)


class ContactsLongRenderer:
    @staticmethod
    def draw_item(img, drawing):
        points = ContactsLongRenderer._points(drawing)
        triggered = bool(drawing.get("triggered"))
        cv2.polylines(
            img,
            [points],
            True,
            COLOR_FAIL if triggered else COLOR_PASS,
            LINE_FAIL if triggered else LINE_THIN,
            lineType=cv2.LINE_AA,
        )

    @staticmethod
    def draw_count_item(img, drawing):
        points = ContactsLongRenderer._points(drawing)
        cv2.polylines(img, [points], True, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)

    @staticmethod
    def draw_invalid_mask(img, drawing):
        points = ContactsLongRenderer._points(drawing)
        cv2.polylines(img, [points], True, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)
        ContactsLongRenderer._draw_cross(img, points)

    @staticmethod
    def draw_ignored(img, drawing):
        points = ContactsLongRenderer._points(drawing)
        raw_points = [tuple(map(int, point)) for point in points.reshape(-1, 2)]
        for index, start in enumerate(raw_points):
            end = raw_points[(index + 1) % len(raw_points)]
            DrawPrimitives.draw_dashed_line(
                img,
                start,
                end,
                COLOR_SKIP,
                LINE_THIN,
                dash_len=6,
            )

    @staticmethod
    def draw_fit_line(img, drawing):
        start = (
            int(drawing.get("x_start", 0)),
            int(drawing.get("y_start", 0)),
        )
        end = (
            int(drawing.get("x_end", 0)),
            int(drawing.get("y_end", 0)),
        )
        label = drawing.get("label")
        color = (
            COLOR_TOP_LINE
            if label in ("top", "center", "damper")
            else COLOR_BOTTOM_LINE
        )
        if drawing.get("triggered"):
            color = COLOR_FAIL
        DrawPrimitives.draw_dashed_line(
            img,
            start,
            end,
            color,
            LINE_FAIL if drawing.get("triggered") else LINE_THIN,
        )
        tolerance = int(drawing.get("tolerance") or 0)
        if tolerance > 0:
            for offset in (-tolerance, tolerance):
                DrawPrimitives.draw_dashed_line(
                    img,
                    (start[0], start[1] + offset),
                    (end[0], end[1] + offset),
                    COLOR_SKIP,
                    LINE_THIN,
                    dash_len=4,
                )

    @staticmethod
    def draw_level_center(img, drawing):
        center = drawing.get("center") or [0, 0]
        point = (int(round(center[0])), int(round(center[1])))
        color = COLOR_FAIL if drawing.get("triggered") else COLOR_REFERENCE_RECT
        cv2.circle(img, point, 4, color, -1)
        cv2.circle(img, point, 6, color, LINE_THIN)

    @staticmethod
    def draw_omission_line(img, drawing):
        color = (
            COLOR_FAIL
            if drawing.get("triggered")
            else COLOR_OMISSION_REFERENCE
        )
        start = (
            int(drawing.get("x_start", 0)),
            int(drawing.get("y_start", 0)),
        )
        end = (
            int(drawing.get("x_end", 0)),
            int(drawing.get("y_end", 0)),
        )
        cv2.line(img, start, end, color, LINE_FAIL)

    @staticmethod
    def draw_omission_distance(img, drawing):
        color = (
            COLOR_FAIL
            if drawing.get("triggered")
            else COLOR_OMISSION_REFERENCE
        )
        contact = tuple(
            int(round(value)) for value in drawing["contact_point"]
        )
        projection = tuple(
            int(round(value)) for value in drawing["projection_point"]
        )
        cv2.line(img, contact, projection, color, LINE_THIN)
        cv2.circle(img, contact, 3, color, -1)
        cv2.circle(img, projection, 3, color, -1)

    @staticmethod
    def draw_omission_missing(img, drawing):
        x1, y1, x2, y2 = map(
            int,
            drawing.get("bbox") or [0, 0, 0, 0],
        )
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

    @staticmethod
    def _draw_cross(img, points):
        flat = points.reshape(-1, 2)
        x1 = int(flat[:, 0].min())
        x2 = int(flat[:, 0].max())
        y1 = int(flat[:, 1].min())
        y2 = int(flat[:, 1].max())
        cv2.line(img, (x1, y1), (x2, y2), COLOR_FAIL, LINE_FAIL)
        cv2.line(img, (x1, y2), (x2, y1), COLOR_FAIL, LINE_FAIL)
