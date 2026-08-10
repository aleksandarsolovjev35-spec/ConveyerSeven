"""Тесты Axis: валидация, команды и парсинг статусов контроллера."""

import unittest

from hardware.axis import Axis


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.reply = ""

    def send(self, command):
        self.sent.append(command)

    def query(self, command, delay=0.0):
        return self.reply


AXIS0_MOVED = (
    "AXIS0 POS=100 TGT=100 MOV=0 EN=1 HOME=0 HOMED=1 LIM=1 ES=0\r\n"
    "AXIS1 POS=0 TGT=0 MOV=0 EN=1 HOME=0 HOMED=0 LIM=1 ES=0\r\n"
)
AXIS0_HOMED = (
    "AXIS0 POS=0 TGT=0 MOV=0 EN=1 HOME=0 HOMED=1 LIM=1 ES=0\r\n"
    "AXIS1 POS=0 TGT=0 MOV=0 EN=1 HOME=0 HOMED=0 LIM=1 ES=0\r\n"
)
AXIS0_CONFIG = (
    "AXIS0 speed=300 accel=100 limMin=0 limMax=340\r\n"
    "AXIS1 speed=300 accel=100 limMin=0 limMax=340\r\n"
)


class AxisTest(unittest.TestCase):
    def test_invalid_params_rejected(self):
        t = FakeTransport()
        t.reply = AXIS0_CONFIG
        with self.assertRaises(ValueError):
            Axis(t, axis_id=5, maximum=340)
        with self.assertRaises(ValueError):
            Axis(t, axis_id=0, maximum=0)
        with self.assertRaises(ValueError):
            Axis(t, axis_id=0, maximum=340, minimum=400)

    def test_move_absolute_validates_range(self):
        t = FakeTransport()
        t.reply = AXIS0_CONFIG
        axis = Axis(t, axis_id=0, maximum=340)
        axis.move_absolute(100)
        self.assertIn("G27 S100 P0", t.sent)

        with self.assertRaises(ValueError):
            axis.move_absolute(-1)
        with self.assertRaises(ValueError):
            axis.move_absolute(341)
        with self.assertRaises(ValueError):
            axis.move_absolute(10.5)

    def test_home_sends_g28(self):
        t = FakeTransport()
        t.reply = AXIS0_CONFIG
        axis = Axis(t, axis_id=1, maximum=340)
        axis.home()
        self.assertIn("G28 P1", t.sent)

    def test_read_status_parses(self):
        t = FakeTransport()
        t.reply = AXIS0_CONFIG
        axis = Axis(t, axis_id=0, maximum=340)

        t.reply = AXIS0_MOVED
        status = axis.read_status()
        self.assertEqual(status["position"], 100)
        self.assertEqual(status["target"], 100)
        self.assertEqual(status["moving"], 0)
        self.assertEqual(status["homed"], 1)
        self.assertEqual(status["limits_enabled"], 1)

    def test_verify_homed_passes_for_homed_axis(self):
        t = FakeTransport()
        t.reply = AXIS0_CONFIG
        axis = Axis(t, axis_id=0, maximum=340)

        t.reply = AXIS0_HOMED
        axis.verify_homed()   # не должно бросить

    def test_verify_homed_fails_for_unhomed(self):
        t = FakeTransport()
        t.reply = AXIS0_CONFIG
        axis = Axis(t, axis_id=1, maximum=340)

        t.reply = AXIS0_MOVED
        with self.assertRaises(RuntimeError):
            axis.verify_homed()


if __name__ == "__main__":
    unittest.main()
