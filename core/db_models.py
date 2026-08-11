"""Declarative SQLAlchemy models of the ConveyerSeven production database.

Смена линии и судьба каждого корпуса должны переживать перезапуск
приложения: архив кадров отвечает на вопрос «как выглядела деталь», а эта
база — на вопросы «сколько сделали за смену» и «куда ушёл корпус №N».

Модели намеренно живут отдельно от :mod:`core.database`: схема — это
контракт данных, а менеджер — способ с ней работать. Тесты и утилиты
миграции могут импортировать только схему, не поднимая движок.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Категории маршрутизации совпадают с domain.part: GOOD / BAD / CLEANUP.
# Здесь хранится строка, а не Enum: значения уже уходят в JSON статуса и в
# манифесты архива, и БД не должна ломать смену при появлении новой ветки
# сортировки на линии.
CATEGORY_LENGTH = 16
DECISION_LENGTH = 64


def utc_now() -> datetime:
    """Текущее время в UTC с зоной: смены сравнимы между площадками."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class for every ConveyerSeven persistence model."""


class ProductionSession(Base):
    """Смена работы конвейера: от START до STOPPED/FAULT.

    Счётчики дублируют то, что видно в HMI, но остаются после выключения
    станка — по ним строится отчёт за сутки без разбора логов.
    """

    __tablename__ = "production_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True,
    )
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    good_parts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bad_parts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cleanup_parts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    empty_trays: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    operator_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Причина закрытия смены: STOPPED, FAULT, shutdown и т.п.
    stop_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    parts: Mapped[list[PartRecord]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="PartRecord.id",
    )

    @property
    def is_open(self) -> bool:
        """Смена ещё не закрыта (линия работает или упала без записи)."""
        return self.end_time is None

    @property
    def total_parts(self) -> int:
        """Сколько корпусов прошло сортировку за смену."""
        return self.good_parts + self.bad_parts + self.cleanup_parts

    @property
    def duration_seconds(self) -> float | None:
        """Длительность смены в секундах или ``None`` для открытой смены."""
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        """JSON-совместимый снимок смены для отчётов и HMI."""
        return {
            "id": self.id,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "good_parts": self.good_parts,
            "bad_parts": self.bad_parts,
            "cleanup_parts": self.cleanup_parts,
            "empty_trays": self.empty_trays,
            "total_parts": self.total_parts,
            "operator_id": self.operator_id,
            "stop_reason": self.stop_reason,
            "duration_seconds": self.duration_seconds,
        }

    def __repr__(self) -> str:
        """Компактное представление для отладки и логов."""
        return (
            f"<ProductionSession #{self.id} "
            f"good={self.good_parts} bad={self.bad_parts} "
            f"cleanup={self.cleanup_parts} "
            f"{'OPEN' if self.is_open else 'CLOSED'}>"
        )


class PartRecord(Base):
    """Запись о каждой детали, прошедшей распределитель.

    ``local_part_id`` — номер корпуса внутри смены (тот же, что видит
    оператор в HMI и что попадает в имя папки архива). Глобальную
    уникальность обеспечивает пара ``(session_id, local_part_id)``.
    """

    __tablename__ = "part_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int | None] = mapped_column(
        # Смена может не открыться (сбой БД на старте) — деталь всё равно
        # должна сохраниться, поэтому связь необязательная.
        ForeignKey("production_sessions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    local_part_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(CATEGORY_LENGTH), nullable=False)
    # Определяющий дефект (первый не-CLEANUP) — то же поле, что и в архиве.
    decision: Mapped[str | None] = mapped_column(String(DECISION_LENGTH), nullable=True)
    defects_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True,
    )
    archive_folder: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)

    session: Mapped[ProductionSession | None] = relationship(back_populates="parts")

    __table_args__ = (
        Index("ix_part_records_session_local", "session_id", "local_part_id"),
    )

    @property
    def defects(self) -> list[str]:
        """Список дефектов, распакованный из ``defects_json``."""
        return decode_defects(self.defects_json)

    @defects.setter
    def defects(self, values: Sequence[str] | None) -> None:
        """Записать список дефектов, сериализовав его в JSON."""
        self.defects_json = encode_defects(values)

    def as_dict(self) -> dict[str, Any]:
        """JSON-совместимый снимок детали для отчётов и HMI."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "local_part_id": self.local_part_id,
            "category": self.category,
            "decision": self.decision,
            "defects": self.defects,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "archive_folder": self.archive_folder,
            "batch_id": self.batch_id,
            "step": self.step,
        }

    def __repr__(self) -> str:
        """Компактное представление для отладки и логов."""
        return (
            f"<PartRecord #{self.id} session={self.session_id} "
            f"part={self.local_part_id} category={self.category}>"
        )


class AuditLog(Base):
    """Неизменяемая запись об изменении настроек оператором.

    Пороги правил и параметры архива меняются прямо в HMI: без журнала
    невозможно ответить, почему вчера линия браковала иначе.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True,
    )
    actor: Mapped[str] = mapped_column(String(128), default="system", nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    def as_dict(self) -> dict[str, Any]:
        """JSON-совместимый снимок записи аудита."""
        return {
            "id": self.id,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "actor": self.actor,
            "action": self.action,
            "payload_json": self.payload_json,
        }


def encode_defects(values: Sequence[str] | None) -> str:
    """Сериализовать дефекты в компактный JSON-массив строк."""
    if not values:
        return "[]"
    return json.dumps([str(value) for value in values], ensure_ascii=False)


def decode_defects(raw: str | None) -> list[str]:
    """Разобрать ``defects_json``; повреждённое значение не роняет отчёт."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]
