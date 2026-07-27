from __future__ import annotations

import hashlib

from common import ROOT, print_header, require
from vision.model_config import MODEL_GROUPS


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    print_header("03 — MODEL FILES CHECK")
    relative_paths = sorted({
        entry["path"]
        for entries in MODEL_GROUPS.values()
        for entry in entries
    })
    require(len(relative_paths) == 12, f"Expected 12 model files, got {len(relative_paths)}")
    errors = []
    total = 0
    for relative in relative_paths:
        path = ROOT / relative
        if not path.is_file():
            errors.append(f"MISSING: {relative}")
            print(f"MISSING  {relative}")
            continue
        size = path.stat().st_size
        if size <= 0:
            errors.append(f"EMPTY: {relative}")
            print(f"EMPTY    {relative}")
            continue
        digest = sha256_file(path)
        total += size
        print(f"OK       {size:>10} bytes  {digest}  {relative}")
    require(not errors, "; ".join(errors))
    print(f"files={len(relative_paths)} total_bytes={total}")
    print("MODEL FILES CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
