"""Интеграционный учёт корпусов на линии: настоящий ProductionCycle + фейки.

Проверяется главный инвариант системы: логические позиции деталей всегда
соответствуют механике линии — сценарий «3 корпуса разных категорий и один
пустой лоток» гоняется через все барьеры шага (MOTION/SETTLE/CAPTURE/
ANALYSIS/REVIEW/PUBLISH) до полного слива линии в STOPPED.

Все задержки обнулены (settle/trace/review), поэтому сценарий занимает
секунды, а не производственные ~11 шагов по ~10 с.
"""

import threading
import time
from types import SimpleNamespace

from core.production_cycle import ProductionCycle
from inspection.result import InspectionResult

CAMERA_ROLES = (
    "INPUT_LEFT", "INPUT_RIGHT",
    "SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT",
    "TOP",
)
INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
SPIDER_ROLES = ("SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP")


def wait_until(predicate, timeout=20.0, interval=0.01, what="условие"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"не дождались: {what} за {timeout} с")


class FakeCameras:
    """Потокобезопасный источник кадров: свежий «кадр» при каждом чтении."""

    def __init__(self):
        self.mapping = {role: index for index, role in enumerate(CAMERA_ROLES)}
        self._lock = threading.Lock()
        self._frame_no = 0
        self.capture_calls = []

    def _fresh(self, role):
        with self._lock:
            self._frame_no += 1
            return f"frame#{self._frame_no}:{role}"

    def capture_single(self, role):
        return self._fresh(role)

    def capture_roles(self, roles):
        roles = tuple(roles)
        with self._lock:
            self.capture_calls.append(roles)
        return {role: self._fresh(role) for role in roles}

    def drain_buffers(self, roles=None):
        pass


class FakeConveyor:
    def __init__(self, fail_on_move=None):
        self.speed = 20000
        self.steps_per_division = 19048
        self.moves = 0
        self.emergency_stops = 0
        self._fail_on_move = fail_on_move

    def move_step(self):
        self.moves += 1

    def wait_stop(self, progress_callback=None):
        if self._fail_on_move is not None and self.moves >= self._fail_on_move:
            raise RuntimeError("симулированная потеря подтверждения остановки")
        if progress_callback is not None:
            progress_callback(
                {"mov": 0, "wait": 0, "pos": 0, "tgt": 0, "lasterr": 0}
            )

    def emergency_stop(self):
        self.emergency_stops += 1


class FakeInspector:
    """Инспектор с детерминированным сценарием по порядку вызовов.

    ``input_script``: True — корпус в лотке, False — пустой лоток.
    Порядок соответствует вызовам inspect_input_consensus, а НЕ id детали:
    при пустом лотке счётчик деталей не двигается, и следующий кандидат
    получает тот же id.
    """

    INPUT_ROLES = INPUT_ROLES
    SPIDER_ROLES = SPIDER_ROLES

    def __init__(self, input_script, spider_defects):
        self._input_script = list(input_script)
        self._spider_defects = dict(spider_defects)
        self.vision = SimpleNamespace(last_health=[])
        self._progress = None
        self.on_last_input = None
        self.input_calls = []
        self.spider_calls = []

    def set_progress_callback(self, callback):
        self._progress = callback

    def inspect_input_consensus(self, part_id, step, frame_runs):
        assert frame_runs is not None
        present = self._input_script.pop(0) if self._input_script else False
        self.input_calls.append(
            {"candidate": part_id, "step": step, "present": present}
        )
        if self._progress:
            self._progress(
                "INPUT_MODELS", "прогон моделей INPUT",
                part_id=part_id, roles=self.INPUT_ROLES,
            )
        result = InspectionResult(
            stage="input",
            defects=[],
            is_empty_tray=not present,
            consensus={"part_present": present},
        )
        if not self._input_script and self.on_last_input is not None:
            hook = self.on_last_input
            self.on_last_input = None
            hook()
        return result

    def inspect_spider_consensus(self, part_id, step, frame_runs):
        assert frame_runs is not None
        defects = list(self._spider_defects.get(part_id, []))
        self.spider_calls.append(
            {"part_id": part_id, "step": step, "defects": defects}
        )
        return InspectionResult(
            stage="spider",
            defects=defects,
            consensus={"defect_count": len(defects)},
        )


class FakeDistributor:
    def __init__(self):
        # ProductionCycle присваивает оба атрибута в конструкторе.
        self.on_state_changed = None
        self.cancel_check = None
        self.dist1_open_position = 340
        self.calls = []
        self._lock = threading.Lock()

    def _record(self, op, **payload):
        with self._lock:
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


class FakeArchive:
    batch_id = "batch_test"

    def __init__(self):
        self.stored = []
        self.finalized = []
        self._lock = threading.Lock()

    def store_frames(self, part_id=None, stage=None, **_kwargs):
        with self._lock:
            self.stored.append({"part_id": part_id, "stage": stage})

    def finalize(self, **kwargs):
        with self._lock:
            self.finalized.append(kwargs)

    def get_part_info(self, part_id):
        return {"relative_folder": f"batch_test/GOOD/part_{part_id:04d}"}


def run_cycle(cycle):
    """Запустить цикл в потоке и вернуть ручку для корректного выхода."""
    thread = threading.Thread(
        target=cycle.start, name="cycle-under-test", daemon=True,
    )
    thread.start()
    return thread


def finish_cycle(cycle, thread):
    cycle.request_exit()
    thread.join(timeout=10.0)
    assert not thread.is_alive(), "цикл обязан завершиться после request_exit"


def build_cycle(input_script, spider_defects, conveyor=None):
    cameras = FakeCameras()
    distributor = FakeDistributor()
    archive = FakeArchive()
    inspector = FakeInspector(input_script, spider_defects)
    cycle = ProductionCycle(
        conveyor=conveyor or FakeConveyor(),
        cameras=cameras,
        inspector=inspector,
        distributor=distributor,
        monitor=None,
        archive=archive,
        jog=None,
        settle_seconds=0.0,
        stage_trace_seconds=0.0,
        review_seconds=0.0,
    )
    return cycle, inspector, distributor, archive, cameras


def test_три_корпуса_и_пустой_лоток_проходят_линию_без_потерь():
    """Сценарий: корпуса GOOD/CLEANUP/BAD + один пустой лоток на входе.

    Скрипт входа по вызовам: корпус, ПУСТО, корпус, корпус. После
    последнего корпуса инспектор сам просит STOP: строка ниже фиксирует
    детерминированный слив (accept_input гаснет на следующей итерации).
    """
    cycle, inspector, distributor, archive, cameras = build_cycle(
        input_script=[True, False, True, True],
        spider_defects={1: [], 2: ["glass"], 3: ["window_geometry"]},
    )
    # STOP запрашивается внутри последнего заскриптованного входа —
    # детерминированно: следующая итерация уже в STOPPING.
    inspector.on_last_input = cycle.request_stop

    thread = run_cycle(cycle)
    try:
        assert cycle.request_start() is True
        wait_until(
            lambda: cycle.state == "STOPPED", timeout=30.0,
            what="линия слилась в STOPPED",
        )

        # --- Счётчики производства ---
        assert cycle.part_counter == 3
        assert cycle.good_count == 1
        assert cycle.cleanup_count == 1
        assert cycle.bad_count == 1
        assert cycle.empty_count == 1
        assert cycle.parts == [], "линия должна быть пуста после STOPPED"

        # --- Хронология входных инспекций ---
        # Первая инспекция идёт БЕЗ движения ленты на step 0; пустой лоток
        # не двигает счётчик деталей (кандидат 2 встречается дважды).
        assert [c["candidate"] for c in inspector.input_calls] == [1, 2, 2, 3]
        assert [c["step"] for c in inspector.input_calls] == [0, 1, 2, 3]
        assert [c["present"] for c in inspector.input_calls] == [
            True, False, True, True,
        ]

        # --- SPIDER-инспекции строго на позиции +4 ---
        # step_created: #1 -> 0 (старт без движения), #2 -> 2, #3 -> 3.
        spider_by_part = {c["part_id"]: c for c in inspector.spider_calls}
        assert spider_by_part[1]["step"] == 0 + 4
        assert spider_by_part[2]["step"] == 2 + 4
        assert spider_by_part[3]["step"] == 3 + 4

        # --- Маршруты распределителя в порядке прохождения ---
        routes = [
            (payload["part_id"], payload["category"])
            for op, payload in distributor.calls
            if op == "prepare_route"
        ]
        assert routes == [(1, "GOOD"), (2, "CLEANUP"), (3, "BAD")]
        transfers = [
            (payload["part_id"], payload["category"])
            for op, payload in distributor.calls
            if op == "confirm_transfer"
        ]
        assert transfers == [(1, "GOOD"), (2, "CLEANUP"), (3, "BAD")]
        park_calls = [
            op for op, _p in distributor.calls if op == "park_production"
        ]
        assert park_calls == ["park_production"], (
            "park_production вызывается ровно один раз при пуске"
        )

        # --- Архив: каждая деталь получила обе стадии кадров ---
        input_stored = sorted(
            s["part_id"] for s in archive.stored if s["stage"] == "input"
        )
        spider_stored = sorted(
            s["part_id"] for s in archive.stored if s["stage"] == "spider"
        )
        assert input_stored == [1, 2, 3]
        assert spider_stored == [1, 2, 3]
        # Пустой лоток в архив не пишется: ровно 3 входные записи.
        assert len(input_stored) == 3

        finalized = {f["part_id"]: f for f in archive.finalized}
        assert set(finalized) == {1, 2, 3}
        assert finalized[1]["category"] == "GOOD"
        assert finalized[1]["defects"] == []
        assert finalized[2]["category"] == "CLEANUP"
        assert finalized[2]["defects"] == ["glass"]
        assert finalized[3]["category"] == "BAD"
        assert finalized[3]["defects"] == ["window_geometry"]
        # Шаг создания детали сохраняется для трассировки.
        assert finalized[2]["step"] == 2

        # --- UI-последние детали с архивной ссылкой ---
        recent = list(cycle.recent_parts)
        assert [r["id"] for r in recent] == [1, 2, 3]
        assert all(r.get("batch_id") == "batch_test" for r in recent)
        assert all(r.get("archive_folder") for r in recent)

        # --- Физика: 11 подтверждённых ходов (старт без движения) ---
        assert cycle.current_step == 11
        assert cycle.conveyor.moves == 11
    finally:
        finish_cycle(cycle, thread)

    # --- Останавливающие команды при выходе ---
    assert cycle.conveyor.emergency_stops >= 1
    assert any(op == "emergency_stop" for op, _p in distributor.calls)


def test_stop_на_пустой_линии_не_двигает_ленту():
    """STOP сразу после пуска: шагов быть не должно вообще."""
    cycle, inspector, distributor, archive, _cameras = build_cycle(
        input_script=[False],   # если инспекция всё же случится — лоток пуст
        spider_defects={},
    )
    thread = run_cycle(cycle)
    try:
        assert cycle.request_start() is True
        cycle.request_stop()
        wait_until(
            lambda: cycle.state == "STOPPED", timeout=15.0,
            what="пустая линия остановилась",
        )
        assert cycle.conveyor.moves == 0, (
            "STOP на пустой линии не должен подавать команду движения"
        )
        assert cycle.current_step == 0
        assert cycle.part_counter == 0
        assert archive.finalized == []
    finally:
        finish_cycle(cycle, thread)


def test_ошибка_хода_это_fault_с_архивацией_незавершённых_деталей():
    """Потеря подтверждения остановки -> FAULT; «висящая» деталь архивируется."""
    conveyor = FakeConveyor(fail_on_move=1)
    cycle, inspector, distributor, archive, _cameras = build_cycle(
        input_script=[True, False],
        spider_defects={},
        conveyor=conveyor,
    )
    thread = run_cycle(cycle)
    try:
        assert cycle.request_start() is True
        wait_until(
            lambda: cycle.state == "FAULT", timeout=15.0,
            what="линия ушла в FAULT",
        )
        # Повторять неподтверждённый шаг нельзя, о состоянии линии
        # дальше неизвестно — линия должна остаться в FAULT, а не
        # «самопочиниться» назад в RUNNING.
        time.sleep(0.3)
        assert cycle.state == "FAULT"
        assert cycle.conveyor.moves == 1
        assert cycle.conveyor.emergency_stops >= 1
    finally:
        finish_cycle(cycle, thread)

    # Деталь, оставшаяся на линии в момент аварии, не должна потеряться:
    # она архивируется как прерванная с принудительной категорией BAD.
    aborted = [f for f in archive.finalized if f.get("extra", {}).get("aborted")]
    assert len(aborted) == 1
    assert aborted[0]["part_id"] == 1
    assert aborted[0]["category"] == "BAD"
    assert aborted[0]["decision"] == "aborted_runtime_shutdown"
    assert aborted[0]["extra"]["abort_reason"] == "runtime_shutdown"
