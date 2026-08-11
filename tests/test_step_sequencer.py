"""StepSequencer: фазы шага и передача камер между live и inspection.

StepSequencer — единственный владелец передачи камер: ``enter_capture``
не просто ставит флаг, а дожидается завершения начатых live-чтений. Тесты
фиксируют:

* порядок фаз проверяется таблицей _ALLOWED — вызов не по порядку
  поднимает StageSequenceError;
* enter_capture ждёт drain live-чтений через gate;
* release_capture_roles возвращает камеры live немедленно;
* reset() увеличивает generation, поэтому начатая передача камер понимает,
  что шаг сброшен, и поднимает StageSequenceError.
"""

import threading
import time

import pytest

from core.step_stages import (
    StageSequenceError,
    StepSequencer,
    StepStage,
)


class FakeLive:
    """Минимальный live-двойник с подсчётом пауз."""

    def __init__(self, pause_delay=0.0, pause_result=True):
        self.pause_calls = 0
        self.resume_calls = 0
        self.pause_roles_calls = []
        self.resume_roles_calls = []
        self._pause_delay = pause_delay
        self._pause_result = pause_result

    def pause(self, timeout=None):
        self.pause_calls += 1
        if self._pause_delay:
            time.sleep(self._pause_delay)
        return self._pause_result

    def resume(self):
        self.resume_calls += 1

    def pause_roles(self, roles, timeout=None):
        self.pause_roles_calls.append(tuple(roles))
        if self._pause_delay:
            time.sleep(self._pause_delay)
        return self._pause_result

    def resume_roles(self, roles):
        self.resume_roles_calls.append(tuple(roles))


def _build(live=None, **kwargs):
    live = live or FakeLive()
    return StepSequencer(live, settle_seconds=0.0, trace_seconds=0.0, **kwargs)


class TestPhaseTransitions:
    def test_начальная_фаза_idle(self):
        seq = _build()
        assert seq.stage == StepStage.IDLE
        assert seq.static is False
        assert seq.static_roles is None

    def test_полный_цикл_фаз(self):
        seq = _build()
        seq.enter_motion()
        assert seq.stage == StepStage.MOTION
        seq.enter_settle()
        assert seq.stage == StepStage.SETTLE
        seq.enter_capture()
        assert seq.stage == StepStage.CAPTURE
        seq.enter_analysis()
        assert seq.stage == StepStage.ANALYSIS
        seq.enter_publish()
        assert seq.stage == StepStage.PUBLISH

    def test_второй_цикл_через_motion(self):
        seq = _build()
        seq.enter_motion()
        seq.enter_settle()
        seq.enter_capture(roles=())
        seq.enter_analysis()
        seq.enter_publish()
        # Второй цикл начинается с PUBLISH -> MOTION
        seq.enter_motion()
        assert seq.stage == StepStage.MOTION

    def test_недопустимый_переход_поднимает_ошибку(self):
        seq = _build()
        with pytest.raises(StageSequenceError):
            seq.enter_settle()  # IDLE -> SETTLE запрещено

    def test_skip_capture_из_motion_запрещён(self):
        seq = _build()
        seq.enter_motion()
        seq.enter_settle()
        with pytest.raises(StageSequenceError):
            seq.enter_analysis()  # SETTLE -> ANALYSIS без CAPTURE

    def test_reset_сбрасывает_в_idle_из_любой_фазы(self):
        seq = _build()
        seq.enter_motion()
        seq.enter_settle()
        seq.enter_capture()
        seq.reset()
        assert seq.stage == StepStage.IDLE
        assert seq.static is False

    def test_on_stage_callback_вызывается_при_переходе(self):
        transitions = []

        def on_stage(prev, target, elapsed):
            transitions.append((prev, target))

        seq = _build(on_stage=on_stage)
        seq.enter_motion()
        seq.enter_settle()
        assert transitions == [
            (StepStage.IDLE, StepStage.MOTION),
            (StepStage.MOTION, StepStage.SETTLE),
        ]

    def test_on_stage_исключение_не_роняет_переход(self):
        def bad_callback(*args):
            raise ValueError("наблюдатель упал")

        seq = _build(on_stage=bad_callback)
        seq.enter_motion()  # не должно бросить
        assert seq.stage == StepStage.MOTION


class TestCaptureRoles:
    def test_capture_с_ролями_приостанавливает_только_их(self):
        live = FakeLive()
        seq = _build(live)
        seq.enter_motion()
        seq.enter_settle()
        seq.enter_capture(roles=("INPUT_LEFT", "INPUT_RIGHT"))
        assert seq.static is True
        assert seq.static_roles == ("INPUT_LEFT", "INPUT_RIGHT")
        assert live.pause_roles_calls == [("INPUT_LEFT", "INPUT_RIGHT")]
        assert live.pause_calls == 0

    def test_capture_без_ролей_приостанавливает_все(self):
        live = FakeLive()
        seq = _build(live)
        seq.enter_motion()
        seq.enter_settle()
        seq.enter_capture(roles=None)
        assert seq.static is True
        assert seq.static_roles is None
        assert live.pause_calls == 1

    def test_capture_с_пустыми_ролями_не_приостанавливает(self):
        live = FakeLive()
        seq = _build(live)
        seq.enter_motion()
        seq.enter_settle()
        seq.enter_capture(roles=())
        assert seq.static is False
        assert live.pause_calls == 0
        assert live.pause_roles_calls == []

    def test_release_capture_roles_возвращает_live(self):
        live = FakeLive()
        seq = _build(live)
        seq.enter_motion()
        seq.enter_settle()
        seq.enter_capture(roles=("TOP",))
        assert seq.static is True
        seq.release_capture_roles()
        assert seq.static is False
        assert live.resume_roles_calls == [("TOP",)]

    def test_release_после_глобальной_паузы_вызывает_resume(self):
        live = FakeLive()
        seq = _build(live)
        seq.enter_motion()
        seq.enter_settle()
        seq.enter_capture(roles=None)
        seq.release_capture_roles()
        assert seq.static is False
        assert live.resume_calls == 1

    def test_release_без_acquire_безопасен(self):
        live = FakeLive()
        seq = _build(live)
        seq.release_capture_roles()
        assert live.resume_calls == 0

    def test_live_отказ_приостановки_поднимает_ошибку(self):
        live = FakeLive(pause_result=False)
        seq = _build(live, handover_timeout=0.01)
        seq.enter_motion()
        seq.enter_settle()
        with pytest.raises(StageSequenceError, match="не освободил"):
            seq.enter_capture()

    def test_live_отказ_ролевой_паузы_поднимает_ошибку(self):
        live = FakeLive(pause_result=False)
        seq = _build(live, handover_timeout=0.01)
        seq.enter_motion()
        seq.enter_settle()
        with pytest.raises(StageSequenceError, match="не освободил"):
            seq.enter_capture(roles=("TOP",))


class TestGenerationReset:
    def test_reset_во_время_acquire_отменяет_передачу(self):
        """Сброс во время ожидания live-паузы отменяет захват камер."""
        live = FakeLive(pause_delay=0.3)
        seq = _build(live)
        seq.enter_motion()
        seq.enter_settle()

        error = None

        def try_capture():
            nonlocal error
            try:
                seq.enter_capture()
            except StageSequenceError as exc:
                error = exc

        t = threading.Thread(target=try_capture, daemon=True)
        t.start()
        # Даём acquire_static дойти до pause и войти в ожидание
        time.sleep(0.05)
        seq.reset()
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert error is not None
        assert "сброшен" in str(error)

    def test_double_release_безопасен(self):
        live = FakeLive()
        seq = _build(live)
        seq.enter_motion()
        seq.enter_settle()
        seq.enter_capture(roles=("TOP",))
        seq.release_capture_roles()
        # Повторный release не должен вызывать второй resume
        seq.release_capture_roles()
        assert len(live.resume_roles_calls) == 1
