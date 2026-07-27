# vision/overlay/renderers/primitives.py

import cv2
import numpy as np

# --- Цвета для debug ---
COLOR_PASS      = (0, 200, 0)
COLOR_FAIL      = (0, 0, 255)
COLOR_SKIP      = (128, 128, 128)
COLOR_AXIS      = (255, 255, 0)
COLOR_ANGLE_ARC = (0, 200, 255)
COLOR_INTERSECT = (0, 0, 255)
COLOR_HEIGHT    = (255, 180, 0)
COLOR_GLASS     = (200, 100, 0)
COLOR_PLATFORM  = (255, 0, 255)

COLOR_SINK_IGNORED = (120, 120, 120)
COLOR_FIT_LINE     = (0, 200, 255)
COLOR_FIT_LINE_BAD = (0, 100, 255)

COLOR_ACCENT    = (0, 200, 255)
COLOR_INDEX_BG  = (0, 0, 0)
COLOR_INDEX_FG  = (255, 255, 255)

PANEL_BG        = (20, 20, 20)
PANEL_ALPHA     = 0.75

FONT            = cv2.FONT_HERSHEY_SIMPLEX
FONT_S          = 0.4
FONT_M          = 0.5
FONT_L          = 0.6
THICK           = 1

LINE_THIN       = 1
LINE_NORMAL     = 1
LINE_FAIL       = 2

MASK_ALPHA      = 0.15

class DrawPrimitives:
    @staticmethod
    def pick_color(d):
        if d.get("triggered"):
            return COLOR_FAIL
        color_hint = d.get("color_hint")
        if color_hint == "glass":
            return COLOR_GLASS
        if color_hint == "platform":
            return COLOR_PLATFORM
        if color_hint == "skip":
            return COLOR_SKIP
        return COLOR_PASS

    @staticmethod
    def draw_rule_bbox(img, d):
        color = DrawPrimitives.pick_color(d)
        x1, y1, x2, y2 = map(int, d["bbox"])
        mask = d.get("mask")
        has_mask = mask and len(mask) >= 3
        thickness = LINE_FAIL if d.get("triggered") else LINE_THIN

        if has_mask:
            pts = np.array(mask, dtype=np.int32)
            overlay = img.copy()
            fill_color = COLOR_FAIL if d.get("triggered") else (60, 60, 60)
            cv2.fillPoly(overlay, [pts], fill_color)
            cv2.addWeighted(overlay, MASK_ALPHA, img, 1 - MASK_ALPHA, 0, img)
            cv2.polylines(img, [pts], True, color, thickness)
        else:
            cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)

    @staticmethod
    def draw_index_badge(img, x, y, index, color):
        text = str(index)
        (tw, th), _ = cv2.getTextSize(text, FONT, FONT_S, THICK)
        pad = 2
        cv2.rectangle(img, (x - pad, y - th - pad), (x + tw + pad, y + pad), color, -1)
        cv2.putText(img, text, (x, y), FONT, FONT_S, COLOR_INDEX_FG, THICK)

    @staticmethod
    def draw_text_with_bg(img, text, pos, color, font_scale=FONT_S, bg_pad=2, center_x=False):
        x, y = pos
        (tw, th), _ = cv2.getTextSize(text, FONT, font_scale, THICK)
        if center_x:
            x = x - tw // 2
        x1, y1, x2, y2 = x - bg_pad, y - th - bg_pad, x + tw + bg_pad, y + bg_pad
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 0), -1)
        cv2.putText(img, text, (x, y), FONT, font_scale, color, THICK)

    @staticmethod
    def draw_dashed_line(img, pt1, pt2, color, thickness, dash_len=8):
        x1, y1 = pt1
        x2, y2 = pt2
        dist = np.hypot(x2 - x1, y2 - y1)
        if dist < 1:
            return
        dashes = int(dist / dash_len)
        if dashes < 1:
            cv2.line(img, pt1, pt2, color, thickness)
            return
        for i in range(0, dashes, 2):
            t1, t2 = i / dashes, min((i + 1) / dashes, 1.0)
            p1 = (int(x1 + (x2 - x1) * t1), int(y1 + (y2 - y1) * t1))
            p2 = (int(x1 + (x2 - x1) * t2), int(y1 + (y2 - y1) * t2))
            cv2.line(img, p1, p2, color, thickness)