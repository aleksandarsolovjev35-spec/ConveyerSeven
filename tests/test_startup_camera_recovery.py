import os
import tempfile
import unittest

os.environ.setdefault("YOLO_CONFIG_DIR", tempfile.gettempdir())

from main import (
    _recover_weak_cameras_after_warmup,
    _weak_camera_warmup_reasons,
)


class FakeWarmupCameras:
    def __init__(self, retry_stats):
        self.retry_stats = retry_stats
        self.calls = []

    def warmup_roles(self, roles, duration=None):
        self.calls.append((tuple(roles), duration))
        return dict(self.retry_stats)


class StartupCameraRecoveryTests(unittest.TestCase):
    def test_detects_empty_and_dark_warmup_roles(self):
        reasons = _weak_camera_warmup_reasons({
            "INPUT_LEFT": {"reads": 0, "brightest": 0.0},
            "TOP": {"reads": 3, "brightest": 0.0},
            "SPIDER_IN": {"reads": 3, "brightest": 120.0},
        })

        self.assertIn("INPUT_LEFT", reasons)
        self.assertIn("TOP", reasons)
        self.assertNotIn("SPIDER_IN", reasons)

    def test_retries_only_weak_roles_and_merges_recovered_stats(self):
        cameras = FakeWarmupCameras({
            "INPUT_LEFT": {"reads": 6, "brightest": 100.0},
        })
        stats = {
            "INPUT_LEFT": {"reads": 1, "brightest": 0.0},
            "TOP": {"reads": 8, "brightest": 130.0},
        }

        merged = _recover_weak_cameras_after_warmup(
            cameras, stats, "test phase",
        )

        self.assertEqual(cameras.calls[0][0], ("INPUT_LEFT",))
        self.assertEqual(merged["INPUT_LEFT"]["brightest"], 100.0)
        self.assertEqual(merged["TOP"]["brightest"], 130.0)

    def test_recovery_fails_closed_if_camera_stays_dark(self):
        cameras = FakeWarmupCameras({
            "INPUT_LEFT": {"reads": 3, "brightest": 0.0},
        })

        with self.assertRaisesRegex(RuntimeError, "INPUT_LEFT"):
            _recover_weak_cameras_after_warmup(
                cameras,
                {"INPUT_LEFT": {"reads": 1, "brightest": 0.0}},
                "test phase",
            )


if __name__ == "__main__":
    unittest.main()
