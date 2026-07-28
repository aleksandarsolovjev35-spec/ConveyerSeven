import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

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
_PREFLIGHT_TIMEOUT = 3.0
# Общий предел на параллельное открытие: preflight каждой камеры уже
# ограничен _PREFLIGHT_TIMEOUT, здесь запас на инициализацию драйвера.
_OPEN_TIMEOUT = 30.0
_PREFLIGHT_VALID_FRAMES = 5
_PREFLIGHT_READ_INTERVAL = 0.05
_NEAR_BLACK_MEAN_MAX = 5.0
_NEAR_BLACK_P99_MAX = 12.0


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
    def _open_capture(camera_id):
        return cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)

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
        """Открыть все камеры параллельно.

        Preflight каждой камеры ждёт стабилизации экспозиции до
        ``_PREFLIGHT_TIMEOUT``. Последовательно это давало бы задержку,
        линейно растущую с числом камер, поэтому роли открываются
        одновременно, по потоку на камеру. Ошибки собираются по всем
        камерам сразу: оператор видит полный список проблем, а не первую.
        """
        started = time.monotonic()
        errors = {}
        opened = {}
        opened_lock = threading.Lock()

        def _open(role, cam_id):
            cap = None
            try:
                cap = self._capture_factory(cam_id)
                if not cap.isOpened():
                    raise RuntimeError(
                        f"Не удалось открыть камеру {cam_id} ({role})"
                    )
                self._configure_capture(cap)
                with opened_lock:
                    opened[role] = cap
                cap = None

                error = self._wait_for_stable_preflight(opened[role])
                if error is not None:
                    raise RuntimeError(
                        f"Камера {cam_id} ({role}) не прошла preflight: {error}"
                    )
            except Exception as exc:
                with opened_lock:
                    errors[role] = f"{type(exc).__name__}: {exc}"
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception as release_error:
                        print(
                            f"[CAMERA] Ошибка освобождения {role}: "
                            f"{release_error}"
                        )

        threads = [
            threading.Thread(
                target=_open,
                args=(role, cam_id),
                daemon=True,
                name=f"open-camera-{role}",
            )
            for role, cam_id in self.mapping.items()
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + _OPEN_TIMEOUT
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                with opened_lock:
                    errors.setdefault(thread.name, "open timeout")

        # Порядок ролей задан camera_mapping.json и не должен зависеть
        # от того, какой поток завершился первым.
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

    @classmethod
    def _wait_for_stable_preflight(cls, capture) -> str | None:
        """Дождаться серии валидных кадров после запуска экспозиции камеры."""

        deadline = time.monotonic() + _PREFLIGHT_TIMEOUT
        consecutive_valid = 0
        last_error = "read returned no frame"
        while time.monotonic() < deadline:
            try:
                ok, frame = capture.read()
            except Exception as exc:
                consecutive_valid = 0
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(_PREFLIGHT_READ_INTERVAL)
                continue
            if not ok or frame is None:
                consecutive_valid = 0
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
            f"{_PREFLIGHT_VALID_FRAMES}; timeout={_PREFLIGHT_TIMEOUT:.1f}s"
        )

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
