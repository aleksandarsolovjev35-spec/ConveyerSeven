"""Manual, versioned release installation helper.

It is intentionally not invoked by the production executable.  A release is
prepared in a new directory and activated with one atomic pointer replacement;
there is no auto-download or in-place overwrite.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path


class ReleaseError(RuntimeError):
    pass


class VersionedReleaseManager:
    def __init__(self, root: str = "releases"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.pointer = self.root / "active.json"

    def install(self, source: str, version: str, *, application_closed: bool, line_empty: bool) -> Path:
        if not application_closed or not line_empty:
            raise ReleaseError("release installation requires closed application and empty line")
        version = str(version).strip()
        if not version or any(char in version for char in "/\\"):
            raise ReleaseError("invalid release version")
        destination = self.root / version
        if destination.exists():
            raise ReleaseError("release version already exists")
        temp = self.root / f".{version}.staging-{os.getpid()}"
        shutil.copytree(source, temp)
        marker = temp / "release.json"
        with marker.open("w", encoding="utf-8") as stream:
            json.dump({"version": version, "installed_at": time.time()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, destination)
        self._activate(version)
        return destination

    def _activate(self, version: str):
        temp = self.pointer.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump({"active_version": version}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.pointer)

    def rollback(self, version: str, *, application_closed: bool, line_empty: bool):
        if not application_closed or not line_empty:
            raise ReleaseError("rollback requires closed application and empty line")
        if not (self.root / str(version)).is_dir():
            raise ReleaseError("known-good release is unavailable")
        self._activate(str(version))
