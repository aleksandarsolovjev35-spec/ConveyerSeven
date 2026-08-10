"""Живой просмотр камер параллельно с работой производственной линии.

Кадры для оператора и инспекции берутся с одних камер, поэтому доступ
разграничивает :class:`LiveCaptureGate`. Перед production-захватом gate
приостанавливает только нужные роли и дожидается завершения их начатых
live-чтений. После копирования кадров роли сразу возвращаются live-потоку,
а модели анализируют сохранённые массивы в памяти.

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
    """Поролевое разграничение live и inspection чтений камер.

    Пауза конкретной роли ждёт только уже начатое чтение этой роли. Поэтому
    INPUT может получать статичный inspection-кадр, пока SPIDER/TOP честно
    продолжают live-поток, и наоборот.
    """

    def __init__(self):
        self._condition = threading.Condition()
        # Глобальные счётчики используются pause()/resume(), ролевые —
        # штатной production-инспекцией.
        self._pause_depth = 0
        self._active_reads = 0
        self._role_pause_depth = {}
        self._role_active_reads = {}

    def pause(self, timeout: float = LIVE_PAUSE_DRAIN_TIMEOUT) -> bool:
        """Приостановить все live-роли и дождаться завершения чтений."""
        deadline = time.monotonic() + timeout
        with self._condition:
            self._pause_depth += 1
            while self._active_reads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pause_depth -= 1
                    if not self._pause_depth: self._condition.notify_all()
                    return False
                self._condition.wait(remaining)
            return True

    def resume(self):
        with self._condition:
            if self._pause_depth: self._pause_depth -= 1
            if not self._pause_depth: self._condition.notify_all()

    def pause_roles(self, roles, timeout: float = LIVE_PAUSE_DRAIN_TIMEOUT) -> bool:
        """Запретить live-чтения только указанных ролей и дождаться их drain."""
        roles = tuple(dict.fromkeys(roles or ()))
        if not roles:
            return True
        deadline = time.monotonic() + timeout
        with self._condition:
            for role in roles:
                self._role_pause_depth[role] = self._role_pause_depth.get(role, 0) + 1
            while any(self._role_active_reads.get(role, 0) for role in roles):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    for role in roles:
                        depth = self._role_pause_depth.get(role, 0) - 1
                        if depth: self._role_pause_depth[role] = depth
                        else: self._role_pause_depth.pop(role, None)
                    self._condition.notify_all()
                    return False
                self._condition.wait(remaining)
            return True

    def resume_roles(self, roles):
        with self._condition:
            for role in tuple(dict.fromkeys(roles or ())):
                depth = self._role_pause_depth.get(role, 0) - 1
                if depth > 0: self._role_pause_depth[role] = depth
                else: self._role_pause_depth.pop(role, None)
            self._condition.notify_all()

    def reset(self):
        with self._condition:
            self._pause_depth = 0
            self._role_pause_depth.clear()
            self._condition.notify_all()

    def is_live_allowed(self, role) -> bool:
        with self._condition:
            return self._pause_depth == 0 and self._role_pause_depth.get(role, 0) == 0

    @contextlib.contextmanager
    def live_read(self, role=None):
        """Занять слот live-чтения одной роли; False означает паузу роли."""
        with self._condition:
            allowed = self._pause_depth == 0 and (
                role is None or self._role_pause_depth.get(role, 0) == 0
            )
            if allowed:
                self._active_reads += 1
                if role is not None:
                    self._role_active_reads[role] = self._role_active_reads.get(role, 0) + 1
        try:
            yield allowed
        finally:
            if allowed:
                with self._condition:
                    self._active_reads -= 1
                    if role is not None:
                        remaining = self._role_active_reads.get(role, 0) - 1
                        if remaining: self._role_active_reads[role] = remaining
                        else: self._role_active_reads.pop(role, None)
                    self._condition.notify_all()

    @contextlib.contextmanager
    def live_reads(self, roles):
        """Занять все доступные роли пакета, пропустив роли inspection."""
        roles = tuple(dict.fromkeys(roles or ()))
        with self._condition:
            allowed_roles = () if self._pause_depth else tuple(
                role for role in roles if self._role_pause_depth.get(role, 0) == 0
            )
            self._active_reads += len(allowed_roles)
            for role in allowed_roles:
                self._role_active_reads[role] = self._role_active_reads.get(role, 0) + 1
        try:
            yield allowed_roles
        finally:
            if allowed_roles:
                with self._condition:
                    self._active_reads -= len(allowed_roles)
                    for role in allowed_roles:
                        remaining = self._role_active_reads.get(role, 0) - 1
                        if remaining: self._role_active_reads[role] = remaining
                        else: self._role_active_reads.pop(role, None)
                    self._condition.notify_all()


class LivePreview:
    """Фоновая публикация кадров камер с учётом ролевых пауз инспекции."""

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

    def pause_roles(self, roles, timeout: float = LIVE_PAUSE_DRAIN_TIMEOUT) -> bool:
        return self.gate.pause_roles(roles, timeout)

    def resume_roles(self, roles):
        self.gate.resume_roles(roles)

    def reset_pause(self):
        self.gate.reset()

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
        """Цикл live-чтения; iteration сама берёт нужные ролевые слоты."""
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
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
                return None
            with self.gate.live_read(active_role) as allowed:
                if not allowed:
                    return None
                frame = self._cameras.capture_single(active_role)
            self._frame_times.append(time.monotonic())
            return {active_role: frame}
        self._run_loop(LIVE_FRAME_INTERVAL, iteration, "selected camera loop")

    def _auxiliary_loop(self):
        def iteration():
            available_roles = self._available_roles()
            active_role = self._active_role(available_roles)
            auxiliary_roles = [role for role in available_roles if role != active_role]
            with self.gate.live_reads(auxiliary_roles) as allowed_roles:
                if not allowed_roles:
                    return None
                capture_roles = getattr(self._cameras, "capture_roles", None)
                if callable(capture_roles):
                    return capture_roles(allowed_roles)
                return {role: frame for role, frame in self._cameras.capture_all().items()
                        if role in allowed_roles}
        self._run_loop(LIVE_AUX_BATCH_INTERVAL, iteration, "auxiliary loop")
