"""Протокол остановки ленты: wait_stop требует доказательство реального хода.

Это фиксация контракта из коммита f819a32: «ход завершён» не должен быть
неотличим от «команда G3 не дошла», а липкая ошибка прошивки lastErr не
может молча удерживать линию до таймаута.
"""

import time

import pytest
from fakes import STATUS_MOVING, STATUS_STOPPED, FakeTransport

from hardware.conveyor import MOTION_EVIDENCE_TIMEOUT, Conveyor


def make_conveyor(transport=None):
    transport = transport or FakeTransport(("0",), (STATUS_STOPPED,))
    return transport, Conveyor(
        transport,
        speed=20000,
        accel=6000,
        steps_per_division=19048,
        divisions_per_movement=2,
    )


def test_init_фиксирует_протокол_остановки_явно():
    transport, _conv = make_conveyor()
    # Автопауза-стоп нужна, дефолтная межходовая пауза 2000 мс — нет:
    # иначе WAIT=1 добавлял бы ~2 с на каждом шаге линии.
    assert "G12 S1" in transport.sent
    assert "G9 S0" in transport.sent
    # Геометрия шага обязана быть передана контроллеру.
    assert "G7 S19048" in transport.sent
    assert "G6 S2" in transport.sent
    assert "G5 S20000" in transport.sent
    assert "G4 S6000" in transport.sent


def test_init_отвергает_нулевую_геометрию():
    for kwargs in (
        {"steps_per_division": 0},
        {"steps_per_division": -1},
        {"divisions_per_movement": 0},
    ):
        with pytest.raises(ValueError):
            Conveyor(FakeTransport(), **kwargs)


def test_нормальный_ход_подтверждается_без_двухсекундного_окна():
    transport, conv = make_conveyor(FakeTransport(
        ("1", "1", "0"),
        (STATUS_MOVING, STATUS_MOVING, STATUS_STOPPED),
    ))
    conv.move_step()
    assert transport.sent[-1] == "G3"
    start = time.monotonic()
    conv.wait_stop(timeout=15.0)
    elapsed = time.monotonic() - start
    assert elapsed < MOTION_EVIDENCE_TIMEOUT, (
        f"остановка заняла {elapsed:.2f} с — похоже на возврат WAIT-окна"
    )


def test_потерянная_команда_g3_обнаруживается_быстро():
    # Контроллер стабильно отвечает «стою» без единого признака хода.
    transport, conv = make_conveyor(FakeTransport(("0",), (STATUS_STOPPED,)))
    conv.move_step()
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="без признаков хода"):
        conv.wait_stop(timeout=15.0)
    elapsed = time.monotonic() - start
    assert elapsed < MOTION_EVIDENCE_TIMEOUT + 2.0, (
        f"потерянный G3 обнаружен за {elapsed:.2f} с"
    )


def test_липкий_lasterr_пробивает_ожидание_немедленно():
    faulty = "MOV=1 WAIT=0 POS=2000 TGT=38096 lastErr=9"
    transport, conv = make_conveyor(FakeTransport(
        ("1", "1", "1"),
        (STATUS_MOVING, faulty, STATUS_MOVING),
    ))
    conv.move_step()
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="lastErr=9"):
        conv.wait_stop(timeout=15.0)
    # Fail-fast: ждать таймаут из-за аппаратной ошибки нельзя.
    assert time.monotonic() - start < MOTION_EVIDENCE_TIMEOUT


def test_мусор_в_i1_компенсируется_доказательством_хода_по_i2():
    # Сервисные строки ломают разбор I1 (None), но I2 показывает ход.
    transport, conv = make_conveyor(FakeTransport(
        ("Movement on pause...", "garbage", "0"),
        (STATUS_MOVING, STATUS_MOVING, STATUS_STOPPED),
    ))
    conv.move_step()
    conv.wait_stop(timeout=15.0)  # не должно упасть


def test_вечное_движение_даёт_обычный_timeout():
    transport, conv = make_conveyor(FakeTransport(("1",), (STATUS_MOVING,)))
    conv.move_step()
    start = time.monotonic()
    with pytest.raises(TimeoutError, match="не остановился"):
        conv.wait_stop(timeout=2.0)
    elapsed = time.monotonic() - start
    assert 1.5 < elapsed < 4.0


def test_progress_callback_получает_распарсенный_статус():
    transport, conv = make_conveyor(FakeTransport(
        ("1", "0"),
        (STATUS_MOVING, STATUS_STOPPED),
    ))
    reports = []
    conv.move_step()
    conv.wait_stop(timeout=15.0, progress_callback=reports.append)
    assert reports, "wait_stop обязан публиковать фактический I2 status"
    assert reports[0]["mov"] == 1
    assert reports[-1]["mov"] == 0


def test_emergency_stop_шлёт_g1():
    transport, conv = make_conveyor()
    conv.emergency_stop()
    assert transport.sent[-1] == "G1"
