"""Durable operational boundary journal.

It is intentionally append-only and synchronous for physical intent and
confirmation events.  A failed append is a production fault, never a reason to
continue optimistically.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path


class OperationalLogError(RuntimeError):
    pass


class OperationalLog:
    def __init__(self, path: str = "production.log.jsonl", *, enabled: bool = True):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Validate access at construction/BOOT.
            with self.path.open("a", encoding="utf-8") as stream:
                stream.flush()
                os.fsync(stream.fileno())

    def append(self, event: str, **fields):
        if not self.enabled:
            return
        record = {
            "ts": time.time(),
            "event": str(event),
            **{str(key): self._safe(value) for key, value in fields.items()},
        }
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception as exc:
                raise OperationalLogError(f"cannot durably append {event}: {exc}") from exc

    @staticmethod
    def _safe(value):
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("operational log cannot contain non-finite number")
            return value
        if isinstance(value, dict):
            return {str(k): OperationalLog._safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [OperationalLog._safe(v) for v in value]
        tolist = getattr(value, "tolist", None)
        if callable(tolist):
            return OperationalLog._safe(tolist())
        return str(value)
