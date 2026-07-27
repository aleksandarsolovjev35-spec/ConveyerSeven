import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from config.camera_mapping import load_camera_mapping
from tools.camera_calibration_console import (
    ROLE_ORDER,
    CameraCalibrationApi,
    atomic_write_mapping,
    detect_available_cameras,
    launch_camera_calibrator,
)


class FakeCapture:
    def __init__(self, frame=None, opened=True):
        self.frame = frame
        self.opened = opened
        self.released = False
        self.settings = []

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.settings.append((prop, value))
        return True

    def read(self):
        if not self.opened or self.frame is None:
            return False, None
        return True, self.frame.copy()

    def release(self):
        self.released = True


class FlakyPreviewCapture(FakeCapture):
    def __init__(self, frame):
        super().__init__(frame=frame, opened=True)
        self.read_count = 0

    def read(self):
        self.read_count += 1
        # Первый кадр проходит scan. Два следующих preview-read временно
        # неуспешны, после чего камера снова возвращает валидное изображение.
        if self.read_count in (2, 3):
            return False, None
        return super().read()


class CaptureFactory:
    def __init__(self, available_ids):
        self.available_ids = set(available_ids)
        self.instances = []
        self.frame = np.full((720, 1280, 3), 110, dtype=np.uint8)

    def __call__(self, camera_id):
        capture = FakeCapture(
            self.frame if camera_id in self.available_ids else None,
            opened=camera_id in self.available_ids,
        )
        self.instances.append((camera_id, capture))
        return capture


class CameraCalibratorTests(unittest.TestCase):
    def test_detector_keeps_only_open_valid_production_cameras(self):
        factory = CaptureFactory({0, 2, 4, 6, 8, 10, 12})
        found = detect_available_cameras(14, capture_factory=factory)
        self.assertEqual(found, [0, 2, 4, 6, 8, 10, 12])
        self.assertTrue(all(capture.released for _, capture in factory.instances))

    def test_transient_preview_read_does_not_abort_calibration(self):
        frame = np.full((720, 1280, 3), 110, dtype=np.uint8)
        captures = {}

        def factory(camera_id):
            capture = FlakyPreviewCapture(frame)
            captures[camera_id] = capture
            return capture

        api = CameraCalibrationApi(
            "unused.json",
            scan_limit=7,
            capture_factory=factory,
        )
        self.assertEqual(api.scan()["status"], "READY")
        preview = api.get_frame()
        self.assertTrue(preview["ok"])
        self.assertEqual(api.get_state()["status"], "READY")
        self.assertEqual(captures[0].read_count, 4)
        api.shutdown()

    def test_less_than_seven_cameras_blocks_calibration_without_json(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "camera_mapping.json"
            factory = CaptureFactory(range(6))
            api = CameraCalibrationApi(
                destination,
                scan_limit=8,
                capture_factory=factory,
            )
            state = api.scan()
            self.assertEqual(state["status"], "ERROR")
            self.assertEqual(state["found"], 6)
            self.assertIn("6/7", state["error"])
            self.assertFalse(destination.exists())
            self.assertFalse(api.saved)
            self.assertTrue(all(
                capture.released for _, capture in factory.instances
            ))
            api.shutdown()

    def test_step_wizard_saves_only_complete_unique_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "camera_mapping.json"
            factory = CaptureFactory(range(7))
            api = CameraCalibrationApi(
                destination,
                scan_limit=9,
                capture_factory=factory,
            )
            state = api.scan()
            self.assertEqual(state["status"], "READY")
            self.assertFalse(destination.exists())
            self.assertEqual(len(factory.instances), 7)
            self.assertTrue(all(
                not capture.released for _, capture in factory.instances
            ))

            for index, role in enumerate(ROLE_ORDER):
                state = api.get_state()
                self.assertEqual(state["current_role"], role)
                self.assertEqual(state["current_camera_id"], index)
                preview = api.get_frame()
                self.assertTrue(preview["ok"])
                self.assertEqual(preview["camera_id"], index)
                state = api.assign_current()
                self.assertFalse(destination.exists())
                self.assertEqual(
                    len(factory.instances),
                    7,
                    "переключение не должно повторно открывать камеры",
                )

            self.assertEqual(state["status"], "REVIEW")
            self.assertEqual(len(state["assignments"]), 7)
            saved = api.save()
            self.assertEqual(saved["status"], "SAVED")
            self.assertTrue(saved["saved"])
            self.assertTrue(destination.exists())
            mapping = load_camera_mapping(destination)
            self.assertEqual(list(mapping), list(ROLE_ORDER))
            self.assertEqual(list(mapping.values()), list(range(7)))
            self.assertTrue(all(
                capture.released for _, capture in factory.instances
            ))
            api.shutdown()

    def test_assignment_requires_visible_live_preview(self):
        api = CameraCalibrationApi(
            "unused.json",
            scan_limit=8,
            capture_factory=CaptureFactory(range(7)),
        )
        api.scan()
        with self.assertRaisesRegex(RuntimeError, "живого кадра"):
            api.assign_current()
        api.shutdown()

    def test_back_releases_previous_role_and_restores_its_camera(self):
        api = CameraCalibrationApi(
            "unused.json",
            scan_limit=8,
            capture_factory=CaptureFactory(range(7)),
        )
        api.scan()
        self.assertTrue(api.get_frame()["ok"])
        api.assign_current()
        self.assertEqual(api.get_state()["current_role"], ROLE_ORDER[1])
        state = api.back()
        self.assertEqual(state["current_role"], ROLE_ORDER[0])
        self.assertEqual(state["current_camera_id"], 0)
        self.assertEqual(state["assignments"], {})
        api.shutdown()

    def test_atomic_mapping_never_accepts_partial_or_duplicate_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "camera_mapping.json"
            with self.assertRaisesRegex(ValueError, "mismatch"):
                atomic_write_mapping(destination, {"TOP": 0})
            self.assertFalse(destination.exists())

            duplicate = {role: 0 for role in ROLE_ORDER}
            with self.assertRaisesRegex(ValueError, "уникальными"):
                atomic_write_mapping(destination, duplicate)
            self.assertFalse(destination.exists())

    def test_parent_launcher_validates_child_result_before_main_continues(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "camera_mapping.json"
            calls = []

            def successful_runner(command, cwd, check):
                calls.append((command, cwd, check))
                mapping = {role: index for index, role in enumerate(ROLE_ORDER)}
                atomic_write_mapping(destination, mapping)
                return SimpleNamespace(returncode=0)

            self.assertTrue(launch_camera_calibrator(
                destination,
                scan_limit=12,
                runner=successful_runner,
            ))
            self.assertEqual(len(calls), 1)
            command, _cwd, check = calls[0]
            self.assertIn("tools.camera_calibration_console", command)
            self.assertIn("--scan-limit", command)
            self.assertFalse(check)

        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "camera_mapping.json"
            self.assertFalse(launch_camera_calibrator(
                destination,
                runner=lambda *args, **kwargs: SimpleNamespace(returncode=1),
            ))
            self.assertFalse(destination.exists())

    def test_calibrator_is_automatic_windowed_dark_hmi(self):
        root = Path(__file__).resolve().parents[1]
        main_source = (root / "main.py").read_text(encoding="utf-8")
        calibrator_source = (
            root / "tools.camera_calibration_console.py"
        ).read_text(encoding="utf-8")
        html = (
            root / "vision/ui/calibration/index.html"
        ).read_text(encoding="utf-8")
        css = (
            root / "vision/ui/calibration/calibration.css"
        ).read_text(encoding="utf-8")
        javascript = (
            root / "vision/ui/calibration/calibration.js"
        ).read_text(encoding="utf-8")

        launch_index = main_source.index("launch_camera_calibrator(")
        monitor_index = main_source.index("monitor = LiveMonitor(")
        self.assertLess(launch_index, monitor_index)
        self.assertNotIn("input(", main_source)
        self.assertIn('fullscreen=False', calibrator_source)
        self.assertIn('width=1280', calibrator_source)
        self.assertIn('../static/css/base.css', html)
        self.assertIn('КАЛИБРОВКА КАМЕР', html)
        self.assertIn('СОХРАНИТЬ И ПРОДОЛЖИТЬ', html)
        self.assertIn('camera_mapping.json создаётся только после', html)
        self.assertIn('var(--bg-0)', css)
        self.assertNotIn('gradient(', css)
        self.assertIn('api().assign_current()', javascript)
        self.assertIn('api().save()', javascript)
        self.assertNotIn('retry', javascript.lower())


if __name__ == "__main__":
    unittest.main()
