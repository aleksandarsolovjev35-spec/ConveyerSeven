"""Настройки хранения архива партий.

Упрощённая версия: папка хранения, качество JPEG и флаг «сжимать партию
при выходе». Метод/уровень ZIP убраны — используется один совместимый
deflated.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


ARCHIVE_CONFIG_FILE = "archive_config.json"

DEFAULTS = {
    "enabled": True,
    "root_path": "archive",
    "jpeg_quality": 92,
    "compress_on_shutdown": True,
    "delete_original_after_zip": True,
}


def _normalise_root(value) -> str:
    text = str(value or DEFAULTS["root_path"]).strip()
    if not text:
        text = DEFAULTS["root_path"]
    return os.path.expandvars(os.path.expanduser(text))


def normalise_archive_config(data: dict | None) -> dict:
    source = data if isinstance(data, dict) else {}
    result = dict(DEFAULTS)
    result["enabled"] = bool(source.get("enabled", result["enabled"]))
    result["root_path"] = _normalise_root(
        source.get("root_path", result["root_path"])
    )

    try:
        quality = int(source.get("jpeg_quality", result["jpeg_quality"]))
    except (TypeError, ValueError):
        quality = result["jpeg_quality"]
    result["jpeg_quality"] = max(70, min(98, quality))

    result["compress_on_shutdown"] = bool(
        source.get("compress_on_shutdown", result["compress_on_shutdown"])
    )
    result["delete_original_after_zip"] = bool(
        source.get(
            "delete_original_after_zip",
            result["delete_original_after_zip"],
        )
    )
    return result


def load_archive_config(path: str = ARCHIVE_CONFIG_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        result = normalise_archive_config(None)
        try:
            save_archive_config(path, result)
        except OSError as exc:
            print(f"[ARCHIVE] Не удалось создать {path}: {exc}")
        return result
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ARCHIVE] Ошибка чтения {path}: {exc}; используются defaults")
        return normalise_archive_config(None)

    result = normalise_archive_config(data)
    if result != data:
        try:
            save_archive_config(path, result)
        except OSError as exc:
            print(f"[ARCHIVE] Не удалось обновить {path}: {exc}")
    return result


def save_archive_config(path: str, data: dict) -> dict:
    result = normalise_archive_config(data)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, destination)
    return result
