from __future__ import annotations

import os
import time

from common import ROOT, print_header, require
from vision.vision_cluster import VisionCluster


def main() -> int:
    print_header("04 — MODEL LOAD AND CPU WARMUP")
    os.chdir(ROOT)
    started = time.perf_counter()
    vision = VisionCluster(device="cpu", verbose=True)
    load_seconds = time.perf_counter() - started
    require(len(vision.models) == 12, f"Expected 12 loaded models, got {len(vision.models)}")

    started = time.perf_counter()
    vision.warmup()
    warmup_seconds = time.perf_counter() - started
    print(f"model_load_seconds={load_seconds:.3f}")
    print(f"warmup_seconds={warmup_seconds:.3f}")
    print("MODEL LOAD AND WARMUP PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
