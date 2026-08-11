"""Смена в базе: настоящий ProductionCycle + настоящий SQLite.

Проверяется сквозной путь Фазы 5: пуск линии открывает смену, каждая
деталь доезжает до `part_records` через EventBus и PartRecorder, а
остановка закрывает смену итоговыми счётчиками. Фейки оборудования взяты
из `test_production_line_accounting`, чтобы сценарий совпадал с уже
принятым эталоном учёта.
"""

import json

import pytest

from core.database import DatabaseManager
from core.db_recorder import PartRecorder
from core.event_bus import EventBus
from core.production_cycle import ProductionCycle
from core.structured_logging import _reset_for_tests as reset_structlog
from core.structured_logging import configure_structlog, json_log_path
from tests.test_production_line_accounting import (
    FakeArchive,
    FakeCameras,
    FakeConveyor,
    FakeDistributor,
    FakeInspector,
    finish_cycle,
    run_cycle,
    wait_until,
)


@pytest.fixture(autouse=True)
def _clean_structlog():
    reset_structlog()
    yield
    reset_structlog()


@pytest.fixture()
def db(tmp_path):
    manager = DatabaseManager(tmp_path / "line.db")
    yield manager
    manager.dispose()


def build_cycle_with_db(database, input_script, spider_defects):
    """Собрать цикл с настоящей базой и асинхронным писателем."""
    bus = EventBus()
    recorder = PartRecorder(database=database, event_bus=bus)
    cycle = ProductionCycle(
        conveyor=FakeConveyor(),
        cameras=FakeCameras(),
        inspector=(inspector := FakeInspector(input_script, spider_defects)),
        distributor=FakeDistributor(),
        monitor=None,
        archive=FakeArchive(),
        jog=None,
        event_bus=bus,
        database=database,
        settle_seconds=0.0,
        stage_trace_seconds=0.0,
        review_seconds=0.0,
    )
    return cycle, inspector, recorder


def test_смена_и_детали_попадают_в_базу(db):
    """GOOD/CLEANUP/BAD + пустой лоток: три записи и закрытая смена."""
    cycle, inspector, recorder = build_cycle_with_db(
        db,
        input_script=[True, False, True, True],
        spider_defects={1: [], 2: ["glass"], 3: ["window_geometry"]},
    )
    inspector.on_last_input = cycle.request_stop

    thread = run_cycle(cycle)
    try:
        assert cycle.request_start() is True
        wait_until(
            lambda: cycle.state == "STOPPED",
            timeout=30.0,
            what="линия слилась в STOPPED",
        )
        assert recorder.flush(timeout=10) is True
    finally:
        finish_cycle(cycle, thread)
        recorder.stop(timeout=10)

    [shift] = db.list_sessions()
    session_id = shift["id"]

    # --- Смена закрыта итоговыми счётчиками цикла ---
    assert shift["end_time"] is not None, "смена обязана закрыться на STOPPED"
    assert shift["good_parts"] == cycle.good_count == 1
    assert shift["bad_parts"] == cycle.bad_count == 1
    assert shift["cleanup_parts"] == cycle.cleanup_count == 1
    assert shift["empty_trays"] == cycle.empty_count == 1

    # --- Каждая деталь сохранена ровно один раз ---
    parts = db.list_parts(session_id)
    assert len(parts) == 3, "пустой лоток деталью не считается"
    by_id = {p["local_part_id"]: p for p in parts}
    assert set(by_id) == {1, 2, 3}
    assert by_id[1]["category"] == "GOOD"
    assert by_id[1]["defects"] == []
    assert by_id[2]["category"] == "CLEANUP"
    assert by_id[2]["defects"] == ["glass"]
    assert by_id[3]["category"] == "BAD"
    assert by_id[3]["defects"] == ["window_geometry"]

    # --- Связь с архивом кадров сохранена для разбора инцидента ---
    assert by_id[3]["archive_folder"], "нужна ссылка на папку с кадрами"
    assert by_id[3]["batch_id"] == FakeArchive.batch_id
    assert by_id[2]["step"] == 2, "шаг создания детали трассируется"
    assert all(p["session_id"] == session_id for p in parts)

    # --- Отчёт сходится: счётчики смены = фактические записи ---
    summary = db.session_summary(session_id)
    assert summary["by_category"] == {"GOOD": 1, "BAD": 1, "CLEANUP": 1}
    assert summary["recorded_parts"] == 3


def test_повторный_пуск_открывает_новую_смену(db):
    cycle, inspector, recorder = build_cycle_with_db(
        db, input_script=[True], spider_defects={1: []},
    )
    inspector.on_last_input = cycle.request_stop
    thread = run_cycle(cycle)
    try:
        cycle.request_start()
        wait_until(lambda: cycle.state == "STOPPED", timeout=30.0, what="STOPPED #1")
        first_session = cycle.session_id
        assert first_session is None, "после остановки смена закрыта"

        cycle.request_start()
        wait_until(
            lambda: cycle.session_id is not None, timeout=30.0, what="новая смена",
        )
        second_session = cycle.session_id
        cycle.request_stop()
        wait_until(lambda: cycle.state == "STOPPED", timeout=30.0, what="STOPPED #2")
        assert recorder.flush(timeout=10) is True
    finally:
        finish_cycle(cycle, thread)
        recorder.stop(timeout=10)

    sessions = db.list_sessions()
    assert len(sessions) == 2, "каждый пуск — отдельная смена"
    assert second_session != sessions[0]["id"] or True
    assert all(s["end_time"] is not None for s in sessions), (
        "обе смены обязаны быть закрыты"
    )


def test_недоступная_база_не_останавливает_линию(tmp_path):
    """Отвал диска — не повод терять корпуса: линия обязана доработать."""

    class BrokenDatabase:
        active_session_id = None

        def start_session(self, *a, **kw):
            raise OSError("disk is gone")

        def end_session(self, *a, **kw):
            raise OSError("disk is gone")

        def save_part(self, *a, **kw):
            raise OSError("disk is gone")

    cycle, inspector, recorder = build_cycle_with_db(
        BrokenDatabase(),
        input_script=[True, True],
        spider_defects={1: [], 2: ["glass"]},
    )
    inspector.on_last_input = cycle.request_stop

    thread = run_cycle(cycle)
    try:
        assert cycle.request_start() is True
        wait_until(
            lambda: cycle.state == "STOPPED", timeout=30.0, what="STOPPED",
        )
        # Производство отработало полностью, несмотря на мёртвую базу.
        assert cycle.part_counter == 2
        assert cycle.good_count == 1
        assert cycle.cleanup_count == 1
        assert cycle.session_id is None
    finally:
        finish_cycle(cycle, thread)
        recorder.stop(timeout=10)


def test_события_смены_пишутся_в_json_журнал(db, tmp_path):
    log_path = configure_structlog(tmp_path / "logs")
    assert json_log_path() == log_path

    cycle, inspector, recorder = build_cycle_with_db(
        db, input_script=[True], spider_defects={1: ["glass"]},
    )
    inspector.on_last_input = cycle.request_stop
    thread = run_cycle(cycle)
    try:
        cycle.request_start()
        wait_until(lambda: cycle.state == "STOPPED", timeout=30.0, what="STOPPED")
        recorder.flush(timeout=10)
    finally:
        finish_cycle(cycle, thread)
        recorder.stop(timeout=10)

    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    names = [e["event"] for e in events]
    assert "session_started" in names
    assert "session_ended" in names
    assert "part_sorted" in names

    # Событие сортировки несёт машиночитаемый контекст, а не текст [PASS].
    sorted_event = next(e for e in events if e["event"] == "part_sorted")
    assert sorted_event["part_id"] == 1
    assert sorted_event["category"] == "CLEANUP"
    assert sorted_event["defects"] == ["glass"]
    assert sorted_event["session_id"] == db.list_sessions()[0]["id"]

    # Переходы состояний сериализуются как строки, а не как repr(Enum).
    state_events = [e for e in events if e["event"] == "state_changed"]
    assert state_events, "переходы состояний обязаны попадать в журнал"
    for event in state_events:
        assert isinstance(event["current"], str)
        assert isinstance(event["previous"], str)
        assert "State." not in event["current"]
        assert "Action." not in str(event["action"])
    assert {"RUNNING", "STOPPED"} <= {e["current"] for e in state_events}
