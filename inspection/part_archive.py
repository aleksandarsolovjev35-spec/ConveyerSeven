"""
Архивация результатов инспекции каждой детали.

Для каждой детали создаётся папка с:
  - сырыми кадрами со всех камер
  - кадрами с сырыми детекциями нейросети (raw overlay)
  - аннотированными кадрами (правила)
  - meta.json с метаданными

Структура (во время работы):
  archive/<date>/<batch>/part_<id>_<category>_<decision>/
    meta.json
    <ROLE>.jpg
    <ROLE>_raw.jpg
    <ROLE>_debug.jpg

После завершения работы compress() упаковывает папку партии в:
  archive/<date>/<batch>.zip
и удаляет оригинальную папку.
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

    def __init__(
        self,
        root_folder: str = "archive",
        batch_id: str | None = None,
        enabled: bool = True,
    ):
        self.root_folder = root_folder
        self.enabled = enabled

        if batch_id is None:
            batch_id = datetime.now().strftime("batch_%H%M%S")
        self.batch_id = batch_id

        self.date_folder = datetime.now().strftime("%Y-%m-%d")

        # Буфер хранит уже JPEG-encoded bytes, а не тяжёлые numpy frames.
        self._buffers: dict[int, dict] = {}

        # Список архивированных деталей (для UI)
        self._archived: list[dict] = []

        # Счётчик сохранённых деталей
        self._finalized_count = 0

        if self.enabled:
            os.makedirs(self.root_folder, exist_ok=True)

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
            run_frames: список из трёх словарей кадров прогонов.
            run_rule_results: список из трёх списков результатов правил по прогонам.
            run_vision_results: список из трёх словарей детекций по прогонам.
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

        # Сохранение всех трёх независимых прогонов для каждого ракурса
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

        safe_decision = self._safe_name(decision)
        folder_name = f"part_{part_id:04d}_{category}_{safe_decision}"

        folder_path = os.path.join(
            self.root_folder,
            self.date_folder,
            self.batch_id,
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

            # Сохранение всех трёх отдельных прогонов на диск
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
            "part_id": part_id,
            "batch_id": self.batch_id,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "timestamp": time.time(),
            "step": step,
            "category": category,
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
        self._archived.append({
            "part_id": part_id,
            "category": category,
            "decision": decision,
            "folder": folder_path.replace("\\", "/"),
            "roles": roles_saved,
            "time": now.strftime("%H:%M:%S"),
        })

        self._finalized_count += 1

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

    @property
    def archive_base_path(self) -> str:
        return os.path.join(
            self.root_folder,
            self.date_folder,
            self.batch_id,
        )

    # Compression

    def compress(self, delete_original: bool = True) -> str | None:
        """
        Сжать папку текущей партии в zip-архив.

        Вызывается при завершении программы.

        Args:
            delete_original: удалить оригинальную папку после сжатия.

        Returns:
            Путь к zip-файлу или None если нечего сжимать.
        """
        if not self.enabled:
            return None

        batch_folder = os.path.join(
            self.root_folder,
            self.date_folder,
            self.batch_id,
        )

        if not os.path.exists(batch_folder):
            print("[ARCHIVE] Нечего сжимать — папка не существует")
            return None

        # Проверить что папка не пустая, гарантированно закрыв iterator.
        with os.scandir(batch_folder) as entries:
            is_empty = not any(entries)
        if is_empty:
            print("[ARCHIVE] Нечего сжимать — папка пустая")
            return None

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
            self._create_zip(batch_folder, temp_zip_path)
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
    def _create_zip(source_folder: str, zip_path: str):
        """
        Рекурсивно упаковать папку в zip.
        Использует ZIP_DEFLATED для сжатия JPEG (даёт ~5-15%).
        Структура внутри zip сохраняет имена подпапок деталей.
        """
        with zipfile.ZipFile(
            zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
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
        return f"-{ratio:.0f}%"

    # Internal

    def _encode_image(self, frame) -> bytes:
        try:
            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, self.JPEG_QUALITY],
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