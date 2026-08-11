"""Dead-man JOG: лента едет только пока жив heartbeat оператора.

В прошивке dead-man нет — эта безопасность целиком на хосте, поэтому
поведение фиксируется тестами: пропавший heartbeat обязан остановить ленту
командой G1 без участия оператора.
"""

import time

import pytest

from hardware.jog_controller import JogController

from fakes import MovingFakeTransport


CALIBRATION = {"jog_hold_steps": 38096, "normal_steps": 19048}


def make_jog(heartbeat_timeout=0.15):
    transport = MovingFakeTransport()
    jog = JogController(
        transport, dict(CALIBRATION), heartbeat_timeout=heartbeat_timeout,
    )
    return transport, jog


def wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestValidation:
    def test_hold_steps_вне_диапазона(self):
        for bad in (9_999, 10_000_001):
            with pytest.raises(ValueError):
                JogController(
                    MovingFakeTransport(),
                    {"jog_hold_steps": bad, "normal_steps": 19048},
                )

    def test_normal_steps_обязан_быть_положительным(self):
        with pytest.raises(ValueError):
            JogController(
                MovingFakeTransport(),
                {"jog_hold_steps": 38096, "normal_steps": 0},
            )

    def test_heartbeat_timeout_вне_диапазона(self):
        for bad in (0.10, 2.5):
            with pytest.raises(ValueError):
                JogController(
                    MovingFakeTransport(),
                    dict(CALIBRATION),
                    heartbeat_timeout=bad,
                )

    def test_неизвестное_направление(self):
        _transport, jog = make_jog()
        with pytest.raises(ValueError):
            jog.start_hold("up")


class TestDeadMan:
    def test_без_heartbeat_работник_сам_останавливает_ленту(self):
        transport, jog = make_jog(heartbeat_timeout=0.15)
        assert jog.start_hold("+") is True
        # Heartbeat не приходит: оператор «отпустил» кнопку без release.
        assert wait_until(lambda: "G1" in transport.sent, timeout=1.5), \
            "работник обязан сам отправить G1 по таймауту heartbeat"
        assert wait_until(lambda: not jog.busy, timeout=2.0)
        status = jog.status
        assert status["last_action"] == "STOP: heartbeat timeout"
        assert status["error"] is None
        # После остановки параметры ленты возвращены к нормальным.
        assert "G7 S19048" in transport.sent
        assert "G6 S2" in transport.sent

    def test_hold_с_heartbeat_двигает_и_release_останавливает(self):
        transport, jog = make_jog(heartbeat_timeout=0.15)
        assert jog.start_hold("+") is True
        assert jog.heartbeat("+") is True
        assert wait_until(lambda: "G3" in transport.sent, timeout=1.5), \
            "с живым heartbeat сегмент хода должен начаться"
        # Команды сегмента: длинный автономный отрезок в выбранную сторону.
        sent = list(transport.sent)
        assert "G7 S38096" in sent
        assert "G6 S1" in sent
        assert sent[-1] == "G3"

        # Поддерживаем heartbeat, пока проверяем, затем отпускаем.
        jog.heartbeat("+")
        assert jog.release("test done") is True
        sent = list(transport.sent)
        g1_index = max(i for i, cmd in enumerate(sent) if cmd == "G1")
        tail = sent[g1_index:]
        # Сначала G1, затем восстановление штатных параметров ленты.
        assert f"G7 S{CALIBRATION['normal_steps']}" in tail
        assert "G6 S2" in tail
        assert jog.status["error"] is None
        assert jog.busy is False

    def test_обратное_направление_даёт_отрицательные_шаги(self):
        transport, jog = make_jog(heartbeat_timeout=0.5)
        jog.start_hold("-")
        jog.heartbeat("-")
        assert wait_until(lambda: "G7 S-38096" in transport.sent, timeout=1.5)
        jog.release("done")

    def test_heartbeat_чужого_направления_отвергается(self):
        _transport, jog = make_jog()
        jog.start_hold("+")
        try:
            assert jog.heartbeat("-") is False
        finally:
            jog.release("cleanup")

    def test_смена_направления_на_ходу_запрещена(self):
        # Большой heartbeat-timeout, чтобы работник гарантированно был жив
        # в момент попытки смены направления.
        transport, jog = make_jog(heartbeat_timeout=0.5)
        jog.start_hold("+")
        jog.heartbeat("+")
        try:
            assert wait_until(lambda: "G3" in transport.sent, timeout=1.5)
            jog.heartbeat("+")
            assert jog.start_hold("-") is False
        finally:
            jog.release("cleanup")
