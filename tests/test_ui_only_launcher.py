import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class UiOnlyLauncherTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.source = (self.root / "ui_demo.py").read_text(encoding="utf-8")
        self.launcher = (self.root / "run_ui_only.bat").read_text(encoding="utf-8")

    def test_launcher_uses_only_ui_demo(self):
        self.assertIn("ui_demo.py", self.launcher)
        self.assertNotIn("main.py", self.launcher)
        self.assertIn("UI_ONLY=1", self.launcher)
        setup = (self.root / "setup_ui_only.bat").read_text(encoding="utf-8")
        requirements = (self.root / "requirements-ui.txt").read_text(encoding="utf-8")
        self.assertIn("requirements-ui.txt", setup)
        self.assertNotIn("ultralytics", requirements)
        self.assertNotIn("torch", requirements)

    def test_ui_demo_imports_no_hardware_camera_or_model_runtime(self):
        forbidden = (
            "hardware.serial_transport",
            "hardware.conveyor",
            "hardware.distributor",
            "vision.camera_manager",
            "vision.vision_cluster",
            "SerialTransport",
            "CameraManager(",
            "VisionCluster(",
        )
        for token in forbidden:
            self.assertNotIn(token, self.source)

    def test_ui_demo_state_and_callbacks_work_without_hardware(self):
        fake_webview = types.ModuleType("webview")
        fake_webview.create_window = lambda **kwargs: None
        fake_webview.start = lambda: None
        with patch.dict(sys.modules, {"webview": fake_webview}):
            module = importlib.import_module("ui_demo")
        demo = module.UiDemo()
        self.assertEqual(len(demo.frames), 7)
        self.assertTrue(demo.controls()["start"])
        self.assertTrue(demo.check_cameras())
        self.assertEqual(demo.diagnostics["status"], "PASSED")
        self.assertTrue(demo.check_vision_rules())
        self.assertTrue(demo.analyze_selected("TOP"))
        self.assertTrue(demo.selected_analysis)
        self.assertTrue(demo.controls()["selected_model_release"])
        self.assertTrue(demo.release_selected())
        self.assertFalse(demo.selected_analysis)
        self.assertTrue(demo.start())
        self.assertEqual(demo.state, "RUNNING")
        self.assertTrue(demo.stop())
        self.assertEqual(demo.state, "STOPPING")

    def test_ui_demo_contains_all_operator_interactions(self):
        for token in (
            "start_callback",
            "stop_callback",
            "distributor_diagnostic_callback",
            "camera_diagnostic_callback",
            "vision_rule_diagnostic_callback",
            "selected_model_analysis_callback",
            "selected_model_release_callback",
            "jog_hold_start_callback",
            "jog_hold_release_callback",
        ):
            self.assertIn(token, self.source)


if __name__ == "__main__":
    unittest.main()
