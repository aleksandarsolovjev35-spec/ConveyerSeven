"""Durable before/after journal used to detect interrupted transactions."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any


class RecoveryJournalError(RuntimeError):
    pass


class RecoveryJournal:
    """Append-only fsynced JSONL with a monotonic sequence number."""

    TERMINAL_EVENTS = frozenset({
        "transaction_completed", "transaction_faulted", "process_closed",
    })

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        records = self.scan()
        self._sequence = max((row.get("sequence", 0) for row in records), default=0)
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.flush()
                os.fsync(stream.fileno())
        except Exception as exc:
            raise RecoveryJournalError(f"journal is not writable: {exc}") from exc

    def append(
        self,
        event: str,
        *,
        transaction_id: str | None = None,
        run_id: int | None = None,
        **fields: Any,
    ) -> int:
        if not isinstance(event, str) or not event:
            raise ValueError("event is required")
        if transaction_id is not None and (
            not isinstance(transaction_id, str) or not transaction_id
        ):
            raise ValueError("transaction_id must be a non-empty string")
        if run_id is not None and (type(run_id) is not int or run_id < 1):
            raise ValueError("run_id must be a positive integer")
        with self._lock:
            sequence = self._sequence + 1
            record = {
                "sequence": sequence,
                "timestamp": time.time(),
                "event": event,
                "transaction_id": transaction_id,
                "run_id": run_id,
                **{str(key): self._safe(value) for key, value in fields.items()},
            }
            try:
                payload = json.dumps(
                    record, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                ) + "\n"
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception as exc:
                raise RecoveryJournalError(
                    f"cannot durably append {event}: {exc}"
                ) from exc
            self._sequence = sequence
            return sequence

    def scan(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        try:
            with self.path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if not isinstance(row, dict) or type(row.get("sequence")) is not int:
                        raise RecoveryJournalError(
                            f"invalid journal record at line {line_number}"
                        )
                    records.append(row)
        except RecoveryJournalError:
            raise
        except Exception as exc:
            raise RecoveryJournalError(f"cannot read journal: {exc}") from exc
        sequences = [row["sequence"] for row in records]
        if sequences != sorted(set(sequences)):
            raise RecoveryJournalError("journal sequence is not strictly monotonic")
        return records

    @staticmethod
    def _safe(value):
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("journal cannot contain non-finite values")
            return value
        if isinstance(value, dict):
            return {str(key): RecoveryJournal._safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [RecoveryJournal._safe(item) for item in value]
        tolist = getattr(value, "tolist", None)
        if callable(tolist):
            return RecoveryJournal._safe(tolist())
        as_dict = getattr(value, "as_dict", None)
        if callable(as_dict):
            return RecoveryJournal._safe(as_dict())
        return str(value)

    def unfinished_transactions(self) -> dict[str, dict[str, Any]]:
        active: dict[str, dict[str, Any]] = {}
        for row in self.scan():
            transaction_id = row.get("transaction_id")
            if not transaction_id:
                continue
            if row.get("event") in self.TERMINAL_EVENTS:
                active.pop(transaction_id, None)
            else:
                active[transaction_id] = row
        return active
