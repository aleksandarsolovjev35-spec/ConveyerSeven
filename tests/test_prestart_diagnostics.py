import unittest
from types import SimpleNamespace

import numpy as np

from core.production_cycle import ProductionCycle


ROLES = (
    "INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
    "SPIDER_IN", "SPIDER_OUT", "TOP",
)


class Conveyor:
    def emergency_stop(self):
        return None


class Cameras:
    def __init__(self):
        self.calls = 0
        self.capture_all_calls = 0
        self.capture_single_calls = []
        self.mapping = {role: index for index, role in enumerate(ROLES)}

    def capture_all(self):
        self.calls += 1
        self.capture_all_calls += 1
        return {
            role: np.full((720, 1280, 3), 100, dtype=np.uint8)
            for role in ROLES
        }

    def capture_single(self, role):
        self.calls += 1
        self.capture_single_calls.append(role)
        value = 100 + len(self.capture_single_calls)
        return np.full((720, 1280, 3), value, dtype=np.uint8)


class Vision:
    def __init__(self, fail=False):
        self.fail = fail
        self.last_health = []
        self.calls = 0
        self.roles_by_call = []

    def process_all(self, frames):
        self.calls += 1
        self.roles_by_call.append(tuple(frames))
        if self.fail:
            raise RuntimeError("model failed")
        self.last_health = [
            {
                "role": role,
                "model": f"weights/{role}.pt",
                "ok": True,
                "elapsed_ms": 12.0,
                "detections": 1,
                "error": None,
            }
            for role in frames
        ]
        return {
            role: [{"class": "demo", "confidence": 0.9}]
            for role in frames
        }


class Decision:
    thresholds = {
        "INPUT_LEFT.input_window_geometry_min_confidence": 0.4,
        "INPUT_RIGHT.input_window_geometry_min_confidence": 0.4,
        "INPUT_LEFT.input_part_presence_false_positive_max_count": 2,
        "INPUT_RIGHT.input_part_presence_false_positive_max_count": 2,
    }

    def __init__(self):
        self.top_rule = SimpleNamespace(name="top_rule", ROLES=("TOP",))
        self.input_rule = SimpleNamespace(
            name="input_rule",
            ROLES=("INPUT_LEFT", "INPUT_RIGHT"),
        )

    @staticmethod
    def _result(name, triggered=False):
        return SimpleNamespace(
            rule_name=name,
            triggered=triggered,
            defect=name if triggered else None,
            details={},
            drawings=[],
        )

    def evaluate_all_detailed(self, vision_results, frames=None):
        return [
            self._result("rule_ok"),
            self._result("rule_triggered", triggered=True),
        ]

    def rules_for_role(self, role):
        if role == "TOP":
            return [self.top_rule]
        if role in ("INPUT_LEFT", "INPUT_RIGHT"):
            return [self.input_rule]
        return []

    def evaluate_rules_detailed(self, rules, vision_results, frames=None):
        return [self._result(rule.name) for rule in rules]


class Inspector:
    INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    SPIDER_ROLES = (
        "SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP",
    )

    def __init__(self, fail=False):
        self.vision = Vision(fail=fail)
        self.decision = Decision()


class Distributor:
    def __init__(self):
        self.on_state_changed = None
        self.cancel_check = None
        self.dist1_open_position = 340

    @property
    def status(self):
        return {
            "dist1_position": 0,
            "dist1_max": 340,
            "dist1_state": "IDLE",
            "dist2_position": 0,
            "dist2_max": 340,
            "dist2_state": "IDLE",
            "dist2_target": "-",
            "last_distributor_action": "-",
        }

    def park_production(self):
        return None

    def emergency_stop(self):
        return None


class PrestartDiagnosticTests(unittest.TestCase):
    def make_cycle(self, fail=False):
        return ProductionCycle(
            Conveyor(),
            Cameras(),
            Inspector(fail=fail),
            Distributor(),
        )

    def test_camera_check_is_available_only_before_or_after_full_stop(self):
        cycle = self.make_cycle()
        self.assertTrue(cycle.diagnostic_check_cameras())
        report = cycle._build_status()["diagnostics"]
        self.assertEqual(report["status"], "PASSED")
        self.assertEqual(report["kind"], "CAMERAS")
        self.assertEqual(len(report["cameras"]), 7)
        self.assertTrue(all(row["width"] == 1280 for row in report["cameras"]))
        self.assertTrue(cycle.request_start())
        self.assertFalse(cycle.diagnostic_check_cameras())

    def test_all_models_and_rules_return_operator_report_without_motion(self):
        cycle = self.make_cycle()
        self.assertTrue(cycle.diagnostic_check_vision_rules())
        status = cycle._build_status()
        report = status["diagnostics"]
        self.assertEqual(report["status"], "PASSED")
        self.assertEqual(report["kind"], "VISION_RULES")
        self.assertEqual(len(report["cameras"]), 7)
        self.assertEqual(len(report["models"]), 7)
        self.assertEqual(len(report["rules"]), 3)
        self.assertEqual(report["rules"][0]["name"], "part_presence")
        self.assertEqual(
            sum(item["triggered"] for item in report["rules"]),
            1,
        )
        self.assertEqual(status["process"]["phase"], "DIAGNOSTIC_DONE")
        self.assertTrue(status["controls"]["start"])

    def test_selected_camera_snapshot_reports_its_models_and_rules_until_release(self):
        cycle = self.make_cycle()
        self.assertTrue(cycle.diagnostic_analyze_selected_camera("TOP"))
        status = cycle._build_status()
        self.assertTrue(status["selected_analysis"]["active"])
        self.assertEqual(status["selected_analysis"]["role"], "TOP")
        self.assertEqual(status["diagnostics"]["kind"], "SELECTED_MODEL")
        self.assertEqual(cycle.cameras.capture_all_calls, 0)
        self.assertEqual(cycle.cameras.capture_single_calls, ["TOP"] * 3)
        self.assertEqual(cycle.inspector.vision.calls, 3)
        self.assertEqual(cycle.inspector.vision.roles_by_call, [("TOP",)] * 3)
        self.assertEqual(len(status["diagnostics"]["cameras"]), 1)
        self.assertEqual(status["diagnostics"]["cameras"][0]["runs"], 3)
        self.assertEqual(len(status["diagnostics"]["models"]), 1)
        self.assertEqual(status["diagnostics"]["models"][0]["runs"], 3)
        self.assertEqual(
            status["diagnostics"]["models"][0]["detections_by_run"],
            [1, 1, 1],
        )
        self.assertEqual(len(status["diagnostics"]["rules"]), 1)
        self.assertEqual(status["diagnostics"]["rules"][0]["name"], "top_rule")
        self.assertEqual(
            status["diagnostics"]["rules"][0]["status_label"],
            "НОРМА · 3/3",
        )
        self.assertIn("3 свежих кадра", status["diagnostics"]["message"])
        self.assertTrue(status["frame_analysis"]["available"])
        self.assertEqual(status["frame_analysis"]["kind"], "SELECTED")
        self.assertEqual(status["frame_analysis"]["title"], "АНАЛИЗ КАДРА")
        self.assertFalse(status["controls"]["start"])
        self.assertFalse(status["controls"]["jog_hold"])
        self.assertFalse(status["controls"]["distributor_diagnostic"])
        self.assertTrue(status["controls"]["selected_model_release"])
        self.assertFalse(cycle.diagnostic_check_cameras())
        self.assertTrue(cycle.diagnostic_release_selected_camera())
        status = cycle._build_status()
        self.assertFalse(status["selected_analysis"]["active"])
        self.assertFalse(status["frame_analysis"]["available"])
        self.assertIsNone(status["diagnostics"]["kind"])
        self.assertTrue(status["controls"]["start"])
        self.assertTrue(status["controls"]["selected_model_analysis"])

    def test_selected_input_uses_only_one_camera_and_skips_joint_presence(self):
        cycle = self.make_cycle()
        self.assertTrue(cycle.diagnostic_analyze_selected_camera("INPUT_LEFT"))
        report = cycle._build_status()["diagnostics"]
        self.assertEqual(cycle.cameras.capture_single_calls, ["INPUT_LEFT"] * 3)
        self.assertEqual(
            cycle.inspector.vision.roles_by_call,
            [("INPUT_LEFT",)] * 3,
        )
        self.assertEqual(len(report["cameras"]), 1)
        self.assertEqual([row["name"] for row in report["rules"]], [
            "part_presence", "input_rule",
        ])
        self.assertTrue(report["rules"][0]["skipped"])
        self.assertIn("INPUT_LEFT и INPUT_RIGHT", report["rules"][0]["detail"])
        self.assertEqual(report["rules"][1]["status_label"], "НОРМА · 3/3")

    def test_model_diagnostic_failure_latches_fault_and_blocks_every_check(self):
        cycle = self.make_cycle(fail=True)
        with self.assertRaisesRegex(RuntimeError, "model failed"):
            cycle.diagnostic_check_vision_rules()
        status = cycle._build_status()
        self.assertEqual(status["state"], "FAULT")
        self.assertEqual(status["diagnostics"]["status"], "ERROR")
        self.assertFalse(status["controls"]["start"])
        self.assertFalse(status["controls"]["camera_diagnostic"])
        self.assertFalse(status["controls"]["vision_rule_diagnostic"])


if __name__ == "__main__":
    unittest.main()
