"""Живой просмотр камер параллельно с работой производственной линии.

Кадры для оператора и кадры для инспекции берутся с одних и тех же камер,
поэтому доступ разграничен :class:`LiveCaptureGate`. Правило простое:

* линия **движется** — работает live-просмотр, оператор видит поток;
* линия **стоит** — live-чтения запрещены, кадры забирает инспекция,
  и только по этим статичным кадрам считаются defect rules.

Инспекция всегда приоритетна: :meth:`LiveCaptureGate.pause` не просто
выставляет флаг, а дожидается завершения уже начатых live-чтений. Без
этого ожидания трёхкратный синхронный захват мог бы конкурировать с
live-потоком за одну и ту же камеру.

Раскладка потоков повторяет реальную нагрузку USB: выбранная оператором
камера обновляется с частотой ``LIVE_TARGET_FPS``, остальные шесть — одним
пакетом и заметно реже, иначе семь камер не помещаются в шину.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque

LIVE_TARGET_FPS = 30.0
LIVE_FRAME_INTERVAL = 1.0 / LIVE_TARGET_FPS
LIVE_AUX_BATCH_INTERVAL = 0.20
LIVE_THREAD_JOIN_TIMEOUT = 6.0
LIVE_PAUSE_DRAIN_TIMEOUT = 5.0

_FPS_WINDOW_SECONDS = 2.0
_PAUSED_POLL_INTERVAL = 0.02


class LiveCaptureGate:
    """Разграничение доступа к камерам между live-просмотром и инспекцией."""

    def __init__(self):
        self._condition = threading.Condition()
        self._pause_depth = 0
        self._active_reads = 0

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._pause_depth > 0

    def pause(self, timeout: float = LIVE_PAUSE_DRAIN_TIMEOUT) -> bool:
        """Запретить live-чтения и дождаться завершения начатых.

        Возвращает False, если live-поток не освободил камеры за timeout;
        вызывающая сторона обязана считать это ошибкой, а не продолжать
        инспекцию параллельно с чужим чтением. При неудаче пауза
        снимается здесь же, иначе повисший счётчик навсегда остановил бы
        live-просмотр.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            self._pause_depth += 1
            while self._active_reads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pause_depth -= 1
                    if not self._pause_depth:
                        self._condition.notify_all()
                    return False
                self._condition.wait(remaining)
            return True

    def resume(self):
        with self._condition:
            if self._pause_depth:
                self._pause_depth -= 1
            if not self._pause_depth:
                self._condition.notify_all()

    def reset(self):
        """Снять все паузы: аварийное завершение, FAULT, force exit."""
        with self._condition:
            self._pause_depth = 0
            self._condition.notify_all()

    @contextlib.contextmanager
    def live_read(self):
        """Занять слот live-чтения.

        Отдаёт False, если сейчас пауза: live-поток обязан пропустить
        итерацию, а не читать камеру во время инспекции.
        """
        with self._condition:
            allowed = self._pause_depth == 0
            if allowed:
                self._active_reads += 1
        try:
            yield allowed
        finally:
            if allowed:
                with self._condition:
                    self._active_reads -= 1
                    if not self._active_reads:
                        self._condition.notify_all()


class LivePreview:
    """Фоновая публикация кадров всех камер, пока линия движется."""

    def __init__(self, cameras, monitor, get_active_role, gate=None):
        self._cameras = cameras
        self._monitor = monitor
        self._get_active_role = get_active_role
        self.gate = gate if gate is not None else LiveCaptureGate()

        # _lifecycle_lock сериализует start/stop целиком; _state_lock
        # защищает только поля, которые читают рабочие потоки и UI.
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._threads = []
        self._frame_times = deque(maxlen=240)
        self._error = None

    # Состояние

    @property
    def running(self) -> bool:
        with self._state_lock:
            return bool(self._threads)

    @property
    def error(self):
        with self._state_lock:
            return self._error

    @property
    def fps(self) -> float:
        now = time.monotonic()
        recent = [
            value for value in list(self._frame_times)
            if now - value <= _FPS_WINDOW_SECONDS
        ]
        if len(recent) < 2:
            return 0.0
        elapsed = recent[-1] - recent[0]
        measured = 0.0 if elapsed <= 0 else (len(recent) - 1) / elapsed
        return min(LIVE_TARGET_FPS, measured)

    # Управление потоками

    def start(self) -> bool:
        """Запустить потоки просмотра. Повторный вызов ничего не делает.

        ``_lifecycle_lock`` сериализует start и stop целиком: иначе старт
        мог бы поднять потоки на ещё не снятом стоп-сигнале предыдущей
        остановки и мгновенно их погасить.
        """
        with self._lifecycle_lock:
            with self._state_lock:
                if self._threads:
                    return False
                self._stop_event.clear()
                self._frame_times.clear()
                self._error = None
                self._threads = [
                    threading.Thread(
                        target=self._selected_loop,
                        daemon=True,
                        name="live-selected-camera",
                    ),
                    threading.Thread(
                        target=self._auxiliary_loop,
                        daemon=True,
                        name="live-aux-cameras",
                    ),
                ]
                threads = list(self._threads)
            self.clear_overlays()
            for thread in threads:
                thread.start()
            print("[LIVE] preview started")
            return True

    def stop(self):
        """Остановить потоки просмотра и дождаться их завершения."""
        with self._lifecycle_lock:
            # Стоп-сигнал выставляется до снятия списка потоков, иначе
            # параллельный start() увидел бы пустой список и поднял новые
            # потоки, которые тут же погасил бы наш set().
            self._stop_event.set()
            with self._state_lock:
                threads = list(self._threads)
                self._threads = []
            if not threads:
                self._stop_event.clear()
                return
            deadline = time.monotonic() + LIVE_THREAD_JOIN_TIMEOUT
            for thread in threads:
                thread.join(max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    print(
                        f"[LIVE] поток {thread.name} не остановился за "
                        f"{LIVE_THREAD_JOIN_TIMEOUT}s"
                    )
            self._stop_event.clear()
            print("[LIVE] preview stopped")

    # Пауза на время статической инспекции

    def pause(self, timeout: float = LIVE_PAUSE_DRAIN_TIMEOUT) -> bool:
        return self.gate.pause(timeout)

    def resume(self):
        self.gate.resume()

    def reset_pause(self):
        self.gate.reset()

    @contextlib.contextmanager
    def paused(self, timeout: float = LIVE_PAUSE_DRAIN_TIMEOUT):
        """Остановить live-чтения на время статического этапа."""
        if not self.pause(timeout):
            # pause() уже снял свою неудачную паузу.
            raise RuntimeError(
                "Live-просмотр не освободил камеры за "
                f"{timeout}s; статическая инспекция отменена"
            )
        try:
            yield
        finally:
            self.resume()

    def clear_overlays(self):
        """Убрать геометрию правил перед показом движущихся кадров.

        Разметка построена по статичному кадру, поэтому на движущемся
        изображении она указывала бы не на те места.
        """
        if self._monitor is None:
            return
        self._monitor.update(vision_results={}, rule_results=[], run_frames=[])

    # Внутреннее

    def _available_roles(self) -> list:
        return list(getattr(self._cameras, "mapping", {}) or {})

    def _active_role(self, available_roles: list):
        try:
            role = self._get_active_role()
        except Exception:
            role = None
        if available_roles and role not in available_roles:
            return available_roles[0]
        return role

    def _publish(self, frames: dict):
        if self._monitor is not None and frames:
            self._monitor.update(frames=frames)

    def _fail(self, exc: Exception, source: str):
        if self._stop_event.is_set():
            return
        message = f"{type(exc).__name__}: {exc}"
        with self._state_lock:
            if self._error is None:
                self._error = message
        print(f"[LIVE] {source} error: {message}")
        self._stop_event.set()

    def _run_loop(self, interval: float, iteration, source: str):
        """Цикл чтения: гейт удерживается только на время работы с камерой.

        ``iteration`` возвращает кадры, а публикация в монитор выполняется
        уже после освобождения гейта. Иначе инспекция ждала бы не доступа
        к камере, а перекодирования JPEG в UI, и каждый шаг линии получал
        бы лишнюю задержку.
        """
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                with self.gate.live_read() as allowed:
                    if not allowed:
                        self._stop_event.wait(_PAUSED_POLL_INTERVAL)
                        continue
                    frames = iteration()
                self._publish(frames)
            except Exception as exc:
                self._fail(exc, source)
                break
            elapsed = time.monotonic() - started
            self._stop_event.wait(max(0.0, interval - elapsed))

    def _selected_loop(self):
        def iteration():
            available_roles = self._available_roles()
            active_role = self._active_role(available_roles)
            if active_role is None:
                # Провайдер камер без карты ролей: читаем всё сразу.
                frames = self._cameras.capture_all()
            else:
                frames = {active_role: self._cameras.capture_single(active_role)}
            self._frame_times.append(time.monotonic())
            return frames

        self._run_loop(LIVE_FRAME_INTERVAL, iteration, "selected camera loop")

    def _auxiliary_loop(self):
        def iteration():
            available_roles = self._available_roles()
            active_role = self._active_role(available_roles)
            auxiliary_roles = [
                role for role in available_roles if role != active_role
            ]
            if not auxiliary_roles:
                return None
            capture_roles = getattr(self._cameras, "capture_roles", None)
            if callable(capture_roles):
                return capture_roles(auxiliary_roles)
            return {
                role: frame
                for role, frame in self._cameras.capture_all().items()
                if role in auxiliary_roles
            }

        self._run_loop(LIVE_AUX_BATCH_INTERVAL, iteration, "auxiliary loop")
