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
        self.i1 = "0"          # "1" — движется, "0" — остановлен
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

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.make(calibration={"jog_hold_steps": 100, "normal_steps": 1})
        with self.assertRaises(ValueError):
            self.make(calibration={"jog_hold_steps": 100000, "normal_steps": 0})
        with self.assertRaises(ValueError):
            self.make(heartbeat_timeout=5.0)

    def test_initial_status(self):
        jog = self.make()
        status = jog.status
        self.assertFalse(status["busy"])
        self.assertIsNone(status["direction"])
        self.assertEqual(status["hold_steps"], 100000)
        self.assertIsNone(status["error"])
        self.assertFalse(jog.busy)

    def test_bad_direction_raises(self):
        jog = self.make()
        with self.assertRaises(ValueError):
            jog.start_hold("X")

    def test_start_heartbeat_release(self):
        jog = self.make()
        self.assertTrue(jog.start_hold("+"))
        self.assertTrue(jog.busy)
        self.assertEqual(jog.status["direction"], "+")
        self.assertEqual(jog.status["last_action"], "HOLD RIGHT")

        self.assertTrue(jog.heartbeat("+"))
        # Неправильное направление — heartbeat отклоняется
        self.assertFalse(jog.heartbeat("-"))

        self.assertTrue(jog.release("button released"))
        self.assertFalse(jog.busy)
        self.assertIn("G1", jog.transport.sent)
        # После release восстанавливается normal_steps (G7 + G6 S2)
        self.assertIn("G7 S19048", jog.transport.sent)
        self.assertIn("G6 S2", jog.transport.sent)

    def test_direction_switch_while_active_rejected(self):
        jog = self.make()
        self.assertTrue(jog.start_hold("+"))
        self.assertFalse(jog.start_hold("-"))
        jog.release("test")

    def test_heartbeat_after_release_rejected(self):
        jog = self.make()
        jog.start_hold("+")
        jog.release("test")
        self.assertFalse(jog.heartbeat("+"))

    def test_release_before_any_heartbeat(self):
        jog = self.make()
        jog.start_hold("+")
        self.assertTrue(jog.release("early release"))
        self.assertFalse(jog.busy)

    def test_heartbeat_timeout_self_stops(self):
        jog = self.make(heartbeat_timeout=0.15)
        jog.start_hold("+")
        # Не шлём heartbeat: worker сам остановит ленту по таймауту
        deadline = time.monotonic() + 2.0
        while jog.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(jog.busy, "worker должен сам остановиться")
        self.assertIn("heartbeat timeout", jog.status["last_action"])
        self.assertIn("G1", jog.transport.sent)

    def test_worker_restores_params_after_timeout(self):
        jog = self.make(heartbeat_timeout=0.15)
        jog.start_hold("+")
        deadline = time.monotonic() + 2.0
        while jog.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("G7 S19048", jog.transport.sent)
        self.assertIn("G6 S2", jog.transport.sent)


if __name__ == "__main__":
    unittest.main()
