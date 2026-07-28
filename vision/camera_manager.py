import json
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
_PREFLIGHT_TIMEOUT = 3.0
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
        self.load_config()
        self._role_locks = {
            role: threading.Lock() for role in self.mapping
        }
        self.open_cameras()

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
        try:
            for role, cam_id in self.mapping.items():
                cap = self._capture_factory(cam_id)
                if not cap.isOpened():
                    try:
                        cap.release()
                    finally:
                        raise RuntimeError(
                            f"Не удалось открыть камеру {cam_id} ({role})"
                        )

                # MJPG существенно снижает USB-нагрузку семи камер. Не все
                # драйверы подтверждают set(), поэтому реальный результат
                # контролируется по фактической частоте, а не по return value.
                cap.set(
                    cv2.CAP_PROP_FOURCC,
                    cv2.VideoWriter_fourcc(*"MJPG"),
                )
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, _EXPECTED_SIZE[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _EXPECTED_SIZE[1])
                cap.set(cv2.CAP_PROP_FPS, _REQUESTED_FPS)
                if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.cameras[role] = cap

                error = self._wait_for_stable_preflight(cap)
                if error is not None:
                    raise RuntimeError(
                        f"Камера {cam_id} ({role}) не прошла preflight: {error}"
                    )
        except Exception:
            self.release()
            raise

        print(f"Открыто камер: {len(self.cameras)}")
        print(
            f"[CAMERA] Запрошено: {_EXPECTED_SIZE[0]}x{_EXPECTED_SIZE[1]} "
            f"MJPG @ {_REQUESTED_FPS:.0f} FPS"
        )

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
        # Менеджер не открывает окна OpenCV, поэтому destroyAllWindows()
        # здесь не нужен: на headless-сборках он лишь бросает исключение.
        with self._state_lock:
            self._closed = True
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
