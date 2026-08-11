"""Асинхронная запись деталей: цикл публикует событие, диск ждёт поток.

Ключевое требование к Фазе 5 — шаг линии не должен ждать fsync. Тесты
проверяют именно это свойство: медленная или сломанная база не тормозит и
не роняет публикацию события, но данные всё равно доезжают.
"""

import threading
import time

import pytest

from core.database import DatabaseManager
from core.db_recorder import PartRecorder
from core.event_bus import EventBus


class SlowDatabase:
    """База, которая пишет медленно: имитация занятого диска."""

    def __init__(self, delay=0.2):
        self.delay = delay
        self.saved = []
        self.active_session_id = None

    def save_part(self, session_id, part_data):
        time.sleep(self.delay)
        self.saved.append((session_id, dict(part_data)))
        return len(self.saved)


class BrokenDatabase:
    """База, которая всегда падает: имитация отвалившегося диска."""

    active_session_id = None

    def __init__(self):
        self.calls = 0

    def save_part(self, session_id, part_data):
        self.calls += 1
        raise OSError("disk is gone")


@pytest.fixture()
def db(tmp_path):
    manager = DatabaseManager(tmp_path / "recorder.db")
    yield manager
    manager.dispose()


def test_событие_шины_попадает_в_базу(db):
    bus = EventBus()
    recorder = PartRecorder(database=db, event_bus=bus)
    session_id = db.start_session()
    try:
        bus.emit(
            "part:archived",
            {
                "session_id": session_id,
                "part_id": 5,
                "category": "GOOD",
                "defects": [],
                "archive_folder": "batch_1/GOOD/part_0005",
            },
        )
        assert recorder.flush(timeout=5) is True
    finally:
        recorder.stop(timeout=5)

    [stored] = db.list_parts(session_id)
    assert stored["local_part_id"] == 5
    assert stored["category"] == "GOOD"
    assert recorder.stats["written"] == 1


def test_публикация_не_ждёт_медленную_запись():
    """Шаг линии обязан вернуться сразу, даже если диск занят."""
    slow = SlowDatabase(delay=0.3)
    bus = EventBus()
    recorder = PartRecorder(database=slow, event_bus=bus)
    try:
        started = time.monotonic()
        for part_id in range(3):
            bus.emit("part:archived", {"part_id": part_id, "category": "GOOD"})
        elapsed = time.monotonic() - started
        # Три записи по 0.3 с = 0.9 с в синхронном варианте.
        assert elapsed < 0.15, f"публикация заблокировала цикл на {elapsed:.2f} с"

        assert recorder.flush(timeout=5) is True
        assert len(slow.saved) == 3
    finally:
        recorder.stop(timeout=5)


def test_сбой_базы_не_роняет_цикл_и_учитывается():
    broken = BrokenDatabase()
    bus = EventBus()
    recorder = PartRecorder(database=broken, event_bus=bus)
    try:
        bus.emit("part:archived", {"part_id": 1, "category": "BAD"})
        recorder.flush(timeout=5)
        assert recorder.stats["failed"] == 1
        # Поток записи выжил: следующая деталь тоже обрабатывается.
        bus.emit("part:archived", {"part_id": 2, "category": "BAD"})
        recorder.flush(timeout=5)
        assert broken.calls == 2
    finally:
        recorder.stop(timeout=5)


def test_переполнение_очереди_теряет_записи_а_не_память():
    slow = SlowDatabase(delay=5)
    recorder = PartRecorder(database=slow, queue_size=16)
    try:
        for part_id in range(200):
            recorder.on_part_archived({"part_id": part_id, "category": "GOOD"})
        stats = recorder.stats
        assert stats["dropped"] > 0
        assert stats["pending"] <= 16, "очередь обязана быть ограничена"
    finally:
        recorder._queue.queue.clear()  # noqa: SLF001 - не ждать 5 с на каждую
        recorder.stop(timeout=1)


def test_session_id_берётся_из_базы_если_событие_его_не_несёт(db):
    recorder = PartRecorder(database=db)
    session_id = db.start_session()
    try:
        recorder.on_part_archived({"part_id": 9, "category": "CLEANUP"})
        assert recorder.flush(timeout=5) is True
    finally:
        recorder.stop(timeout=5)

    [stored] = db.list_parts(session_id)
    assert stored["session_id"] == session_id


def test_stop_идемпотентен_и_дописывает_хвост(db):
    bus = EventBus()
    recorder = PartRecorder(database=db, event_bus=bus)
    session_id = db.start_session()
    bus.emit("part:archived", {"session_id": session_id, "part_id": 1, "category": "GOOD"})

    recorder.stop(timeout=5)
    recorder.stop(timeout=5)  # повторный вызов из shutdown() не должен падать

    assert db.count_parts(session_id) == 1
    # После stop подписка снята: новые события в базу не попадают.
    bus.emit("part:archived", {"session_id": session_id, "part_id": 2, "category": "GOOD"})
    assert db.count_parts(session_id) == 1


def test_поток_записи_демон_и_не_держит_выход(db):
    recorder = PartRecorder(database=db)
    try:
        worker = next(t for t in threading.enumerate() if t.name == "db-recorder")
        assert worker.daemon is True
    finally:
        recorder.stop(timeout=5)
