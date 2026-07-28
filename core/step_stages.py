"""Явные этапы производственного шага и барьеры между ними.

Шаг линии разбит на фазы с одним владельцем камер у каждой:

```text
MOTION    лента едет            камеры у live-просмотра
SETTLE    лента встала          камеры у live-просмотра, гасим вибрацию
CAPTURE   лента неподвижна      камеры только у инспекции
ANALYSIS  модели и правила      камеры не читаются
PUBLISH   результат на экран    камеры не читаются
```

Переход между фазами — единственное место, где меняется владелец камер.
Поэтому «снять кадр для правил во время движения» или «читать камеру из
двух потоков» невозможно не по договорённости, а по построению:

* :meth:`StepSequencer.enter_capture` не просто ставит флаг, а дожидается
  завершения уже начатых live-чтений и только затем отдаёт кадры
  инспекции;
* порядок фаз проверяется таблицей ``_ALLOWED``: вызов не по порядку
  поднимает :class:`StageSequenceError`, а не тихо портит шаг.

``SETTLE`` существует отдельно от ``CAPTURE``, потому что контроллер
подтверждает остановку по счётчику шагов, а механика в этот момент ещё
качается. Кадр, снятый сразу после подтверждения, может быть смазан.
``STAGE_SETTLE_SECONDS`` — консервативное значение по умолчанию, его
нужно уточнить на реальной линии.
"""

from __future__ import annotations

import threading
import time
from enum import Enum

# Пауза между подтверждённой остановкой ленты и первым кадром инспекции.
STAGE_SETTLE_SECONDS = 0.15

# Предел ожидания освобождения камер live-просмотром перед захватом.
STAGE_CAPTURE_HANDOVER_TIMEOUT = 5.0


class StageSequenceError(RuntimeError):
    """Фазы шага вызваны не в том порядке."""


class StepStage(str, Enum):
    IDLE = "IDLE"
    MOTION = "MOTION"
    SETTLE = "SETTLE"
    CAPTURE = "CAPTURE"
    ANALYSIS = "ANALYSIS"
    PUBLISH = "PUBLISH"


# Разрешённые переходы. Возврат в IDLE доступен всегда: это сброс шага
# при STOP, FAULT и завершении работы.
_ALLOWED = {
    StepStage.IDLE: (StepStage.MOTION,),
    StepStage.MOTION: (StepStage.SETTLE,),
    StepStage.SETTLE: (StepStage.CAPTURE,),
    StepStage.CAPTURE: (StepStage.ANALYSIS,),
    StepStage.ANALYSIS: (StepStage.PUBLISH,),
    StepStage.PUBLISH: (StepStage.MOTION,),
}

# Фазы, на которых камеры принадлежат инспекции, а не live-просмотру.
_STATIC_STAGES = (StepStage.CAPTURE, StepStage.ANALYSIS, StepStage.PUBLISH)


class StepSequencer:
    """Владелец фаз шага и единственная точка передачи камер.

    Класс не читает камеры сам: он только решает, кому они принадлежат
    в текущей фазе, и гарантирует, что смена владельца завершена до
    начала следующей фазы.
    """

    def __init__(
        self,
        live,
        settle_seconds: float = STAGE_SETTLE_SECONDS,
        handover_timeout: float = STAGE_CAPTURE_HANDOVER_TIMEOUT,
        sleep=time.sleep,
    ):
        self._live = live
        self._settle_seconds = float(settle_seconds)
        self._handover_timeout = float(handover_timeout)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._stage = StepStage.IDLE
        self._static_held = False

    @property
    def stage(self) -> StepStage:
        with self._lock:
            return self._stage

    @property
    def static(self) -> bool:
        """True, когда кадры принадлежат инспекции."""
        with self._lock:
            return self._static_held

    def _switch(self, target: StepStage):
        with self._lock:
            current = self._stage
            if target not in _ALLOWED[current]:
                raise StageSequenceError(
                    f"Недопустимый переход шага: {current.value} -> "
                    f"{target.value}"
                )
            self._stage = target

    def enter_motion(self):
        """Начать движение: камеры возвращаются live-просмотру."""
        self._switch(StepStage.MOTION)
        self._release_static()

    def enter_settle(self):
        """Лента подтвердила остановку; ждём затухания вибрации."""
        self._switch(StepStage.SETTLE)
        if self._settle_seconds > 0:
            self._sleep(self._settle_seconds)

    def enter_capture(self):
        """Передать камеры инспекции и дождаться завершения live-чтений."""
        self._switch(StepStage.CAPTURE)
        self._acquire_static()

    def enter_analysis(self):
        """Кадры сняты; камеры больше не читаются до конца шага."""
        self._switch(StepStage.ANALYSIS)

    def enter_publish(self):
        """Опубликовать результат поверх статичных кадров."""
        self._switch(StepStage.PUBLISH)

    def reset(self):
        """Сбросить шаг в IDLE и вернуть камеры live-просмотру."""
        with self._lock:
            self._stage = StepStage.IDLE
        self._release_static()

    def _acquire_static(self):
        with self._lock:
            if self._static_held:
                return
        if not self._live.pause(self._handover_timeout):
            # pause() снимает свою неудачную паузу сам.
            raise StageSequenceError(
                "Live-просмотр не освободил камеры за "
                f"{self._handover_timeout}s; шаг остановлен"
            )
        with self._lock:
            self._static_held = True

    def _release_static(self):
        with self._lock:
            if not self._static_held:
                return
            self._static_held = False
        self._live.resume()
