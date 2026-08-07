"""Регрессия физической очередности маршрута корпуса на +7.

DIST1=0 направляет в GOOD. DIST1=340 передаёт корпус на DIST2;
DIST2=0 выбирает BAD, DIST2=340 — CLEANUP. Никакого удерживающего
лепестка в этой механике нет.
"""
import unittest
from types import SimpleNamespace

from core.production_cycle import ProductionCycle
from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP, CATEGORY_GOOD, CATEGORY_UNKNOWN, Part


class RecorderDistributor:
    def __init__(self): self.calls = []
    def prepare_route(self, category, part_id): self.calls.append(("prepare", part_id, category))
    def confirm_transfer(self, part_id, category): self.calls.append(("confirm", part_id, category))
    def reset_target(self): self.calls.append(("reset",))


class DropFlowTest(unittest.TestCase):
    def cycle_with(self, category):
        cycle = ProductionCycle.__new__(ProductionCycle)
        cycle.distributor = RecorderDistributor()
        cycle._pending_drop = Part(17, 1)
        cycle._pending_drop.route_category = category
        cycle._pending_drop.final_decision = "test"
        cycle.parts = [cycle._pending_drop]
        cycle.good_count = cycle.bad_count = cycle.cleanup_count = 0
        cycle.archive = None
        cycle.recent_parts = []
        return cycle

    def test_every_route_is_prepared_before_transfer(self):
        for category in (CATEGORY_GOOD, CATEGORY_BAD, CATEGORY_CLEANUP):
            with self.subTest(category=category):
                cycle = self.cycle_with(category)
                cycle._prepare_drop()
                cycle._execute_drop()
                self.assertEqual(cycle.distributor.calls, [
                    ("prepare", 17, category), ("confirm", 17, category),
                ])

    def test_unknown_is_fail_safe_bad(self):
        cycle = self.cycle_with(CATEGORY_UNKNOWN)
        cycle._prepare_drop()
        self.assertEqual(cycle._pending_drop.route_category, CATEGORY_BAD)
        self.assertEqual(cycle.distributor.calls, [("prepare", 17, CATEGORY_BAD)])

    def test_counter_and_line_removal_happen_only_after_confirmation(self):
        cycle = self.cycle_with(CATEGORY_CLEANUP)
        cycle._prepare_drop()
        self.assertEqual(cycle.cleanup_count, 0)
        cycle._execute_drop()
        self.assertEqual(cycle.cleanup_count, 1)
        self.assertEqual(cycle.parts, [])
        self.assertIsNone(cycle._pending_drop)


if __name__ == "__main__":
    unittest.main()
