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


CAMERA_SCAN_LIMIT = 10
EXPECTED_SIZE = (1280, 720)
REQUESTED_FPS = 30.0
JPEG_QUALITY = 78
PREVIEW_MAX_WIDTH = 960
NEAR_BLACK_MEAN_MAX = 5.0
NEAR_BLACK_P99_MAX = 12.0
CAMERA_SCAN_BATCH_SIZE = 7
CAMERA_SCAN_BATCH_TIMEOUT = 10.0
CAMERA_SCAN_POLL_INTERVAL = 0.01
CAMERA_SCAN_CANCEL_GRACE = 0.25

ROLE_ORDER = (
    "INPUT_LEFT",
    "INPUT_RIGHT",
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
)

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
    if sys.platform == "win32":
        return cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)
    return cv2.VideoCapture(camera_id)


def _configure_capture(capture):
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, EXPECTED_SIZE[0])
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, EXPECTED_SIZE[1])
    capture.set(cv2.CAP_PROP_FPS, REQUESTED_FPS)
    if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)


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


def _probe_capture(capture, attempts: int = 5, cancel_event=None):
    error = "камера не вернула кадр"
    for _ in range(attempts):
        if cancel_event is not None and cancel_event.is_set():
            return None, "проверка отменена"
        ok, frame = capture.read()
        if ok and frame is not None:
            error = _frame_error(frame)
            if error is None:
                return frame, None
        if cancel_event is None:
            time.sleep(0.03)
        elif cancel_event.wait(0.03):
            return None, "проверка отменена"
    return None, error


def _probe_camera_batch(camera_ids, factory):
    """Параллельно открыть и проверить одну ограниченную группу Camera ID."""

    cancel = threading.Event()
    lock = threading.Lock()
    captures = {}
    finished = set()

    def _worker(camera_id):
        capture = None
        try:
            capture = factory(camera_id)
            if capture is None or not capture.isOpened():
                return
            _configure_capture(capture)
            _frame, error = _probe_capture(
                capture,
                cancel_event=cancel,
            )
            if error is not None:
                return
            with lock:
                if cancel.is_set():
                    return
                captures[camera_id] = capture
                capture = None
        except Exception as exc:
            print(f"[CAMERA CALIBRATION] Camera {camera_id}: {exc}")
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass
            with lock:
                finished.add(camera_id)

    ids = tuple(int(camera_id) for camera_id in camera_ids)
    workers = []
    for camera_id in ids:
        worker = threading.Thread(
            target=_worker,
            args=(camera_id,),
            daemon=True,
            name=f"camera-scan-{camera_id}",
        )
        workers.append(worker)
        worker.start()

    deadline = time.monotonic() + CAMERA_SCAN_BATCH_TIMEOUT
    while time.monotonic() < deadline:
        with lock:
            if len(finished) == len(ids):
                break
        time.sleep(CAMERA_SCAN_POLL_INTERVAL)
    else:
        cancel.set()
        pending = [
            str(camera_id)
            for camera_id, worker in zip(ids, workers)
            if worker.is_alive()
        ]
        if pending:
            print(
                "[CAMERA CALIBRATION] Timeout Camera ID: "
                + ", ".join(pending)
            )

    if cancel.is_set():
        cancel_deadline = time.monotonic() + CAMERA_SCAN_CANCEL_GRACE
        for worker in workers:
            worker.join(max(0.0, cancel_deadline - time.monotonic()))

    with lock:
        return {
            camera_id: captures[camera_id]
            for camera_id in sorted(captures)
        }


def _camera_id_batches(max_tested):
    all_ids = range(max(0, int(max_tested)))
    batch = []
    for camera_id in all_ids:
        batch.append(camera_id)
        if len(batch) >= CAMERA_SCAN_BATCH_SIZE:
            yield tuple(batch)
            batch = []
    if batch:
        yield tuple(batch)


def detect_available_cameras(max_tested=CAMERA_SCAN_LIMIT, capture_factory=None):
    """Найти Camera ID, которые дают валидный production-кадр."""

    factory = capture_factory or _open_capture
    available = []
    for camera_ids in _camera_id_batches(max_tested):
        batch = _probe_camera_batch(camera_ids, factory)
        available.extend(batch)
        _release_camera_pool(batch)
    return sorted(available)


def _open_camera_pool(
    max_tested=CAMERA_SCAN_LIMIT,
    required_count=len(ROLE_ORDER),
    capture_factory=None,
):
    """Открыть необходимое число камер и оставить их handles живыми.

    Camera ID проверяются группами: семь физических устройств больше не ждут
    последовательного запуска DirectShow. Следующая группа открывается только
    если в предыдущей не набран полный комплект, поэтому лишние камеры не
    занимают USB и тестовый wizard по-прежнему останавливается на 7/7.
    """

    factory = capture_factory or _open_capture
    required = max(0, int(required_count))
    pool = {}
    for camera_ids in _camera_id_batches(max_tested):
        if len(pool) >= required:
            break
        batch = _probe_camera_batch(camera_ids, factory)
        for camera_id, capture in batch.items():
            if len(pool) < required:
                pool[camera_id] = capture
            else:
                try:
                    capture.release()
                except Exception:
                    pass
    return {camera_id: pool[camera_id] for camera_id in sorted(pool)}


def _release_camera_pool(pool):
    for capture in list(pool.values()):
        try:
            capture.release()
        except Exception:
            pass
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
        except Exception:
            pass
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

        try:
            pool = _open_camera_pool(
                self.scan_limit,
                required_count=len(ROLE_ORDER),
                capture_factory=self.capture_factory,
            )
        except Exception as exc:
            pool = {}
            scan_error = f"Ошибка поиска камер: {type(exc).__name__}: {exc}"
        else:
            scan_error = None

        with self.lock:
            if self.closed:
                _release_camera_pool(pool)
                return self._state_locked()
            self._captures = pool
            self.available_cameras = list(pool)
            if scan_error is not None:
                self.status = "ERROR"
                self.error = scan_error
                self._release_all_captures_locked()
            elif len(pool) < len(ROLE_ORDER):
                found = len(pool)
                self.status = "ERROR"
                self.error = (
                    f"Найдено исправных камер: {found}/{len(ROLE_ORDER)}. "
                    "Проверьте USB-подключение, питание и разрешение 1280x720."
                )
                self._release_all_captures_locked(keep_available=True)
            else:
                self.status = "READY"
                self.error = None
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
                frame, error = _probe_capture(capture, attempts=3)
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
        capture = self._captures.pop(camera_id, None)
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass
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
