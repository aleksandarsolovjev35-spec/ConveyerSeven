"""
Отрисовка ВСЕХ сырых детекций от нейросетей.
Показывает абсолютно всё что нашли модели,
независимо от правил.
"""

import cv2
import numpy as np


# Палитра — разные классы разными цветами
CLASS_COLORS = {
    "mechanics":        (0,   0,   255),
    "sinks":            (0,   100, 255),
    "contacts":         (0,   255, 0),
    "contacts-long":    (0,   200, 100),
    "flatness_short":   (255, 200, 0),
    "flatness":         (255, 150, 0),
    "platform":         (255, 0,   255),
    "glass":            (200, 100, 0),
    "output_glass":     (100, 0,   0),
    "omission-long":    (0,   255, 255),
    "omission-short":   (255, 255, 0),
    "shells":           (255, 255, 0),
    "objects":          (0,   100, 255),
    "pin":              (80,   100, 255),
    "case":             (100,   100, 255),
    "case_central":     (200,   100, 255),
}

DEFAULT_COLOR = (180, 180, 180)

LINE_THIN  = 1
MASK_ALPHA = 0.15


class RawOverlay:
    """
    Рисует ВСЕ сырые детекции на кадре.
    Каждый класс — своим цветом.
    Тонкие линии, без подписей.
    """

    @staticmethod
    def render(frame, detections: list) -> np.ndarray:
        img = frame.copy()

        for det in detections:
            cls_name = det.get("class", "?")
            bbox     = det.get("bbox")
            mask     = det.get("mask")

            color = CLASS_COLORS.get(cls_name, DEFAULT_COLOR)

            if mask and len(mask) >= 3:
                RawOverlay._draw_mask(img, mask, color)
            elif bbox:
                RawOverlay._draw_bbox(img, bbox, color)

        return img

    @staticmethod
    def _draw_mask(img, mask, color):
        pts = np.array(mask, dtype=np.int32)

        overlay = img.copy()
        cv2.fillPoly(overlay, [pts], color)
        cv2.addWeighted(
            overlay, MASK_ALPHA,
            img, 1 - MASK_ALPHA,
            0, img,
        )

        cv2.polylines(img, [pts], True, color, LINE_THIN, lineType=cv2.LINE_AA)

    @staticmethod
    def _draw_bbox(img, bbox, color):
        x1, y1, x2, y2 = map(int, bbox)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, LINE_THIN, lineType=cv2.LINE_AA)