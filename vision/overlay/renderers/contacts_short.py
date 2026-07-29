import cv2

from vision.overlay.renderers.contacts_long import (
    COLOR_BOTTOM_LINE,
    COLOR_TOP_LINE,
    ContactsLongRenderer,
)
from vision.overlay.renderers.primitives import (
    COLOR_FAIL,
    COLOR_SKIP,
    DrawPrimitives,
    LINE_FAIL,
    LINE_THIN,
)

COLOR_HEIGHT_MEASURE = (180, 100, 255)


class ContactsShortRenderer:
    @staticmethod
    def draw_item(img, drawing):
        ContactsLongRenderer.draw_item(img, drawing)

    @staticmethod
    def draw_count_item(img, drawing):
        ContactsLongRenderer.draw_count_item(img, drawing)

    @staticmethod
    def draw_invalid_mask(img, drawing):
        ContactsLongRenderer.draw_invalid_mask(img, drawing)

    @staticmethod
    def draw_ignored(img, drawing):
        ContactsLongRenderer.draw_ignored(img, drawing)

    @staticmethod
    def draw_level_line(img, drawing):
        x_start = int(drawing.get("x_start", 0))
        x_end = int(drawing.get("x_end", 0))
        x_a = int(drawing.get("x_a", 0))
        y_a = int(drawing.get("y_a", 0))
        x_b = int(drawing.get("x_b", 0))
        y_b = int(drawing.get("y_b", 0))
        label = drawing.get("label")
        color = COLOR_TOP_LINE if label == "T" else COLOR_BOTTOM_LINE
        failed = bool(drawing.get("triggered"))
        if failed:
            color = COLOR_FAIL
        width = LINE_FAIL if failed else LINE_THIN
        cv2.line(img, (x_a, y_a), (x_b, y_b), color, width)
        DrawPrimitives.draw_dashed_line(
            img, (x_start, y_a), (x_a, y_a), color, LINE_THIN, dash_len=4,
        )
        DrawPrimitives.draw_dashed_line(
            img, (x_b, y_b), (x_end, y_b), color, LINE_THIN, dash_len=4,
        )
        tolerance = int(round(float(drawing.get("tolerance") or 0)))
        if tolerance > 0:
            for offset in (-tolerance, tolerance):
                DrawPrimitives.draw_dashed_line(
                    img,
                    (x_a, y_a + offset),
                    (x_b, y_b + offset),
                    COLOR_SKIP,
                    LINE_THIN,
                    dash_len=4,
                )

    @staticmethod
    def draw_level_center(img, drawing):
        ContactsLongRenderer.draw_level_center(img, drawing)

    @staticmethod
    def draw_height_segment(img, drawing):
        x = int(drawing.get("x", 0))
        y_top = int(drawing.get("y_top", 0))
        y_bottom = int(drawing.get("y_bottom", 0))
        failed = bool(drawing.get("triggered"))
        color = COLOR_FAIL if failed else COLOR_HEIGHT_MEASURE
        width = LINE_FAIL if failed else LINE_THIN
        cv2.line(img, (x, y_top), (x, y_bottom), color, width)
        tick = 4
        cv2.line(img, (x-tick, y_top), (x+tick, y_top), color, width)
        cv2.line(img, (x-tick, y_bottom), (x+tick, y_bottom), color, width)

    @staticmethod
    def draw_omission_line(img, drawing):
        ContactsLongRenderer.draw_omission_line(img, drawing)

    @staticmethod
    def draw_omission_distance(img, drawing):
        ContactsLongRenderer.draw_omission_distance(img, drawing)

    @staticmethod
    def draw_omission_missing(img, drawing):
        ContactsLongRenderer.draw_omission_missing(img, drawing)

    @staticmethod
    def draw_inscribed_rect(img, drawing):
        ContactsLongRenderer.draw_inscribed_rect(img, drawing)
