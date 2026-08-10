"""Тесты Conveyor: параметры сохраняются в атрибуты после _set_params.

Production-цикл (_on_conveyor_progress) читает ``conveyor.speed`` и
``conveyor.steps_per_division`` для расчёта длительности движения линии в UI.
Раньше _set_params отправлял значения контроллеру, но не сохранял их, и UI
всегда получал дефолты (20000/19048) — анимация не учитывала реальную
скорость из calibration.json.
"""

import unittest


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.sleep_calls = 0

    def send(self, command):
        self.sent.append(command)

    def query(self, command, delay=0.0):
        return ""


class ConveyorParamsTest(unittest.TestCase):
    def test_params_saved_as_attributes(self):
        from hardware.conveyor import Conveyor

        transport = FakeTransport()
        conveyor = Conveyor(
            transport,
            speed=25000,
            accel=7000,
            steps_per_division=20000,
            divisions_per_movement=2,
        )

        self.assertEqual(conveyor.speed, 25000)
        self.assertEqual(conveyor.accel, 7000)
        self.assertEqual(conveyor.steps_per_division, 20000)
        self.assertEqual(conveyor.divisions_per_movement, 2)
        self.assertEqual(transport.sent[:4], [
            "G5 S25000", "G4 S7000", "G7 S20000", "G6 S2",
        ])

    def test_defaults_negative_geometry_rejected(self):
        from hardware.conveyor import Conveyor

        with self.assertRaises(ValueError):
            Conveyor(FakeTransport(), steps_per_division=0)
        with self.assertRaises(ValueError):
            Conveyor(FakeTransport(), divisions_per_movement=-1)


if __name__ == "__main__":
    unittest.main()
