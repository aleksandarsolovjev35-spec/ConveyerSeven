"""SQLite persistence for shifts, inspected parts, and configuration audit events."""

from __future__ import annotations

from contextlib import AbstractContextManager as ContextManager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.orm import Session as OrmSession


class Base(DeclarativeBase):
    """Base class for all ConveyerSeven persistence models."""


class Session(Base):
    """A production shift/session."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parts: Mapped[list[PartRecord]] = relationship(back_populates="session")


class PartRecord(Base):
    """Persistent outcome for one inspected physical part."""

    __tablename__ = "part_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    external_part_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(32))
    photo_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    inspected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    session: Mapped[Session] = relationship(back_populates="parts")


class AuditLog(Base):
    """Immutable audit record for operator-visible configuration changes."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    actor: Mapped[str] = mapped_column(String(128), default="system")
    action: Mapped[str] = mapped_column(String(128))
    payload_json: Mapped[str] = mapped_column(Text)


class Database:
    """Small unit-of-work facade over the embedded SQLite production database."""

    def __init__(self, database_file: Path = Path("conveyer.db")) -> None:
        """Open SQLite with transaction-safe sessions and create its schema."""
        self._engine = create_engine(f"sqlite:///{database_file}", future=True)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        Base.metadata.create_all(self._engine)

    def open_shift(self, operator_id: str | None = None) -> int:
        """Create a shift and return its database identifier."""
        with self._session() as db:
            shift = Session(operator_id=operator_id)
            db.add(shift)
            db.flush()
            return shift.id

    def close_shift(self, session_id: int) -> None:
        """Mark an existing shift as closed."""
        with self._session() as db:
            shift = db.get(Session, session_id)
            if shift is None:
                raise ValueError(f"unknown session id: {session_id}")
            shift.ended_at = datetime.now(UTC)

    def record_part(self, session_id: int, external_part_id: int, status: str, photo_path: str | None) -> int:
        """Persist one part decision and return the database record identifier."""
        with self._session() as db:
            record = PartRecord(
                session_id=session_id,
                external_part_id=external_part_id,
                status=status,
                photo_path=photo_path,
            )
            db.add(record)
            db.flush()
            return record.id

    def audit(self, action: str, payload_json: str, actor: str = "system") -> int:
        """Append a configuration audit entry."""
        with self._session() as db:
            entry = AuditLog(actor=actor, action=action, payload_json=payload_json)
            db.add(entry)
            db.flush()
            return entry.id

    def _session(self) -> ContextManager[OrmSession]:
        """Yield a committing transactional ORM session."""
        return self._sessions.begin()
