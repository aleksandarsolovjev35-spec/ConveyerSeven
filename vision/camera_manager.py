import inspect
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

import cv2
import numpy as np


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        print(f"[CAMERA] {name}={raw!r} не число, используется {default}")
        return default


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        print(f"[CAMERA] {name}={raw!r} не число, используется {default}")
        return default


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
# Семь USB-камер стартуют не мгновенно: UVC-драйвер сначала строит граф,
# затем резервирует полосу шины и только после этого отдаёт первый кадр.
# Трёх секунд последней в очереди камере не хватало, и старт падал на
# "read returned no frame" даже на исправном железе.
_PREFLIGHT_TIMEOUT = _env_float("CAMERA_PREFLIGHT_TIMEOUT", 10.0)
# Каждая попытка — это полное пересоздание VideoCapture. Разовая
# неудача резервирования полосы на USB-хабе лечится именно повтором, а
# не увеличением таймаута.
_OPEN_ATTEMPTS = _env_int("CAMERA_OPEN_ATTEMPTS", 2)
_OPEN_RETRY_DELAY = _env_float("CAMERA_OPEN_RETRY_DELAY", 0.4, minimum=0.0)
# Одновременный старт всех семи камер перегружает и DirectShow, и
# USB-контроллер: устройства наперегонки запрашивают полосу, и часть из
# них остаётся без изохронных слотов. Открываем волнами.
_OPEN_CONCURRENCY = _env_int("CAMERA_OPEN_CONCURRENCY", 3)
# Общий предел на открытие набора: preflight каждой попытки уже ограничен
# _PREFLIGHT_TIMEOUT, здесь запас на инициализацию драйвера и повторы.
_OPEN_TIMEOUT = _env_float("CAMERA_OPEN_TIMEOUT", 120.0)
_WARMUP_FRAMES = _env_int("CAMERA_WARMUP_FRAMES", 15)
_PREFLIGHT_VALID_FRAMES = 5
_PREFLIGHT_READ_INTERVAL = 0.05
_NEAR_BLACK_MEAN_MAX = 5.0
_NEAR_BLACK_P99_MAX = 12.0

_BACKEND_ALIASES = {
    "dshow": "CAP_DSHOW",
    "msmf": "CAP_MSMF",
    "v4l2": "CAP_V4L2",
    "avfoundation": "CAP_AVFOUNDATION",
    "gstreamer": "CAP_GSTREAMER",
    "any": "CAP_ANY",
}


def _default_backends() -> tuple:
    """Порядок backend-ов для перебора при открытии камеры.

    На Windows одна и та же камера может молчать под DirectShow и
    нормально работать под Media Foundation (и наоборот): это зависит от
    UVC-прошивки, а не от исправности устройства. Перебор backend-ов
    отличает "камера сломана" от "камера не дружит с этим API".
    """
    raw = os.environ.get("CAMERA_BACKENDS")
    if raw:
        backends = []
        for token in raw.split(","):
            attribute = _BACKEND_ALIASES.get(token.strip().lower())
            value = getattr(cv2, attribute, None) if attribute else None
            if value is None:
                print(f"[CAMERA] Неизвестный backend {token!r}, пропущен")
                continue
            backends.append(value)
        if backends:
            return tuple(backends)
    if sys.platform == "win32":
        return tuple(
            backend
            for backend in (
                getattr(cv2, "CAP_DSHOW", None),
                getattr(cv2, "CAP_MSMF", None),
            )
            if backend is not None
        )
    return (getattr(cv2, "CAP_ANY", 0),)


def default_backends() -> tuple:
    """Публичный доступ к порядку backend-ов (используется калибратором)."""
    return _default_backends()


def _backend_label(backend) -> str:
    if backend is None:
        return "default"
    for name in (
        "CAP_DSHOW",
        "CAP_MSMF",
        "CAP_V4L2",
        "CAP_AVFOUNDATION",
        "CAP_GSTREAMER",
        "CAP_ANY",
    ):
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
        self._role_locks = {
            role: threading.Lock() for role in self.mapping
        }
        # Пул живёт всё время работы менеджера: захват набора кадров
        # происходит несколько раз на каждый шаг линии, и создавать семь
        # потоков заново на каждый захват незачем.
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, len(self.mapping)),
            thread_name_prefix="camera-read",
        )
        try:
            self.open_cameras()
        except Exception:
            self._shutdown_pool()
            raise

    def _require_pool(self) -> ThreadPoolExecutor:
        pool = self._pool
        if pool is None:
            raise RuntimeError("CameraManager уже закрыт")
        return pool

    def _shutdown_pool(self):
        pool, self._pool = self._pool, None
        if pool is not None:
            # Не ждём зависшие чтения драйвера: release() не должен
            # блокироваться на камере, которая перестала отвечать.
            pool.shutdown(wait=False)

    @staticmethod
    def _factory_supports_backend(factory) -> bool:
        """Понять, принимает ли фабрика backend вторым аргументом.

        Тесты и калибровка передают простую ``lambda camera_id``; ломать
        их сигнатуру перебором backend-ов нельзя.
        """
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return False
        parameters = list(signature.parameters.values())
        if any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters
        ):
            return True
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        return len(positional) >= 2

    @staticmethod
    def _open_capture(camera_id, backend=None):
        if backend is None:
            return cv2.VideoCapture(camera_id)
        return cv2.VideoCapture(camera_id, backend)

    def _create_capture(self, camera_id, backend):
        if self._factory_takes_backend:
            return self._capture_factory(camera_id, backend)
        return self._capture_factory(camera_id)

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
        if any(type(camera_id) is not int or camera_id < 0 for camera_id in ids):
            raise RuntimeError("Индексы камер должны быть неотрицательными int")
        if len(ids) != len(set(ids)):
            raise RuntimeError("Индексы камер должны быть уникальными")
        self.mapping = mapping

        print("Конфигурация камер:")
        for role, cam_id in self.mapping.items():
            print(f"  {role} -> {cam_id}")

    def open_cameras(self):
        """Открыть все камеры и убедиться, что каждая отдаёт кадр.

        Открытие идёт волнами по ``_OPEN_CONCURRENCY`` камер, а не всеми
        семью сразу: одновременный старт перегружает USB-контроллер, и
        часть камер остаётся без изохронной полосы, отдавая
        "read returned no frame" на исправном железе. Внутри волны
        камеры стартуют параллельно, поэтому preflight не суммируется.

        Каждая камера получает ``_OPEN_ATTEMPTS`` попыток и перебор
        backend-ов: разовый отказ резервирования полосы лечится
        пересозданием VideoCapture, а не увеличением таймаута. Ошибки
        собираются по всем камерам сразу: оператор видит полный список
        проблем, а не первую.
        """
        started = time.monotonic()
        errors = {}
        opened = {}
        state_lock = threading.Lock()
        finalized = False
        deadline = started + _OPEN_TIMEOUT

        def _publish(role, capture) -> bool:
            """Отдать открытую камеру менеджеру.

            Поток, признанный зависшим по общему таймауту, может
            завершиться позже. Его результат уже никому не нужен, и
            класть его в набор нельзя: handle остался бы навсегда.
            """
            with state_lock:
                if finalized:
                    return False
                opened[role] = capture
                return True

        def _try_once(role, cam_id, backend):
            cap = None
            try:
                cap = self._create_capture(cam_id, backend)
                if cap is None or not cap.isOpened():
                    return None, "устройство не открылось"
                self._configure_capture(cap)
                error = self._wait_for_stable_preflight(cap)
                if error is not None:
                    self._safe_release(role, cap)
                    return None, error
                capture, cap = cap, None
                return capture, None
            except Exception as exc:
                return None, f"{type(exc).__name__}: {exc}"
            finally:
                if cap is not None:
                    self._safe_release(role, cap)

        def _open(role, cam_id):
            attempt_errors = []
            attempt = 0
            for retry in range(_OPEN_ATTEMPTS):
                for backend in self._backends:
                    attempt += 1
                    if time.monotonic() >= deadline:
                        attempt_errors.append("общий таймаут открытия камер")
                        break
                    capture, error = _try_once(role, cam_id, backend)
                    if capture is not None:
                        if attempt > 1:
                            print(
                                f"[CAMERA] {role} (id={cam_id}) открыта "
                                f"с попытки {attempt} "
                                f"[{_backend_label(backend)}]"
                            )
                        if not _publish(role, capture):
                            self._safe_release(role, capture)
                        return
                    attempt_errors.append(
                        f"попытка {attempt} [{_backend_label(backend)}]: "
                        f"{error}"
                    )
                    print(
                        f"[CAMERA] {role} (id={cam_id}) попытка {attempt} "
                        f"[{_backend_label(backend)}]: {error}"
                    )
                else:
                    if retry + 1 < _OPEN_ATTEMPTS and _OPEN_RETRY_DELAY:
                        # Драйверу нужно время, чтобы отпустить устройство
                        # перед повторным открытием.
                        time.sleep(_OPEN_RETRY_DELAY)
                    continue
                break
            with state_lock:
                errors[role] = (
                    f"камера {cam_id} не отдала валидный кадр; "
                    + "; ".join(attempt_errors)
                )

        roles = list(self.mapping.items())
        wave_size = max(1, min(_OPEN_CONCURRENCY, len(roles) or 1))
        for index in range(0, len(roles), wave_size):
            wave = roles[index : index + wave_size]
            threads = [
                threading.Thread(
                    target=_open,
                    args=(role, cam_id),
                    daemon=True,
                    name=f"open-camera-{role}",
                )
                for role, cam_id in wave
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    with state_lock:
                        errors.setdefault(
                            thread.name.replace("open-camera-", ""),
                            "open timeout",
                        )

        with state_lock:
            # Порядок ролей задан camera_mapping.json и не должен зависеть
            # от того, какой поток завершился первым.
            finalized = True
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
        print(
            f"[CAMERA] Запрошено: {_EXPECTED_SIZE[0]}x{_EXPECTED_SIZE[1]} "
            f"MJPG @ {_REQUESTED_FPS:.0f} FPS"
        )

    @staticmethod
    def _safe_release(role, capture):
        try:
            capture.release()
        except Exception as exc:
            print(f"[CAMERA] Ошибка освобождения {role}: {exc}")

    @staticmethod
    def _configure_capture(cap):
        # MJPG существенно снижает USB-нагрузку семи камер. Не все
        # драйверы подтверждают set(), поэтому реальный результат
        # контролируется по фактической частоте, а не по return value.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, _EXPECTED_SIZE[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _EXPECTED_SIZE[1])
        cap.set(cv2.CAP_PROP_FPS, _REQUESTED_FPS)
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    @staticmethod
    def _negotiated_format(capture) -> str:
        """Что драйвер реально согласовал после set().

        Молчаливый откат MJPG -> YUY2 поднимает поток одной камеры
        1280x720@30 примерно с 25 Мбит/с до ~440 Мбит/с. На общем
        USB-контроллере это ровно та ситуация, когда часть камер
        открывается, а последняя не получает полосу и не отдаёт кадры.
        """
        try:
            fourcc = int(capture.get(cv2.CAP_PROP_FOURCC))
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(capture.get(cv2.CAP_PROP_FPS))
        except Exception:
            return "формат недоступен"
        codec = (
            "".join(chr((fourcc >> shift) & 0xFF) for shift in (0, 8, 16, 24))
            if fourcc
            else "----"
        )
        codec = "".join(char if char.isprintable() else "?" for char in codec)
        return f"{codec} {width}x{height}@{fps:.0f}"

    @classmethod
    def _wait_for_stable_preflight(
        cls, capture, warmup_frames: int | None = None,
    ) -> str | None:
        """Дождаться серии валидных кадров после запуска экспозиции камеры."""
        warmup_count = _WARMUP_FRAMES if warmup_frames is None else warmup_frames
        for _ in range(max(0, warmup_count)):
            try:
                capture.read()
            except Exception:
                break

        deadline = time.monotonic() + _PREFLIGHT_TIMEOUT
        consecutive_valid = 0
        empty_reads = 0
        total_reads = 0
        last_error = "read returned no frame"
        while time.monotonic() < deadline:
            total_reads += 1
            try:
                ok, frame = capture.read()
            except Exception as exc:
                consecutive_valid = 0
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(_PREFLIGHT_READ_INTERVAL)
                continue
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
            f"{_PREFLIGHT_VALID_FRAMES}; empty_reads={empty_reads}/"
            f"{total_reads}; negotiated={cls._negotiated_format(capture)}; "
            f"timeout={_PREFLIGHT_TIMEOUT:.1f}s"
        )

    def warmup_cameras(self, frames: int | None = None):
        """Прогрев камер (вычитка кадров для стабилизации экспозиции после простоя)."""
        pool = self._require_pool()
        warmup_count = _WARMUP_FRAMES if frames is None else int(frames)

        def _warmup_role(role):
            with self._role_locks[role]:
                cap = self.cameras.get(role)
                if cap is None:
                    return
                for _ in range(max(1, warmup_count)):
                    try:
                        cap.read()
                    except Exception:
                        break

        list(pool.map(_warmup_role, list(self.cameras)))

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

        У каждой VideoCapture свой lock, поэтому одна камера никогда не
        читается двумя потоками сразу, а чтение выбранной роли не ждёт
        остальных шести.

        Результаты собираются через futures: истёкший таймаут не бросает
        исключение поверх ещё работающих воркеров. Зависшая камера
        латчит менеджер, и следующий вызов сразу получит отказ, вместо
        того чтобы копить фоновые чтения одного и того же устройства.
        """
        requested = tuple(dict.fromkeys(roles))
        if not requested:
            return {}
        unknown = set(requested) - set(self.cameras)
        if unknown:
            raise RuntimeError(f"Неизвестные камеры: {sorted(unknown)}")
        self._ensure_usable()

        def _grab(role):
            with self._role_locks[role]:
                self._ensure_usable()
                cap = self.cameras.get(role)
                if cap is None:
                    raise RuntimeError(f"Камера {role} не найдена")
                ok, frame = cap.read()
                if not ok or frame is None:
                    raise RuntimeError("read returned no frame")
                error = self._frame_error(frame)
                if error is not None and "near-black" in error:
                    # Прогрев после простоя: если камера отдала тёмный кадр,
                    # даём автоэкспозиции подстроиться на серии кадров
                    for _ in range(_WARMUP_FRAMES):
                        ok, next_frame = cap.read()
                        if ok and next_frame is not None:
                            frame = next_frame
                            error = self._frame_error(frame)
                            if error is None:
                                break
            if error is not None:
                raise RuntimeError(error)
            return frame

        pool = self._require_pool()
        futures = {role: pool.submit(_grab, role) for role in requested}

        errors = {}
        results = {}
        deadline = time.monotonic() + _CAPTURE_TIMEOUT
        for role, future in futures.items():
            timeout = max(0.0, deadline - time.monotonic())
            try:
                results[role] = future.result(timeout=timeout)
            except FuturesTimeoutError:
                # Воркер продолжает удерживать role-lock. Отменить чтение
                # драйвера нельзя, поэтому менеджер выводится из работы.
                errors[role] = "capture timeout"
            except Exception as exc:
                errors[role] = f"{type(exc).__name__}: {exc}"

        if errors:
            details = ", ".join(
                f"{role}: {error}" for role, error in sorted(errors.items())
            )
            self._latch_failure(details)
            raise RuntimeError(f"Ошибка камер: {details}")
        return results

    def capture_single(self, role: str):
        """Прочитать одну камеру, не блокируя другие роли."""
        return self.capture_roles((role,))[role]

    def release(self):
        # Менеджер не открывает окна OpenCV, поэтому destroyAllWindows()
        # здесь не нужен: на headless-сборках он лишь бросает исключение.
        with self._state_lock:
            self._closed = True
        self._shutdown_pool()
        for cap in list(self.cameras.values()):
            try:
                cap.release()
            except Exception as exc:
                print(f"[CAMERA] Ошибка освобождения камеры: {exc}")
        self.cameras.clear()

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
