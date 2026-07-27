# vision/camera_manager.py

import json
import sys
import threading
import time

import cv2
import numpy as np

CONFIG_FILE = "camera_mapping.json"
_CAPTURE_TIMEOUT = 5.0
_REQUIRED_ROLES = (
    "INPUT_LEFT",
    "INPUT_RIGHT",
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
)
_EXPECTED_SIZE = (1280, 720)
_REQUESTED_FPS = 30.0
_PREFLIGHT_TIMEOUT = 5.0
_PREFLIGHT_VALID_FRAMES = 5
_PREFLIGHT_READ_INTERVAL = 0.05
_CAMERA_STARTUP_TIMEOUT = 10.0
_CAMERA_STARTUP_POLL_INTERVAL = 0.01
_CAMERA_STARTUP_CANCEL_GRACE = 0.25
_NEAR_BLACK_MEAN_MAX = 5.0
_NEAR_BLACK_P99_MAX = 12.0
_STARTUP_CANCELLED = "startup cancelled"


class CameraManager:

    def __init__(self, config_file=CONFIG_FILE, capture_factory=None):
        self.cameras = {}
        self.mapping = {}
        self._state_lock = threading.RLock()
        self._role_locks = {}
        self._closed = False
        self._failed_reason = None
        self._config_file = config_file
        self._capture_factory = capture_factory or self._open_capture
        self.load_config()
        self._role_locks = {
            role: threading.Lock() for role in self.mapping
        }
        self.open_cameras()

    @staticmethod
    def _open_capture(camera_id):
        # DirectShow нужен на рабочем Windows-ПК. На других платформах
        # принудительный CAP_DSHOW не открывает даже исправную камеру.
        if sys.platform == "win32":
            return cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)
        return cv2.VideoCapture(camera_id)

    def load_config(self):
        try:
            with open(self._config_file, "r", encoding="utf-8") as stream:
                mapping = json.load(stream)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Файл {self._config_file} не найден. Запусти калибровку."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Ошибка чтения {self._config_file}: {exc}"
            ) from exc

        if not isinstance(mapping, dict):
            raise RuntimeError("camera_mapping.json должен содержать объект")
        missing = set(_REQUIRED_ROLES) - set(mapping)
        extra = set(mapping) - set(_REQUIRED_ROLES)
        if missing or extra:
            raise RuntimeError(
                "Неверный набор камер: "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )
        ids = list(mapping.values())
        if any(type(camera_id) is not int or camera_id < 0 for camera_id in ids):
            raise RuntimeError("Индексы камер должны быть неотрицательными int")
        if len(ids) != len(set(ids)):
            raise RuntimeError("Индексы камер должны быть уникальными")
        self.mapping = mapping

        print("Конфигурация камер:")
        for role, cam_id in self.mapping.items():
            print(f"  {role} -> {cam_id}")

    def open_cameras(self):
        """Параллельно открыть, настроить и проверить все семь камер.

        Старый последовательный запуск добавлял каждой камере фиксированные
        0,5 с, а затем по очереди ждал её экспозицию. Для семи устройств это
        давало несколько секунд искусственной задержки и до 35 с ожидания
        preflight. Независимые DirectShow handles можно запускать одновременно:
        общий timeout теперь относится ко всему комплекту, а не к каждой
        камере последовательно.
        """

        if self.cameras:
            raise RuntimeError("Камеры уже открыты")

        started = time.monotonic()
        abort = threading.Event()
        state_lock = threading.Lock()
        opened = {}
        errors = {}
        finished = set()

        def _record_error(role, message):
            with state_lock:
                # Отмена остальных workers после первой реальной ошибки не
                # должна скрывать её пачкой вторичных "startup cancelled".
                if message != _STARTUP_CANCELLED:
                    errors[role] = message
            abort.set()

        def _open_one(role, camera_id):
            capture = None
            worker_started = time.monotonic()
            try:
                if abort.is_set():
                    return
                capture = self._capture_factory(camera_id)
                if capture is None or not capture.isOpened():
                    raise RuntimeError(
                        f"Не удалось открыть камеру {camera_id} ({role})"
                    )

                self._configure_capture(capture)
                error = self._wait_for_stable_preflight(
                    capture,
                    cancel_event=abort,
                )
                if error == _STARTUP_CANCELLED:
                    return
                if error is not None:
                    raise RuntimeError(
                        f"Камера {camera_id} ({role}) не прошла preflight: "
                        f"{error}"
                    )
                if abort.is_set():
                    return

                with state_lock:
                    # abort может быть выставлен сразу после предыдущей
                    # проверки другим worker или управляющим потоком.
                    if abort.is_set():
                        return
                    opened[role] = capture
                    capture = None  # ownership transferred to ``opened``
                print(
                    f"[CAMERA] {role} ({camera_id}) готова за "
                    f"{time.monotonic() - worker_started:.2f} с"
                )
            except Exception as exc:
                _record_error(role, f"{type(exc).__name__}: {exc}")
            finally:
                if capture is not None:
                    self._safe_release_capture(capture)
                with state_lock:
                    finished.add(role)

        workers = []
        for role, camera_id in self.mapping.items():
            worker = threading.Thread(
                target=_open_one,
                args=(role, camera_id),
                daemon=True,
                name=f"camera-startup-{role}",
            )
            workers.append((role, worker))
            worker.start()

        deadline = started + _CAMERA_STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            with state_lock:
                all_finished = len(finished) == len(workers)
                has_error = bool(errors)
            if all_finished or has_error:
                break
            time.sleep(_CAMERA_STARTUP_POLL_INTERVAL)

        with state_lock:
            all_finished = len(finished) == len(workers)
            has_error = bool(errors)
        startup_timed_out = not all_finished and not has_error
        if not all_finished:
            abort.set()
            cancel_deadline = time.monotonic() + _CAMERA_STARTUP_CANCEL_GRACE
            for _role, worker in workers:
                worker.join(max(0.0, cancel_deadline - time.monotonic()))

        with state_lock:
            pending = [
                role for role, worker in workers if worker.is_alive()
            ]
            snapshot_errors = dict(errors)
            snapshot_opened = dict(opened)
            snapshot_finished = set(finished)

        if startup_timed_out:
            unfinished = pending or sorted(set(self.mapping) - snapshot_finished)
            suffix = (
                f"; не завершены: {', '.join(unfinished)}"
                if unfinished else ""
            )
            snapshot_errors["STARTUP"] = (
                "общий timeout открытия камер "
                f"{_CAMERA_STARTUP_TIMEOUT:.1f} с{suffix}"
            )
        elif pending:
            snapshot_errors["STARTUP"] = (
                "не завершены после отмены: " + ", ".join(pending)
            )

        expected = set(self.mapping)
        actual = set(snapshot_opened)
        if not snapshot_errors and actual != expected:
            snapshot_errors["STARTUP"] = (
                f"неполный набор: expected={sorted(expected)}, "
                f"actual={sorted(actual)}"
            )

        if snapshot_errors:
            abort.set()
            self._release_capture_map(snapshot_opened)
            with self._state_lock:
                self._closed = True
            details = "; ".join(
                f"{role}: {message}"
                for role, message in sorted(snapshot_errors.items())
            )
            raise RuntimeError(f"Ошибка запуска камер: {details}")

        # Сохраняем порядок ролей из mapping независимо от порядка завершения
        # worker-потоков. Это делает UI и диагностику детерминированными.
        self.cameras = {
            role: snapshot_opened[role] for role in self.mapping
        }
        elapsed = time.monotonic() - started
        print(f"Открыто камер: {len(self.cameras)} за {elapsed:.2f} с")
        print(
            f"[CAMERA] Запрошено: {_EXPECTED_SIZE[0]}x{_EXPECTED_SIZE[1]} "
            f"MJPG @ {_REQUESTED_FPS:.0f} FPS"
        )

    @staticmethod
    def _configure_capture(capture):
        # MJPG существенно снижает USB-нагрузку семи камер. Не все драйверы
        # подтверждают set(), поэтому результат проверяется по реальным кадрам.
        capture.set(
            cv2.CAP_PROP_FOURCC,
            cv2.VideoWriter_fourcc(*"MJPG"),
        )
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, _EXPECTED_SIZE[0])
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, _EXPECTED_SIZE[1])
        capture.set(cv2.CAP_PROP_FPS, _REQUESTED_FPS)
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            # DirectShow может проигнорировать свойство, но backends, которые
            # его поддерживают, не оставят read() заблокированным бесконечно.
            capture.set(
                cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                int(_CAPTURE_TIMEOUT * 1000),
            )

    @classmethod
    def _wait_for_stable_preflight(
        cls,
        capture,
        *,
        cancel_event=None,
    ) -> str | None:
        """Дождаться серии валидных кадров после запуска экспозиции камеры."""

        deadline = time.monotonic() + _PREFLIGHT_TIMEOUT
        consecutive_valid = 0
        last_error = "read returned no frame"
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return _STARTUP_CANCELLED
            try:
                ok, frame = capture.read()
            except Exception as exc:
                consecutive_valid = 0
                last_error = f"{type(exc).__name__}: {exc}"
                if cls._preflight_wait(cancel_event):
                    return _STARTUP_CANCELLED
                continue
            if not ok or frame is None:
                consecutive_valid = 0
                last_error = "read returned no frame"
                if cls._preflight_wait(cancel_event):
                    return _STARTUP_CANCELLED
                continue
            last_frame_error = cls._frame_error(frame)
            if last_frame_error is None:
                consecutive_valid += 1
                if consecutive_valid >= _PREFLIGHT_VALID_FRAMES:
                    return None
                # Успешный read реальной камеры уже синхронизирован с её FPS.
                # Дополнительные 50 мс между каждым из пяти хороших кадров
                # только замедляли запуск; пауза нужна лишь при невалидном read.
                continue
            consecutive_valid = 0
            last_error = last_frame_error
            if cls._preflight_wait(cancel_event):
                return _STARTUP_CANCELLED
        return (
            f"{last_error}; stable_valid={consecutive_valid}/"
            f"{_PREFLIGHT_VALID_FRAMES}; timeout={_PREFLIGHT_TIMEOUT:.1f}s"
        )

    @staticmethod
    def _preflight_wait(cancel_event) -> bool:
        if cancel_event is None:
            time.sleep(_PREFLIGHT_READ_INTERVAL)
            return False
        return cancel_event.wait(_PREFLIGHT_READ_INTERVAL)

    def capture_all(self) -> dict:
        """Одновременный захват полного production-набора камер."""
        results = self.capture_roles(_REQUIRED_ROLES)
        if set(results) != set(_REQUIRED_ROLES):
            self._latch_failure("incomplete camera result")
            raise RuntimeError(
                f"Неполный набор кадров: {sorted(results)}"
            )
        return results

    def capture_roles(self, roles) -> dict:
        """Параллельно прочитать указанные независимые камеры.

        У каждой VideoCapture свой lock. Поэтому LIVE выбранной камеры не
        блокируется чтением остальных шести камер, а одна и та же камера
        никогда не читается конкурентно из двух потоков.
        """
        requested = tuple(dict.fromkeys(roles))
        if not requested:
            return {}
        unknown = set(requested) - set(self.cameras)
        if unknown:
            raise RuntimeError(f"Неизвестные камеры: {sorted(unknown)}")
        self._ensure_usable()

        errors = {}
        results = {}
        result_lock = threading.Lock()

        def _grab(role):
            frame = None
            error = None
            try:
                with self._role_locks[role]:
                    self._ensure_usable()
                    cap = self.cameras.get(role)
                    if cap is None:
                        raise RuntimeError(f"Камера {role} не найдена")
                    ok, frame = cap.read()
                error = (
                    None
                    if ok and frame is not None
                    else "read returned no frame"
                )
                if error is None:
                    error = self._frame_error(frame)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            with result_lock:
                if error is None:
                    results[role] = frame
                else:
                    errors[role] = error

        threads = []
        for role in requested:
            thread = threading.Thread(
                target=_grab,
                args=(role,),
                daemon=True,
                name=f"capture-{role}",
            )
            threads.append((role, thread))
            thread.start()

        deadline = time.monotonic() + _CAPTURE_TIMEOUT
        for role, thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                errors[role] = "capture timeout"

        if errors:
            details = ", ".join(
                f"{role}: {error}" for role, error in sorted(errors.items())
            )
            self._latch_failure(details)
            raise RuntimeError(f"Ошибка камер: {details}")
        if set(results) != set(requested):
            details = (
                f"incomplete subset: expected={sorted(requested)}, "
                f"actual={sorted(results)}"
            )
            self._latch_failure(details)
            raise RuntimeError(f"Неполный набор кадров: {details}")
        return results

    def capture_single(self, role: str):
        """Прочитать одну камеру, не блокируя другие роли."""
        return self.capture_roles((role,))[role]

    def release(self):
        with self._state_lock:
            self._closed = True
        self._release_capture_map(self.cameras)
        self.cameras.clear()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass

    @classmethod
    def _release_capture_map(cls, captures):
        released_ids = set()
        for capture in list(captures.values()):
            # Защита от случайного двойного alias одного VideoCapture.
            identity = id(capture)
            if identity in released_ids:
                continue
            released_ids.add(identity)
            cls._safe_release_capture(capture)

    @staticmethod
    def _safe_release_capture(capture):
        try:
            capture.release()
        except Exception as exc:
            print(f"[CAMERA] Ошибка освобождения камеры: {exc}")

    def _latch_failure(self, reason: str):
        with self._state_lock:
            if self._failed_reason is None:
                self._failed_reason = reason

    def _ensure_usable(self):
        with self._state_lock:
            if self._closed:
                raise RuntimeError("CameraManager уже закрыт")
            if self._failed_reason is not None:
                raise RuntimeError(
                    "CameraManager заблокирован после ошибки: "
                    f"{self._failed_reason}"
                )

    @staticmethod
    def _frame_error(frame):
        array = np.asarray(frame)
        if array.ndim != 3 or array.shape[2] < 3:
            return f"invalid frame shape: {array.shape}"
        height, width = array.shape[:2]
        if (width, height) != _EXPECTED_SIZE:
            return (
                f"invalid resolution {width}x{height}; "
                f"expected {_EXPECTED_SIZE[0]}x{_EXPECTED_SIZE[1]}"
            )
        sample = array[::12, ::12, :3].astype(np.float32)
        luminance = sample.mean(axis=2)
        mean = float(luminance.mean())
        p99 = float(np.percentile(luminance, 99))
        if mean <= _NEAR_BLACK_MEAN_MAX and p99 <= _NEAR_BLACK_P99_MAX:
            return f"near-black frame: mean={mean:.2f}, p99={p99:.2f}"
        return None
