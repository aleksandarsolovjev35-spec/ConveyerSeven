"""Background writer that persists line events published on the EventBus.

Запись в SQLite — это ввод-вывод на диск. Делать её прямо в шаге линии
нельзя: цикл обязан отдать команду распределителю вовремя, а не ждать
fsync. Поэтому :class:`PartRecorder` подписывается на события
``part:archived``/``session:*`` и пишет их из собственного потока через
очередь.

Гарантии:

* публикация события никогда не блокирует цикл дольше постановки в
  очередь и никогда не поднимает исключение в шаг линии;
* очередь ограничена: при отвале диска теряются старые записи, а не
  оперативная память станка;
* :meth:`flush` даёт остановке дождаться, пока «хвост» смены доедет до
  базы.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Mapping
from typing import Any

from core.app_logging import get_logger
from core.structured_logging import get_struct_logger

log = get_logger("db-recorder")
slog = get_struct_logger("db-recorder")

# Одна деталь проходит линию за ~10 с: тысячи записей в очереди означают,
# что диск умер несколько часов назад.
DEFAULT_QUEUE_SIZE = 2048
JOIN_TIMEOUT = 10.0

_STOP = object()


class PartRecorder:
    """Асинхронный мост EventBus -> :class:`~core.database.DatabaseManager`."""

    def __init__(
        self,
        database,
        event_bus=None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        """Подписаться на события линии и поднять поток записи."""
        self.database = database
        self._queue: queue.Queue = queue.Queue(maxsize=max(16, int(queue_size)))
        self._unsubscribe: list = []
        self._dropped = 0
        self._written = 0
        self._failed = 0
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="db-recorder", daemon=True,
        )
        self._worker.start()
        if event_bus is not None:
            self.attach(event_bus)

    # Wiring

    def attach(self, event_bus) -> None:
        """Подписаться на события линии, публикуемые ProductionCycle."""
        self._unsubscribe.append(
            event_bus.subscribe("part:archived", self.on_part_archived)
        )

    def detach(self) -> None:
        """Отписаться от шины (например, при пересборке цикла)."""
        for unsubscribe in self._unsubscribe:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001 - отписка не должна ронять выход
                pass
        self._unsubscribe.clear()

    # Event handlers

    def on_part_archived(self, part_data: Mapping[str, Any]) -> None:
        """Поставить деталь в очередь на запись; цикл не ждёт диск."""
        self._enqueue(dict(part_data or {}))

    # Stats

    @property
    def stats(self) -> dict[str, int]:
        """Счётчики записи: для диагностики и HMI."""
        with self._lock:
            return {
                "written": self._written,
                "failed": self._failed,
                "dropped": self._dropped,
                "pending": self._queue.qsize(),
            }

    # Lifecycle

    def flush(self, timeout: float = JOIN_TIMEOUT) -> bool:
        """Дождаться опустошения очереди. ``False`` — не успели за timeout."""
        deadline = threading.Event()
        finished = threading.Timer(max(0.0, float(timeout)), deadline.set)
        finished.daemon = True
        finished.start()
        try:
            while not deadline.is_set():
                if self._queue.unfinished_tasks == 0:
                    return True
                deadline.wait(0.02)
            return self._queue.unfinished_tasks == 0
        finally:
            finished.cancel()

    def stop(self, timeout: float = JOIN_TIMEOUT) -> None:
        """Дописать очередь и остановить поток записи."""
        if self._stopped.is_set():
            return
        self._stopped.set()
        self.detach()
        self.flush(timeout=timeout)
        self._queue.put(_STOP)
        self._worker.join(timeout=timeout)

    # Internals

    def _enqueue(self, payload: dict) -> None:
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            with self._lock:
                self._dropped += 1
                dropped = self._dropped
            log.error(
                "[DB] Очередь записи переполнена; запись потеряна (всего: %s)",
                dropped,
            )

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return
            try:
                self._write(item)
            except Exception as exc:  # noqa: BLE001 - поток записи не падает
                with self._lock:
                    self._failed += 1
                log.error("[DB] Не удалось сохранить деталь: %s", exc)
                slog.error(
                    "part_persist_failed",
                    part_id=item.get("part_id"),
                    error=str(exc),
                )
            finally:
                self._queue.task_done()

    def _write(self, payload: dict) -> None:
        session_id = payload.pop("session_id", None)
        if session_id is None:
            session_id = getattr(self.database, "active_session_id", None)
        record_id = self.database.save_part(session_id, payload)
        with self._lock:
            self._written += 1
        slog.debug(
            "part_persisted",
            record_id=record_id,
            session_id=session_id,
            part_id=payload.get("part_id"),
            category=payload.get("category"),
        )
