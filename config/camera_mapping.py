"""Stable camera-role mapping validation."""

from __future__ import annotations

import json


CAMERA_MAPPING_FILE = "camera_mapping.json"
REQUIRED_ROLES = {
    "INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
    "SPIDER_IN", "SPIDER_OUT", "TOP",
}


def validate_camera_mapping(mapping: dict, *, require_serial: bool = False) -> dict:
    if not isinstance(mapping, dict):
        raise ValueError("camera_mapping должен содержать объект")
    missing = REQUIRED_ROLES - set(mapping)
    extra = set(mapping) - REQUIRED_ROLES
    if missing or extra:
        raise ValueError(
            f"camera_mapping mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
        )
    normalized = {}
    serials = []
    for role, value in mapping.items():
        if isinstance(value, dict):
            index = value.get("index", value.get("device_index"))
            serial = value.get("serial")
            if require_serial and (not isinstance(serial, str) or not serial.strip()):
                raise ValueError(f"{role}: stable serial is required")
            if serial is not None:
                serials.append(str(serial).strip())
        else:
            index = value
            if require_serial:
                raise ValueError(f"{role}: integer USB index is not a stable serial")
        if type(index) is not int or index < 0:
            raise ValueError(f"{role}: camera index must be a non-negative int")
        normalized[role] = index
    indices = [
        value.get("index", value.get("device_index")) if isinstance(value, dict) else value
        for value in mapping.values()
    ]
    if len(indices) != len(set(indices)):
        raise ValueError("camera indices must be unique")
    if serials and len(serials) != len(set(serials)):
        raise ValueError("camera serials must be unique")
    return dict(normalized)


def load_camera_mapping(path: str | None = None, *, require_serial: bool = False) -> dict:
    path = path or CAMERA_MAPPING_FILE
    try:
        with open(path, encoding="utf-8") as stream:
            mapping = json.load(stream)
    except FileNotFoundError as exc:
        raise RuntimeError(f"[CAMERA_MAPPING] {path} не найден") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"[CAMERA_MAPPING] Ошибка чтения {path}: {exc}") from exc
    return validate_camera_mapping(mapping, require_serial=require_serial)
