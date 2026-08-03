"""Нормализация кадров перед инференсом.

Модели обучались на кадрах, снятых при одном освещении. Когда на линии
меняют свет (внутренний/наружный вид, вход, верх), яркость и контраст
кадра сдвигаются, уверенность YOLO падает ниже conf-порога, и детектор
«не видит» omission. CLAHE (контрастно-ограниченное выравнивание
гистограммы) по L-каналу LAB частично компенсирует и недосвет, и
пересвет, приводя картинку к более стабильному виду.

По умолчанию нормализация ВЫКЛЮЧЕНА, чтобы поведение системы не
менялось без явного решения. Включение и настройка — через env:

    VISION_NORMALIZE=1                 включить для всех ролей
    VISION_NORMALIZE_ROLES=SPIDER_IN,SPIDER_OUT
                                       включить только для этих ролей
    VISION_NORMALIZE_CLIP_LIMIT=2.0    сила выравнивания контраста
    VISION_NORMALIZE_TILE=8            размер тайла CLAHE (px)

Имена ролей: INPUT_LEFT, INPUT_RIGHT, SPIDER_LEFT, SPIDER_RIGHT,
SPIDER_IN, SPIDER_OUT, TOP.
"""

from __future__ import annotations

import os

import cv2
import numpy as np

ENV_ENABLED = "VISION_NORMALIZE"
ENV_ROLES = "VISION_NORMALIZE_ROLES"
ENV_CLIP_LIMIT = "VISION_NORMALIZE_CLIP_LIMIT"
ENV_TILE = "VISION_NORMALIZE_TILE"

ALL_ROLES = (
    "INPUT_LEFT",
    "INPUT_RIGHT",
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
)

_DEFAULT_CLIP_LIMIT = 2.0
_DEFAULT_TILE = 8


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        print(f"[VISION] {name}={raw!r} не число, используется {default}")
        return default
    if value < minimum:
        print(f"[VISION] {name}={value} меньше {minimum}, используется {default}")
        return default
    return value


def normalize_enabled(role: str) -> bool:
    """Включена ли нормализация для роли (env-конфигурация)."""
    if not _env_flag(ENV_ENABLED):
        return False
    raw_roles = os.environ.get(ENV_ROLES)
    if raw_roles is None:
        return True
    roles = {part.strip() for part in raw_roles.split(",") if part.strip()}
    return role in roles


def normalize_frame(
    frame,
    clip_limit: float = _DEFAULT_CLIP_LIMIT,
    tile_size: int = _DEFAULT_TILE,
):
    """CLAHE по L-каналу LAB.

    Принимает BGR-кадр uint8 (как от OpenCV). Если кадр не подходит под
    формат или преобразование падает, возвращает кадр без изменений —
    нормализация никогда не должна ронять инспекцию.
    """
    array = np.asarray(frame)
    if (
        array.ndim != 3
        or array.shape[2] != 3
        or array.dtype != np.uint8
    ):
        return frame
    try:
        lab = cv2.cvtColor(array, cv2.COLOR_BGR2LAB)
        l_ch, a_ch, b_ch = cv2.split(lab)
        clahe = cv2.createCLAHE(
            clipLimit=float(clip_limit),
            tileGridSize=(int(tile_size), int(tile_size)),
        )
        l_ch = clahe.apply(l_ch)
        merged = cv2.merge((l_ch, a_ch, b_ch))
        return cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)
    except Exception as exc:
        print(f"[VISION] Нормализация кадра не удалась: {type(exc).__name__}: {exc}")
        return frame


def normalize_for_role(frame, role: str):
    """Нормализовать кадр для роли, если это включено в конфигурации."""
    if not normalize_enabled(role):
        return frame
    return normalize_frame(
        frame,
        clip_limit=_env_float(ENV_CLIP_LIMIT, _DEFAULT_CLIP_LIMIT, minimum=0.1),
        tile_size=int(_env_float(ENV_TILE, _DEFAULT_TILE, minimum=1.0)),
    )
