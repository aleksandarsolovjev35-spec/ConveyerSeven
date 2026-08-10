"""Тесты калибратора камер: сканирование, диагностика и backend-перебор."""

import importlib
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from config.camera_mapping import REQUIRED_ROLES, load_camera_mapping
from vision.camera_calibration_console import (
    ROLE_ORDER,
    CameraCalibrationApi,
    _open_preview_pool,
    _probe_camera,
)

MODULE = "vision.camera_calibration_console"

GOOD_FRAME = np.full((720, 1280, 3), 128, dtype=np.uint8)
BAD_SIZE_FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


class FakeCapture:
    """Минимальный VideoCapture-двойник для сканирования без железа."""

    def __init__(self, frame, opened=True):
        self.frame = frame
        self.opened = bool(opened)
        self.released = False

    def isOpened(self):
        return self.opened

    def read(self):
        if not self.opened or self.frame is None:
            return False, None
        return True, self.frame

    def set(self, prop, value):
        return True

    def get(self, prop):
        try:
            if prop == cv2.CAP_PROP_FOURCC:
                return float(cv2.VideoWriter_fourcc(*"MJPG"))
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                return 1280.0
            if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                return 720.0
            if prop == cv2.CAP_PROP_FPS:
                return 30.0
        except Exception:
            pass
        return 0.0

    def release(self):
        self.released = True


def make_factory(working_ids, frame=GOOD_FRAME):
    """Фабрика, где работают только перечисленные Camera ID."""

    def factory(camera_id, backend=None):
        if camera_id in working_ids:
            return FakeCapture(frame)
        return FakeCapture(None, opened=False)

    return factory


class CameraCalibrationTestBase(unittest.TestCase):

    def setUp(self):
        self.module = importlib.import_module(MODULE)
        self._originals = {}
        # Ускоряем сканирование: без пауз и с минимумом повторных чтений.
        for name, value in {
            "PROBE_READ_INTERVAL": 0.0,
            "SCAN_PROBE_ATTEMPTS": 2,
            "SCAN_OPEN_ATTEMPTS": 1,
            "SCAN_RETRY_DELAY": 0.0,
        }.items():
            self._originals[name] = getattr(self.module, name)
            setattr(self.module, name, value)
        self.addCleanup(self._restore_module)

    def _restore_module(self):
        for name, value in self._originals.items():
            setattr(self.module, name, value)

    def _config_path(self):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        )
        handle.close()
        path = Path(handle.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return str(path)

    def _api(self, working_ids, scan_limit=10):
        return CameraCalibrationApi(
            self._config_path(),
            scan_limit=scan_limit,
            capture_factory=make_factory(working_ids),
        )


class ScanFlowTest(CameraCalibrationTestBase):

    def test_full_scan_ready_with_seven_available(self):
        """Полный комплект 7/7 после сканирования — READY, роли не заняты."""
        api = self._api(list(range(7)))
        try:
            state = api.scan()
            self.assertEqual(state["status"], "READY")
            self.assertEqual(state["found"], 7)
            self.assertEqual(state["available_camera_ids"], [0, 1, 2, 3, 4, 5, 6])
            self.assertEqual(state["assignments"], {})
            self.assertEqual(state["step"], 1)
        finally:
            api.shutdown()

    def test_scan_with_extra_cameras_keeps_seven_available(self):
        """Лишние исправные камеры не мешают: доступны первые 7 по порядку."""
        api = self._api([0, 1, 2, 3, 4, 6, 7, 9])
        try:
            state = api.scan()
            self.assertEqual(state["status"], "READY")
            self.assertEqual(
                state["available_camera_ids"],
                [0, 1, 2, 3, 4, 6, 7],
            )
        finally:
            api.shutdown()

    def test_insufficient_cameras_reports_per_id_diagnostics(self):
        """Меньше 7 камер — ERROR с поимённым списком причин отказа."""
        api = self._api([0, 1, 2, 3, 4])
        try:
            state = api.scan()
            self.assertEqual(state["status"], "ERROR")
            self.assertIn("5/7", state["error"])
            self.assertIn("Camera ID 5", state["error"])
            self.assertIn("Camera ID 9", state["error"])
            self.assertIn("устройство не открылось", state["error"])
            self.assertEqual(state["assignments"], {})
        finally:
            api.shutdown()

    def test_cameras_lost_together_report_usb(self):
        """Поодиночке исправны, вместе — нет: сигнал про USB-шину."""

        opened_count = {}

        def factory(camera_id, backend=None):
            # Первое открытие (фаза 1) успешно, повторное (фаза 2) — нет:
            # имитация нехватки изохронной полосы при одновременном старте.
            opened_count[camera_id] = opened_count.get(camera_id, 0) + 1
            if opened_count[camera_id] > 1:
                return FakeCapture(None, opened=False)
            return FakeCapture(GOOD_FRAME)

        api = CameraCalibrationApi(
            self._config_path(),
            scan_limit=10,
            capture_factory=factory,
        )
        try:
            state = api.scan()
            self.assertEqual(state["status"], "ERROR")
            self.assertIn("0/7", state["error"])
            self.assertIn("USB", state["error"])
            self.assertIn("Camera ID 0", state["error"])
        finally:
            api.shutdown()


class ManualAssignTest(CameraCalibrationTestBase):

    def test_manual_assignment_cycle(self):
        """Ручной сценарий: 7 назначений → REVIEW → шаг назад по ролям."""
        api = self._api(list(range(7)))
        try:
            with api.lock:
                api.status = "READY"
                api.available_cameras = [0, 1, 2, 3, 4, 5, 6]
                api.candidate_index = 0
            for expected_id in range(7):
                with api.lock:
                    api._preview_verified_id = api._free_cameras_locked()[
                        api.candidate_index
                    ]
                state = api.assign_current()
            self.assertEqual(state["status"], "REVIEW")
            self.assertEqual(
                [state["assignments"][role] for role in ROLE_ORDER],
                [0, 1, 2, 3, 4, 5, 6],
            )

            # ИЗМЕНИТЬ ПОСЛЕДНЮЮ: шаг назад возвращает роль TOP в READY.
            state = api.back()
            self.assertEqual(state["status"], "READY")
            self.assertEqual(state["step"], 7)
            self.assertNotIn("TOP", state["assignments"])
            self.assertIn(6, state["free_camera_ids"])
        finally:
            api.shutdown()

    def test_back_from_review_keeps_earlier_assignments(self):
        """Откат из REVIEW затирает только последнюю роль."""
        api = self._api(list(range(7)))
        try:
            with api.lock:
                api.status = "REVIEW"
                api.role_index = 7
                api.assignments = {
                    role: index for index, role in enumerate(ROLE_ORDER)
                }
            state = api.back()
            self.assertEqual(state["status"], "READY")
            self.assertEqual(
                set(state["assignments"]),
                set(ROLE_ORDER[:-1]),
            )
            self.assertEqual(state["assignments"]["INPUT_LEFT"], 0)
        finally:
            api.shutdown()


class PreviewPoolTest(CameraCalibrationTestBase):

    def test_pool_opens_all_cameras_by_waves(self):
        """Фаза 2 волнами открывает весь комплект и держит его открытым."""
        factory = make_factory(list(range(7)))
        pool, lost = _open_preview_pool(list(range(7)), 7, factory)
        try:
            self.assertEqual(lost, {})
            self.assertEqual(sorted(pool), [0, 1, 2, 3, 4, 5, 6])
            for capture in pool.values():
                self.assertTrue(capture.isOpened())
        finally:
            for capture in pool.values():
                capture.release()

    def test_pool_reports_lost_cameras(self):
        """Камера, не открывшаяся в фазе 2, попадает в lost с причиной."""
        factory = make_factory([0, 1, 2, 3, 4, 5])  # ID 6 не работает
        pool, lost = _open_preview_pool(list(range(7)), 7, factory)
        try:
            self.assertEqual(len(pool), 6)
            self.assertIn(6, lost)
        finally:
            for capture in pool.values():
                capture.release()


class ProbeBackendTest(CameraCalibrationTestBase):

    def _patch_backends(self, backends):
        original = self.module._camera_backends
        self.module._camera_backends = lambda: backends
        self.addCleanup(setattr, self.module, "_camera_backends", original)

    def test_probe_camera_tries_next_backend(self):
        """Камера, молчащая под одним backend-ом, ищется через другой."""
        self._patch_backends(("DSHOW", "MSMF"))

        def factory(camera_id, backend=None):
            if backend == "DSHOW":
                return FakeCapture(BAD_SIZE_FRAME)  # открылась, но кадр битый
            return FakeCapture(GOOD_FRAME)

        opened, error = _probe_camera(0, factory)
        self.assertIsNone(error)
        self.assertIsNone(opened)  # keep=False: handle освобождён

    def test_probe_camera_reports_failure_when_all_backends_bad(self):
        self._patch_backends(("DSHOW", "MSMF"))

        def factory(camera_id, backend=None):
            return FakeCapture(BAD_SIZE_FRAME)

        opened, error = _probe_camera(0, factory)
        self.assertIsNone(opened)
        self.assertIsNotNone(error)
        self.assertIn("разрешение", error)


if __name__ == "__main__":
    unittest.main()
