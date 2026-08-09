"""Latched root-cause fault reporting."""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field


@dataclass
class FaultRecord:
    code: str
    message: str
    phase: str | None = None
    role: str | None = None
    axis: str | None = None
    timestamp: float = field(default_factory=time.time)
    root: bool = True
    details: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "phase": self.phase,
            "role": self.role,
            "axis": self.axis,
            "timestamp": self.timestamp,
            "root": self.root,
            "details": dict(self.details),
        }


class FaultLatch:
    def __init__(self):
        self.root: FaultRecord | None = None
        self.secondary: list[FaultRecord] = []

    def latch(self, code: str, message: str, **context) -> FaultRecord:
        record = FaultRecord(code=str(code), message=str(message), **context)
        if self.root is None:
            self.root = record
        else:
            record.root = False
            self.secondary.append(record)
        return self.root

    def add_secondary(self, code: str, message: str, **context):
        record = FaultRecord(code=str(code), message=str(message), root=False, **context)
        self.secondary.append(record)
        return record

    def report(self, *, include_traceback: bool = False) -> dict:
        result = {
            "root": self.root.as_dict() if self.root else None,
            "secondary": [item.as_dict() for item in self.secondary],
        }
        if include_traceback and self.root:
            result["traceback"] = traceback.format_exc()
        return result

    @property
    def active(self):
        return self.root is not None
