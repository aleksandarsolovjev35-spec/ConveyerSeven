"""Оконный HMI-мастер назначения физических камер ролям линии."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from config.camera_mapping import (
    CAMERA_MAPPING_FILE,
    load_camera_mapping,
    validate_camera_mapping,
)
from vision.camera_manager import (
    CameraManager,
    _REQUESTED_FPS,
    _env_float,
    _env_int,
)
from vision.camera_manager import default_backends as _camera_backends


CAMERA_SCAN_LIMIT = 10
EXPECTED_SIZE = (1280, 720)
# FPS согласуется тот же, что запросит production (CAMERA_FPS): мастер
# обязан проверять камеры ровно в том режиме, в котором им работать.
REQUESTED_FPS = _REQUESTED_FPS
JPEG_QUALITY = 78
PREVIEW_MAX_WIDTH = 960
NEAR_BLACK_MEAN_MAX = 5.0
NEAR_BLACK_P99_MAX = 12.0
PROBE_READ_INTERVAL = 0.03
# Сканирование должно давать камере столько же времени на старт, сколько
# даёт production-preflight, иначе мастер объявит исправную камеру
# отсутствующей и не соберёт комплект 7/7.
SCAN_PROBE_ATTEMPTS = _env_int("CAMERA_SCAN_PROBE_ATTEMPTS", 60, minimum=1)
PREVIEW_PROBE_ATTEMPTS = 10
# Разовый отказ резервирования полосы на USB-хабе лечится пересозданием
# VideoCapture, а не увеличением таймаута чтения. Без повторных попыток
# исправная камера случайно выпадала из сканирования, и мастер находил
# то 5, то 6 камер из семи на одном и том же железе.
SCAN_OPEN_ATTEMPTS = _env_int("CAMERA_SCAN_OPEN_ATTEMPTS", 3, minimum=1)
SCAN_RETRY_DELAY = _env_float("CAMERA_SCAN_RETRY_DELAY", 0.4, minimum=0.0)

ROLE_ORDER = (
    "INPUT_LEFT",
    "INPUT_RIGHT",
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
)

REQUIRED_CAMERA_COUNT = len(ROLE_ORDER)

ROLE_LABELS = {
    "INPUT_LEFT": "ВХОД · СЛЕВА",
    "INPUT_RIGHT": "ВХОД · СПРАВА",
    "SPIDER_LEFT": "КОНТРОЛЬ · ДЛИННАЯ СЛЕВА",
    "SPIDER_RIGHT": "КОНТРОЛЬ · ДЛИННАЯ СПРАВА",
    "SPIDER_IN": "КОНТРОЛЬ · КОРОТКАЯ ВНУТРИ",
    "SPIDER_OUT": "КОНТРОЛЬ · КОРОТКАЯ СНАРУЖИ",
    "TOP": "ВИД СВЕРХУ",
}


def _open_capture(camera_id: int):
    """Открыть камеру, перебирая backend-ы в том же порядке, что и runtime.

    Мастер обязан находить ровно те камеры, которые потом откроет
    CameraManager. Если здесь остаётся только DirectShow, а рабочая
    камера отвечает лишь под Media Foundation, оператор соберёт mapping,
    который упадёт на первом же запуске линии.
    """
    last = None
    for backend in _camera_backends():
        capture = None
        try:
            capture = (
                cv2.VideoCapture(camera_id)
                if backend is None
                else cv2.VideoCapture(camera_id, backend)
            )
        except Exception as exc:
            print(f"[CAMERA CALIBRATION] Camera {camera_id}: {exc}")
            continue
        if capture is not None and capture.isOpened():
            return capture
        _safe_release(capture)
        last = capture
    return last


def _configure_capture(capture):
    # Мастер и production настраивают камеру одинаково — включая жёсткое
    # согласование MJPG: камера, молча откатившаяся в YUY2, съедает в разы
    # больше полосы USB, и собранный с ней mapping упадёт на старте линии.
    CameraManager._configure_capture(capture)


def _frame_error(frame) -> str | None:
    array = np.asarray(frame)
    if array.ndim != 3 or array.shape[2] < 3:
        return f"неверная форма кадра: {array.shape}"
    height, width = array.shape[:2]
    if (width, height) != EXPECTED_SIZE:
        return (
            f"разрешение {width}x{height}; "
            f"требуется {EXPECTED_SIZE[0]}x{EXPECTED_SIZE[1]}"
        )
    sample = array[::12, ::12, :3].astype(np.float32)
    luminance = sample.mean(axis=2)
    mean = float(luminance.mean())
    p99 = float(np.percentile(luminance, 99))
    if mean <= NEAR_BLACK_MEAN_MAX and p99 <= NEAR_BLACK_P99_MAX:
        return f"почти чёрный кадр: mean={mean:.2f}, p99={p99:.2f}"
    return None


def _probe_capture(capture, attempts: int = SCAN_PROBE_ATTEMPTS):
    """Дать камере отдать валидный кадр.

    UVC-камера после открытия сначала строит граф и резервирует полосу
    USB, и только потом отдаёт первый кадр. Пяти чтений подряд для этого
    мало: исправная камера отбраковывалась как "не вернула кадр".
    """
    error = "камера не вернула кадр"
    for _ in range(max(1, int(attempts))):
        ok, frame = capture.read()
        if ok and frame is not None:
            error = _frame_error(frame)
            if error is None:
                return frame, None
        time.sleep(PROBE_READ_INTERVAL)
    return None, error


def _safe_release(capture) -> None:
    """Освободить handle камеры; ошибка закрытия не должна ломать сценарий."""
    if capture is None:
        return
    try:
        capture.release()
    except Exception as exc:
        print(f"[CAMERA CALIBRATION] Ошибка освобождения камеры: {exc}")


def _probe_camera(camera_id: int, factory, *, keep: bool = False):
    """Проверить камеру с повторными открытиями.

    Каждая попытка — это полное пересоздание VideoCapture: разовая
    неудача резервирования изохронной полосы на USB контроллере лечится
    именно повтором. Между попытками драйверу даётся
    ``SCAN_RETRY_DELAY`` на освобождение устройства, но только если
    драйвер был реально задействован: несуществующий индекс отклоняется
    сразу и без пауз, чтобы сканирование пустых ID не растягивалось.

    При ``keep=True`` успешный handle возвращается вызывающему (пул
    предпросмотра), иначе освобождается здесь же.
    """

    last_error = "устройство не открылось"
    for attempt in range(1, SCAN_OPEN_ATTEMPTS + 1):
        capture = None
        engaged_driver = False
        try:
            capture = factory(camera_id)
            if capture is None or not capture.isOpened():
                last_error = "устройство не открылось"
            else:
                engaged_driver = True
                _configure_capture(capture)
                _frame, error = _probe_capture(capture)
                if error is None:
                    if attempt > 1:
                        print(
                            f"[CAMERA CALIBRATION] Camera {camera_id}: OK "
                            f"с попытки {attempt}"
                        )
                    if keep:
                        opened, capture = capture, None
                        return opened, None
                    return None, None
                last_error = error
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        finally:
            _safe_release(capture)
        if (
            engaged_driver
            and attempt < SCAN_OPEN_ATTEMPTS
            and SCAN_RETRY_DELAY
        ):
            # Драйверу нужно время отпустить устройство перед повтором.
            time.sleep(SCAN_RETRY_DELAY)
    print(
        f"[CAMERA CALIBRATION] Camera {camera_id}: не отвечает "
        f"за {SCAN_OPEN_ATTEMPTS} поп. ({last_error})"
    )
    return None, last_error


def _scan_working_cameras(max_tested, factory):
    """Фаза 1: изолированно проверить каждый Camera ID.

    Каждая камера открывается, проверяется и сразу освобождается: во
    время проверки индекса N не стримят N-1 уже найденных соседей, и
    исправная камера не проигрывает гонку за полосу USB уже открытым.
    Результат детерминирован: список ID, которые отвечают в production-
    формате, будучи предоставленными сами себе. Это ответ на вопрос
    "сколько камер исправны вообще", а не "кто успел первым".
    """

    working = []
    for camera_id in range(int(max_tested)):
        _capture, error = _probe_camera(camera_id, factory)
        if error is None:
            working.append(camera_id)
            print(f"[CAMERA CALIBRATION] Camera {camera_id}: OK")
    return working


def detect_available_cameras(max_tested=CAMERA_SCAN_LIMIT, capture_factory=None):
    """Найти Camera ID, которые дают валидный production-кадр."""

    factory = capture_factory or _open_capture
    return _scan_working_cameras(max_tested, factory)


def _open_preview_pool(working, required_count, factory):
    """Фаза 2: одновременно открыть проверенные камеры для предпросмотра.

    На этой фазе камеры остаются стримить одновременно, ровно как в
    production: мастер обязан собрать тот набор, который потом откроет
    CameraManager. Камера, исправная поодиночке, но потерянная здесь,
    недоступна и линии — об этом сообщается отдельно, потому что это
    симптом нехватки пропускной способности USB-контроллера, а не
    поломки устройства.
    """

    pool = {}
    lost = {}
    for camera_id in list(working)[: int(required_count)]:
        capture, error = _probe_camera(camera_id, factory, keep=True)
        if capture is not None:
            pool[camera_id] = capture
        else:
            lost[camera_id] = error
            print(
                f"[CAMERA CALIBRATION] Camera {camera_id}: потеряна при "
                f"одновременном открытии набора ({error})"
            )
    return pool, lost


def _release_camera_pool(pool):
    for capture in list(pool.values()):
        _safe_release(capture)
    pool.clear()


def atomic_write_mapping(path, mapping: dict):
    """Валидировать и атомарно сохранить только полный mapping 7/7."""

    validated = validate_camera_mapping(mapping)
    ordered = {role: int(validated[role]) for role in ROLE_ORDER}
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(ordered, stream, ensure_ascii=False, indent=4)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[CAMERA CALIBRATION] Не удалён временный файл: {exc}")
        raise
    return ordered


class CameraCalibrationApi:
    """Thread-safe backend пошагового pywebview-мастера."""

    def __init__(
        self,
        config_path=CAMERA_MAPPING_FILE,
        *,
        scan_limit=CAMERA_SCAN_LIMIT,
        capture_factory=None,
    ):
        self.config_path = Path(config_path).resolve()
        self.scan_limit = int(scan_limit)
        self.capture_factory = capture_factory or _open_capture
        self.lock = threading.RLock()
        self.status = "WAITING"
        self.error = None
        self.available_cameras: list[int] = []
        self.assignments: dict[str, int] = {}
        self.role_index = 0
        self.candidate_index = 0
        self.saved = False
        self.closed = False
        self._captures: dict[int, object] = {}
        self._active_camera_id = None
        self._preview_verified_id = None
        self._close_callback = None
        self._scan_thread = None

    def set_close_callback(self, callback):
        with self.lock:
            self._close_callback = callback

    def scan(self):
        with self.lock:
            if self.closed:
                return self.get_state()
            self.status = "SCANNING"
            self.error = None
            self.assignments = {}
            self.role_index = 0
            self.candidate_index = 0
            self._release_all_captures_locked()

        pool = {}
        lost = {}
        working = []
        scan_error = None
        try:
            factory = self.capture_factory
            print(
                f"[CAMERA CALIBRATION] Фаза 1: изолированная проверка "
                f"Camera ID 0..{self.scan_limit - 1}"
            )
            working = _scan_working_cameras(self.scan_limit, factory)
            print(
                f"[CAMERA CALIBRATION] Фаза 1: исправны {len(working)}/"
                f"{len(ROLE_ORDER)}: {working}"
            )
            if len(working) >= len(ROLE_ORDER):
                print(
                    f"[CAMERA CALIBRATION] Фаза 2: одновременное открытие "
                    f"{REQUIRED_CAMERA_COUNT} камер (как в production)"
                )
                pool, lost = _open_preview_pool(
                    working,
                    REQUIRED_CAMERA_COUNT,
                    factory,
                )
        except Exception as exc:
            pool = {}
            scan_error = f"Ошибка поиска камер: {type(exc).__name__}: {exc}"

        with self.lock:
            if self.closed:
                _release_camera_pool(pool)
                return self._state_locked()
            self._captures = pool
            self.available_cameras = (
                list(pool) if len(working) >= len(ROLE_ORDER) else list(working)
            )
            if scan_error is not None:
                self.status = "ERROR"
                self.error = scan_error
                self._release_all_captures_locked()
            elif len(working) < len(ROLE_ORDER):
                # Камера не отвечает даже наедине с шиной: проблема в
                # подключении/питании/драйвере, а не в конкуренции за USB.
                found = len(working)
                self.status = "ERROR"
                self.error = (
                    f"Найдено исправных камер: {found}/{len(ROLE_ORDER)} "
                    f"при изолированной проверке каждой. Камера не отвечает "
                    "даже без конкуренции за USB: проверьте подключение, "
                    "питание, свободна ли она от других программ и режим "
                    "1280x720 MJPG. Исправьте и нажмите ПОВТОРИТЬ ПОИСК."
                )
                self._release_all_captures_locked(keep_available=True)
            elif lost:
                # Поодиночке исправны, вместе не влезли: классическая
                # нехватка пропускной способности одного USB-контроллера.
                found = len(pool)
                self.status = "ERROR"
                self.error = (
                    f"Одновременно открылось {found}/{len(ROLE_ORDER)}. "
                    f"Камеры {sorted(lost)} исправны поодиночке, но не "
                    "работают вместе — не хватает пропускной способности "
                    "USB-контроллера. Разведите камеры по разным корневым "
                    "хабам/контроллерам, убедитесь что все работают в MJPG "
                    "(а не YUY2), затем нажмите ПОВТОРИТЬ ПОИСК."
                )
                self._release_all_captures_locked(keep_available=True)
            else:
                self.status = "READY"
                self.error = None
            return self._state_locked()

    def rescan(self):
        """Повторить поиск камер из состояния ошибки, не закрывая мастер.

        Сканирование длится десятки секунд, поэтому выполняется фоновым
        потоком: JS-вызов pywebview возвращается сразу, а UI следит за
        прогрессом через периодический get_state. Повтор безопасен только
        после завершения предыдущего сканирования: мастер в ERROR ничего
        не стримит, и USB-шина свободна для нового изолированного прогона.
        """

        with self.lock:
            if self.closed or self.status != "ERROR":
                return self._state_locked()
            thread = self._scan_thread
            if thread is not None and thread.is_alive():
                return self._state_locked()
            self.status = "SCANNING"
            self.error = None
            thread = threading.Thread(
                target=self.scan,
                daemon=True,
                name="calibration-rescan",
            )
            self._scan_thread = thread
            thread.start()
            return self._state_locked()

    def get_state(self):
        with self.lock:
            return self._state_locked()

    def next_camera(self):
        return self._move_candidate(1)

    def previous_camera(self):
        return self._move_candidate(-1)

    def _move_candidate(self, delta: int):
        with self.lock:
            self._require_status("READY")
            free = self._free_cameras_locked()
            if not free:
                raise RuntimeError("Нет свободной камеры для назначения")
            self.candidate_index = (self.candidate_index + delta) % len(free)
            self._clear_active_camera_locked()
            return self._state_locked()

    def assign_current(self):
        with self.lock:
            self._require_status("READY")
            free = self._free_cameras_locked()
            if not free:
                raise RuntimeError("Нет свободной камеры для назначения")
            camera_id = free[self.candidate_index % len(free)]
            role = ROLE_ORDER[self.role_index]
            if camera_id in self.assignments.values():
                raise RuntimeError(f"Camera ID {camera_id} уже назначен")
            if self._preview_verified_id != camera_id:
                raise RuntimeError(
                    "Сначала дождитесь живого кадра выбранной камеры"
                )
            self.assignments[role] = int(camera_id)
            self._clear_active_camera_locked()
            self.role_index += 1
            self.candidate_index = 0
            self.status = (
                "REVIEW" if self.role_index == len(ROLE_ORDER) else "READY"
            )
            return self._state_locked()

    def back(self):
        with self.lock:
            if self.status not in ("READY", "REVIEW"):
                return self._state_locked()
            if self.role_index <= 0:
                return self._state_locked()
            self._clear_active_camera_locked()
            self.role_index -= 1
            role = ROLE_ORDER[self.role_index]
            previous_camera = self.assignments.pop(role, None)
            self.status = "READY"
            free = self._free_cameras_locked()
            self.candidate_index = (
                free.index(previous_camera)
                if previous_camera in free else 0
            )
            return self._state_locked()

    def save(self):
        with self.lock:
            self._require_status("REVIEW")
            mapping = {role: self.assignments[role] for role in ROLE_ORDER}
        try:
            atomic_write_mapping(self.config_path, mapping)
            load_camera_mapping(self.config_path)
        except Exception as exc:
            with self.lock:
                self.status = "ERROR"
                self.error = f"Не удалось сохранить mapping: {type(exc).__name__}: {exc}"
                return self._state_locked()

        with self.lock:
            self.saved = True
            self.status = "SAVED"
            self.error = None
            self._release_all_captures_locked(keep_available=True)
            return self._state_locked()

    def finish(self):
        with self.lock:
            if not self.saved:
                return False
            callback = self._close_callback
        if callback is not None:
            callback()
        return True

    def cancel(self):
        with self.lock:
            self.closed = True
            if not self.saved:
                self.status = "CANCELLED"
            self._release_all_captures_locked()
            callback = self._close_callback
        if callback is not None:
            callback()
        return True

    def shutdown(self):
        with self.lock:
            self.closed = True
            self._release_all_captures_locked()

    def get_frame(self):
        with self.lock:
            if self.status != "READY":
                return {"ok": False, "error": "preview unavailable"}
            free = self._free_cameras_locked()
            if not free:
                return {"ok": False, "error": "нет свободной камеры"}
            camera_id = free[self.candidate_index % len(free)]
            try:
                capture = self._captures.get(camera_id)
                if capture is None or not capture.isOpened():
                    raise RuntimeError(
                        f"Camera ID {camera_id} больше не открыта"
                    )
                self._active_camera_id = camera_id
                frame, error = _probe_capture(
                    capture, attempts=PREVIEW_PROBE_ATTEMPTS
                )
                if error is not None or frame is None:
                    raise RuntimeError(error or "камера не вернула кадр")
                height, width = frame.shape[:2]
                if width > PREVIEW_MAX_WIDTH:
                    target_height = max(1, round(height * PREVIEW_MAX_WIDTH / width))
                    frame = cv2.resize(
                        frame,
                        (PREVIEW_MAX_WIDTH, target_height),
                        interpolation=cv2.INTER_AREA,
                    )
                encoded_ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
                )
                if not encoded_ok:
                    raise RuntimeError("JPEG encode failed")
                data = base64.b64encode(encoded.tobytes()).decode("ascii")
                self._preview_verified_id = int(camera_id)
                return {
                    "ok": True,
                    "camera_id": int(camera_id),
                    "data": "data:image/jpeg;base64," + data,
                }
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self._drop_camera_locked(camera_id)
                self.status = "ERROR"
                self.error = (
                    f"Camera ID {camera_id} потеряла валидный кадр. {message}"
                )
                return {
                    "ok": False,
                    "camera_id": int(camera_id),
                    "error": message,
                }

    def _state_locked(self):
        free = self._free_cameras_locked()
        current_role = (
            ROLE_ORDER[self.role_index]
            if self.role_index < len(ROLE_ORDER)
            else None
        )
        current_camera = (
            free[self.candidate_index % len(free)]
            if self.status == "READY" and free
            else None
        )
        roles = []
        for index, role in enumerate(ROLE_ORDER):
            if role in self.assignments:
                row_status = "assigned"
            elif index == self.role_index and self.status == "READY":
                row_status = "current"
            else:
                row_status = "pending"
            roles.append({
                "role": role,
                "label": ROLE_LABELS[role],
                "status": row_status,
                "camera_id": self.assignments.get(role),
            })
        return {
            "status": self.status,
            "error": self.error,
            "found": len(self.available_cameras),
            "required": len(ROLE_ORDER),
            "available_camera_ids": list(self.available_cameras),
            "free_camera_ids": free,
            "step": min(self.role_index + 1, len(ROLE_ORDER)),
            "total_steps": len(ROLE_ORDER),
            "current_role": current_role,
            "current_role_label": (
                ROLE_LABELS.get(current_role) if current_role else None
            ),
            "current_camera_id": current_camera,
            "candidate_position": (
                self.candidate_index % len(free) + 1 if free else 0
            ),
            "candidate_count": len(free),
            "assignments": dict(self.assignments),
            "roles": roles,
            "saved": self.saved,
            "config_path": str(self.config_path),
        }

    def _free_cameras_locked(self):
        used = set(self.assignments.values())
        return [
            camera_id
            for camera_id in self.available_cameras
            if camera_id not in used
        ]

    def _clear_active_camera_locked(self):
        self._active_camera_id = None
        self._preview_verified_id = None

    def _drop_camera_locked(self, camera_id: int):
        _safe_release(self._captures.pop(camera_id, None))
        if camera_id in self.available_cameras:
            self.available_cameras.remove(camera_id)
        self._clear_active_camera_locked()

    def _release_all_captures_locked(self, *, keep_available=False):
        _release_camera_pool(self._captures)
        if not keep_available:
            self.available_cameras = []
        self._clear_active_camera_locked()

    def _require_status(self, expected: str):
        if self.status != expected:
            raise RuntimeError(
                f"Недопустимая операция: status={self.status}, expected={expected}"
            )


def calibrate_cameras(
    config_path=CAMERA_MAPPING_FILE,
    *,
    scan_limit=CAMERA_SCAN_LIMIT,
) -> bool:
    """Открыть отдельное оконное HMI и дождаться полного mapping 7/7."""

    import webview

    api = CameraCalibrationApi(config_path, scan_limit=scan_limit)
    html_path = (
        Path(__file__).resolve().parent
        / "ui"
        / "calibration"
        / "index.html"
    )
    window = webview.create_window(
        title="КАЛИБРОВКА КАМЕР",
        url=html_path.as_uri(),
        js_api=api,
        width=1280,
        height=820,
        min_size=(1040, 700),
        resizable=True,
        fullscreen=False,
        background_color="#0b0f13",
    )
    api.set_close_callback(window.destroy)
    try:
        webview.start(api.scan)
    finally:
        api.shutdown()
    return bool(api.saved and Path(config_path).is_file())


def launch_camera_calibrator(
    config_path=CAMERA_MAPPING_FILE,
    *,
    scan_limit=CAMERA_SCAN_LIMIT,
    runner=None,
) -> bool:
    """Запустить мастер отдельным процессом и проверить его результат."""

    destination = Path(config_path).resolve()
    if destination.exists():
        try:
            load_camera_mapping(destination)
        except Exception as exc:
            print(f"[CAMERA CALIBRATION] Existing mapping is invalid: {exc}")
            return False
        return True

    command = [
        sys.executable,
        "-m",
        "vision.camera_calibration_console",
        "--config",
        str(destination),
        "--scan-limit",
        str(int(scan_limit)),
    ]
    run = runner or subprocess.run
    print("[CAMERA CALIBRATION] camera_mapping.json отсутствует")
    print("[CAMERA CALIBRATION] Запуск оконного мастера")
    try:
        completed = run(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            check=False,
        )
    except Exception as exc:
        print(f"[CAMERA CALIBRATION] Не удалось запустить мастер: {exc}")
        return False
    if int(getattr(completed, "returncode", 1)) != 0:
        print("[CAMERA CALIBRATION] Калибровка отменена или завершилась ошибкой")
        return False
    try:
        mapping = load_camera_mapping(destination)
    except Exception as exc:
        print(f"[CAMERA CALIBRATION] Некорректный результат: {exc}")
        return False
    print(f"[CAMERA CALIBRATION] Сохранено ролей: {len(mapping)}/{len(ROLE_ORDER)}")
    return True


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Калибратор семи камер")
    parser.add_argument("--config", default=CAMERA_MAPPING_FILE)
    parser.add_argument("--scan-limit", type=int, default=CAMERA_SCAN_LIMIT)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    success = calibrate_cameras(
        args.config,
        scan_limit=args.scan_limit,
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
