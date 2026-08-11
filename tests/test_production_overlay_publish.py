"""Публикация геометрии стадии в монитор: инкрементальная разметка INPUT,
очистка при STOPPED и сохранение кадров стадии при FAULT.

Регрессия жалоб оператора:
- «геометрия не рисуется во время анализа» — разметка публиковалась только
  в конце ANALYSIS, хотя была готова раньше;
- «геометрия висит на экране / показывает не ту» — после остановки линии
  или FAULT общий набор правил оставался в мониторе и рисовался поверх
  движущегося live-изображения.
"""

import threading

from core.production_cycle import ProductionCycle
from domain.defect_rules.base import RuleResult
from inspection.result import InspectionResult

CAMERA_ROLES = (
    "INPUT_LEFT", "INPUT_RIGHT",
    "SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT",
    "TOP",
)
INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")


def wait_until(predicate, timeout=20.0, interval=0.01, what="условие"):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"не дождались: {what} за {timeout} с")


class RecordingMonitor:
    """Записывает все публикации монитора, как это делал бы UIServer."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()
        self.server = None  # нет реального UIServer: активная роль не нужна

    def update(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)

    def clear_evidence(self):
        with self._lock:
            self.calls.append({"__clear_evidence__": True})

    def snapshot(self):
        with self._lock:
            return list(self.calls)


class FakeFrame:
    shape = (480, 640, 3)

    def __init__(self, value=0):
        self.value = value

    def copy(self):
        return self


class FakeCameras:
    def __init__(self):
        self.mapping = {role: i for i, role in enumerate(CAMERA_ROLES)}
        self._lock = threading.Lock()
        self._n = 0

    def _fresh(self, role):
        with self._lock:
            self._n += 1
            return FakeFrame(self._n)

    def capture_single(self, role):
        return self._fresh(role)

    def capture_roles(self, roles):
        roles = tuple(roles)
        return {role: self._fresh(role) for role in roles}

    def capture_all(self):
        return {role: self._fresh(role) for role in CAMERA_ROLES}

    def drain_buffers(self, roles=None):
        pass


class FakeConveyor:
    def __init__(self, fail_on_move=None):
        self.moves = 0
        self.emergency_stops = 0
        self._fail_on_move = fail_on_move

    def move_step(self):
        self.moves += 1

    def wait_stop(self, progress_callback=None):
        if self._fail_on_move is not None and self.moves >= self._fail_on_move:
            raise RuntimeError("симулированная потеря подтверждения остановки")
        if progress_callback is not None:
            progress_callback({"mov": 0, "wait": 0, "pos": 0, "tgt": 0, "lasterr": 0})

    def emergency_stop(self):
        self.emergency_stops += 1


class FakeDistributor:
    def __init__(self):
        self.on_state_changed = None
        self.cancel_check = None
        self.calls = []

    def _record(self, op, **payload):
        self.calls.append((op, payload))

    def park_production(self):
        self._record("park_production")

    def prepare_route(self, category, part_id=None):
        self._record("prepare_route", category=category, part_id=part_id)

    def confirm_transfer(self, part_id, category):
        self._record("confirm_transfer", part_id=part_id, category=category)

    def reset_target(self):
        self._record("reset_target")

    def emergency_stop(self):
        self._record("emergency_stop")

    @property
    def status(self):
        return {
            "dist1_position": 0, "dist1_max": 340, "dist1_state": "GOOD",
            "dist2_position": 0, "dist2_max": 340, "dist2_state": "IDLE",
            "dist2_target": "BAD", "last_distributor_action": "-",
        }


def _drawing(role):
    return {"role": role, "type": "rule_bbox", "bbox": [10, 10, 60, 60]}


class FakeInspector:
    INPUT_ROLES = INPUT_ROLES
    SPIDER_ROLES = ("SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP")

    def __init__(self, input_script, spider_defects):
        self._input_script = list(input_script)
        self._spider_defects = dict(spider_defects)
        self.vision = type("V", (), {"last_health": []})()
        self.on_last_input = None
        self.input_calls = []
        self.spider_calls = []

    def set_progress_callback(self, callback):
        pass

    def _input_result(self, part_id, step, present, frames):
        if not present:
            return InspectionResult(
                stage="input",
                defects=[],
                is_empty_tray=True,
                consensus={"part_present": False},
                raw_frames=frames,
                run_frames=[frames],
                run_rule_results=[[]],
            )
        rules = [RuleResult(rule_name="window_geometry", triggered=False, drawings=[_drawing(r)]) for r in INPUT_ROLES]
        return InspectionResult(
            stage="input",
            defects=[],
            vision_results={r: [{"class": "case", "bbox": [10, 10, 60, 60]}] for r in INPUT_ROLES},
            rule_results=list(rules),
            consensus={"part_present": True},
            raw_frames=frames,
            run_frames=[frames],
            run_rule_results=[rules],
        )

    def inspect_input_consensus(self, part_id, step, frame_runs):
        present = self._input_script.pop(0) if self._input_script else False
        self.input_calls.append({"candidate": part_id, "step": step, "present": present})
        frames = dict(frame_runs[-1]) if frame_runs else {}
        if not self._input_script and self.on_last_input is not None:
            hook = self.on_last_input
            self.on_last_input = None
            hook()
        return self._input_result(part_id, step, present, frames)

    def inspect_spider_consensus(self, part_id, step, frame_runs):
        defects = list(self._spider_defects.get(part_id, []))
        self.spider_calls.append({"part_id": part_id, "step": step, "defects": defects})
        frames = dict(frame_runs[-1]) if frame_runs else {}
        rules = [
            RuleResult(
                rule_name="top_contacts",
                triggered=False,
                drawings=[_drawing(role)],
            )
            for role in self.SPIDER_ROLES
        ]
        return InspectionResult(
            stage="spider",
            defects=defects,
            vision_results={r: [{"class": "case", "bbox": [10, 10, 60, 60]}] for r in self.SPIDER_ROLES},
            rule_results=list(rules),
            consensus={"defect_count": len(defects)},
            raw_frames=frames,
            run_frames=[frames],
            run_rule_results=[rules],
        )


def build_cycle(input_script, spider_defects, conveyor=None):
    monitor = RecordingMonitor()
    cameras = FakeCameras()
    distributor = FakeDistributor()
    inspector = FakeInspector(input_script, spider_defects)
    cycle = ProductionCycle(
        conveyor=conveyor or FakeConveyor(),
        cameras=cameras,
        inspector=inspector,
        distributor=distributor,
        monitor=monitor,
        archive=None,
        jog=None,
        settle_seconds=0.0,
        stage_trace_seconds=0.0,
        review_seconds=0.0,
    )
    return cycle, monitor, inspector


def run_cycle(cycle):
    thread = threading.Thread(target=cycle.start, name="cycle-under-test", daemon=True)
    thread.start()
    return thread


def finish_cycle(cycle, thread):
    cycle.request_exit()
    thread.join(timeout=10.0)
    assert not thread.is_alive(), "цикл обязан завершиться после request_exit"


def test_разметка_input_публикуется_до_конца_анализа():
    """Геометрия INPUT появляется в мониторе сразу после построения, а не
    только в финальной публикации ANALYSIS (иначе оператор видит пустой
    кадр во время SPIDER-моделей)."""
    cycle, monitor, inspector = build_cycle(
        input_script=[True],
        spider_defects={1: []},
    )
    thread = run_cycle(cycle)
    try:
        assert cycle.request_start() is True
        wait_until(
            lambda: len(inspector.input_calls) >= 1,
            what="INPUT анализ выполнен",
        )
        wait_until(
            lambda: any(
                call.get("run_frames") is not None
                and call.get("run_rule_results")
                and len(call["run_rule_results"]) == 1
                and len(call["run_rule_results"][0]) == len(INPUT_ROLES)
                for call in monitor.snapshot()
            ),
            what="разметка INPUT опубликована в монитор",
        )
        # Дожидаемся и SPIDER-стадии, чтобы финальная публикация шага уже
        # ушла в монитор.
        wait_until(
            lambda: len(inspector.spider_calls) >= 1,
            what="SPIDER анализ выполнен",
        )
        # Публикация должна произойти до финального publish шага.
        calls = monitor.snapshot()
        input_publish = next(
            i for i, call in enumerate(calls)
            if call.get("run_rule_results")
            and len(call["run_rule_results"]) == 1
            and len(call["run_rule_results"][0]) == len(INPUT_ROLES)
        )
        # После инкрементальной INPUT-разметки приходит финальная публикация
        # шага: на шаге SPIDER-контроля это уже разметка с правилами SPIDER
        # (вход к тому моменту пуст, поэтому >= 5 правил).
        assert any(
            call.get("run_rule_results")
            and len(call["run_rule_results"]) == 1
            and len(call["run_rule_results"][0]) >= len(inspector.SPIDER_ROLES)
            for call in calls[input_publish:]
        ), "после INPUT-разметки должна прийти финальная разметка шага"
    finally:
        finish_cycle(cycle, thread)


def test_остановка_линии_убирает_геометрию_с_экрана():
    """STOPPED: разметка и кадры стадии очищаются, чтобы геометрия не
    «висела» на live-изображении после остановки."""
    cycle, monitor, inspector = build_cycle(
        input_script=[True],
        spider_defects={1: []},
    )
    inspector.on_last_input = cycle.request_stop
    thread = run_cycle(cycle)
    try:
        assert cycle.request_start() is True
        wait_until(
            lambda: cycle.state == "STOPPED", timeout=30.0,
            what="линия слилась в STOPPED",
        )
        calls = monitor.snapshot()
        # После перехода в STOPPED была публикация очистки оверлеев.
        assert any(
            call.get("rule_results") == []
            and call.get("vision_results") == {}
            and call.get("run_frames") == []
            for call in calls
        ), "в STOPPED не опубликована очистка геометрии"
    finally:
        finish_cycle(cycle, thread)


def test_fault_убирает_правила_но_сохраняет_кадры_стадии():
    """FAULT: общий набор правил очищается (не рисуем «чужую» геометрию на
    остановленном live), а замороженные кадры стадии остаются для
    диагностики."""
    cycle, monitor, inspector = build_cycle(
        input_script=[True],
        spider_defects={1: []},
        conveyor=FakeConveyor(fail_on_move=1),
    )
    thread = run_cycle(cycle)
    try:
        assert cycle.request_start() is True
        wait_until(
            lambda: cycle.state == "FAULT", timeout=30.0,
            what="линия ушла в FAULT",
        )
        calls = monitor.snapshot()
        assert any("__clear_evidence__" in call for call in calls), (
            "FAULT обязан вызвать clear_evidence"
        )
    finally:
        finish_cycle(cycle, thread)
