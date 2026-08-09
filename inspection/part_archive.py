"""
Архивация результатов инспекции каждой детали.

Для каждой детали создаётся папка с:
  - сырыми кадрами со всех камер
  - кадрами с сырыми детекциями нейросети (raw overlay)
  - аннотированными кадрами (правила)
  - meta.json с метаданными

Структура (во время работы):
  <root>/<date>/<batch>/
    batch.json
    GOOD/part_<id>/
    BAD/part_<id>/
    CLEANUP/part_<id>/
      meta.json
      <ROLE>.jpg
      <ROLE>_raw.jpg
      <ROLE>_debug.jpg

После завершения работы compress() упаковывает папку партии в:
  <root>/<date>/<batch>.zip
и при включённой политике удаляет распакованную папку.
"""

import contextlib
import hashlib
import uuid
import json
import os
import shutil
import time
import zipfile
from datetime import datetime

import cv2


class PartArchive:
    """
    Архиватор деталей.
    Накапливает кадры по стадиям, при финализации сохраняет всё на диск.
    При завершении работы сжимает папку партии в zip.
    """

    JPEG_QUALITY = 92
    SCHEMA_VERSION = 3
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

    # Накопительная статистика по корпусам (годные / брак / очистка):
    # ведётся между запусками в <root>/stats.json.
    STATS_FILE = "stats.json"

    def __init__(
        self,
        root_folder: str = "archive",
        batch_id: str | None = None,
        enabled: bool = True,
        jpeg_quality: int = JPEG_QUALITY,
        zip_compression: str = "deflated",
        zip_level: int = 6,
        compress_on_shutdown: bool = True,
        delete_original_after_zip: bool = False,
        reserve_bytes: int = 512 * 1024 * 1024,
        identity_manifest: dict | None = None,
    ):
        self.root_folder = os.path.abspath(
            os.path.expandvars(os.path.expanduser(str(root_folder)))
        )
        self.enabled = bool(enabled)
        self.jpeg_quality = max(70, min(98, int(jpeg_quality)))
        self.zip_compression = str(zip_compression).lower()
        if self.zip_compression not in ("deflated", "stored", "lzma"):
            self.zip_compression = "deflated"
        self.zip_level = max(0, min(9, int(zip_level)))
        self.compress_on_shutdown = bool(compress_on_shutdown)
        self.delete_original_after_zip = bool(delete_original_after_zip)
        if type(reserve_bytes) is not int or reserve_bytes < 0:
            raise ValueError("reserve_bytes must be a non-negative int")
        self.reserve_bytes = int(reserve_bytes)
        self.identity_manifest = self._json_safe(identity_manifest or {})

        if batch_id is None:
            batch_id = (
                datetime.now().strftime("batch_%Y%m%d_%H%M%S")
                + f"_{os.getpid()}_{uuid.uuid4().hex[:10]}"
            )
        self.batch_id = self._safe_name(batch_id)

        self.date_folder = datetime.now().strftime("%Y-%m-%d")
        self.batch_started_at = datetime.now().isoformat(timespec="seconds")

        # Буфер хранит уже JPEG-encoded bytes, а не тяжёлые numpy frames.
        self._buffers: dict[int, dict] = {}

        # Список архивированных деталей (для UI). Он живёт в памяти текущего
        # запуска; batch.json является постоянным индексом партии.
        self._archived: list[dict] = []
        self._batch_parts: list[dict] = []
        self._runs: list[dict] = []
        self._batch_stats = {"total": 0, "good": 0, "bad": 0, "cleanup": 0}
        # Сырые выходы моделей хранятся отдельно от debug-кадров: их можно
        # просматривать и превращать в датасет, не обучаясь на красных
        # обводках и прочей визуальной разметке.
        self._vision_buffers: dict[int, dict[str, list]] = {}
        self._frame_stage_buffers: dict[int, dict[str, str]] = {}

        # Счётчик сохранённых деталей
        self._finalized_count = 0

        # Статистика по корпусам: восстанавливается из stats.json,
        # обновляется при каждой финализации.
        self.stats: dict = self._load_stats()

        self.startup_error = None
        if self.enabled:
            try:
                os.makedirs(self.root_folder, exist_ok=True)
            except OSError as exc:
                # Не роняем splash: оператор должен иметь возможность
                # выбрать другой диск в настройках архива.
                self.startup_error = str(exc)
            else:
                self._load_committed_parts()

    @property
    def batch_folder(self) -> str:
        return os.path.join(self.root_folder, self.date_folder, self.batch_id)

    @classmethod
    def normalise_category(cls, category: str) -> str:
        """Свести маршрут к одному из трёх архивных разделов.

        Неизвестный/аварийный маршрут попадает в BAD, но исходное значение
        сохраняется в meta.json как requested_category.
        """
        value = str(category or "").upper()
        return value if value in cls.CATEGORY_DIRS else "BAD"

    @classmethod
    def validate_root(cls, root_folder: str) -> dict:
        """Проверить, что каталог можно создать и в него можно писать."""
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
        """Путь можно менять до появления данных текущей партии."""
        return not self._buffers and not self._archived and not os.path.exists(
            self.batch_folder
        )

    def reconfigure(
        self,
        *,
        root_folder: str,
        enabled: bool,
        jpeg_quality: int,
        zip_compression: str,
        zip_level: int,
        compress_on_shutdown: bool,
        delete_original_after_zip: bool,
        reserve_bytes: int | None = None,
    ) -> dict:
        if not self.can_reconfigure():
            raise RuntimeError(
                "Настройки архива можно менять только до начала партии"
            )
        checked = self.validate_root(root_folder)
        self.root_folder = checked["path"]
        self.startup_error = None
        self.enabled = bool(enabled)
        self.jpeg_quality = max(70, min(98, int(jpeg_quality)))
        self.zip_compression = str(zip_compression).lower()
        if self.zip_compression not in ("deflated", "stored", "lzma"):
            raise ValueError("Недопустимый метод сжатия архива")
        self.zip_level = max(0, min(9, int(zip_level)))
        self.compress_on_shutdown = bool(compress_on_shutdown)
        self.delete_original_after_zip = bool(delete_original_after_zip)
        if reserve_bytes is not None:
            if type(reserve_bytes) is not int or reserve_bytes < 0:
                raise ValueError("reserve_bytes must be a non-negative int")
            self.reserve_bytes = reserve_bytes
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
                validation = {"path": self.root_folder, "writable": False, "error": str(exc)}
        return {
            "enabled": self.enabled,
            "root_path": self.root_folder,
            "jpeg_quality": self.jpeg_quality,
            "zip_compression": self.zip_compression,
            "zip_level": self.zip_level,
            "compress_on_shutdown": self.compress_on_shutdown,
            "delete_original_after_zip": self.delete_original_after_zip,
            "reserve_bytes": self.reserve_bytes,
            "batch_id": self.batch_id,
            "batch_folder": self.batch_folder,
            "batch_stats": dict(self._batch_stats),
            "editable": self.can_reconfigure(),
            "validation": validation,
        }

    # Public API

    def storage_status(self) -> dict:
        if not self.enabled:
            return {"enabled": False, "ok": True, "free_bytes": None, "reserve_bytes": self.reserve_bytes}
        try:
            usage = shutil.disk_usage(self.root_folder)
        except OSError as exc:
            raise RuntimeError(f"archive disk unavailable: {exc}") from exc
        return {
            "enabled": True, "path": self.root_folder,
            "free_bytes": int(usage.free),
            "reserve_bytes": self.reserve_bytes,
            "ok": int(usage.free) > self.reserve_bytes,
        }

    def require_space(self):
        status = self.storage_status()
        if not status["ok"]:
            raise RuntimeError(
                f"archive disk reserve exhausted: free={status['free_bytes']} "
                f"reserve={status['reserve_bytes']}"
            )
        return status

    def store_frames(
        self,
        part_id: int,
        stage: str,
        raw_frames: dict,
        annotated_frames: dict,
        raw_overlay_frames: dict | None = None,
        run_frames: list | None = None,
        run_rule_results: list | None = None,
        run_vision_results: list | None = None,
    ):
        """
        Сохранить кадры одной стадии инспекции в буфер.

        Args:
            part_id: номер детали.
            stage: "input" или "spider".
            raw_frames: {role: frame} — чистые кадры.
            annotated_frames: {role: frame} — обрисовка правил.
            raw_overlay_frames: {role: frame} — сырые детекции нейросети.
            run_frames: список словарей кадров прогонов (один элемент).
            run_rule_results: список списков результатов правил по прогонам.
            run_vision_results: список словарей детекций по прогонам.
        """
        if not self.enabled:
            return

        if part_id not in self._buffers:
            self._buffers[part_id] = {}

        buf = self._buffers[part_id]
        stage_key = str(stage or "unknown").lower()
        stage_map = self._frame_stage_buffers.setdefault(part_id, {})

        if run_vision_results:
            self._vision_buffers.setdefault(part_id, {})[stage_key] = [
                self._json_safe(run_result)
                for run_result in run_vision_results
            ]

        for role, frame in raw_frames.items():
            stage_map[role] = stage_key
            if role not in buf:
                buf[role] = {}
            buf[role]["raw"] = self._encode_image(frame)

        for role, frame in annotated_frames.items():
            stage_map[role] = stage_key
            if role not in buf:
                buf[role] = {}
            buf[role]["debug"] = self._encode_image(frame)

        if raw_overlay_frames:
            for role, frame in raw_overlay_frames.items():
                stage_map[role] = stage_key
                if role not in buf:
                    buf[role] = {}
                buf[role]["raw_overlay"] = self._encode_image(frame)

        # Сохранение набора прогонов для каждого ракурса
        if run_frames:
            for idx, r_frames in enumerate(run_frames):
                run_num = idx + 1
                r_rules = run_rule_results[idx] if (run_rule_results and idx < len(run_rule_results)) else []
                r_vision = run_vision_results[idx] if (run_vision_results and idx < len(run_vision_results)) else {}

                for role, frame in r_frames.items():
                    stage_map[role] = stage_key
                    if role not in buf:
                        buf[role] = {}

                    # 1. Сырой кадр прогона
                    buf[role][f"raw_run{run_num}"] = self._encode_image(frame)

                    # 2. Обрисовка правил прогона
                    try:
                        from vision.overlay.debug_overlay import DebugOverlay
                        debug_frame = DebugOverlay.render_frame(frame, role, r_rules)
                    except Exception as e:
                        print(f"[ARCHIVE] Error rendering debug frame for {role} run {run_num}: {e}")
                        debug_frame = frame
                    buf[role][f"debug_run{run_num}"] = self._encode_image(debug_frame)

                    # 3. Сырые детекции прогона
                    r_dets = r_vision.get(role, []) if isinstance(r_vision, dict) else []
                    try:
                        from vision.overlay.raw_overlay import RawOverlay
                        if r_dets:
                            raw_overlay_frame = RawOverlay.render(frame, r_dets)
                        else:
                            raw_overlay_frame = frame.copy()
                    except Exception as e:
                        print(f"[ARCHIVE] Error rendering raw overlay for {role} run {run_num}: {e}")
                        raw_overlay_frame = frame
                    buf[role][f"raw_overlay_run{run_num}"] = self._encode_image(raw_overlay_frame)

    def finalize(
        self,
        part_id: int,
        category: str,
        decision: str,
        defects: list,
        step: int,
        extra: dict | None = None,
    ) -> str | None:
        """Durably commit one part through staging -> marker -> atomic rename.

        A committed part is independently recoverable.  Staging directories,
        even those containing a marker, are never auto-promoted.
        """
        if not self.enabled:
            return None
        self.require_space()
        requested_category = str(category or "").upper()
        stored_category = self.normalise_category(requested_category)
        folder_name = f"part_{part_id:04d}"
        parts_root = os.path.join(self.batch_folder, "parts")
        staging_root = os.path.join(self.batch_folder, "staging")
        final_folder = os.path.join(parts_root, self.CATEGORY_DIRS[stored_category], folder_name)
        if os.path.isdir(final_folder) and self._verify_committed_part(final_folder):
            # Idempotent recovery of a commit that completed before the
            # caller observed the result.  Never re-run physical/archive
            # writes and never auto-promote staging.
            self._load_committed_parts()
            self._buffers.pop(part_id, None)
            self._vision_buffers.pop(part_id, None)
            self._frame_stage_buffers.pop(part_id, None)
            return final_folder

        stage_folder = os.path.join(staging_root, f"{folder_name}_{uuid.uuid4().hex}")
        os.makedirs(stage_folder, exist_ok=False)
        buf = self._buffers.get(part_id, {})
        roles_saved = []
        annotation_files = []
        try:
            for role, frames in buf.items():
                for field, suffix in (("raw", ".jpg"), ("raw_overlay", "_raw.jpg"), ("debug", "_debug.jpg")):
                    content = frames.get(field)
                    if content is not None:
                        self._save_image(content, os.path.join(stage_folder, f"{role}{suffix}"))
                for run in (1, 2, 3):
                    for field, suffix in ((f"raw_run{run}", f"_run{run}.jpg"),
                                          (f"raw_overlay_run{run}", f"_run{run}_raw.jpg"),
                                          (f"debug_run{run}", f"_run{run}_debug.jpg")):
                        content = frames.get(field)
                        if content is not None:
                            self._save_image(content, os.path.join(stage_folder, f"{role}{suffix}"))
                roles_saved.append(role)

            annotation_files = self._save_vision_annotations(part_id, stage_folder)
            sample_records = self._build_training_samples(
                part_id=part_id, folder_path=stage_folder, category=stored_category,
                decision=decision, annotation_files=annotation_files,
            )
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
                "defects": list(defects or []),
                "roles": roles_saved,
                "folder": os.path.join(
                    "parts", self.CATEGORY_DIRS[stored_category], folder_name
                ).replace("\\", "/"),
                "training": {
                    "raw_images": [f"{role}.jpg" for role in roles_saved if os.path.exists(os.path.join(stage_folder, f"{role}.jpg"))],
                    "pseudo_annotation_files": annotation_files,
                    "annotations_are_model_predictions": True,
                    "sample_count": len(sample_records),
                    "samples_index": "../samples.jsonl",
                },
            }
            if extra:
                meta.update(self._json_safe(extra))
            self._write_json_durable(os.path.join(stage_folder, "meta.json"), meta)

            manifest = self._build_part_manifest(stage_folder)
            self._write_json_durable(os.path.join(stage_folder, "manifest.json"), manifest)
            # Rebuild after manifest itself exists, excluding the marker.
            manifest = self._build_part_manifest(stage_folder)
            self._write_json_durable(os.path.join(stage_folder, "manifest.json"), manifest)
            self._write_json_durable(
                os.path.join(stage_folder, "commit.marker"),
                {"schema_version": self.SCHEMA_VERSION, "part_id": part_id, "manifest_sha256": manifest["manifest_sha256"]},
            )
            self._fsync_tree(stage_folder)
            os.makedirs(os.path.dirname(final_folder), exist_ok=True)
            os.replace(stage_folder, final_folder)
            self._fsync_directory(os.path.dirname(final_folder))
            self._fsync_directory(os.path.dirname(os.path.dirname(final_folder)))
            if not self._verify_committed_part(final_folder):
                raise RuntimeError("committed part manifest verification failed")

            # The sample builder ran while the part lived in staging.  Rewrite
            # only its relative path prefix after the atomic rename.
            stage_rel = os.path.relpath(stage_folder, self.batch_folder).replace("\\", "/")
            final_rel = os.path.relpath(final_folder, self.batch_folder).replace("\\", "/")
            for sample in sample_records:
                for key in ("image", "annotation"):
                    value = sample.get(key)
                    if isinstance(value, str) and value.startswith(stage_rel + "/"):
                        sample[key] = final_rel + value[len(stage_rel):]

            # The secondary samples index is written only after the part is
            # committed; a failure here leaves the committed part intact and
            # is surfaced to the caller as a traceability fault.
            self._append_training_samples(sample_records)
            # Read-only compatibility alias for legacy viewers.  The canonical
            # committed catalog remains exclusively under parts/; a symlink
            # does not create a second production copy.
            legacy_folder = os.path.join(
                self.batch_folder, self.CATEGORY_DIRS[stored_category], folder_name
            )
            try:
                os.makedirs(os.path.dirname(legacy_folder), exist_ok=True)
                os.symlink(
                    os.path.relpath(final_folder, os.path.dirname(legacy_folder)),
                    legacy_folder,
                    target_is_directory=True,
                )
            except (OSError, NotImplementedError):
                legacy_folder = final_folder
            self._buffers.pop(part_id, None)
            self._vision_buffers.pop(part_id, None)
            self._frame_stage_buffers.pop(part_id, None)
            relative_folder = os.path.relpath(final_folder, self.batch_folder).replace("\\", "/")
            archived_item = {
                "part_id": part_id, "category": stored_category, "decision": decision,
                "folder": legacy_folder.replace("\\", "/"),
                "committed_folder": final_folder.replace("\\", "/"),
                "relative_folder": relative_folder, "roles": roles_saved,
                "annotation_files": annotation_files, "sample_count": len(sample_records),
                "time": now.strftime("%H:%M:%S"),
                "manifest": os.path.join(final_folder, "manifest.json").replace("\\", "/"),
            }
            self._archived.append(archived_item)
            self._batch_parts.append(dict(archived_item))
            self._finalized_count += 1
            category_key = {"GOOD": "good", "BAD": "bad", "CLEANUP": "cleanup"}[stored_category]
            self.stats["total"] = int(self.stats.get("total") or 0) + 1
            self.stats[category_key] = int(self.stats.get(category_key) or 0) + 1
            self._batch_stats["total"] += 1
            self._batch_stats[category_key] += 1
            self._save_stats()
            self._save_batch_manifest()
            return final_folder
        except Exception:
            # Keep staging and all in-memory evidence for best-effort abort
            # preservation.  Never silently turn a failed archive into success.
            raise

    def _build_part_manifest(self, folder_path: str) -> dict:
        files = {}
        for root, _dirs, names in os.walk(folder_path):
            for name in sorted(names):
                if name in {"commit.marker", "manifest.json"}:
                    continue
                path = os.path.join(root, name)
                relative = os.path.relpath(path, folder_path).replace("\\", "/")
                files[relative] = self._sha256_file(path)
        payload = {"schema_version": self.SCHEMA_VERSION, "files": files}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
        return payload

    @classmethod
    def _verify_committed_part(cls, folder_path: str) -> bool:
        try:
            with open(os.path.join(folder_path, "manifest.json"), encoding="utf-8") as stream:
                manifest = json.load(stream)
            with open(os.path.join(folder_path, "commit.marker"), encoding="utf-8") as stream:
                marker = json.load(stream)
            if marker.get("manifest_sha256") != manifest.get("manifest_sha256"):
                return False
            files = manifest.get("files")
            if not isinstance(files, dict):
                return False
            actual_files = set()
            for root, _dirs, names in os.walk(folder_path):
                for name in names:
                    if name in {"commit.marker", "manifest.json"}:
                        continue
                    actual_files.add(os.path.relpath(os.path.join(root, name), folder_path).replace("\\", "/"))
            if actual_files != set(files):
                return False
            for relative, expected in files.items():
                path = os.path.join(folder_path, relative)
                if not os.path.isfile(path) or cls._sha256_file(path) != expected:
                    return False
            canonical = {"schema_version": manifest.get("schema_version"), "files": files}
            digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            return digest == manifest.get("manifest_sha256")
        except Exception:
            return False

    @staticmethod
    def _sha256_file(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _write_json_durable(path: str, payload: dict):
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    @staticmethod
    def _fsync_directory(path: str):
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    @classmethod
    def _fsync_tree(cls, path: str):
        for root, dirs, files in os.walk(path):
            for name in files:
                with open(os.path.join(root, name), "rb") as stream:
                    os.fsync(stream.fileno())
            cls._fsync_directory(root)

    def _load_committed_parts(self):
        """Load only verified ``parts/`` directories; never promote staging."""
        root = os.path.join(self.batch_folder, "parts")
        if not os.path.isdir(root):
            return
        for category in self.CATEGORY_DIRS.values():
            category_root = os.path.join(root, category)
            if not os.path.isdir(category_root):
                continue
            for name in os.listdir(category_root):
                if not name.startswith("part_"):
                    continue
                folder = os.path.join(category_root, name)
                if not self._verify_committed_part(folder):
                    continue
                try:
                    part_id = int(name.split("_", 1)[1])
                except (IndexError, ValueError):
                    continue
                meta = {}
                try:
                    with open(os.path.join(folder, "meta.json"), encoding="utf-8") as stream:
                        meta = json.load(stream)
                except (OSError, ValueError):
                    continue
                item = {
                    "part_id": part_id,
                    "category": meta.get("category", category),
                    "decision": meta.get("decision", "unknown"),
                    "folder": folder.replace("\\", "/"),
                    "relative_folder": os.path.relpath(folder, self.batch_folder).replace("\\", "/"),
                    "roles": list(meta.get("roles") or []),
                    "annotation_files": list((meta.get("training") or {}).get("pseudo_annotation_files") or []),
                    "sample_count": int((meta.get("training") or {}).get("sample_count") or 0),
                    "time": meta.get("time"),
                    "manifest": os.path.join(folder, "manifest.json").replace("\\", "/"),
                }
                if not any(existing.get("part_id") == part_id for existing in self._archived):
                    self._archived.append(item)
                    self._batch_parts.append(dict(item))
                    category_key = {"GOOD": "good", "BAD": "bad", "CLEANUP": "cleanup"}.get(
                        str(item.get("category")).upper()
                    )
                    if category_key:
                        self._batch_stats["total"] += 1
                        self._batch_stats[category_key] += 1
                    self._finalized_count += 1

    def get_part_info(self, part_id: int) -> dict | None:
        """Получить информацию об архивированной детали."""
        for item in self._archived:
            if item["part_id"] == part_id:
                return item
        return None

    def get_part_images(self, part_id: int) -> dict:
        """
        Получить пути к изображениям детали.

        Returns:
            {role: {"raw": path, "raw_overlay": path, "debug": path, "raw_run1": path, ...}, ...}
        """
        info = self.get_part_info(part_id)
        if not info:
            return {}

        folder = info["folder"]
        result = {}

        for role in info.get("roles", []):
            entry = {}

            raw_path = os.path.join(folder, f"{role}.jpg")
            if os.path.exists(raw_path):
                entry["raw"] = raw_path

            raw_overlay_path = os.path.join(folder, f"{role}_raw.jpg")
            if os.path.exists(raw_overlay_path):
                entry["raw_overlay"] = raw_overlay_path

            debug_path = os.path.join(folder, f"{role}_debug.jpg")
            if os.path.exists(debug_path):
                entry["debug"] = debug_path

            # Пути к изображениям по прогонам (1, 2, 3)
            for r in (1, 2, 3):
                r_raw_path = os.path.join(folder, f"{role}_run{r}.jpg")
                if os.path.exists(r_raw_path):
                    entry[f"raw_run{r}"] = r_raw_path

                r_raw_overlay_path = os.path.join(folder, f"{role}_run{r}_raw.jpg")
                if os.path.exists(r_raw_overlay_path):
                    entry[f"raw_overlay_run{r}"] = r_raw_overlay_path

                r_debug_path = os.path.join(folder, f"{role}_run{r}_debug.jpg")
                if os.path.exists(r_debug_path):
                    entry[f"debug_run{r}"] = r_debug_path

            if entry:
                result[role] = entry

        return result

    def _save_vision_annotations(self, part_id: int, folder_path: str) -> list[str]:
        """Сохранить сырые выходы моделей рядом с кадрами для датасета.

        Это именно pseudo-labels: модельные детекции не считаются
        подтверждённой разметкой и должны быть проверены человеком перед
        дообучением. JSON оставляет маски, bbox, confidence и model_path,
        поэтому позже его можно конвертировать в YOLO/COCO без повторного
        запуска камер.
        """
        records = self._vision_buffers.get(part_id, {})
        saved = []
        for stage, runs in records.items():
            for run_index, run_results in enumerate(runs, start=1):
                if not isinstance(run_results, dict):
                    continue
                for role, detections in run_results.items():
                    safe_role = self._safe_name(role)
                    filename = (
                        f"{stage}_{safe_role}_run{run_index}_detections.json"
                    )
                    path = os.path.join(folder_path, filename)
                    payload = {
                        "schema_version": 1,
                        "kind": "model_predictions",
                        "part_id": part_id,
                        "batch_id": self.batch_id,
                        "stage": stage,
                        "role": role,
                        "run": run_index,
                        "image": f"{role}_run{run_index}.jpg",
                        "pseudo_labels": True,
                        "detections": self._json_safe(detections or []),
                    }
                    temp_path = path + ".tmp"
                    with open(temp_path, "w", encoding="utf-8") as stream:
                        json.dump(payload, stream, indent=2, ensure_ascii=False)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp_path, path)
                    saved.append(filename)
        return saved

    @staticmethod
    def _json_safe(value):
        """Преобразовать numpy/tuple-значения в стабильный JSON."""
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            return value if value == value and abs(value) != float("inf") else None
        if isinstance(value, dict):
            return {str(key): PartArchive._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [PartArchive._json_safe(item) for item in value]
        tolist = getattr(value, "tolist", None)
        if callable(tolist):
            return PartArchive._json_safe(tolist())
        item = getattr(value, "item", None)
        if callable(item):
            try:
                return PartArchive._json_safe(item())
            except Exception:
                pass
        return str(value)

    def _build_training_samples(
        self,
        *,
        part_id: int,
        folder_path: str,
        category: str,
        decision: str,
        annotation_files: list[str],
    ) -> list[dict]:
        """Построить лёгкие ссылки на raw-кадры и их pseudo-labels."""
        records = self._vision_buffers.get(part_id, {})
        stage_map = self._frame_stage_buffers.get(part_id, {})
        annotation_set = set(annotation_files)
        samples = []
        used_images = set()
        used_roles = set()
        part_folder = os.path.relpath(
            folder_path, self.batch_folder,
        ).replace("\\", "/")

        for stage, runs in records.items():
            for run_index, run_results in enumerate(runs, start=1):
                if not isinstance(run_results, dict):
                    continue
                for role in run_results:
                    safe_role = self._safe_name(role)
                    annotation_name = (
                        f"{stage}_{safe_role}_run{run_index}_detections.json"
                    )
                    image_name = f"{role}_run{run_index}.jpg"
                    if not os.path.exists(os.path.join(folder_path, image_name)):
                        image_name = f"{role}.jpg"
                    image_path = os.path.join(folder_path, image_name)
                    if not os.path.exists(image_path):
                        continue
                    if image_name in used_images:
                        continue
                    used_images.add(image_name)
                    used_roles.add(role)
                    samples.append({
                        "schema_version": 1,
                        "sample_id": f"{part_id}:{stage}:{role}:{run_index}",
                        "part_id": part_id,
                        "batch_id": self.batch_id,
                        "category": category,
                        "decision": decision,
                        "stage": stage,
                        "role": role,
                        "run": run_index,
                        "image": f"{part_folder}/{image_name}",
                        "annotation": (
                            f"{part_folder}/{annotation_name}"
                            if annotation_name in annotation_set else None
                        ),
                        "labels": (
                            "pseudo" if annotation_name in annotation_set
                            else "unannotated"
                        ),
                        "verified": False,
                    })

        # Если для роли нет model_predictions, raw evidence всё равно остаётся
        # полноценным кандидатом для ручной разметки.
        for role in self._buffers.get(part_id, {}):
            if role in used_roles:
                continue
            image_name = f"{role}.jpg"
            image_path = os.path.join(folder_path, image_name)
            if not os.path.exists(image_path) or image_name in used_images:
                continue
            used_images.add(image_name)
            stage = stage_map.get(role, "unknown")
            samples.append({
                "schema_version": 1,
                "sample_id": f"{part_id}:{stage}:{role}:evidence",
                "part_id": part_id,
                "batch_id": self.batch_id,
                "category": category,
                "decision": decision,
                "stage": stage,
                "role": role,
                "run": None,
                "image": f"{part_folder}/{image_name}",
                "annotation": None,
                "labels": "unannotated",
                "verified": False,
            })
        return samples

    def _append_training_samples(self, samples: list[dict]):
        if not self.enabled or not samples:
            return
        path = os.path.join(self.batch_folder, "samples.jsonl")
        with open(path, "a", encoding="utf-8") as stream:
            for sample in samples:
                json.dump(sample, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _save_batch_manifest(self, status: str = "OPEN"):
        """Атомарно обновить постоянный индекс текущей партии."""
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
            "identity": {
                "batch_id": self.batch_id,
                **dict(self.identity_manifest),
            },
            "runs": list(self._runs),
            "counts": dict(self._batch_stats),
            "samples_index": "samples.jsonl",
            "parts": [
                {
                    "part_id": item["part_id"],
                    "category": item["category"],
                    "decision": item["decision"],
                    "folder": item["relative_folder"],
                    "time": item["time"],
                    "annotation_files": item.get("annotation_files", []),
                    "sample_count": item.get("sample_count", 0),
                }
                for item in self._batch_parts
            ],
            "training": {
                "raw_images_are_training_inputs": True,
                "annotations": "pseudo-labels from the production models; review before training",
                "image_quality": self.jpeg_quality,
            },
            "compression": {
                "jpeg_quality": self.jpeg_quality,
                "zip_compression": self.zip_compression,
                "zip_level": self.zip_level,
            },
        }
        path = os.path.join(self.batch_folder, "batch.json")
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        committed_index = {
            "schema_version": self.SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "parts": [
                {
                    "part_id": item["part_id"],
                    "category": item["category"],
                    "folder": item["relative_folder"],
                    "manifest": item.get("manifest"),
                }
                for item in self._batch_parts
            ],
        }
        self._write_json_durable(
            os.path.join(self.batch_folder, "committed_index.json"),
            committed_index,
        )
        self._fsync_directory(self.batch_folder)

    def register_run(self, run_id: str, threshold_revision=None):
        if not self.enabled:
            return
        if any(row.get("run_id") == run_id for row in self._runs):
            return
        self._runs.append({
            "run_id": str(run_id),
            "threshold_revision": threshold_revision,
            "started_at": time.time(),
        })
        self._save_batch_manifest()

    def close_batch(self, status: str = "CLOSED"):
        """Durably close the batch manifest, including an empty batch."""
        if not self.enabled:
            return None
        self._save_batch_manifest(status=str(status).upper())
        return self.batch_folder

    def get_batch_stats(self) -> dict:
        return dict(self._batch_stats)

    # Statistics

    def get_stats(self) -> dict:
        """Накопительная статистика по корпусам: total/good/bad/cleanup."""
        return dict(self.stats)

    def _load_stats(self) -> dict:
        """Восстановить статистику корпусов из stats.json (если есть)."""
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
        """Атомарно сохранить статистику корпусов в stats.json."""
        if not self.enabled:
            return
        path = os.path.join(self.root_folder, self.STATS_FILE)
        temp_path = path + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        except OSError as exc:
            print(f"[ARCHIVE] Не удалось сохранить статистику: {exc}")

    # Compression

    def compress(self, delete_original: bool | None = None) -> str | None:
        """
        Сжать папку текущей партии в ZIP-архив.

        JPEG уже сжат внутри себя, поэтому смена ZIP-метода почти не
        уменьшает изображения. Существенная экономия задаётся jpeg_quality;
        метод ZIP влияет в основном на JSON и скорость упаковки.
        """
        if not self.enabled:
            return None
        if delete_original is None:
            delete_original = self.delete_original_after_zip

        batch_folder = self.batch_folder

        if not os.path.exists(batch_folder):
            print("[ARCHIVE] Нечего сжимать — папка не существует")
            return None

        # Проверить что папка не пустая, гарантированно закрыв iterator.
        with os.scandir(batch_folder) as entries:
            is_empty = not any(entries)
        if is_empty:
            print("[ARCHIVE] Нечего сжимать — папка пустая")
            return None

        # Сначала фиксируем финальную статистику, чтобы batch.json попал
        # внутрь ZIP уже с корректными счётчиками.
        self._save_batch_manifest(status="CLOSED")

        zip_path = batch_folder + ".zip"
        temp_zip_path = zip_path + ".tmp"

        print(
            f"[ARCHIVE] Сжатие {batch_folder} -> {zip_path} "
            f"({self._finalized_count} деталей)..."
        )

        start_time = time.time()

        try:
            if os.path.exists(temp_zip_path):
                os.remove(temp_zip_path)
            self._create_zip(
                batch_folder,
                temp_zip_path,
                compression=self.zip_compression,
                level=self.zip_level,
            )
            with zipfile.ZipFile(temp_zip_path, "r") as archive:
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise RuntimeError(
                        f"ZIP CRC verification failed: {bad_member}"
                    )
            os.replace(temp_zip_path, zip_path)
        except Exception as e:
            print(f"[ARCHIVE] Ошибка сжатия: {e}")
            if os.path.exists(temp_zip_path):
                try:
                    os.remove(temp_zip_path)
                except OSError as remove_error:
                    print(
                        "[ARCHIVE] Не удалён временный zip "
                        f"{temp_zip_path}: {remove_error}"
                    )
            return None

        elapsed = time.time() - start_time
        zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)

        # Размер оригинала
        original_size_mb = self._dir_size_mb(batch_folder)

        print(
            f"[ARCHIVE] Сжатие завершено за {elapsed:.1f}с: "
            f"{original_size_mb:.1f} MB -> {zip_size_mb:.1f} MB "
            f"({self._compression_ratio(original_size_mb, zip_size_mb)})"
        )

        if delete_original:
            try:
                shutil.rmtree(batch_folder)
                print(f"[ARCHIVE] Оригинал удалён: {batch_folder}")
            except Exception as e:
                print(
                    f"[ARCHIVE] Не удалось удалить оригинал: {e}"
                )

        return zip_path

    @staticmethod
    def _create_zip(
        source_folder: str,
        zip_path: str,
        compression: str = "deflated",
        level: int = 6,
    ):
        """Рекурсивно упаковать папку партии в ZIP.

        ``deflated`` — совместимый сбалансированный вариант, ``stored``
        быстрее и почти не уступает для JPEG, ``lzma`` полезен только для
        больших текстовых метаданных и может быть медленным на слабом ПК.
        """
        methods = {
            "deflated": zipfile.ZIP_DEFLATED,
            "stored": zipfile.ZIP_STORED,
            "lzma": zipfile.ZIP_LZMA,
        }
        method = methods.get(str(compression).lower(), zipfile.ZIP_DEFLATED)
        kwargs = {"compression": method}
        if method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            kwargs["compresslevel"] = max(0, min(9, int(level)))

        with zipfile.ZipFile(zip_path, "w", **kwargs) as zf:
            for root, _dirs, files in os.walk(source_folder):
                for filename in files:
                    file_path = os.path.join(root, filename)
                    arcname = os.path.relpath(
                        file_path, source_folder
                    )
                    zf.write(file_path, arcname)

    @staticmethod
    def _dir_size_mb(path: str) -> float:
        """Размер директории в МБ."""
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                with contextlib.suppress(OSError):
                    total += os.path.getsize(fp)
        return total / (1024 * 1024)

    @staticmethod
    def _compression_ratio(original_mb: float, compressed_mb: float) -> str:
        """Строка с коэффициентом сжатия."""
        if original_mb <= 0:
            return "—"
        ratio = (1 - compressed_mb / original_mb) * 100
        if ratio >= 0:
            return f"-{ratio:.0f}%"
        return f"+{abs(ratio):.0f}%"

    # Internal

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