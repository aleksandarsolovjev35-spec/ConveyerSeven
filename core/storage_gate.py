"""Production archive free-space gate."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


class StorageFault(RuntimeError):
    pass


@dataclass(frozen=True)
class StorageStatus:
    path: str
    free_bytes: int
    reserve_bytes: int
    ok: bool


class StorageGate:
    def __init__(self, path: str, reserve_bytes: int):
        if reserve_bytes < 0:
            raise ValueError("reserve_bytes must be non-negative")
        self.path = os.path.abspath(path)
        self.reserve_bytes = int(reserve_bytes)

    def status(self) -> StorageStatus:
        try:
            free = int(shutil.disk_usage(self.path).free)
        except OSError as exc:
            raise StorageFault(f"cannot inspect archive disk: {exc}") from exc
        return StorageStatus(self.path, free, self.reserve_bytes, free > self.reserve_bytes)

    def require(self):
        current = self.status()
        if not current.ok:
            raise StorageFault(
                f"archive disk reserve exhausted: free={current.free_bytes}, "
                f"reserve={current.reserve_bytes}"
            )
        return current
