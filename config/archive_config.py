"""Настройки хранения и сжатия архива партий.

Архив не является частью калибровки: калибровка описывает механику и камеры,
а этот файл — операторское место хранения и политику сохранения доказательств.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


ARCHIVE_CONFIG_FILE = "archive_config.json"
COMPRESSION_METHODS = ("deflated", "stored", "lzma")

DEFAULTS = {
    "enabled": True,
    "root_path": "archive",
    # 92 сохраняет текущую политику качества. Для экономии места оператор
    # может выбрать 88–90, не меняя структуру и состав доказательств.
    "jpeg_quality": 92,
    "zip_compression": "deflated",
    "zip_level": 6,
    "compress_on_shutdown": True,
    "delete_original_after_zip": False,
}


def _normalise_root(value) -> str:
    text = str(value or DEFAULTS["root_path"]).strip()
    if not text:
        text = DEFAULTS["root_path"]
    return os.path.expandvars(os.path.expanduser(text))


def normalise_archive_config(data: dict | None) -> dict:
    """Вернуть проверенную конфигурацию, добавив значения новых полей."""
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

    method = str(
        source.get("zip_compression", result["zip_compression"])
    ).lower()
    result["zip_compression"] = (
        method if method in COMPRESSION_METHODS else DEFAULTS["zip_compression"]
    )

    try:
        level = int(source.get("zip_level", result["zip_level"]))
    except (TypeError, ValueError):
        level = result["zip_level"]
    result["zip_level"] = max(0, min(9, level))

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
    """Загрузить настройки; при отсутствии/повреждении вернуть безопасные defaults."""
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
    """Атомарно сохранить настройки и вернуть нормализованный вариант."""
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
