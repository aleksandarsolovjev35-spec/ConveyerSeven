"""Архивация результатов инспекции каждой детали.

Для детали сохраняются исходные кадры, сырые оверлеи, кадры с разметкой
правил и ``meta.json``. При завершении партии каталог можно упаковать в
ZIP Deflate.

Структура::

    <root>/<date>/<batch>/
      batch.json
      stats.json            (на уровень выше, в <root>)
      GOOD/part_0001/
      BAD/part_0002/
      CLEANUP/part_0003/
        meta.json
        <ROLE>.jpg          — исходный кадр
        <ROLE>_raw.jpg      — сырые детекции нейросети (если есть)
        <ROLE>_debug.jpg    — разметка правил (если есть)
"""

import contextlib
import json
import os
import shutil
import time
import zipfile
from datetime import datetime

import cv2


class PartArchive:
    """Архиватор деталей: копит кадры по стадиям и пишет их на диск."""

    JPEG_QUALITY = 92
    SCHEMA_VERSION = 2
    CATEGORY_DIRS = {
        "GOOD": "GOOD",
        "BAD": "BAD",
        "CLEANUP": "CLEANUP",
    }
    CATEGORY_LABELS = {
        "GOOD": "ГОДНОЕ",
        "BAD": "БРАК",
        "CLEANUP": "ОЧИСТКА",
    }
    STATS_FILE = "stats.json"

    def __init__(
        self,
        root_folder: str = "archive",
        batch_id: str | None = None,
        enabled: bool = True,
        jpeg_quality: int = JPEG_QUALITY,
        # Параметры принимаются вызывающим API, но ZIP всегда использует
        # Deflate уровня 6.
        zip_compression: str = "deflated",
        zip_level: int = 6,
        compress_on_shutdown: bool = True,
        delete_original_after_zip: bool = True,
    ):
        self.root_folder = os.path.abspath(
            os.path.expandvars(os.path.expanduser(str(root_folder)))
        )
        self.enabled = bool(enabled)
        self.jpeg_quality = max(70, min(98, int(jpeg_quality)))
        # Простая опция: сжимать партию в один ZIP при завершении.
        self.compress_on_shutdown = bool(compress_on_shutdown)
        self.delete_original_after_zip = bool(delete_original_after_zip)

        if batch_id is None:
            batch_id = datetime.now().strftime("batch_%Y%m%d_%H%M%S")
        self.batch_id = self._safe_name(batch_id)
        self.date_folder = datetime.now().strftime("%Y-%m-%d")
        self.batch_started_at = datetime.now().isoformat(timespec="seconds")

        # Буфер хранит уже JPEG-encoded bytes по ролям.
        self._buffers: dict[int, dict] = {}
        self._archived: list[dict] = []
        self._batch_parts: list[dict] = []
        self._batch_stats = {"total": 0, "good": 0, "bad": 0, "cleanup": 0}
        self._finalized_count = 0

        self.stats: dict = self._load_stats()

        self.startup_error = None
        if self.enabled:
            try:
                os.makedirs(self.root_folder, exist_ok=True)
            except OSError as exc:
                self.startup_error = str(exc)

    # ---------- свойства ----------

    @property
    def batch_folder(self) -> str:
        return os.path.join(self.root_folder, self.date_folder, self.batch_id)

    @classmethod
    def normalise_category(cls, category: str) -> str:
        value = str(category or "").upper()
        return value if value in cls.CATEGORY_DIRS else "BAD"

    @classmethod
    def validate_root(cls, root_folder: str) -> dict:
        candidate = os.path.abspath(
            os.path.expandvars(os.path.expanduser(str(root_folder or "")))
        )
        if not candidate:
            raise ValueError("Папка архива не указана")
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(
                candidate,
                f".archive_write_test_{os.getpid()}_{time.time_ns()}",
            )
            with open(probe, "xb") as stream:
                stream.write(b"ok")
                stream.flush()
                os.fsync(stream.fileno())
            os.remove(probe)
            usage = shutil.disk_usage(candidate)
        except OSError as exc:
            raise ValueError(f"Папка архива недоступна: {exc}") from exc
        return {
            "path": candidate,
            "writable": True,
            "free_bytes": int(usage.free),
            "free_mb": round(usage.free / (1024 * 1024), 1),
        }

    def can_reconfigure(self) -> bool:
        return not self._buffers and not self._archived and not os.path.exists(
            self.batch_folder
        )

    def reconfigure(self, *, root_folder, enabled, jpeg_quality, **_ignored) -> dict:
        """Изменить настройки до начала партии (лишние kwargs игнорируются)."""
        if not self.can_reconfigure():
            raise RuntimeError(
                "Настройки архива можно менять только до начала партии"
            )
        checked = self.validate_root(root_folder)
        self.root_folder = checked["path"]
        self.startup_error = None
        self.enabled = bool(enabled)
        self.jpeg_quality = max(70, min(98, int(jpeg_quality)))
        self.stats = self._load_stats()
        if self.enabled:
            os.makedirs(self.root_folder, exist_ok=True)
        return self.get_settings()

    def get_settings(self, validate: bool = True) -> dict:
        validation = None
        if self.enabled and validate:
            try:
                validation = self.validate_root(self.root_folder)
            except ValueError as exc:
                validation = {
                    "path": self.root_folder,
                    "writable": False,
                    "error": str(exc),
                }
        return {
            "enabled": self.enabled,
            "root_path": self.root_folder,
            "jpeg_quality": self.jpeg_quality,
            "compress_on_shutdown": self.compress_on_shutdown,
            "delete_original_after_zip": self.delete_original_after_zip,
            "batch_id": self.batch_id,
            "batch_folder": self.batch_folder,
            "batch_stats": dict(self._batch_stats),
            "editable": self.can_reconfigure(),
            "validation": validation,
        }

    # ---------- public API ----------

    def store_frames(
        self,
        part_id: int,
        stage: str,
        raw_frames: dict,
        annotated_frames: dict,
        raw_overlay_frames: dict | None = None,
        run_frames=None,
        run_rule_results=None,
        run_vision_results=None,
    ):
        """Сохранить кадры стадии в буфер (JPEG-bytes)."""
        if not self.enabled:
            return

        buf = self._buffers.setdefault(part_id, {})

        for role, frame in (raw_frames or {}).items():
            buf.setdefault(role, {})["raw"] = self._encode_image(frame)

        for role, frame in (annotated_frames or {}).items():
            buf.setdefault(role, {})["debug"] = self._encode_image(frame)

        for role, frame in (raw_overlay_frames or {}).items():
            buf.setdefault(role, {})["raw_overlay"] = self._encode_image(frame)

    def finalize(
        self,
        part_id: int,
        category: str,
        decision: str,
        defects: list,
        step: int,
        extra: dict | None = None,
    ) -> str | None:
        """Записать все кадры детали и meta.json на диск."""
        if not self.enabled:
            return None

        requested_category = str(category or "").upper()
        stored_category = self.normalise_category(requested_category)
        folder_name = f"part_{part_id:04d}"
        folder_path = os.path.join(
            self.batch_folder, self.CATEGORY_DIRS[stored_category], folder_name,
        )
        os.makedirs(folder_path, exist_ok=True)

        roles_saved = []
        buf = self._buffers.get(part_id, {})
        for role, frames in buf.items():
            for kind, filename in (
                ("raw", f"{role}.jpg"),
                ("raw_overlay", f"{role}_raw.jpg"),
                ("debug", f"{role}_debug.jpg"),
            ):
                content = frames.get(kind)
                if content is not None:
                    self._save_image(content, os.path.join(folder_path, filename))
            roles_saved.append(role)

        now = datetime.now()
        meta = {
            "schema_version": self.SCHEMA_VERSION,
            "part_id": part_id,
            "batch_id": self.batch_id,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "timestamp": time.time(),
            "step": step,
            "category": stored_category,
            "category_label": self.CATEGORY_LABELS[stored_category],
            "requested_category": requested_category,
            "decision": decision,
            "defects": defects,
            "roles": roles_saved,
            "folder": folder_path.replace("\\", "/"),
        }
        if extra:
            meta.update(extra)

        meta_path = os.path.join(folder_path, "meta.json")
        self._write_json(meta_path, meta)

        self._buffers.pop(part_id, None)
        relative_folder = os.path.relpath(
            folder_path, self.batch_folder,
        ).replace("\\", "/")
        item = {
            "part_id": part_id,
            "category": stored_category,
            "decision": decision,
            "folder": folder_path.replace("\\", "/"),
            "relative_folder": relative_folder,
            "roles": roles_saved,
            "time": now.strftime("%H:%M:%S"),
        }
        self._archived.append(item)
        self._batch_parts.append(dict(item))
        self._finalized_count += 1

        self.stats["total"] = int(self.stats.get("total") or 0) + 1
        category_key = {
            "GOOD": "good", "BAD": "bad", "CLEANUP": "cleanup",
        }[stored_category]
        self.stats[category_key] = int(self.stats.get(category_key) or 0) + 1
        self._batch_stats["total"] += 1
        self._batch_stats[category_key] += 1
        self._save_stats()
        self._save_batch_manifest()

        print(
            f"[ARCHIVE] Деталь #{part_id} -> {folder_path} "
            f"({len(roles_saved)} ролей)"
        )
        return folder_path

    def get_part_info(self, part_id: int) -> dict | None:
        for item in self._archived:
            if item["part_id"] == part_id:
                return item
        return None

    def get_part_images(self, part_id: int) -> dict:
        info = self.get_part_info(part_id)
        if not info:
            return {}
        folder = info["folder"]
        result = {}
        for role in info.get("roles", []):
            entry = {}
            for kind, filename in (
                ("raw", f"{role}.jpg"),
                ("raw_overlay", f"{role}_raw.jpg"),
                ("debug", f"{role}_debug.jpg"),
            ):
                path = os.path.join(folder, filename)
                if os.path.exists(path):
                    entry[kind] = path
            if entry:
                result[role] = entry
        return result

    def get_batch_stats(self) -> dict:
        return dict(self._batch_stats)

    def get_stats(self) -> dict:
        return dict(self.stats)

    # ---------- статистика и манифест ----------

    def _load_stats(self) -> dict:
        stats = {"total": 0, "good": 0, "bad": 0, "cleanup": 0}
        if not self.enabled:
            return stats
        path = os.path.join(self.root_folder, self.STATS_FILE)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return stats
        if not isinstance(data, dict):
            return stats
        for key in stats:
            value = data.get(key)
            if isinstance(value, int) and value >= 0:
                stats[key] = value
        return stats

    def _save_stats(self):
        if not self.enabled:
            return
        self._write_json(
            os.path.join(self.root_folder, self.STATS_FILE), self.stats,
        )

    def _save_batch_manifest(self, status: str = "OPEN"):
        if not self.enabled:
            return
        os.makedirs(self.batch_folder, exist_ok=True)
        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "date": self.date_folder,
            "started_at": self.batch_started_at,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "root_path": self.root_folder,
            "counts": dict(self._batch_stats),
            "parts": [
                {
                    "part_id": item["part_id"],
                    "category": item["category"],
                    "decision": item["decision"],
                    "folder": item["relative_folder"],
                    "time": item["time"],
                }
                for item in self._batch_parts
            ],
        }
        self._write_json(os.path.join(self.batch_folder, "batch.json"), manifest)

    # ---------- сжатие ----------

    def compress(self, delete_original: bool | None = None) -> str | None:
        """Упаковать папку партии в один ZIP (deflated)."""
        if not self.enabled:
            return None
        if delete_original is None:
            delete_original = self.delete_original_after_zip

        batch_folder = self.batch_folder
        if not os.path.isdir(batch_folder):
            return None
        with os.scandir(batch_folder) as entries:
            if not any(entries):
                return None

        self._save_batch_manifest(status="CLOSED")
        zip_path = batch_folder + ".zip"
        temp_zip = zip_path + ".tmp"
        try:
            with zipfile.ZipFile(
                temp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6,
            ) as zf:
                for root, _dirs, files in os.walk(batch_folder):
                    for filename in files:
                        file_path = os.path.join(root, filename)
                        arcname = os.path.relpath(file_path, batch_folder)
                        zf.write(file_path, arcname)
            with zipfile.ZipFile(temp_zip, "r") as archive:
                bad = archive.testzip()
                if bad is not None:
                    raise RuntimeError(f"ZIP CRC failed: {bad}")
            os.replace(temp_zip, zip_path)
        except Exception as exc:
            print(f"[ARCHIVE] Ошибка сжатия: {exc}")
            if os.path.exists(temp_zip):
                with contextlib.suppress(OSError):
                    os.remove(temp_zip)
            return None

        if delete_original:
            with contextlib.suppress(Exception):
                shutil.rmtree(batch_folder)
        return zip_path

    # ---------- внутреннее ----------

    def _encode_image(self, frame) -> bytes:
        try:
            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality],
            )
        except Exception as exc:
            raise RuntimeError(f"Ошибка JPEG-кодирования: {exc}") from exc
        if not ok or encoded is None:
            raise RuntimeError("cv2.imencode вернул ошибку")
        return encoded.tobytes()

    @staticmethod
    def _save_image(content: bytes, path: str):
        temp_path = path + ".tmp"
        try:
            with open(temp_path, "xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        except Exception:
            if os.path.exists(temp_path):
                with contextlib.suppress(OSError):
                    os.remove(temp_path)
            raise

    @staticmethod
    def _write_json(path: str, payload):
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)

    @staticmethod
    def _safe_name(name: str) -> str:
        if not name:
            return "none"
        return (
            name.replace("/", "_")
            .replace("\\", "_")
            .replace(" ", "_")
            .replace(":", "_")
            [:50]
        )
