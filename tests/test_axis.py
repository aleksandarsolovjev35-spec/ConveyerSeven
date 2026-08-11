"""Ось распределителя: homing, move_absolute, verify_limit_config, wait_stop.

Axis — единственная обёртка над NEMA-осью контроллера. Все физические
перемещения (DIST1, DIST2) идут через этот класс. Тесты используют
FakeTransport с ответами в формате прошивки convey15 и FakeAxis-ответы
для read_config/read_status.
"""

import time

import pytest

from hardware.axis import Axis


class AxisTransport:
    """Транспорт, отдающий заданные ответы на I10 (status) и I11 (config)."""

    def __init__(self, status_reply="", config_reply=""):
        self.sent = []
        self.queries = []
        self._status = status_reply
        self._config = config_reply
        self._status_script = []

    def set_status_script(self, script):
        """Скрипт ответов I10: последний элемент повторяется."""
        self._status_script = list(script)

    def send(self, cmd):
        self.sent.append(cmd)

    def query(self, cmd, delay=0.0):
        self.queries.append(cmd)
        if cmd == "I10":
            if len(self._status_script) > 1:
                return self._status_script.pop(0)
            if self._status_script:
                return self._status_script[0]
            return self._status
        if cmd == "I11":
            return self._config
        return ""


def _config_reply(min_val=0, max_val=340, speed=300, accel=100):
    return f"AXIS0 speed={speed} accel={accel} limMin={min_val} limMax={max_val}"


def _status_reply(pos=0, tgt=0, moving=0, enabled=1, home_phase=0,
                  homed=1, limits=1, endstop=0):
    return (
        f"AXIS0 POS={pos} TGT={tgt} MOV={moving} EN={enabled} "
        f"HOME={home_phase} HOMED={homed} LIM={limits} ES={endstop}"
    )


class TestValidation:
    def test_axis_id_должен_быть_0_или_1(self):
        transport = AxisTransport(config_reply=_config_reply())
        with pytest.raises(ValueError, match="axis_id"):
            Axis(transport, axis_id=2, maximum=340)

    def test_minimum_должен_быть_неотрицательным(self):
        transport = AxisTransport(config_reply=_config_reply(min_val=-1, max_val=340))
        with pytest.raises(ValueError, match="0 <= minimum"):
            Axis(transport, axis_id=0, minimum=-1, maximum=340)

    def test_maximum_должен_превышать_minimum(self):
        transport = AxisTransport(config_reply=_config_reply(min_val=0, max_val=0))
        with pytest.raises(ValueError, match="minimum < maximum"):
            Axis(transport, axis_id=0, minimum=0, maximum=0)

    def test_limits_должны_быть_int(self):
        transport = AxisTransport(config_reply=_config_reply())
        with pytest.raises(ValueError, match="int"):
            Axis(transport, axis_id=0, minimum=0.0, maximum=340)

    def test_verify_limit_config_отвергает_несовпадение_min(self):
        transport = AxisTransport(
            config_reply=_config_reply(min_val=5, max_val=340)
        )
        with pytest.raises(RuntimeError, match="limMin"):
            Axis(transport, axis_id=0, minimum=0, maximum=340)

    def test_verify_limit_config_отвергает_несовпадение_max(self):
        transport = AxisTransport(
            config_reply=_config_reply(min_val=0, max_val=100)
        )
        with pytest.raises(RuntimeError, match="limMax"):
            Axis(transport, axis_id=0, minimum=0, maximum=340)


class TestMovement:
    def _make_axis(self, **kwargs):
        transport = AxisTransport(
            config_reply=_config_reply(),
            status_reply=_status_reply(),
        )
        axis = Axis(
            transport, axis_id=0, minimum=0, maximum=340,
            speed=300, accel=100,
        )
        return axis, transport

    def test_set_params_отправляет_скорость_и_ускорение(self):
        axis, transport = self._make_axis()
        # G21 (speed), G22 (accel) отправляются при инициализации
        assert "G21 S300 P0" in transport.sent
        assert "G22 S100 P0" in transport.sent

    def test_set_limits_отправляет_границы(self):
        axis, transport = self._make_axis()
        assert "G31 S0 P0" in transport.sent
        assert "G32 S340 P0" in transport.sent
        assert "G33 S1 P0" in transport.sent

    def test_move_absolute_отправляет_g27(self):
        axis, transport = self._make_axis()
        transport.sent.clear()
        axis.move_absolute(170)
        assert "G27 S170 P0" in transport.sent

    def test_move_absolute_отвергает_позицию_вне_границ(self):
        axis, transport = self._make_axis()
        with pytest.raises(ValueError, match="absolute position"):
            axis.move_absolute(341)
        with pytest.raises(ValueError, match="absolute position"):
            axis.move_absolute(-1)

    def test_move_absolute_отвергает_не_int(self):
        axis, transport = self._make_axis()
        with pytest.raises(ValueError, match="int"):
            axis.move_absolute(170.5)

    def test_home_отправляет_g28(self):
        axis, transport = self._make_axis()
        transport.sent.clear()
        axis.home()
        assert "G28 P0" in transport.sent

    def test_position_читает_status(self):
        transport = AxisTransport(
            config_reply=_config_reply(),
            status_reply=_status_reply(pos=123),
        )
        axis = Axis(transport, axis_id=0, minimum=0, maximum=340)
        assert axis.position == 123

    def test_position_без_ответа_бросает_ошибку(self):
        transport = AxisTransport(
            config_reply=_config_reply(),
            status_reply="AXIS0 MOV=0",  # нет POS=
        )
        axis = Axis(transport, axis_id=0, minimum=0, maximum=340)
        with pytest.raises(RuntimeError, match="no position"):
            _ = axis.position


class TestWaitStop:
    def test_wait_stop_подтверждает_немедленную_остановку(self):
        transport = AxisTransport(
            config_reply=_config_reply(),
            status_reply=_status_reply(moving=0),
        )
        axis = Axis(transport, axis_id=0, minimum=0, maximum=340)
        start = time.monotonic()
        axis.wait_stop(timeout=2.0)
        elapsed = time.monotonic() - start
        # Ожидание sleep(0.05) перед возвратом
        assert elapsed < 1.0

    def test_wait_stop_с_движением_потом_остановка(self):
        transport = AxisTransport(
            config_reply=_config_reply(),
        )
        transport.set_status_script([
            _status_reply(moving=1, pos=100),
            _status_reply(moving=1, pos=200),
            _status_reply(moving=0, pos=340),
        ])
        axis = Axis(transport, axis_id=0, minimum=0, maximum=340)
        axis.wait_stop(timeout=5.0)

    def test_wait_stop_timeout(self):
        transport = AxisTransport(
            config_reply=_config_reply(),
            status_reply=_status_reply(moving=1),
        )
        axis = Axis(transport, axis_id=0, minimum=0, maximum=340)
        with pytest.raises(TimeoutError, match="не остановилась"):
            axis.wait_stop(timeout=0.2)

    def test_wait_stop_progress_callback(self):
        transport = AxisTransport(
            config_reply=_config_reply(),
        )
        transport.set_status_script([
            _status_reply(moving=1, pos=100),
            _status_reply(moving=0, pos=340),
        ])
        axis = Axis(transport, axis_id=0, minimum=0, maximum=340)
        positions = []

        def progress(pos, moving):
            positions.append((pos, moving))

        axis.wait_stop(timeout=5.0, progress_callback=progress)
        assert len(positions) >= 2
        # Последний callback: остановка
        assert positions[-1][1] == 0


class TestVerifyHomed:
    def test_verify_homed_проходит_при_штатных_значениях(self):
        transport = AxisTransport(
            config_reply=_config_reply(),
            status_reply=_status_reply(pos=0, moving=0, homed=1, limits=1),
        )
        axis = Axis(transport, axis_id=0, minimum=0, maximum=340)
        result = axis.verify_homed()
        assert result["position"] == 0
        assert result["homed"] == 1

    def test_verify_homed_отвергает_неhomed(self):
        transport = AxisTransport(
            config_reply=_config_reply(),
            status_reply=_status_reply(pos=0, moving=0, homed=0, limits=1),
        )
        axis = Axis(transport, axis_id=0, minimum=0, maximum=340)
        with pytest.raises(RuntimeError, match="homing postcondition"):
            axis.verify_homed()

    def test_verify_homed_отвергает_не_ноль(self):
        transport = AxisTransport(
            config_reply=_config_reply(),
            status_reply=_status_reply(pos=50, moving=0, homed=1, limits=1),
        )
        axis = Axis(transport, axis_id=0, minimum=0, maximum=340)
        with pytest.raises(RuntimeError, match="homing postcondition"):
            axis.verify_homed()
