"""Управление семью USB-камерами.

Камеры открываются волнами с перебором backend-ов. Каждая роль имеет
отдельный lock для параллельного безопасного чтения. При открытии и захвате
проверяются разрешение и яркость кадра; перед production-съёмкой доступен
дренаж буфера драйвера. Ошибка чтения блокирует менеджер до перезапуска.
"""

import inspect
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        print(f"[CAMERA] {name}={raw!r} не число, используется {default}")
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        print(f"[CAMERA] {name}={raw!r} не число, используется {default}")
        return default


CONFIG_FILE = "camera_mapping.json"

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

_CAPTURE_TIMEOUT = 5.0
_REQUESTED_FPS = 30.0
_PREFLIGHT_TIMEOUT = 5.0
_PREFLIGHT_VALID_FRAMES = 5
_PREFLIGHT_READ_INTERVAL = 0.05
_WARMUP_SECONDS = 0.5
_WARMUP_READ_INTERVAL = 0.05
_BUFFER_DRAIN_COUNT = 3
_NEAR_BLACK_MEAN_MAX = 5.0
_NEAR_BLACK_P99_MAX = 12.0
_OPEN_CONCURRENCY = 3
_OPEN_RETRY_DELAY = 0.4

_BACKEND_ALIASES = {
    "dshow": "CAP_DSHOW",
    "msmf": "CAP_MSMF",
    "v4l2": "CAP_V4L2",
    "avfoundation": "CAP_AVFOUNDATION",
    "any": "CAP_ANY",
}


def _default_backends() -> tuple:
    """Порядок backend-ов для перебора при открытии камеры.

    На Windows камера может молчать под одним API и работать под другим;
    перебор отличает «камера сломана» от «камера не дружит с API».
    """
    raw = os.environ.get("CAMERA_BACKENDS")
    if raw:
        backends = []
        for token in raw.split(","):
            attribute = _BACKEND_ALIASES.get(token.strip().lower())
            value = getattr(cv2, attribute, None) if attribute else None
            if value is not None:
                backends.append(value)
        if backends:
            return tuple(backends)
    if sys.platform == "win32":
        return tuple(
            b for b in (
                getattr(cv2, "CAP_DSHOW", None),
                getattr(cv2, "CAP_MSMF", None),
            ) if b is not None
        )
    return (getattr(cv2, "CAP_ANY", 0),)


def default_backends() -> tuple:
    return _default_backends()


def _backend_label(backend) -> str:
    if backend is None:
        return "default"
    for name in ("CAP_DSHOW", "CAP_MSMF", "CAP_V4L2", "CAP_ANY"):
        if getattr(cv2, name, None) == backend:
            return name.replace("CAP_", "")
    return str(backend)


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
        self._backends = _default_backends() or (None,)
        self._factory_takes_backend = self._factory_supports_backend(
            self._capture_factory
        )
        self._pool = None
        self.load_config()
        self._role_locks = {role: threading.Lock() for role in self.mapping}
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, len(self.mapping)),
            thread_name_prefix="camera-read",
        )
        try:
            self.open_cameras()
        except Exception:
            self._shutdown_pool()
            raise

    # ---------- инициализация ----------

    @staticmethod
    def _open_capture(camera_id, backend=None):
        if backend is None:
            return cv2.VideoCapture(camera_id)
        return cv2.VideoCapture(camera_id, backend)

    def _create_capture(self, camera_id, backend):
        if self._factory_takes_backend:
            return self._capture_factory(camera_id, backend)
        return self._capture_factory(camera_id)

    @staticmethod
    def _factory_supports_backend(factory) -> bool:
        try:
            import inspect as _inspect
            signature = _inspect.signature(factory)
        except (TypeError, ValueError):
            return False
        parameters = list(signature.parameters.values())
        if any(
            p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters
        ):
            return True
        positional = [
            p for p in parameters
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        return len(positional) >= 2

    def load_config(self):
        try:
            with open(self._config_file, encoding="utf-8") as stream:
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
        if any(type(i) is not int or i < 0 for i in ids):
            raise RuntimeError("Индексы камер должны быть неотрицательными int")
        if len(ids) != len(set(ids)):
            raise RuntimeError("Индексы камер должны быть уникальными")
        self.mapping = mapping

        print("Конфигурация камер:")
        for role, cam_id in self.mapping.items():
            print(f"  {role} -> {cam_id}")

    def open_cameras(self):
        """Открыть все камеры волнами и убедиться, что каждая отдаёт кадр."""
        started = time.monotonic()
        errors = {}
        opened = {}
        state_lock = threading.Lock()

        def _open(role, cam_id):
            attempt_errors = []
            for _retry in range(2):
                for backend in self._backends:
                    cap = None
                    try:
                        cap = self._create_capture(cam_id, backend)
                        if cap is None or not cap.isOpened():
                            attempt_errors.append("устройство не открылось")
                            continue
                        self._configure_capture(cap)
                        error = self._wait_for_stable_preflight(cap)
                        if error is not None:
                            attempt_errors.append(error)
                            cap.release()
                            cap = None
                            continue
                        with state_lock:
                            opened[role] = cap
                        return
                    except Exception as exc:
                        attempt_errors.append(f"{type(exc).__name__}: {exc}")
                        if cap is not None:
                            cap.release()
                        cap = None
                time.sleep(_OPEN_RETRY_DELAY)
            with state_lock:
                errors[role] = (
                    f"камера {cam_id} не отдала валидный кадр; "
                    + "; ".join(attempt_errors)
                )

        roles = list(self.mapping.items())
        for index in range(0, len(roles), _OPEN_CONCURRENCY):
            wave = roles[index:index + _OPEN_CONCURRENCY]
            threads = [
                threading.Thread(target=_open, args=(role, cam_id), daemon=True)
                for role, cam_id in wave
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(_PREFLIGHT_TIMEOUT * 2 + 5.0)

        self.cameras = {
            role: opened[role] for role in self.mapping if role in opened
        }

        if errors:
            self.release()
            details = ", ".join(
                f"{role}: {error}" for role, error in sorted(errors.items())
            )
            raise RuntimeError(f"Ошибка открытия камер: {details}")

        elapsed = time.monotonic() - started
        print(f"Открыто камер: {len(self.cameras)} за {elapsed:.1f} с")

    # ---------- формат и предполётная проверка ----------

    @staticmethod
    def _configure_capture(cap):
        """Запросить MJPG, 1280x720 и минимальный буфер драйвера."""
        mjpg = cv2.VideoWriter_fourcc(*"MJPG")
        cap.set(cv2.CAP_PROP_FOURCC, mjpg)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, _EXPECTED_SIZE[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _EXPECTED_SIZE[1])
        cap.set(cv2.CAP_PROP_FPS, _REQUESTED_FPS)
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    @classmethod
    def _wait_for_stable_preflight(cls, capture) -> str | None:
        """Дождаться серии валидных кадров; вернуть ошибку или None."""
        deadline = time.monotonic() + _PREFLIGHT_TIMEOUT
        consecutive_valid = 0
        empty_reads = 0
        last_error = "read returned no frame"
        while time.monotonic() < deadline:
            ok, frame = capture.read()
            if not ok or frame is None:
                consecutive_valid = 0
                empty_reads += 1
                last_error = "read returned no frame"
                time.sleep(_PREFLIGHT_READ_INTERVAL)
                continue
            last_error = cls._frame_error(frame)
            if last_error is None:
                consecutive_valid += 1
                if consecutive_valid >= _PREFLIGHT_VALID_FRAMES:
                    return None
            else:
                consecutive_valid = 0
            time.sleep(_PREFLIGHT_READ_INTERVAL)
        return (
            f"{last_error}; stable_valid={consecutive_valid}/"
            f"{_PREFLIGHT_VALID_FRAMES}; empty_reads={empty_reads}"
        )

    # ---------- прогрев ----------

    def warmup_all(self, duration: float | None = None) -> dict:
        return self.warmup_roles(tuple(self.cameras.keys()), duration=duration)

    def warmup_roles(self, roles, duration: float | None = None) -> dict:
        """Прочитать выбранные камеры в течение duration, вернув статистику."""
        actual = float(duration) if duration is not None else _WARMUP_SECONDS
        requested = tuple(dict.fromkeys(roles))
        if actual <= 0.0 or not requested or not self.cameras:
            return {}
        unknown = set(requested) - set(self.cameras)
        if unknown:
            raise RuntimeError(f"Неизвестные камеры: {sorted(unknown)}")
        self._ensure_usable()
        stats = {}
        stats_lock = threading.Lock()

        def _warm(role):
            cap = self.cameras.get(role)
            if cap is None:
                return
            lock = self._role_locks.get(role)
            reads = 0
            deadline = time.monotonic() + actual
            while time.monotonic() < deadline:
                try:
                    if lock is not None:
                        with lock:
                            ok, _ = cap.read()
                    else:
                        ok, _ = cap.read()
                    if ok:
                        reads += 1
                except Exception:
                    pass
                time.sleep(_WARMUP_READ_INTERVAL)
            with stats_lock:
                stats[role] = {"reads": reads}
            print(f"[CAMERA] Прогрев {role}: {reads} кадров за {actual:.1f}с")

        threads = [
            threading.Thread(target=_warm, args=(role,), daemon=True)
            for role in requested
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return stats

    # ---------- захват ----------

    def capture_all(self) -> dict:
        results = self.capture_roles(_REQUIRED_ROLES)
        if set(results) != set(_REQUIRED_ROLES):
            self._latch_failure("incomplete camera result")
            raise RuntimeError(f"Неполный набор кадров: {sorted(results)}")
        return results

    def capture_roles(self, roles) -> dict:
        """Параллельно прочитать указанные камеры (по lock на роль)."""
        requested = tuple(dict.fromkeys(roles))
        if not requested:
            return {}
        unknown = set(requested) - set(self.cameras)
        if unknown:
            raise RuntimeError(f"Неизвестные камеры: {sorted(unknown)}")
        self._ensure_usable()
        deadline = time.monotonic() + _CAPTURE_TIMEOUT

        def _grab(role):
            cap = self.cameras.get(role)
            if cap is None:
                raise RuntimeError(f"Камера {role} не найдена")
            with self._role_locks[role]:
                ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError(f"{role}: read returned no frame")
            error = self._frame_error(frame)
            if error is not None:
                raise RuntimeError(f"{role}: {error}")
            return frame

        futures = {role: self._pool.submit(_grab, role) for role in requested}
        errors = {}
        results = {}
        for role, future in futures.items():
            timeout = max(0.0, deadline - time.monotonic())
            try:
                results[role] = future.result(timeout=timeout)
            except Exception as exc:
                errors[role] = (
                    str(exc) or f"{type(exc).__name__}"
                )
        if errors:
            details = ", ".join(
                f"{role}: {error}" for role, error in sorted(errors.items())
            )
            self._latch_failure(details)
            raise RuntimeError(f"Ошибка камер: {details}")
        return results

    def capture_single(self, role: str):
        return self.capture_roles((role,))[role]

    def drain_buffers(self, roles=None):
        """Сбросить устаревшие кадры из внутреннего буфера драйвера."""
        requested = tuple(dict.fromkeys(roles or self.cameras.keys()))
        if not requested:
            return
        unknown = set(requested) - set(self.cameras)
        if unknown:
            raise RuntimeError(f"Неизвестные камеры: {sorted(unknown)}")
        self._ensure_usable()

        def _drain(role):
            cap = self.cameras.get(role)
            if cap is None:
                return
            lock = self._role_locks.get(role)
            for _ in range(_BUFFER_DRAIN_COUNT):
                try:
                    if lock is not None:
                        with lock:
                            cap.read()
                    else:
                        cap.read()
                except Exception:
                    pass

        futures = [self._pool.submit(_drain, role) for role in requested]
        for future in futures:
            try:
                future.result(timeout=_CAPTURE_TIMEOUT)
            except Exception:
                pass

    # ---------- восстановление ----------

    def reopen_roles(self, roles, timeout: float | None = None) -> dict:
        """Сообщить, что переоткрытие потоков без перезапуска недоступно."""
        requested = tuple(dict.fromkeys(roles))
        return {role: False for role in requested}

    # ---------- завершение ----------

    def release(self):
        with self._state_lock:
            self._closed = True
        self._shutdown_pool()
        for cap in list(self.cameras.values()):
            try:
                cap.release()
            except Exception as exc:
                print(f"[CAMERA] Ошибка освобождения камеры: {exc}")
        self.cameras.clear()

    def _shutdown_pool(self):
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False)

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

    # ---------- валидация кадра ----------

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
