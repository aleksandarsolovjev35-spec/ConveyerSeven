"""Контракт физической маршрутизации двух заслонок."""

import unittest

from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP, CATEGORY_GOOD
from hardware.distributor import Distributor


class FakeAxis:
    def __init__(self, position=0):
        self.position, self.calls = position, []
        self.transport = type("T", (), {"send": lambda self, cmd: None})()

    def move_absolute(self, position):
        self.calls.append(("move", position))
        self.position = position

    def wait_stop(self, timeout=12.0, progress_callback=None):
        if progress_callback:
            progress_callback(self.position, 0)


class DistributorTest(unittest.TestCase):
    def make(self):
        return Distributor(FakeAxis(), FakeAxis(), 340, 0, 340, drop_time=0)

    def test_good_uses_only_first_gate_zero(self):
        dist = self.make()
        dist.prepare_route(CATEGORY_GOOD, 1)
        self.assertEqual(dist.dist1.position, 0)
        self.assertEqual(dist.dist2.calls, [])
        self.assertEqual(dist.dist1_state, "GOOD")

    def test_bad_sets_dist2_then_dist1(self):
        dist = self.make()
        dist.prepare_route(CATEGORY_BAD, 1)
        self.assertEqual(dist.dist2.position, 0)
        self.assertEqual(dist.dist1.position, 340)
        self.assertIn("BAD READY", dist.last_action)

    def test_cleanup_sets_dist2_before_dist1(self):
        dist = self.make()
        dist.prepare_route(CATEGORY_CLEANUP, 2)
        self.assertEqual(dist.dist1.position, 340)
        self.assertEqual(dist.dist2.position, 340)


if __name__ == "__main__":
    unittest.main()
