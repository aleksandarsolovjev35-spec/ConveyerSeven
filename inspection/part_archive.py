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
        delete_original_after_zip: bool = True,
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

        if batch_id is None:
            batch_id = datetime.now().strftime("batch_%Y%m%d_%H%M%S")
        self.batch_id = self._safe_name(batch_id)

        self.date_folder = datetime.now().strftime("%Y-%m-%d")
        self.batch_started_at = datetime.now().isoformat(timespec="seconds")

        # Буфер хранит уже JPEG-encoded bytes, а не тяжёлые numpy frames.
        self._buffers: dict[int, dict] = {}

        # Список архивированных деталей (для UI). Он живёт в памяти текущего
        # запуска; batch.json является постоянным индексом партии.
        self._archived: list[dict] = []
        self._batch_parts: list[dict] = []
        self._batch_stats = {"total": 0, "good": 0, "bad": 0, "cleanup": 0}

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
            "batch_id": self.batch_id,
            "batch_folder": self.batch_folder,
            "batch_stats": dict(self._batch_stats),
            "editable": self.can_reconfigure(),
            "validation": validation,
        }

    # Public API

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

        for role, frame in raw_frames.items():
            if role not in buf:
                buf[role] = {}
            buf[role]["raw"] = self._encode_image(frame)

        for role, frame in annotated_frames.items():
            if role not in buf:
                buf[role] = {}
            buf[role]["debug"] = self._encode_image(frame)

        if raw_overlay_frames:
            for role, frame in raw_overlay_frames.items():
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
        """
        Финализировать деталь: записать все кадры и метаданные на диск.

        Returns:
            Путь к папке детали или None если архивация отключена.
        """
        if not self.enabled:
            return None

        requested_category = str(category or "").upper()
        stored_category = self.normalise_category(requested_category)
        category_folder = self.CATEGORY_DIRS[stored_category]
        folder_name = f"part_{part_id:04d}"

        folder_path = os.path.join(
            self.batch_folder,
            category_folder,
            folder_name,
        )
        os.makedirs(folder_path, exist_ok=True)

        roles_saved = []
        # Keep the buffer until every image and meta.json is written.
        # A disk/JPEG failure must not silently lose the part data.
        buf = self._buffers.get(part_id, {})

        for role, frames in buf.items():
            raw = frames.get("raw")
            raw_overlay = frames.get("raw_overlay")
            debug = frames.get("debug")

            if raw is not None:
                self._save_image(
                    raw,
                    os.path.join(folder_path, f"{role}.jpg"),
                )

            if raw_overlay is not None:
                self._save_image(
                    raw_overlay,
                    os.path.join(folder_path, f"{role}_raw.jpg"),
                )

            if debug is not None:
                self._save_image(
                    debug,
                    os.path.join(folder_path, f"{role}_debug.jpg"),
                )

            # Сохранение отдельных прогонов на диск
            for r in (1, 2, 3):
                r_raw = frames.get(f"raw_run{r}")
                if r_raw is not None:
                    self._save_image(
                        r_raw,
                        os.path.join(folder_path, f"{role}_run{r}.jpg"),
                    )

                r_raw_overlay = frames.get(f"raw_overlay_run{r}")
                if r_raw_overlay is not None:
                    self._save_image(
                        r_raw_overlay,
                        os.path.join(folder_path, f"{role}_run{r}_raw.jpg"),
                    )

                r_debug = frames.get(f"debug_run{r}")
                if r_debug is not None:
                    self._save_image(
                        r_debug,
                        os.path.join(folder_path, f"{role}_run{r}_debug.jpg"),
                    )

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
        temp_meta_path = meta_path + ".tmp"
        with open(temp_meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_meta_path, meta_path)

        self._buffers.pop(part_id, None)
        relative_folder = os.path.relpath(
            folder_path, self.batch_folder,
        ).replace("\\", "/")
        archived_item = {
            "part_id": part_id,
            "category": stored_category,
            "decision": decision,
            "folder": folder_path.replace("\\", "/"),
            "relative_folder": relative_folder,
            "roles": roles_saved,
            "time": now.strftime("%H:%M:%S"),
        }
        self._archived.append(archived_item)
        self._batch_parts.append(dict(archived_item))

        self._finalized_count += 1

        # Статистика по корпусам: годные / брак / очистка.
        self.stats["total"] = int(self.stats.get("total") or 0) + 1
        category_key = {
            "GOOD": "good",
            "BAD": "bad",
            "CLEANUP": "cleanup",
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