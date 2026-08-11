"""SQLite persistence manager for shifts, parts, and configuration audit.

Единственная точка записи в базу линии. Модели живут в
:mod:`core.db_models`, здесь — только транзакции и удобные для цикла
методы. Правила модуля:

* каждая операция — отдельная короткая транзакция (цикл не держит
  открытый курсор между шагами линии);
* соединение работает в режиме WAL: запись детали не блокирует чтение
  отчётов из HMI и переживает жёсткое выключение станка;
* объекты ORM наружу не отдаются — только числа и словари, иначе UI
  получил бы отсоединённые от сессии инстансы.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.app_logging import get_logger
from core.db_models import (
    AuditLog,
    Base,
    PartRecord,
    ProductionSession,
    encode_defects,
    utc_now,
)

log = get_logger("database")

# Память используется тестами и симуляцией: файл на диске не создаётся.
IN_MEMORY = ":memory:"


class DatabaseManager:
    """Unit-of-work фасад над встроенной SQLite-базой линии."""

    def __init__(self, db_path: str | Path = Path("conveyer.db")) -> None:
        """Открыть базу по пути ``db_path`` и создать схему при первом старте."""
        self.db_path = str(db_path)
        self._engine = self._create_engine(self.db_path)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        Base.metadata.create_all(self._engine)
        # Смена, открытая этим процессом: цикл может не передавать id явно.
        self._active_session_id: int | None = None
        self._lock = threading.RLock()
        log.info("SQLite база готова: %s", self.db_path)

    # Engine

    @staticmethod
    def _create_engine(db_path: str) -> Engine:
        """Создать движок SQLite, пригодный для многопоточной линии."""
        url = (
            "sqlite://"
            if db_path in (IN_MEMORY, "")
            else f"sqlite:///{db_path}"
        )
        if db_path not in (IN_MEMORY, ""):
            Path(db_path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True,
            )
        kwargs: dict[str, Any] = {
            "future": True,
            # Цикл линии, EventBus и HTTP-хендлеры HMI живут в разных
            # потоках; SQLAlchemy сам сериализует доступ через пул.
            "connect_args": {"check_same_thread": False, "timeout": 15.0},
        }
        if db_path in (IN_MEMORY, ""):
            # У ":memory:" база живёт внутри соединения. Пул по умолчанию
            # (SingletonThreadPool) выдал бы потоку записи собственное
            # пустое соединение — и деталь ушла бы в никуда с ошибкой
            # «no such table». StaticPool держит ровно одно соединение на
            # весь процесс, поэтому поток PartRecorder видит те же данные.
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _configure_connection(dbapi_connection, _record):
            """WAL + NORMAL: запись не рвётся при внезапном обесточивании."""
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        return engine

    @contextmanager
    def _session(self) -> Iterator[OrmSession]:
        """Короткая транзакция: commit при успехе, rollback при ошибке."""
        with self._sessions.begin() as session:
            yield session

    # Shift lifecycle

    @property
    def active_session_id(self) -> int | None:
        """Идентификатор открытой этим процессом смены (или ``None``)."""
        with self._lock:
            return self._active_session_id

    def start_session(self, operator_id: str | None = None) -> int:
        """Открыть смену и вернуть её идентификатор."""
        with self._session() as db:
            shift = ProductionSession(operator_id=operator_id)
            db.add(shift)
            db.flush()
            session_id = int(shift.id)
        with self._lock:
            self._active_session_id = session_id
        log.info("Смена #%s открыта (оператор: %s)", session_id, operator_id or "-")
        return session_id

    def end_session(
        self,
        session_id: int,
        good: int = 0,
        bad: int = 0,
        cleanup: int = 0,
        empty: int = 0,
        reason: str | None = None,
    ) -> None:
        """Закрыть смену и зафиксировать её итоговые счётчики."""
        with self._session() as db:
            shift = db.get(ProductionSession, session_id)
            if shift is None:
                raise ValueError(f"unknown session id: {session_id}")
            shift.end_time = utc_now()
            shift.good_parts = int(good)
            shift.bad_parts = int(bad)
            shift.cleanup_parts = int(cleanup)
            shift.empty_trays = int(empty)
            if reason:
                shift.stop_reason = str(reason)[:128]
        with self._lock:
            if self._active_session_id == session_id:
                self._active_session_id = None
        log.info(
            "Смена #%s закрыта: good=%s bad=%s cleanup=%s (%s)",
            session_id, good, bad, cleanup, reason or "-",
        )

    def update_session_counters(
        self,
        session_id: int,
        good: int = 0,
        bad: int = 0,
        cleanup: int = 0,
        empty: int = 0,
    ) -> None:
        """Обновить счётчики открытой смены, не закрывая её.

        Смена может оборваться вместе с питанием: периодическая
        синхронизация счётчиков оставляет в базе почти актуальный итог.
        """
        with self._session() as db:
            shift = db.get(ProductionSession, session_id)
            if shift is None:
                raise ValueError(f"unknown session id: {session_id}")
            shift.good_parts = int(good)
            shift.bad_parts = int(bad)
            shift.cleanup_parts = int(cleanup)
            shift.empty_trays = int(empty)

    # Parts

    def save_part(self, session_id: int | None, part_data: Mapping[str, Any]) -> int:
        """Сохранить деталь смены и вернуть идентификатор записи.

        ``part_data`` — словарь события ``part:archived``: ``part_id``,
        ``category``, ``decision``, ``defects``, ``archive_folder``,
        ``batch_id``, ``step``, ``timestamp``.
        """
        record = PartRecord(
            session_id=session_id,
            local_part_id=int(part_data.get("part_id") or part_data.get("local_part_id") or 0),
            category=str(part_data.get("category") or "UNKNOWN"),
            decision=_optional_str(part_data.get("decision"), 64),
            defects_json=encode_defects(part_data.get("defects")),
            archive_folder=_optional_str(part_data.get("archive_folder")),
            batch_id=_optional_str(part_data.get("batch_id"), 64),
            step=_optional_int(part_data.get("step")),
        )
        # Цикл публикует время как time.time(); отчёты и ORM работают с
        # datetime — приводим обе формы к UTC-моменту.
        timestamp = part_data.get("timestamp")
        if isinstance(timestamp, datetime):
            record.timestamp = timestamp
        elif isinstance(timestamp, (int, float)):
            record.timestamp = datetime.fromtimestamp(float(timestamp), tz=UTC)
        with self._session() as db:
            db.add(record)
            db.flush()
            return int(record.id)

    def count_parts(self, session_id: int) -> int:
        """Сколько деталей записано в указанную смену."""
        with self._session() as db:
            rows = db.execute(
                select(PartRecord.id).where(PartRecord.session_id == session_id)
            ).all()
            return len(rows)

    # Reporting

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        """Вернуть смену в виде словаря (или ``None``, если её нет)."""
        with self._session() as db:
            shift = db.get(ProductionSession, session_id)
            return shift.as_dict() if shift is not None else None

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Последние смены, самая свежая первой."""
        with self._session() as db:
            shifts = db.scalars(
                select(ProductionSession)
                .order_by(ProductionSession.id.desc())
                .limit(max(1, int(limit)))
            ).all()
            return [shift.as_dict() for shift in shifts]

    def list_parts(
        self,
        session_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Последние детали (опционально — только выбранной смены)."""
        statement = select(PartRecord).order_by(PartRecord.id.desc())
        if session_id is not None:
            statement = statement.where(PartRecord.session_id == session_id)
        with self._session() as db:
            records = db.scalars(statement.limit(max(1, int(limit)))).all()
            return [record.as_dict() for record in records]

    def session_summary(self, session_id: int) -> dict[str, Any] | None:
        """Смена вместе с пересчитанной по записям деталей статистикой."""
        with self._session() as db:
            shift = db.get(ProductionSession, session_id)
            if shift is None:
                return None
            categories: dict[str, int] = {}
            for category in db.scalars(
                select(PartRecord.category).where(
                    PartRecord.session_id == session_id
                )
            ).all():
                categories[category] = categories.get(category, 0) + 1
            summary = shift.as_dict()
            summary["recorded_parts"] = sum(categories.values())
            summary["by_category"] = categories
            return summary

    def close_open_sessions(self, reason: str = "recovered") -> int:
        """Закрыть смены, оставшиеся открытыми после аварийного выключения.

        Возвращает количество восстановленных смен: без этого отчёт за
        сутки показывал бы вечно идущую смену.
        """
        with self._session() as db:
            open_shifts = db.scalars(
                select(ProductionSession).where(ProductionSession.end_time.is_(None))
            ).all()
            for shift in open_shifts:
                shift.end_time = utc_now()
                shift.stop_reason = reason[:128]
            recovered = len(open_shifts)
        if recovered:
            log.warning("Восстановлено незакрытых смен: %s (%s)", recovered, reason)
        return recovered

    # Audit

    def audit(
        self,
        action: str,
        payload_json: str,
        actor: str = "system",
    ) -> int:
        """Добавить запись аудита конфигурации и вернуть её идентификатор."""
        with self._session() as db:
            entry = AuditLog(actor=actor, action=action, payload_json=payload_json)
            db.add(entry)
            db.flush()
            return int(entry.id)

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        """Последние записи аудита, самая свежая первой."""
        with self._session() as db:
            entries = db.scalars(
                select(AuditLog).order_by(AuditLog.id.desc()).limit(max(1, int(limit)))
            ).all()
            return [entry.as_dict() for entry in entries]

    # Lifecycle

    def dispose(self) -> None:
        """Закрыть пул соединений при завершении процесса."""
        self._engine.dispose()


def _optional_str(value: Any, limit: int | None = None) -> str | None:
    """Привести значение к строке или ``None``, обрезав до ``limit``."""
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return text[:limit] if limit else text


def _optional_int(value: Any) -> int | None:
    """Привести значение к ``int`` или вернуть ``None`` при мусоре."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def defect_list(values: Sequence[str] | None) -> list[str]:
    """Нормализовать дефекты к списку строк (удобно для вызывающего кода)."""
    return [str(value) for value in (values or [])]


# Историческое имя класса: main.py и утилиты продолжают работать без правок.
Database = DatabaseManager

__all__ = [
    "AuditLog",
    "Database",
    "DatabaseManager",
    "PartRecord",
    "ProductionSession",
    "defect_list",
]
