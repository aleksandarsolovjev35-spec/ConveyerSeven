"""Read-only archive compatibility adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path

from inspection.part_archive import PartArchive


class ArchiveReadError(RuntimeError):
    pass


class ReadOnlyArchiveReader:
    """Open current committed catalogs without any migration/write side effect."""

    def __init__(self, folder: str):
        self.folder = Path(folder).resolve()
        if not self.folder.is_dir():
            raise ArchiveReadError(f"archive folder does not exist: {folder}")

    def read_part(self, category: str, part_id: int) -> dict:
        path = self.folder / "parts" / str(category).upper() / f"part_{part_id:04d}"
        if not path.is_dir() or not PartArchive._verify_committed_part(str(path)):
            raise ArchiveReadError("part is not a verified committed catalog")
        try:
            with (path / "meta.json").open(encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, ValueError) as exc:
            raise ArchiveReadError(f"invalid part metadata: {exc}") from exc

    def list_committed(self) -> list[dict]:
        result = []
        parts = self.folder / "parts"
        if not parts.is_dir():
            return result
        for category_dir in parts.iterdir():
            if not category_dir.is_dir():
                continue
            for part_dir in category_dir.iterdir():
                if not part_dir.is_dir() or not PartArchive._verify_committed_part(str(part_dir)):
                    continue
                result.append({
                    "category": category_dir.name,
                    "part": part_dir.name,
                    "folder": str(part_dir),
                })
        return sorted(result, key=lambda row: (row["category"], row["part"]))
