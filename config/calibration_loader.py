import json
import math


DEFAULTS = {
    "conveyor_speed":         20000,
    "conveyor_accel":         6000,
    "dist1_open_position":    340,
    "dist2_bad_position":     0,
    "dist2_cleanup_position": 340,
    "drop_time":              0.8,
    "axis_speed":             300,
    "axis_accel":             100,
    "micro_steps":            500,
    "nudge_limit_steps":      2000,
    "pause_hold_speed":       2000,
    "nudge_hold_chunk_steps": 100,
    "jog_hold_steps":         1_000_000,
    "normal_steps":           19048,
}

_INTEGER_KEYS = tuple(key for key in DEFAULTS if key != "drop_time")


def _validate(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("calibration.json должен содержать объект")
    missing = set(DEFAULTS) - set(data)
    extra = set(data) - set(DEFAULTS)
    if missing or extra:
        raise ValueError(
            f"Неверные поля calibration: missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )
    for key in _INTEGER_KEYS:
        if type(data[key]) is not int:
            raise ValueError(f"{key} должен быть int")
    if type(data["drop_time"]) not in (int, float):
        raise ValueError("drop_time должен быть числом")
    if not math.isfinite(float(data["drop_time"])):
        raise ValueError("drop_time должен быть конечным")

    positive = (
        "conveyor_speed",
        "conveyor_accel",
        "dist1_open_position",
        "axis_speed",
        "axis_accel",
        "micro_steps",
        "nudge_limit_steps",
        "pause_hold_speed",
        "nudge_hold_chunk_steps",
        "jog_hold_steps",
        "normal_steps",
    )
    if any(data[key] <= 0 for key in positive):
        raise ValueError("Положительные calibration-параметры должны быть > 0")
    if not 1 <= data["micro_steps"] <= 5000:
        raise ValueError("micro_steps должен быть в диапазоне 1..5000")
    if not 1 <= data["nudge_limit_steps"] <= 5000:
        raise ValueError("nudge_limit_steps должен быть в диапазоне 1..5000")
    # Удержание в паузе идёт на пониженной скорости: на производственных
    # 20000 шаг/с весь бюджет коррекции выбирается за сотые доли секунды и
    # кнопку невозможно отпустить вовремя.
    if not 100 <= data["pause_hold_speed"] <= data["conveyor_speed"]:
        raise ValueError(
            "pause_hold_speed должен быть в диапазоне 100..conveyor_speed"
        )
    # Отпускание кнопки применяется на границе чанка: слишком крупный чанк
    # означал бы, что лента едет заметно дольше, чем держали кнопку.
    if not 1 <= data["nudge_hold_chunk_steps"] <= data["nudge_limit_steps"]:
        raise ValueError(
            "nudge_hold_chunk_steps должен быть в диапазоне "
            "1..nudge_limit_steps"
        )
    chunk_seconds = data["nudge_hold_chunk_steps"] / data["pause_hold_speed"]
    if chunk_seconds > 0.2:
        raise ValueError(
            "Чанк коррекции должен проходиться быстрее 0.2s, иначе лента "
            "заметно едет после отпускания кнопки"
        )
    if data["micro_steps"] > data["nudge_limit_steps"]:
        raise ValueError(
            "micro_steps не может превышать nudge_limit_steps: "
            "одно нажатие обязано укладываться в суммарный лимит коррекции"
        )
    # Коррекция обязана оставаться внутри одной ячейки. Иначе накопленное
    # смещение сдвинет деталь в соседнюю позицию, а current_step об этом
    # не узнает: логическая карта линии разойдётся с физикой.
    cell_steps = data["normal_steps"] * 2
    if data["nudge_limit_steps"] * 2 >= cell_steps:
        raise ValueError(
            "Суммарный ход коррекции (±nudge_limit_steps) должен быть строго "
            f"меньше шага ячейки {cell_steps}"
        )
    if not 10_000 <= data["jog_hold_steps"] <= 10_000_000:
        raise ValueError("jog_hold_steps должен быть в диапазоне 10000..10000000")
    if data["dist2_bad_position"] < 0 or data["dist2_cleanup_position"] < 0:
        raise ValueError("Позиции DIST2 не могут быть отрицательными")
    if data["dist2_bad_position"] == data["dist2_cleanup_position"]:
        raise ValueError("BAD и CLEANUP позиции должны различаться")
    if not 0.05 <= float(data["drop_time"]) <= 30.0:
        raise ValueError("drop_time должен быть в диапазоне 0.05..30 секунд")
    return dict(data)


def load_calibration(path: str = "calibration.json") -> dict:
    """Загрузить полную проверенную калибровку; unsafe defaults запрещены."""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Файл калибровки не найден: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Ошибка чтения {path}: {exc}") from exc
    result = _validate(data)
    print(f"[CALIB] Loaded and validated from {path}")
    return result
