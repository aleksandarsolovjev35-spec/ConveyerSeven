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


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
# Частота кадров — прямой множитель полосы USB на камеру. На слабом
# контроллере (все семь камер на одном корневом хабе) CAMERA_FPS=15
# делит нагрузку пополам: для статичной инспекции 15 кадров достаточно,
# а изохронных слотов хватает всем.
_REQUESTED_FPS = _env_float("CAMERA_FPS", 30.0, minimum=1.0)
# Семь USB-камер стартуют не мгновенно: UVC-драйвер сначала строит граф,
# затем резервирует полосу шины и только после этого отдаёт первый кадр.
# Трёх секунд последней в очереди камере не хватало, и старт падал на
# "read returned no frame" даже на исправном железе.
_PREFLIGHT_TIMEOUT = _env_float("CAMERA_PREFLIGHT_TIMEOUT", 5.0, minimum=0.5)
# Каждая попытка — это полное пересоздание VideoCapture. Разовая
# неудача резервирования полосы на USB-хабе лечится именно повтором, а
# не увеличением таймаута.
_OPEN_ATTEMPTS = _env_int("CAMERA_OPEN_ATTEMPTS", 2, minimum=1)
_OPEN_RETRY_DELAY = _env_float("CAMERA_OPEN_RETRY_DELAY", 0.4, minimum=0.0)
# Одновременный старт всех семи камер перегружает и DirectShow, и
# USB-контроллер: устройства наперегонки запрашивают полосу, и часть из
# них остаётся без изохронных слотов. Открываем волнами.
_OPEN_CONCURRENCY = _env_int("CAMERA_OPEN_CONCURRENCY", 3, minimum=1)
# Общий предел на открытие набора: preflight каждой попытки уже ограничен
# _PREFLIGHT_TIMEOUT, здесь запас на инициализацию драйвера и повторы.
_OPEN_TIMEOUT = _env_float("CAMERA_OPEN_TIMEOUT", 120.0, minimum=5.0)
_PREFLIGHT_VALID_FRAMES = 5
_PREFLIGHT_READ_INTERVAL = 0.05
_NEAR_BLACK_MEAN_MAX = 5.0
_NEAR_BLACK_P99_MAX = 12.0
# Прогрев камер после простоя: первые кадры могут быть тёмными из-за
# не успевшего выставить экспозицию AGC. Отбрасываем их.
# Короткая фаза в preflight (0.5с) + длинный прогрев в main (2.5с).
_WARMUP_SECONDS = _env_float("CAMERA_WARMUP_SECONDS", 0.5, minimum=0.0)
_WARMUP_READ_INTERVAL = _env_float(
    "CAMERA_WARMUP_READ_INTERVAL", 0.05, minimum=0.01
)
# Повтор при пустом/near-black кадре во время обычного захвата. После
# загрузки моделей камеры могли простаивать несколько секунд, и отдельные
# UVC-устройства снова отдавали 1-2 секунды пустые или тёмные кадры.
# 30 * 0.08 ~= 2.4с — меньше общего _CAPTURE_TIMEOUT, но достаточно,
# чтобы переждать повторную автоэкспозицию перед preview/инспекцией.
_DARK_RETRY_ATTEMPTS = _env_int("CAMERA_DARK_RETRY_ATTEMPTS", 30, minimum=0)
_DARK_RETRY_INTERVAL = _env_float(
    "CAMERA_DARK_RETRY_INTERVAL", 0.08, minimum=0.01
)
# Восстановление потока в production: при просадке питания/полосы USB
# драйвер прекращает отдавать кадры, оставляя устройство "открытым", и
# единственное лекарство — пересоздать VideoCapture конкретной роли.
# Раньше любой такой сбой навсегда латчил весь CameraManager; теперь роль
# пересоздаётся на месте, а латч остаётся только для случаев, когда и
# пересоздание не помогло.
_RECOVERY_ATTEMPTS = _env_int("CAMERA_RECOVERY_ATTEMPTS", 2, minimum=0)
_RECOVERY_PREFLIGHT = _env_float(
    "CAMERA_RECOVERY_PREFLIGHT", 2.0, minimum=0.2
)
# Бюджет на пересоздание одного потока при стартовом восстановлении
# (reopen_roles). Мёртвый поток повторным чтением не оживить, но и
# виснуть на семи мёртвых камерах подряд старт не должен.
_RECOVERY_REOPEN_SECONDS = _env_float(
    "CAMERA_RECOVERY_REOPEN_SECONDS", 6.0, minimum=1.0
)

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

    def warmup_all(self, duration: float | None = None) -> dict:
        """Прогреть все открытые камеры после простоя.

        После долгого простоя AGC камер уходит в минимум и первые кадры
        оказываются почти чёрными, что приводило к ``near-black frame``
        ошибке на этапе preview. Метод читает камеры в течение
        ``duration`` секунд, позволяя автоэкспозиции стабилизироваться,
        и возвращает статистику по каждой роли.

        Отдельный шаг используется в ``main.py`` после открытия камер и
        перед первым ``capture_all``.
        """
        return self.warmup_roles(tuple(self.cameras.keys()), duration=duration)

    def warmup_roles(self, roles, duration: float | None = None) -> dict:
        """Прогреть выбранные роли камер и вернуть статистику чтений.

        Используется не только для общего прогрева, но и для точечного
        восстановления роли, которая после простоя отдаёт пустые/тёмные
        кадры. Метод не латчит CameraManager в ошибку: прогрев является
        подготовительной процедурой, а не production-захватом.
        """
        requested = tuple(dict.fromkeys(roles))
        actual_duration = (
            float(duration) if duration is not None else float(_WARMUP_SECONDS)
        )
        if actual_duration <= 0.0 or not requested:
            return {}
        if not self.cameras:
            return {}
        unknown = set(requested) - set(self.cameras)
        if unknown:
            raise RuntimeError(f"Неизвестные камеры: {sorted(unknown)}")
        self._ensure_usable()
        stats = {}
        stats_lock = threading.Lock()

        def _warm(role: str):
            cap = self.cameras.get(role)
            if cap is None:
                return
            lock = self._role_locks.get(role)
            reads = 0
            darkest = 255.0
            brightest = 0.0
            deadline = time.monotonic() + actual_duration
            while time.monotonic() < deadline:
                try:
                    if lock is not None:
                        with lock:
                            ok, frame = cap.read()
                    else:
                        ok, frame = cap.read()
                except Exception:
                    ok = False
                    frame = None
                if ok and frame is not None:
                    reads += 1
                    try:
                        # Быстрая оценка яркости по разреженной выборке
                        sample = np.asarray(frame)[::24, ::24]
                        mean = float(sample.mean())
                        darkest = min(darkest, mean)
                        brightest = max(brightest, mean)
                    except Exception:
                        pass
                time.sleep(_WARMUP_READ_INTERVAL)
            with stats_lock:
                stats[role] = {
                    "reads": reads,
                    "darkest": darkest,
                    "brightest": brightest,
                }
            if reads:
                print(
                    f"[CAMERA] Прогрев {role}: {reads} кадров, "
                    f"luminance {darkest:.1f} -> {brightest:.1f} за {actual_duration:.1f}с"
                )
            else:
                print(
                    f"[CAMERA] Прогрев {role}: нет кадров за {actual_duration:.1f}с"
                )

        threads = []
        for role in requested:
            thread = threading.Thread(
                target=_warm, args=(role,), daemon=True,
                name=f"warmup-{role}"
            )
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        return stats

    @staticmethod
    def _safe_release(role, capture):
        try:
            capture.release()
        except Exception as exc:
            print(f"[CAMERA] Ошибка освобождения {role}: {exc}")

    @staticmethod
    def _apply_format(cap, *, fourcc_first: bool):
        mjpg = cv2.VideoWriter_fourcc(*"MJPG")
        if fourcc_first:
            cap.set(cv2.CAP_PROP_FOURCC, mjpg)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, _EXPECTED_SIZE[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _EXPECTED_SIZE[1])
        else:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, _EXPECTED_SIZE[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _EXPECTED_SIZE[1])
            cap.set(cv2.CAP_PROP_FOURCC, mjpg)
        cap.set(cv2.CAP_PROP_FPS, _REQUESTED_FPS)
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    @staticmethod
    def _negotiated_fourcc(capture) -> str | None:
        """Фактический FOURCC после set(); None, если драйвер не отвечает."""
        try:
            fourcc = int(capture.get(cv2.CAP_PROP_FOURCC))
        except Exception:
            return None
        if not fourcc:
            return None
        codec = "".join(
            chr((fourcc >> shift) & 0xFF) for shift in (0, 8, 16, 24)
        )
        codec = "".join(char if char.isprintable() else "?" for char in codec)
        return codec

    @classmethod
    def _configure_capture(cls, cap):
        """Запросить MJPG и убедиться, что драйвер действительно согласился.

        Часть UVC-драйверов принимает FOURCC только до смены разрешения,
        часть — только после, а подтверждение set() ни о чём не говорит.
        Молчаливый откат MJPG -> YUY2 поднимает поток одной камеры
        1280x720 примерно с 25 до ~440 Мбит/с: на общем USB-контроллере
        ровно одна такая камера лишает изохронной полосы все остальные.
        Поэтому пробуем оба порядка установки и проверяем фактический
        FOURCC. Откат без CAMERA_REQUIRE_MJPG — громкое предупреждение
        (связка может жить на быстрой шине), с флагом — ошибка, потому
        что для семи камер на одном хабе это гарантированный сбой набора.
        """
        codec = None
        for fourcc_first in (True, False, True):
            cls._apply_format(cap, fourcc_first=fourcc_first)
            codec = cls._negotiated_fourcc(cap)
            # None — драйвер не отвечает на get(): проверить нечего,
            # доверяем запросу и идём дальше (тестовые doubles ведут
            # себя именно так).
            if codec is None or codec == "MJPG":
                return
        message = (
            f"драйвер откатился с MJPG на {codec}: поток "
            f"{_EXPECTED_SIZE[0]}x{_EXPECTED_SIZE[1]}@{_REQUESTED_FPS:.0f} "
            "займёт в разы больше полосы USB и утопит остальные камеры"
        )
        if _env_flag("CAMERA_REQUIRE_MJPG"):
            raise RuntimeError(message)
        print(f"[CAMERA] ВНИМАНИЕ: {message}")

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
    def _warmup_phase(cls, capture, seconds: float) -> int:
        """Прогреть камеру после открытия, дать AGC разогнаться.

        После простоя первые кадры могут быть затемнёнными — это не
        признак закрытой крышки, а переходный процесс сенсора. Читаем и
        отбрасываем кадры без проверки _frame_error.
        """
        if seconds <= 0.0:
            return 0
        deadline = time.monotonic() + seconds
        reads = 0
        while time.monotonic() < deadline:
            try:
                ok, _ = capture.read()
                if ok:
                    reads += 1
            except Exception:
                pass
            time.sleep(_WARMUP_READ_INTERVAL)
        return reads

    @classmethod
    def _wait_for_stable_preflight(
        cls,
        capture,
        timeout: float | None = None,
        *,
        warmup: bool = True,
    ) -> str | None:
        """Дождаться серии валидных кадров после запуска экспозиции камеры.

        Сначала выполняется фаза прогрева ``_WARMUP_SECONDS`` для
        исключения ложной ошибки ``near-black`` сразу после простоя.
        После прогрева требуется ``_PREFLIGHT_VALID_FRAMES`` подряд
        валидных кадров. ``timeout`` переопределяет
        ``_PREFLIGHT_TIMEOUT`` — используется восстановлением потока,
        где бюджет времени ограничен общим дедлайном захвата.
        """

        warmup_reads = 0
        if warmup and _WARMUP_SECONDS > 0.0:
            warmup_reads = cls._warmup_phase(capture, _WARMUP_SECONDS)

        limit = _PREFLIGHT_TIMEOUT if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
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
            f"{total_reads}; warmup_reads={warmup_reads}; "
            f"negotiated={cls._negotiated_format(capture)}; "
            f"timeout={limit:.1f}s"
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

        Добавлен повтор при ``near-black``: автоэкспозиция после простоя
        может выдать несколько тёмных кадров подряд, и однократная
        попытка приводила к падению production-цикла.
        """
        requested = tuple(dict.fromkeys(roles))
        if not requested:
            return {}
        unknown = set(requested) - set(self.cameras)
        if unknown:
            raise RuntimeError(f"Неизвестные камеры: {sorted(unknown)}")
        self._ensure_usable()
        deadline = time.monotonic() + _CAPTURE_TIMEOUT

        def _grab(role):
            last_error = RuntimeError("read returned no frame")
            recoveries = 0
            attempt = 0
            while True:
                cap = self.cameras.get(role)
                if cap is None:
                    raise RuntimeError(f"Камера {role} не найдена")
                with self._role_locks[role]:
                    self._ensure_usable()
                    ok, frame = cap.read()
                if not ok or frame is None:
                    frame_error = "read returned no frame"
                else:
                    frame_error = self._frame_error(frame)
                if frame_error is None:
                    return frame
                last_error = RuntimeError(frame_error)
                recoverable = (
                    frame_error == "read returned no frame"
                    or "near-black" in frame_error
                )
                if not recoverable:
                    raise last_error
                if attempt < _DARK_RETRY_ATTEMPTS:
                    # near-black → попробовать ещё раз, это может быть AGC
                    attempt += 1
                    time.sleep(_DARK_RETRY_INTERVAL)
                    continue
                # Чтения исчерпаны: USB-поток мог отвалиться на слабом
                # контроллере. Пересоздаём VideoCapture роли вместо того,
                # чтобы латчить весь менеджер из-за одной просадки.
                if (
                    recoveries < _RECOVERY_ATTEMPTS
                    and time.monotonic() < deadline - 0.2
                ):
                    recoveries += 1
                    if self._reopen_role(role, deadline):
                        attempt = 0
                        continue
                raise last_error

        pool = self._require_pool()
        futures = {role: pool.submit(_grab, role) for role in requested}

        errors = {}
        results = {}
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

    def _reopen_role(self, role: str, deadline: float) -> bool:
        """Пересоздать поток роли после сбоя чтения, не латча менеджер.

        При просадке питания или нехватке изохронной полосы USB-драйвер
        перестаёт отдавать кадры, оставляя устройство формально открытым;
        единственный способ вернуть поток — закрыть и заново построить
        граф захвата. Новый handle проверяется preflight-ом ещё до того,
        как попадёт в набор: читатели других ролей затронуты не будут,
        а читатели этой роли увидят только валидный поток.

        Замена делается под role-lock с таймаутом: если предыдущее
        чтение зависло внутри драйвера, handle трогать нельзя — общий
        таймаут захвата залатчит менеджер, как и раньше.
        """

        cam_id = self.mapping.get(role)
        if cam_id is None:
            return False
        for backend in self._backends:
            remaining = deadline - time.monotonic()
            if remaining < 0.3:
                return False
            capture = None
            try:
                capture = self._create_capture(cam_id, backend)
                if capture is None or not capture.isOpened():
                    continue
                self._configure_capture(capture)
                error = self._wait_for_stable_preflight(
                    capture,
                    timeout=min(_RECOVERY_PREFLIGHT, remaining - 0.1),
                    warmup=False,
                )
                if error is not None:
                    continue
                lock = self._role_locks.get(role)
                if lock is None:
                    return False
                if not lock.acquire(timeout=0.5):
                    # Поток роли завис в read(): освобождать handle из-под
                    # заблокированного драйвера небезопасно.
                    return False
                try:
                    if self._closed:
                        return False
                    old = self.cameras.get(role)
                    self.cameras[role] = capture
                    capture = None
                finally:
                    lock.release()
                if old is not None:
                    # release() драйвера может блокироваться: старый handle
                    # никто больше не читает, поэтому закрываем его фоново.
                    threading.Thread(
                        target=self._safe_release,
                        args=(role, old),
                        daemon=True,
                        name=f"release-stale-{role}",
                    ).start()
                print(
                    f"[CAMERA] {role} (id={cam_id}): поток пересоздан "
                    f"[{_backend_label(backend)}]"
                )
                return True
            except Exception:
                continue
            finally:
                if capture is not None:
                    self._safe_release(role, capture)
        return False

    def reopen_roles(self, roles, timeout: float | None = None) -> dict:
        """Пересоздать потоки выбранных ролей (стартовое восстановление).

        Повторное чтение того же VideoCapture помогает только против
        медленного перехода автоэкспозиции. Если UVC-поток мёртв
        (драйвер не зарезервировал изохронную полосу или backend собрал
        битый граф), кадры из него не появятся никогда — нужен новый
        handle. Production уже делает это через ``_reopen_role`` в
        ``capture_roles``; метод даёт тот же механизм прогреву на
        старте, возвращая ``{role: успех}``.

        Роли пересоздаются последовательно: параллельный перезапуск
        нескольких камер повторяет гонку за полосу USB, которая чаще
        всего и является причиной мёртвого потока. Метод не латчит
        менеджер: неудача по роли отражается только в результате.
        """
        requested = tuple(dict.fromkeys(roles))
        if not requested:
            return {}
        unknown = set(requested) - set(self.cameras)
        if unknown:
            raise RuntimeError(f"Неизвестные камеры: {sorted(unknown)}")
        self._ensure_usable()
        budget = (
            float(timeout)
            if timeout is not None
            else float(_RECOVERY_REOPEN_SECONDS)
        )
        results = {}
        for role in requested:
            deadline = time.monotonic() + budget
            try:
                reopened = self._reopen_role(role, deadline)
            except Exception as exc:
                print(
                    f"[CAMERA] {role}: ошибка пересоздания потока: "
                    f"{type(exc).__name__}: {exc}"
                )
                reopened = False
            results[role] = reopened
            if not reopened:
                print(f"[CAMERA] {role}: поток пересоздать не удалось")
        return results

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
