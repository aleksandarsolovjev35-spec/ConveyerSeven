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

COLOR_GROUP_REFERENCE = (255, 210, 80)
COLOR_DISTANCE = (180, 100, 255)
COLOR_REFERENCE_RECT = (255, 200, 100)


class TopContactsRenderer:
    @staticmethod
    def draw_platform_bbox(img, drawing):
        x1, y1, x2, y2 = map(int, drawing.get("bbox") or [0, 0, 0, 0])
        cv2.rectangle(img, (x1, y1), (x2, y2), COLOR_SKIP, LINE_THIN, lineType=cv2.LINE_AA)

    @staticmethod
    def draw_group_reference(img, drawing):
        x1, y1, x2, y2 = map(int, drawing.get("line") or [0, 0, 0, 0])
        DrawPrimitives.draw_dashed_line(
            img,
            (x1, y1),
            (x2, y2),
            COLOR_GROUP_REFERENCE,
            LINE_THIN,
            dash_len=6,
        )

    @staticmethod
    def draw_distance(img, drawing):
        start = tuple(int(round(value)) for value in drawing.get("start") or [0, 0])
        end = tuple(int(round(value)) for value in drawing.get("end") or [0, 0])
        failed = bool(drawing.get("triggered"))
        color = COLOR_FAIL if failed else COLOR_DISTANCE
        width = LINE_FAIL if failed else LINE_THIN
        cv2.line(img, start, end, color, width)
        cv2.circle(img, start, 3, color, -1)
        cv2.circle(img, end, 3, color, -1)

    @staticmethod
    def draw_item(img, drawing):
        points = TopContactsRenderer._points(drawing)
        failed = bool(drawing.get("triggered"))
        cv2.polylines(
            img,
            [points],
            True,
            COLOR_FAIL if failed else COLOR_PASS,
            LINE_FAIL if failed else LINE_THIN,
            lineType=cv2.LINE_AA,
        )

    @staticmethod
    def draw_count_item(img, drawing):
        points = TopContactsRenderer._points(drawing)
        cv2.polylines(img, [points], True, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)

    @staticmethod
    def draw_invalid_mask(img, drawing):
        points = TopContactsRenderer._points(drawing)
        cv2.polylines(img, [points], True, COLOR_FAIL, LINE_FAIL, lineType=cv2.LINE_AA)
        flat = points.reshape(-1, 2)
        x1 = int(flat[:, 0].min())
        x2 = int(flat[:, 0].max())
        y1 = int(flat[:, 1].min())
        y2 = int(flat[:, 1].max())
        cv2.line(img, (x1, y1), (x2, y2), COLOR_FAIL, LINE_FAIL)
        cv2.line(img, (x1, y2), (x2, y1), COLOR_FAIL, LINE_FAIL)

    @staticmethod
    def draw_ignored(img, drawing):
        points = TopContactsRenderer._points(drawing)
        raw_points = [tuple(map(int, point)) for point in points.reshape(-1, 2)]
        for index, start in enumerate(raw_points):
            end = raw_points[(index+1) % len(raw_points)]
            DrawPrimitives.draw_dashed_line(
                img,
                start,
                end,
                COLOR_SKIP,
                LINE_THIN,
                dash_len=6,
            )

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
