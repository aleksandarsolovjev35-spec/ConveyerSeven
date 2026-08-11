"""Двухступенчатый распределитель: маршрутная матрица и межосевые инварианты.

Физический смысл:
- DIST1 в 0 — корпус падает в GOOD; в открытой позиции — уходит на DIST2.
- DIST2 выбирает BAD или CLEANUP только для переданного ему корпуса.
- DIST2 НИКОГДА не двигается, пока DIST1 направляет на неё корпус
  (сначала DIST1 возвращается в GOOD=0).
"""

import pytest
from fakes import FakeAxis, FakeTransport

from hardware.distributor import Distributor


def make_distributor(dist1_pos=0, dist2_pos=0):
    transport = FakeTransport()
    shared = []
    dist1 = FakeAxis(transport, "dist1", shared, pos=dist1_pos)
    dist2 = FakeAxis(transport, "dist2", shared, pos=dist2_pos)
    dist = Distributor(
        dist1, dist2,
        dist1_open_position=340,
        dist2_bad_position=0,
        dist2_cleanup_position=340,
        drop_time=0.0,
    )
    dist._dist1_position = dist1_pos
    dist._dist2_position = dist2_pos
    return dist, dist1, dist2, shared, transport


class TestValidation:
    @pytest.mark.parametrize("kwargs", [
        {"dist1_open_position": 0},
        {"dist1_open_position": -5},
        {"dist1_open_position": 340.5},          # float недопустим
        {"dist2_bad_position": -1},
        {"dist2_cleanup_position": 0},
        {"dist2_bad_position": 100,
         "dist2_cleanup_position": 100},          # маршруты обязаны различаться
    ])
    def test_невалидные_позиции(self, kwargs):
        base = dict(
            dist1_open_position=340,
            dist2_bad_position=0,
            dist2_cleanup_position=340,
        )
        base.update(kwargs)
        with pytest.raises(ValueError):
            Distributor(None, None, **base)

    def test_неизвестная_категория_маршрута(self):
        dist, *_ = make_distributor()
        with pytest.raises(ValueError):
            dist.prepare_route("MAYBE")
        with pytest.raises(ValueError):
            dist.confirm_transfer(1, "MAYBE")
        with pytest.raises(ValueError):
            dist.diagnostic_route("GOOD")  # DIST2 знает только BAD/CLEANUP


class TestRouting:
    def test_good_маршрут_это_только_dist1_в_ноль(self):
        dist, dist1, dist2, shared, _t = make_distributor(dist1_pos=340)
        dist.prepare_route("GOOD", part_id=7)
        assert ("dist1", "move", 0) in shared
        # DIST2 с места не сходит: GOOD не касается второй заслонки.
        assert not [op for op in shared if op[0] == "dist2"]
        assert dist.dist1_state == "GOOD"

    def test_bad_маршрут_с_чистого_состояния(self):
        # dist2 уже стоит на BAD=0: движется только DIST1 в открытую позицию.
        dist, dist1, dist2, shared, _t = make_distributor()
        dist.prepare_route("BAD", part_id=7)
        assert shared == [("dist1", "move", 340)]
        assert dist.dist1_state == "TO_DIST2"
        assert dist.dist2_target == "BAD"
        assert "PART #7 -> BAD READY" == dist.last_action

    def test_cleanup_маршрут_с_чистого_состояния(self):
        dist, _d1, _d2, shared, _t = make_distributor()
        dist.prepare_route("CLEANUP", part_id=9)
        assert shared == [("dist2", "move", 340), ("dist1", "move", 340)]
        assert dist.dist2_target == "CLEANUP"

    def test_смена_cleanup_на_bad_сначала_возвращает_dist1(self):
        """Инвариант: DIST2 не двигается, пока DIST1 направляет на неё."""
        dist, _d1, _d2, shared, _t = make_distributor()
        dist.prepare_route("CLEANUP")
        shared.clear()
        dist.prepare_route("BAD")
        assert shared == [
            ("dist1", "move", 0),     # путь на DIST2 закрыт
            ("dist2", "move", 0),     # только теперь DIST2 меняет канал
            ("dist1", "move", 340),   # и снова открывается передача
        ]

    def test_повторная_подготовка_того_же_маршрута_без_движений(self):
        dist, _d1, _d2, shared, _t = make_distributor()
        dist.prepare_route("BAD")
        shared.clear()
        dist.prepare_route("BAD")
        assert shared == []

    def test_confirm_transfer_без_смены_маршрута(self):
        dist, _d1, _d2, shared, _t = make_distributor()
        dist.prepare_route("BAD", part_id=3)
        shared.clear()
        dist.confirm_transfer(3, "BAD")
        assert shared == []
        assert dist.last_action == "PART #3 -> BAD DONE"

    def test_cancel_check_прерывает_операции(self):
        dist, _d1, _d2, shared, _t = make_distributor()
        dist.cancel_check = lambda: True
        with pytest.raises(RuntimeError, match="cancelled"):
            dist.prepare_route("CLEANUP")
        # Ни одна ось не должна была тронуться.
        assert shared == []


class TestHomingAndEmergency:
    def test_штатный_homing_без_g25(self):
        dist, _d1, _d2, _shared, transport = make_distributor()
        dist.initialize()
        assert "G25" not in transport.sent
        assert dist.dist1_state == "GOOD"
        assert dist.dist2_state == "IDLE"
        assert dist.last_action == "HOMED"

    def test_таймаут_homing_глушит_оси_командой_g25(self):
        # Прошивка ищет концевик без ограничения пробега: сдавшийся по
        # таймауту хост обязан остановить ось, а не бросить её на упор.
        dist, dist1, _d2, _shared, transport = make_distributor()
        dist1.fail_home = True
        with pytest.raises(TimeoutError):
            dist.initialize()
        assert "G25" in transport.sent
        assert dist.dist1_state == "FAULT"
        assert dist.dist2_state == "FAULT"
        assert dist.last_action == "EMERGENCY STOP"

    def test_emergency_stop_вручную(self):
        dist, _d1, _d2, _shared, transport = make_distributor()
        dist.emergency_stop()
        assert transport.sent[-1] == "G25"
        assert dist.dist1_state == dist.dist2_state == "FAULT"

    def test_park_production_ставит_good_и_bad(self):
        dist, _d1, _d2, shared, _t = make_distributor(
            dist1_pos=340, dist2_pos=340,
        )
        dist.park_production()
        # DIST1 -> 0 (GOOD), DIST2 -> 0 (BAD).
        assert ("dist1", "move", 0) in shared
        assert ("dist2", "move", 0) in shared
        assert dist.dist2_target == "BAD"
        assert dist.last_action == "PRODUCTION READY"

    def test_status_полон_и_без_отрицательных_позиций(self):
        dist, _d1, _d2, _shared, _t = make_distributor()
        status = dist.status
        for key in (
            "dist1_position", "dist1_max", "dist1_state",
            "dist2_position", "dist2_max", "dist2_state",
            "dist2_target", "last_distributor_action",
        ):
            assert key in status
        assert status["dist1_position"] == 0
        assert status["dist1_max"] == 340
