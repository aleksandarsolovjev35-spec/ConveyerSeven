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


class ScriptedRecoveryCameras:
    """Фейк со сценарием: очередь результатов прогревов + reopen_roles.

    Повторное чтение мёртвого потока не помогает, поэтому после
    безуспешного повторного прогрева main должен эскалировать до
    пересоздания VideoCapture (как это делает production-захват).
    """

    def __init__(self, warmup_results, reopen_result=None):
        self.warmup_results = [dict(result) for result in warmup_results]
        self.reopen_result = dict(reopen_result or {})
        self.calls = []
        self.reopen_calls = []

    def warmup_roles(self, roles, duration=None):
        self.calls.append((tuple(roles), duration))
        return dict(self.warmup_results.pop(0))

    def reopen_roles(self, roles, timeout=None):
        self.reopen_calls.append(tuple(roles))
        return dict(self.reopen_result)


class StartupCameraRecoveryTests(unittest.TestCase):
    def test_detects_empty_warmup_roles_but_not_dark(self):
        """Только камеры без кадров (reads=0) считаются проблемными.

        Тёмные кадры (reads>0, низкая яркость) — нормальный переходный
        процесс AGC; production _grab() повторяет чтение до 30 раз.
        """
        reasons = _weak_camera_warmup_reasons({
            "INPUT_LEFT": {"reads": 0, "brightest": 0.0},
            "TOP": {"reads": 3, "brightest": 0.0},
            "SPIDER_IN": {"reads": 3, "brightest": 120.0},
        })

        self.assertIn("INPUT_LEFT", reasons)
        # reads=3 — камера жива, просто тёмная; _grab() разберётся.
        self.assertNotIn("TOP", reasons)
        self.assertNotIn("SPIDER_IN", reasons)

    def test_retries_only_zero_read_roles_and_merges_recovered_stats(self):
        cameras = FakeWarmupCameras({
            "INPUT_LEFT": {"reads": 6, "brightest": 100.0},
        })
        stats = {
            "INPUT_LEFT": {"reads": 0, "brightest": 0.0},
            "TOP": {"reads": 8, "brightest": 130.0},
        }

        merged = _recover_weak_cameras_after_warmup(
            cameras, stats, "test phase",
        )

        self.assertEqual(cameras.calls[0][0], ("INPUT_LEFT",))
        self.assertEqual(merged["INPUT_LEFT"]["brightest"], 100.0)
        self.assertEqual(merged["TOP"]["brightest"], 130.0)

    def test_recovery_fails_closed_if_camera_stays_empty(self):
        cameras = FakeWarmupCameras({
            "INPUT_LEFT": {"reads": 0, "brightest": 0.0},
        })

        with self.assertRaisesRegex(RuntimeError, "INPUT_LEFT"):
            _recover_weak_cameras_after_warmup(
                cameras,
                {"INPUT_LEFT": {"reads": 0, "brightest": 0.0}},
                "test phase",
            )

    def test_no_reopen_when_retry_warmup_gets_frames(self):
        """Камера, получившая кадры при повторном прогреве, считается живой."""
        cameras = ScriptedRecoveryCameras(
            warmup_results=[
                {"INPUT_LEFT": {"reads": 6, "brightest": 100.0}},
            ],
        )

        merged = _recover_weak_cameras_after_warmup(
            cameras,
            {"INPUT_LEFT": {"reads": 0, "brightest": 0.0}},
            "test phase",
        )

        self.assertEqual(cameras.reopen_calls, [])
        self.assertEqual(merged["INPUT_LEFT"]["brightest"], 100.0)

    def test_reopens_dead_stream_when_rewarmup_still_empty(self):
        cameras = ScriptedRecoveryCameras(
            warmup_results=[
                {"INPUT_LEFT": {"reads": 0, "brightest": 0.0}},
                {"INPUT_LEFT": {"reads": 12, "brightest": 110.0}},
            ],
            reopen_result={"INPUT_LEFT": True},
        )

        merged = _recover_weak_cameras_after_warmup(
            cameras,
            {"INPUT_LEFT": {"reads": 0, "brightest": 0.0}},
            "test phase",
        )

        self.assertEqual(cameras.reopen_calls, [("INPUT_LEFT",)])
        self.assertEqual(len(cameras.calls), 2)
        self.assertEqual(merged["INPUT_LEFT"]["brightest"], 110.0)

    def test_reopen_failure_is_reported_in_final_error(self):
        cameras = ScriptedRecoveryCameras(
            warmup_results=[
                {"INPUT_LEFT": {"reads": 0, "brightest": 0.0}},
                {"INPUT_LEFT": {"reads": 0, "brightest": 0.0}},
            ],
            reopen_result={"INPUT_LEFT": False},
        )

        with self.assertRaisesRegex(
            RuntimeError, "поток не пересоздался: INPUT_LEFT"
        ):
            _recover_weak_cameras_after_warmup(
                cameras,
                {"INPUT_LEFT": {"reads": 0, "brightest": 0.0}},
                "test phase",
            )


if __name__ == "__main__":
    unittest.main()
