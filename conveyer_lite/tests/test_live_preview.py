"""Тесты LivePreview: запуск/остановка потоков, пауза, публикация кадров."""

import time
import unittest

import numpy as np

from core.live_preview import LiveCaptureGate, LivePreview


class FakeCameras:
    mapping = {
        "INPUT_LEFT": 0, "INPUT_RIGHT": 1, "SPIDER_LEFT": 2,
        "SPIDER_RIGHT": 3, "SPIDER_IN": 4, "SPIDER_OUT": 5, "TOP": 6,
    }

    def __init__(self):
        self.captures = []

    def capture_single(self, role):
        self.captures.append(role)
        return np.zeros((8, 8, 3), dtype=np.uint8)

    def capture_roles(self, roles):
        for role in roles:
            self.captures.append(role)
        return {role: np.zeros((8, 8, 3), dtype=np.uint8)
                for role in roles}

    def capture_all(self):
        return {role: np.zeros((8, 8, 3), dtype=np.uint8)
                for role in self.mapping}


class FakeMonitor:
    def __init__(self):
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)


class LivePreviewTest(unittest.TestCase):
    def setUp(self):
        self.cameras = FakeCameras()
        self.monitor = FakeMonitor()
        self.preview = LivePreview(
            cameras=self.cameras,
            monitor=self.monitor,
            get_active_role=lambda: "INPUT_LEFT",
        )

    def tearDown(self):
        if self.preview.running:
            self.preview.stop()
        self.preview.reset_pause()

    def test_start_stop(self):
        self.assertTrue(self.preview.start())
        self.assertFalse(self.preview.start())  # повторный start — no-op
        self.assertTrue(self.preview.running)
        self.preview.stop()
        self.assertFalse(self.preview.running)

    def test_publish_frames_while_running(self):
        self.preview.start()
        deadline = time.monotonic() + 2.0
        while not self.monitor.updates and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.monitor.updates, "поток должен публиковать кадры")
        # Активная камера публикуется основным потоком (выбранная роль),
        # вспомогательный поток — остальные шесть.
        has_active = any(
            "INPUT_LEFT" in update.get("frames", {})
            for update in self.monitor.updates
        )
        self.assertTrue(has_active, "должен быть кадр активной камеры")
        self.preview.stop()

    def test_pause_blocks_and_resume_restores(self):
        self.preview.start()
        time.sleep(0.1)
        self.assertTrue(self.preview.pause())
        updates_before = len(self.monitor.updates)
        time.sleep(0.15)
        # Во время паузы live-чтения запрещены — новых публикаций быть не должно
        self.assertLessEqual(len(self.monitor.updates), updates_before + 1)
        self.preview.resume()
        deadline = time.monotonic() + 2.0
        while len(self.monitor.updates) <= updates_before + 1 \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        self.preview.stop()

    def test_clear_overlays_publishes_empty(self):
        self.preview.start()
        time.sleep(0.1)
        self.preview.clear_overlays()
        self.preview.stop()

    def test_error_sets_flag(self):
        class FailingCameras(FakeCameras):
            def capture_single(self, role):
                raise RuntimeError("camera failed")

        preview = LivePreview(
            cameras=FailingCameras(),
            monitor=self.monitor,
            get_active_role=lambda: "INPUT_LEFT",
        )
        preview.start()
        deadline = time.monotonic() + 2.0
        while preview.error is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(preview.error)
        self.assertIn("camera failed", preview.error)
        preview.stop()

    def test_gate_reset(self):
        gate = LiveCaptureGate()
        gate.pause()
        self.preview.gate = gate
        self.preview.reset_pause()
        self.assertEqual(gate._pause_depth, 0)


if __name__ == "__main__":
    unittest.main()
