"""Тесты Distributor: валидация, маршруты, статус для UI."""

import unittest

from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP
from hardware.distributor import Distributor


class FakeAxis:
    def __init__(self, position=0):
        self.position = position
        self.calls = []
        self.transport = type("T", (), {"send": lambda self, cmd: None})()

    def move_absolute(self, position):
        self.calls.append(("move", position))
        self.position = position

    def wait_stop(self, timeout=12.0, progress_callback=None):
        if progress_callback:
            progress_callback(self.position, 0)


class DistributorTest(unittest.TestCase):
    def make(self, **kwargs):
        defaults = dict(
            dist1_open_position=120,
            dist2_bad_position=80,
            dist2_cleanup_position=200,
            drop_time=0.0,
        )
        defaults.update(kwargs)
        return Distributor(FakeAxis(), FakeAxis(), **defaults)

    def test_invalid_positions_rejected(self):
        with self.assertRaises(ValueError):
            self.make(dist1_open_position=0)
        with self.assertRaises(ValueError):
            self.make(dist2_cleanup_position=0)
        with self.assertRaises(ValueError):
            self.make(dist2_bad_position=80, dist2_cleanup_position=80)

    def test_status_shape(self):
        dist = self.make()
        status = dist.status
        for key in ("dist1_position", "dist1_max", "dist1_state",
                    "dist2_position", "dist2_max", "dist2_state",
                    "dist2_target", "last_distributor_action"):
            self.assertIn(key, status, key)
        self.assertEqual(status["dist1_max"], 120)
        self.assertEqual(status["dist2_max"], 200)

    def test_route_bad_prepare_and_drop(self):
        dist = self.make()
        dist.prepare(CATEGORY_BAD, part_id=7)
        self.assertEqual(dist.dist2_target, CATEGORY_BAD)
        self.assertIn("7", dist.last_action)
        self.assertEqual(dist.dist1_state, "OPEN")
        self.assertEqual(dist.dist1.position, 120)
        self.assertEqual(dist.dist2.position, 80)

        dist.drop_and_close(7, CATEGORY_BAD)
        self.assertEqual(dist.dist1_state, "IDLE")
        self.assertEqual(dist.dist1.position, 0)

    def test_route_cleanup(self):
        dist = self.make()
        dist.prepare(CATEGORY_CLEANUP, part_id=3)
        self.assertEqual(dist.dist2_target, CATEGORY_CLEANUP)
        self.assertEqual(dist.dist2.position, 200)

    def test_mark_pass(self):
        dist = self.make()
        dist.mark_pass(5)
        self.assertEqual(dist.dist1_state, "IDLE")
        self.assertIn("PASS", dist.last_action)

    def test_on_state_changed_callback(self):
        dist = self.make()
        seen = []
        dist.on_state_changed = lambda: seen.append(True)
        dist.park_production()
        self.assertGreaterEqual(len(seen), 1)
        self.assertEqual(dist.last_action, "PRODUCTION READY")

    def test_cancel_check_raises(self):
        dist = self.make()
        dist.cancel_check = lambda: True
        with self.assertRaises(RuntimeError):
            dist.park_production()

    def test_emergency_stop(self):
        dist = self.make()
        dist.emergency_stop()
        self.assertEqual(dist.dist1_state, "FAULT")
        self.assertEqual(dist.dist2_state, "FAULT")
        self.assertEqual(dist.last_action, "EMERGENCY STOP")


if __name__ == "__main__":
    unittest.main()
