"""Manual-recovery helpers for an abandoned production batch."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class AbandonedBatchError(RuntimeError):
    pass


def mark_batch_aborted(root_folder: str, batch_id: str) -> Path:
    """Durably mark the exact previous batch ABORTED without promoting staging."""
    root = Path(root_folder).expanduser().resolve()
    candidates = [
        path for path in root.glob(f"*/{batch_id}/batch.json")
        if path.is_file()
    ]
    if len(candidates) != 1:
        raise AbandonedBatchError(
            f"expected one previous batch {batch_id!r}, found {len(candidates)}"
        )
    path = candidates[0]
    with path.open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("batch_id") != batch_id:
        raise AbandonedBatchError("previous batch identity mismatch")
    manifest["status"] = "ABORTED"
    manifest["aborted_at"] = time.time()
    manifest["abort_reason"] = "previous_process_unclean_manual_cleanup_required"
    _write_json(path, manifest)
    parts_copy = path.parent / "parts" / "batch.json"
    if parts_copy.parent.is_dir():
        _write_json(parts_copy, manifest)
    return path.parent


def _write_json(path: Path, payload):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
