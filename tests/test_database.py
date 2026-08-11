"""Хранение смен и деталей: DatabaseManager поверх реального SQLite.

База — единственный источник истины для отчёта за смену, поэтому тесты
работают с настоящим движком SQLAlchemy (файл в tmp_path), а не с
моками: проверяется схема, транзакции и переживание перезапуска.
"""

import json
from datetime import UTC, datetime

import pytest

from core.database import DatabaseManager
from core.db_models import PartRecord, ProductionSession, decode_defects, encode_defects


@pytest.fixture()
def db(tmp_path):
    manager = DatabaseManager(tmp_path / "conveyer.db")
    yield manager
    manager.dispose()


class TestSessionLifecycle:
    def test_start_session_возвращает_идентификатор_открытой_смены(self, db):
        session_id = db.start_session()
        assert isinstance(session_id, int)
        assert db.active_session_id == session_id

        stored = db.get_session(session_id)
        assert stored["end_time"] is None
        assert stored["good_parts"] == 0
        assert stored["bad_parts"] == 0

    def test_end_session_фиксирует_итоги_смены(self, db):
        session_id = db.start_session(operator_id="operator-7")
        db.end_session(session_id, good=12, bad=3, cleanup=2, empty=1, reason="STOPPED")

        stored = db.get_session(session_id)
        assert stored["good_parts"] == 12
        assert stored["bad_parts"] == 3
        assert stored["cleanup_parts"] == 2
        assert stored["empty_trays"] == 1
        assert stored["total_parts"] == 17
        assert stored["operator_id"] == "operator-7"
        assert stored["stop_reason"] == "STOPPED"
        assert stored["end_time"] is not None
        assert stored["duration_seconds"] >= 0.0
        # Активная смена процесса сброшена: следующий пуск откроет новую.
        assert db.active_session_id is None

    def test_end_session_неизвестной_смены_явная_ошибка(self, db):
        with pytest.raises(ValueError, match="unknown session id"):
            db.end_session(999, good=1, bad=0)

    def test_update_session_counters_не_закрывает_смену(self, db):
        session_id = db.start_session()
        db.update_session_counters(session_id, good=5, bad=1)

        stored = db.get_session(session_id)
        assert stored["good_parts"] == 5
        assert stored["end_time"] is None, "смена обязана остаться открытой"

    def test_close_open_sessions_восстанавливает_после_обесточивания(self, db):
        first = db.start_session()
        second = db.start_session()
        db.end_session(second, good=1, bad=0)

        recovered = db.close_open_sessions(reason="power_loss")
        assert recovered == 1
        assert db.get_session(first)["stop_reason"] == "power_loss"
        assert db.get_session(first)["end_time"] is not None


class TestParts:
    def test_save_part_сохраняет_все_поля_записи(self, db):
        session_id = db.start_session()
        record_id = db.save_part(
            session_id,
            {
                "part_id": 42,
                "category": "BAD",
                "decision": "window_geometry",
                "defects": ["window_geometry", "glass"],
                "archive_folder": "batch_1/BAD/part_0042",
                "batch_id": "batch_1",
                "step": 7,
            },
        )
        assert isinstance(record_id, int)

        [stored] = db.list_parts(session_id)
        assert stored["local_part_id"] == 42
        assert stored["category"] == "BAD"
        assert stored["decision"] == "window_geometry"
        assert stored["defects"] == ["window_geometry", "glass"]
        assert stored["archive_folder"] == "batch_1/BAD/part_0042"
        assert stored["batch_id"] == "batch_1"
        assert stored["step"] == 7

    def test_defects_хранятся_валидным_json(self, db):
        session_id = db.start_session()
        db.save_part(session_id, {"part_id": 1, "category": "BAD", "defects": ["glass"]})
        [stored] = db.list_parts(session_id)
        # Поле читается и как список, и как сырой JSON (для выгрузки).
        assert json.loads(json.dumps(stored["defects"])) == ["glass"]

    def test_деталь_без_смены_всё_равно_сохраняется(self, db):
        """Сбой открытия смены не должен терять деталь."""
        record_id = db.save_part(None, {"part_id": 3, "category": "GOOD"})
        assert record_id > 0
        [stored] = db.list_parts()
        assert stored["session_id"] is None
        assert stored["defects"] == []

    def test_timestamp_принимает_и_float_и_datetime(self, db):
        session_id = db.start_session()
        db.save_part(session_id, {"part_id": 1, "category": "GOOD", "timestamp": 1_700_000_000.0})
        db.save_part(
            session_id,
            {
                "part_id": 2,
                "category": "GOOD",
                "timestamp": datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
            },
        )
        stamps = {p["local_part_id"]: p["timestamp"] for p in db.list_parts(session_id)}
        assert stamps[1].startswith("2023-11-14")
        assert stamps[2].startswith("2026-08-11")

    def test_session_summary_пересчитывает_категории_по_записям(self, db):
        session_id = db.start_session()
        for part_id, category in ((1, "GOOD"), (2, "GOOD"), (3, "BAD"), (4, "CLEANUP")):
            db.save_part(session_id, {"part_id": part_id, "category": category})
        db.end_session(session_id, good=2, bad=1, cleanup=1)

        summary = db.session_summary(session_id)
        assert summary["recorded_parts"] == 4
        assert summary["by_category"] == {"GOOD": 2, "BAD": 1, "CLEANUP": 1}
        # Счётчики смены и фактические записи сходятся: потерь нет.
        assert summary["good_parts"] == summary["by_category"]["GOOD"]

    def test_count_parts_считает_только_свою_смену(self, db):
        first = db.start_session()
        db.save_part(first, {"part_id": 1, "category": "GOOD"})
        db.end_session(first, good=1, bad=0)
        second = db.start_session()
        db.save_part(second, {"part_id": 1, "category": "BAD"})

        assert db.count_parts(first) == 1
        assert db.count_parts(second) == 1


class TestPersistence:
    def test_данные_переживают_перезапуск_приложения(self, tmp_path):
        path = tmp_path / "shift.db"
        first = DatabaseManager(path)
        session_id = first.start_session()
        first.save_part(session_id, {"part_id": 1, "category": "GOOD", "defects": []})
        first.end_session(session_id, good=1, bad=0, reason="STOPPED")
        first.dispose()

        # Новый процесс: та же схема, те же данные.
        second = DatabaseManager(path)
        try:
            stored = second.get_session(session_id)
            assert stored["good_parts"] == 1
            assert stored["stop_reason"] == "STOPPED"
            assert second.count_parts(session_id) == 1
        finally:
            second.dispose()

    def test_in_memory_база_не_создаёт_файл(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        manager = DatabaseManager(":memory:")
        try:
            session_id = manager.start_session()
            manager.save_part(session_id, {"part_id": 1, "category": "GOOD"})
            assert manager.count_parts(session_id) == 1
        finally:
            manager.dispose()
        assert list(tmp_path.iterdir()) == []


class TestAudit:
    def test_audit_пишет_изменения_конфигурации(self, db):
        entry_id = db.audit(
            action="thresholds.updated",
            payload_json=json.dumps({"role": "TOP", "keys": ["TOP.glass_area"]}),
            actor="operator-7",
        )
        assert entry_id > 0
        [entry] = db.list_audit()
        assert entry["action"] == "thresholds.updated"
        assert entry["actor"] == "operator-7"
        assert json.loads(entry["payload_json"])["role"] == "TOP"


class TestModels:
    def test_encode_decode_дефектов_обратимы(self):
        assert decode_defects(encode_defects(["glass", "contacts"])) == ["glass", "contacts"]
        assert decode_defects(encode_defects([])) == []
        assert decode_defects(encode_defects(None)) == []

    def test_decode_не_падает_на_повреждённом_json(self):
        """Битая строка не должна ронять отчёт за смену."""
        assert decode_defects("не json") == ["не json"]
        assert decode_defects(None) == []

    def test_свойства_модели_смены(self):
        shift = ProductionSession(
            start_time=datetime(2026, 8, 11, 8, 0, tzinfo=UTC),
            good_parts=10,
            bad_parts=2,
            cleanup_parts=1,
        )
        assert shift.is_open is True
        assert shift.total_parts == 13
        assert shift.duration_seconds is None

        shift.end_time = datetime(2026, 8, 11, 16, 0, tzinfo=UTC)
        assert shift.is_open is False
        assert shift.duration_seconds == 8 * 3600

    def test_сеттер_дефектов_сериализует_в_json(self):
        record = PartRecord(local_part_id=1, category="BAD")
        record.defects = ["glass"]
        assert record.defects_json == '["glass"]'
        assert record.defects == ["glass"]
