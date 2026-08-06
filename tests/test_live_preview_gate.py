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


if __name__ == "__main__":
    unittest.main()
