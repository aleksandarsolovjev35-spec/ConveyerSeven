"""Тесты диагностик и JOG ProductionCycle (на фейковом железе)."""

import unittest
from types import SimpleNamespace

import numpy as np

from core.production_cycle import ProductionCycle
from domain.defect_rules.base import RuleResult


ROLES = ("INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
         "SPIDER_IN", "SPIDER_OUT", "TOP")


def _frame():
    return np.full((240, 320, 3), 100, dtype=np.uint8)


class FakeCameras:
    mapping = {role: {"index": index} for index, role in enumerate(ROLES)}

    def capture_all(self):
        return {role: _frame() for role in ROLES}

    def drain_buffers(self, roles=None):
        pass

    def capture_single(self, role):
        return _frame()


class FakeConveyor:
    speed = 20000
    steps_per_division = 19048

    def move_step(self):
        pass

    def wait_stop(self, timeout=15.0, progress_callback=None):
        pass

    def emergency_stop(self):
        pass


class FakeDistributor:
    def __init__(self):
        self.status = {
            "dist1_state": "IDLE", "dist1_position": 0, "dist1_max": 340,
            "dist2_state": "IDLE", "dist2_position": 0, "dist2_max": 340,
            "dist2_target": "-", "last_distributor_action": "-",
        }
        self.on_state_changed = None
        self.cancel_check = None
        self.dist1_open_position = 0

    def park_production(self):
        pass

    def emergency_stop(self):
        pass


class FakeJog:
    def __init__(self):
        self._busy = False
        self._error = None
        self.release_calls = 0

    @property
    def busy(self):
        return self._busy

    @property
    def status(self):
        return {
            "hold_steps": 100000,
            "last_action": "-",
            "busy": self._busy,
            "direction": None,
            "error": self._error,
        }

    def start_hold(self, direction):
        self._busy = True
        return True

    def heartbeat(self, direction):
        return self._busy

    def release(self, reason):
        self.release_calls += 1
        self._busy = False
        return True


class FakeVision:
    def __init__(self):
        self.last_health = []

    def process_all(self, frames):
        self.last_health = []
        result = {}
        for role in frames:
            result[role] = []
            self.last_health.append({
                "role": role, "model": "fake", "ok": True,
                "run": 1, "elapsed_ms": 1.0, "detections": 0,
            })
        return result


class FakeDecision:
    def __init__(self):
        from domain.threshold_loader import ThresholdLoader
        self.thresholds = ThresholdLoader("thresholds.json").get_all()

    def evaluate_all_detailed(self, vision_results, frames=None):
        return []

    def evaluate_rules_detailed(self, rules, vision_results, frames=None):
        return [RuleResult("window_geometry", False,
                           details={"per_role": {}}, drawings=[])]

    def rules_for_role(self, role):
        return [RuleResult("window_geometry", False,
                           details={"per_role": {}}, drawings=[])]


class FakeInspector:
    INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    SPIDER_ROLES = ("SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN",
                    "SPIDER_OUT", "TOP")

    def __init__(self):
        self.vision = FakeVision()
        self.decision = FakeDecision()

    def _evaluate_part_presence(self, vision_results):
        from domain.defect_rules import InputPartPresenceRule
        rule = InputPartPresenceRule(
            thresholds=self.decision.thresholds,
        )
        return rule.check(vision_results)


class FakeMonitor:
    def __init__(self):
        self.server = SimpleNamespace(active_camera_role=None)
        self.calls = []

    def update(self, **kwargs):
        self.calls.append(kwargs)


class CycleDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.cycle = ProductionCycle(
            conveyor=FakeConveyor(),
            cameras=FakeCameras(),
            inspector=FakeInspector(),
            distributor=FakeDistributor(),
            monitor=FakeMonitor(),
            jog=FakeJog(),
            settle_seconds=0.0,
            stage_trace_seconds=0.0,
            review_seconds=0.0,
        )

    def test_diagnostic_check_cameras(self):
        self.assertTrue(self.cycle.diagnostic_check_cameras())
        diag = self.cycle._diagnostics
        self.assertEqual(diag["status"], "PASSED")
        self.assertEqual(diag["kind"], "CAMERAS")
        self.assertEqual(len(diag["cameras"]), 7)
        for row in diag["cameras"]:
            self.assertTrue(row["ok"])
            self.assertEqual(row["width"], 320)

    def test_diagnostic_blocked_while_running(self):
        self.cycle.sm.request_start()
        self.assertFalse(self.cycle.diagnostic_check_cameras())

    def test_analyze_selected_camera_input(self):
        # Пустой лоток на входе: part_presence → empty_tray, defect-правила
        # не выполняются; диагностика завершается PASSED.
        self.assertTrue(self.cycle.diagnostic_analyze_selected_camera(
            "INPUT_LEFT",
        ))
        diag = self.cycle._diagnostics
        self.assertEqual(diag["status"], "PASSED")
        self.assertEqual(diag["kind"], "SELECTED_MODEL")
        self.assertTrue(self.cycle._selected_analysis_active)
        self.assertEqual(self.cycle._selected_analysis_role, "INPUT_LEFT")
        # Строки правил включают part_presence
        names = [row["name"] for row in diag["rules"]]
        self.assertIn("part_presence", names)

    def test_release_selected_camera(self):
        self.cycle._selected_analysis_active = True
        self.cycle._selected_analysis_role = "INPUT_LEFT"
        self.assertTrue(self.cycle.diagnostic_release_selected_camera())
        self.assertFalse(self.cycle._selected_analysis_active)
        self.assertIsNone(self.cycle._selected_analysis_role)
        # После release поток восстановлен (live.resume вызван без ошибок)

    def test_can_enter_jog_idle(self):
        self.assertTrue(self.cycle.can_enter_jog())
        self.assertTrue(self.cycle.enter_jog())
        self.assertTrue(self.cycle.jog_active)
        self.assertTrue(self.cycle.can_enter_jog() or not self.cycle.jog_active)
        self.assertTrue(self.cycle.exit_jog())
        self.assertFalse(self.cycle.jog_active)

    def test_can_enter_jog_blocked_while_running(self):
        self.cycle.sm.request_start()
        self.assertFalse(self.cycle.can_enter_jog())
        self.assertFalse(self.cycle.enter_jog())

    def test_jog_hold_requires_active_jog(self):
        self.assertFalse(self.cycle.jog_hold_start("+"))
        self.assertTrue(self.cycle.enter_jog())
        self.assertTrue(self.cycle.jog_hold_start("+"))
        self.assertTrue(self.cycle.jog_hold_heartbeat("+"))
        self.assertTrue(self.cycle.jog_hold_release("test"))
        self.cycle.exit_jog()

    def test_pause_before_motion_is_immediate(self):
        self.cycle.sm.request_start()
        self.assertTrue(self.cycle.request_pause())
        self.assertEqual(self.cycle.state, "PAUSED")
        self.assertEqual(self.cycle.sm.pause_continuation.value, "NEXT_STEP")

    def test_resume_from_paused(self):
        self.cycle.sm.request_start()
        self.assertTrue(self.cycle.sm.request_pause())
        self.assertEqual(self.cycle.state, "PAUSED")
        self.assertTrue(self.cycle.request_resume())
        self.assertEqual(self.cycle.state, "RUNNING")


if __name__ == "__main__":
    unittest.main()
