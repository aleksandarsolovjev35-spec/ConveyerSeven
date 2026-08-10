"""Process, batch, run and part identity contracts."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessIdentity:
    """One application process owns exactly one batch."""

    batch_id: str
    process_started_at: float

    @classmethod
    def create(cls) -> "ProcessIdentity":
        # UUID avoids collisions when two process starts happen in one second.
        return cls(
            batch_id=f"batch-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-"
            f"{os.getpid()}-{uuid.uuid4().hex[:12]}",
            process_started_at=time.time(),
        )


@dataclass(frozen=True)
class RunIdentity:
    batch_id: str
    run_id: int
    started_at: float

    @classmethod
    def create(cls, batch_id: str, run_id: int) -> "RunIdentity":
        if type(run_id) is not int or run_id < 1:
            raise ValueError("run_id must be a positive integer")
        return cls(
            batch_id=batch_id,
            run_id=run_id,
            started_at=time.time(),
        )


class IdentityCounters:
    """Monotonic part counter scoped to one process/batch."""

    def __init__(self, batch_id: str | None = None):
        self.identity = ProcessIdentity.create() if batch_id is None else ProcessIdentity(
            str(batch_id), time.time()
        )
        self._next_part_id = 1
        self._next_run_id = 1

    @property
    def batch_id(self) -> str:
        return self.identity.batch_id

    def next_part_id(self) -> int:
        value = self._next_part_id
        self._next_part_id += 1
        return value

    @property
    def next_id(self) -> int:
        return self._next_part_id

    def new_run(self) -> RunIdentity:
        identity = RunIdentity.create(self.batch_id, self._next_run_id)
        self._next_run_id += 1
        return identity
