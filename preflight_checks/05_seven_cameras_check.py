from __future__ import annotations

import numpy as np

from common import ROOT, print_header, require
from vision.camera_manager import CameraManager


def main() -> int:
    print_header("05 — SEVEN CAMERAS CHECK (NO MODELS, NO COM, NO MOTION)")
    manager = CameraManager(config_file=ROOT / "camera_mapping.json")
    try:
        # Прогрев после простоя, чтобы не ловить near-black на первом кадре
        try:
            manager.warmup_all(duration=2.0)
        except Exception as exc:
            print(f"[WARN] warmup failed: {exc}")
        frames = manager.capture_all()
        require(len(frames) == 7, f"Expected 7 frames, got {len(frames)}")
        for role, frame in frames.items():
            array = np.asarray(frame)
            height, width = array.shape[:2]
            mean = float(array[:, :, :3].mean())
            p99 = float(np.percentile(array[:, :, :3], 99))
            print(
                f"OK  {role:<14} {width}x{height} "
                f"mean={mean:.2f} p99={p99:.2f}"
            )
            require((width, height) == (1280, 720), f"Invalid resolution for {role}")
    finally:
        manager.release()
    print("SEVEN CAMERAS CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
