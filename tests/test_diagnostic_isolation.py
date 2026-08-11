"""Диагностика не уводит линию в терминальный FAULT из-за прикладной ошибки.

Диагностика запускается оператором вручную в IDLE/STOPPED и не двигает
ленту. Раньше любое исключение внутри неё вызывало ``_handle_fault``: линия
попадала в FAULT, из которого выход только через перезапуск процесса —
даже если причиной была отключённая модель или опечатка в имени команды.

Здесь проверяется новое разделение:
* прикладная ошибка -> статус диагностики ERROR, состояние линии сохраняется,
  повторный запуск возможен;
* реальный отказ оборудования (залипший отказ CameraManager, упавший
  live-просмотр) -> по-прежнему FAULT.
"""

import threading

import pytest

from core.production_cycle import ProductionCycle
from inspection.result import InspectionResult

CAMERA_ROLES = (
    "INPUT_LEFT", "INPUT_RIGHT",
    "SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT",
    "TOP",
)


class FakeFrame:
    """Минимальный кадр: диагностике камер нужен только ``shape``."""

    shape = (720, 1280, 3)


class FakeCameras:
    """Камеры с управляемым отказом.

    ``failure_reason`` повторяет свойство реального CameraManager: оно не
    ``None`` только после залипшего отказа оборудования.
    """

    def __init__(self, capture_error=None, failure_reason=None):
        self.mapping = {role: i for i, role in enumerate(CAMERA_ROLES)}
        self.capture_error = capture_error
        self.failure_reason = failure_reason
        self.capture_calls = 0

    def _frames(self, roles=None):
        self.capture_calls += 1
        if self.capture_error is not None:
            raise self.capture_error
        return {role: FakeFrame() for role in (roles or CAMERA_ROLES)}

    def capture_all(self):
        return self._frames()

    def capture_roles(self, roles):
        return self._frames(tuple(roles))

    def capture_single(self, role):
        return FakeFrame()

    def drain_buffers(self, roles=None):
        pass


class FakeConveyor:
    speed = 20000
    steps_per_division = 19048

    def move_step(self):
        pass

    def wait_stop(self, progress_callback=None):
        pass

    def emergency_stop(self):
        pass


class FakeVision:
    """Кластер моделей: process_all либо возвращает пустые детекции, либо падает."""

    def __init__(self, error=None):
        self.last_health = []
        self._error = error

    def process_all(self, frames):
        if self._error is not None:
            raise self._error
        return {role: [] for role in frames}


class FakeInspector:
    INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    SPIDER_ROLES = ("SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP")

    def __init__(self, error=None):
        self.vision = FakeVision(error)
        self._error = error

    def set_progress_callback(self, callback):
        pass

    def inspect_input_consensus(self, part_id, step, frame_runs):
        if self._error is not None:
            raise self._error
        return InspectionResult(stage="input", defects=[], is_empty_tray=True)

    def inspect_spider_consensus(self, part_id, step, frame_runs):
        if self._error is not None:
            raise self._error
        return InspectionResult(stage="spider", defects=[])


class FakeDistributor:
    def __init__(self, gate_error=None):
        self.on_state_changed = None
        self.cancel_check = None
        self.dist1_open_position = 340
        self.gate_error = gate_error
        self.calls = []
        self._lock = threading.Lock()

    def _record(self, op, payload):
        with self._lock:
            self.calls.append((op, payload))

    def park_production(self):
        self._record("park_production", None)

    def reset_target(self):
        self._record("reset_target", None)

    def emergency_stop(self):
        self._record("emergency_stop", None)

    def diagnostic_gate(self, position):
        self._record("diagnostic_gate", position)
        if self.gate_error is not None:
            raise self.gate_error

    def diagnostic_route(self, category):
        self._record("diagnostic_route", category)

    @property
    def status(self):
        return {
            "dist1_position": 0, "dist1_max": 340, "dist1_state": "GOOD",
            "dist2_position": 0, "dist2_max": 340, "dist2_state": "IDLE",
            "dist2_target": "BAD", "last_distributor_action": "-",
        }


class FakeArchive:
    batch_id = "batch_test"

    def store_frames(self, **kwargs):
        pass

    def finalize(self, **kwargs):
        pass

    def get_part_info(self, part_id):
        return {}


def build_cycle(cameras=None, inspector=None, distributor=None):
    cycle = ProductionCycle(
        conveyor=FakeConveyor(),
        cameras=cameras or FakeCameras(),
        inspector=inspector or FakeInspector(),
        distributor=distributor or FakeDistributor(),
        monitor=None,
        archive=FakeArchive(),
        jog=None,
        settle_seconds=0.0,
        stage_trace_seconds=0.0,
        review_seconds=0.0,
    )
    # Живой просмотр в тестах не запускаем: реальные потоки чтения камер
    # здесь не нужны, а его stop()/reset_pause() безопасны и без старта.
    return cycle


def test_ошибка_проверки_камер_не_переводит_линию_в_fault():
    """Камеры исправны (failure_reason=None), но capture_all бросил ошибку."""
    cameras = FakeCameras(capture_error=RuntimeError("кадр не разобран"))
    cycle = build_cycle(cameras=cameras)

    with pytest.raises(RuntimeError):
        cycle.diagnostic_check_cameras()

    assert cycle.state == "IDLE", "линия обязана остаться в исходном состоянии"
    assert cycle._fault_reason is None, "прикладная ошибка не должна давать FAULT"
    assert cycle._diagnostics["status"] == "ERROR"
    assert cycle._diagnostics["kind"] == "CAMERAS"
    assert "кадр не разобран" in cycle._diagnostics["message"]


def test_после_ошибки_диагностику_можно_повторить():
    """Главный смысл правки: оператор чинит причину и запускает проверку снова."""
    cameras = FakeCameras(capture_error=RuntimeError("камера занята"))
    cycle = build_cycle(cameras=cameras)

    with pytest.raises(RuntimeError):
        cycle.diagnostic_check_cameras()
    assert cycle._diagnostics["status"] == "ERROR"

    # Оператор устранил причину.
    cameras.capture_error = None
    assert cycle.diagnostic_check_cameras() is True
    assert cycle._diagnostics["status"] == "PASSED"
    assert len(cycle._diagnostics["cameras"]) == len(CAMERA_ROLES)
    assert cycle._fault_reason is None


def test_залипший_отказ_камер_всё_ещё_переводит_в_fault():
    """Потеря оборудования — это настоящая авария, FAULT сохраняется."""
    cameras = FakeCameras(
        capture_error=RuntimeError("VideoCapture закрыт"),
        failure_reason="USB-камера TOP отвалилась",
    )
    cycle = build_cycle(cameras=cameras)

    with pytest.raises(RuntimeError):
        cycle.diagnostic_check_cameras()

    assert cycle.state == "FAULT"
    assert cycle._fault_reason is not None
    assert "USB-камера TOP отвалилась" in cycle._fault_reason
    assert cycle._diagnostics["status"] == "ERROR"


def test_неизвестная_команда_распределителя_не_двигает_железо():
    """Опечатка в команде — ошибка вызывающего, а не повод для аварии."""
    distributor = FakeDistributor()
    cycle = build_cycle(distributor=distributor)

    with pytest.raises(ValueError):
        cycle.distributor_diagnostic("DIST3_WHATEVER")

    assert cycle.state == "IDLE"
    assert cycle._fault_reason is None
    assert not any(
        op in ("diagnostic_gate", "diagnostic_route")
        for op, _ in distributor.calls
    ), "неизвестная команда не должна доходить до распределителя"


def test_отказ_распределителя_переводит_в_fault():
    """А вот сбой самого механизма распределителя останавливает линию."""
    distributor = FakeDistributor(gate_error=RuntimeError("ось не дошла до HOME"))
    cycle = build_cycle(distributor=distributor)

    with pytest.raises(RuntimeError):
        cycle.distributor_diagnostic("DIST1_HOME")

    assert cycle.state == "FAULT"
    assert "ось не дошла до HOME" in cycle._fault_reason


def test_ошибка_моделей_и_правил_не_переводит_линию_в_fault():
    """Правило или модель упали — оператор правит пороги и повторяет проверку."""
    inspector = FakeInspector(error=RuntimeError("модель top.pt не загружена"))
    cycle = build_cycle(inspector=inspector)

    with pytest.raises(RuntimeError):
        cycle.diagnostic_check_vision_rules()

    assert cycle.state == "IDLE"
    assert cycle._fault_reason is None
    assert cycle._diagnostics["status"] == "ERROR"
    assert cycle._diagnostics["kind"] == "VISION_RULES"
