"""Тесты JogController: dead-man hold, heartbeat, release и таймаут."""

import time
import unittest

from hardware.jog_controller import JogController

CALIB = {
    "jog_hold_steps": 100000,
    "normal_steps": 19048,
}


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.i1 = "0"
        self.i2 = "MOV=0 WAIT=0 lastErr=0"

    def send(self, command):
        self.sent.append(command)

    def query(self, command, delay=0.0):
        if command == "I1":
            return self.i1
        if command == "I2":
            return self.i2
        return ""


class JogControllerTest(unittest.TestCase):
    def make(self, **kwargs):
        defaults = dict(calibration=CALIB)
        defaults.update(kwargs)
        return JogController(FakeTransport(), **defaults)

    def test_start_heartbeat_release(self):
        jog = self.make()
        self.assertTrue(jog.start_hold("+"))
        self.assertTrue(jog.busy)
        self.assertTrue(jog.heartbeat("+"))
        self.assertFalse(jog.heartbeat("-"))
        self.assertTrue(jog.release("button released"))
        self.assertFalse(jog.busy)
        self.assertIn("G1", jog.transport.sent)

    def test_heartbeat_timeout_self_stops(self):
        jog = self.make(heartbeat_timeout=0.15)
        jog.start_hold("+")
        deadline = time.monotonic() + 2.0
        while jog.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(jog.busy, "worker должен сам остановиться")
        self.assertIn("heartbeat timeout", jog.status["last_action"])

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.make(calibration={"jog_hold_steps": 100, "normal_steps": 1})


if __name__ == "__main__":
    unittest.main()
