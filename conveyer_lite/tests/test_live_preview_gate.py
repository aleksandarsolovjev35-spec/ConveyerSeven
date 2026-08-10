"""Тесты LiveCaptureGate: разграничение доступа к камерам."""

import threading
import time
import unittest

from core.live_preview import LiveCaptureGate


class LiveCaptureGateTest(unittest.TestCase):
    def test_pause_resume(self):
        gate = LiveCaptureGate()
        self.assertTrue(gate.pause())
        self.assertFalse(gate._pause_depth == 0)
        gate.resume()
        self.assertEqual(gate._pause_depth, 0)

    def test_nested_pause_depth(self):
        gate = LiveCaptureGate()
        self.assertTrue(gate.pause())
        self.assertTrue(gate.pause())
        gate.resume()
        self.assertFalse(gate._pause_depth == 0, "двойная пауза держится")
        gate.resume()
        self.assertEqual(gate._pause_depth, 0)

    def test_live_read_blocked_while_paused(self):
        gate = LiveCaptureGate()
        gate.pause()
        with gate.live_read() as allowed:
            self.assertFalse(allowed)
        gate.resume()

    def test_live_read_allowed_when_not_paused(self):
        gate = LiveCaptureGate()
        with gate.live_read() as allowed:
            self.assertTrue(allowed)

    def test_pause_waits_for_active_read(self):
        gate = LiveCaptureGate()
        release = threading.Event()

        def reader():
            with gate.live_read():
                release.wait(timeout=2.0)

        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(0.05)

        # pause() должен дождаться завершения чтения
        self.assertTrue(gate.pause())
        thread.join(timeout=1.0)
        gate.resume()

    def test_reset_unblocks_pause(self):
        gate = LiveCaptureGate()
        gate.pause()
        gate.pause()
        gate.reset()
        self.assertEqual(gate._pause_depth, 0)
        with gate.live_read() as allowed:
            self.assertTrue(allowed)

    def test_pause_roles_blocks_only_requested_camera_roles(self):
        gate = LiveCaptureGate()
        self.assertTrue(gate.pause_roles(("INPUT_LEFT", "INPUT_RIGHT")))
        with gate.live_read("INPUT_LEFT") as input_allowed:
            self.assertFalse(input_allowed)
        with gate.live_read("TOP") as top_allowed:
            self.assertTrue(top_allowed)
        gate.resume_roles(("INPUT_LEFT", "INPUT_RIGHT"))
        with gate.live_read("INPUT_RIGHT") as input_allowed:
            self.assertTrue(input_allowed)

    def test_pause_roles_waits_only_for_read_of_same_role(self):
        gate = LiveCaptureGate()
        release = threading.Event()

        def read_top():
            with gate.live_read("TOP"):
                release.wait(timeout=1.0)

        thread = threading.Thread(target=read_top)
        thread.start()
        time.sleep(0.03)
        # TOP занят, но INPUT должен перейти inspection без ожидания TOP.
        self.assertTrue(gate.pause_roles(("INPUT_LEFT",), timeout=0.1))
        release.set()
        thread.join(timeout=1.0)
        gate.resume_roles(("INPUT_LEFT",))


if __name__ == "__main__":
    unittest.main()
