import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
from unittest.mock import patch

from config.calibration_loader import load_calibration
from config.camera_mapping import REQUIRED_ROLES, load_camera_mapping
from domain.defect_rules import (
    SpiderContactsLongRule,
    SpiderContactsShortRule,
    SpiderShortOmissionRule,
    TopSinksRule,
)
from domain.threshold_loader import (
    INPUT_PART_PRESENCE_PARAMETER_NAMES,
    INPUT_WINDOW_GEOMETRY_PARAMETER_NAMES,
    INPUT_WINDOW_SINK_PARAMETER_NAMES,
    LONG_CONTACT_PARAMETER_NAMES,
    OMISSION_BOUNDARY_SUFFIXES,
    SHORT_CONTACT_PARAMETER_NAMES,
    ThresholdLoader,
)
from vision.camera_manager import CameraManager
from vision.model_config import MODEL_GROUPS


class FakeCapture:
    def __init__(self, frame, opened=True):
        self.frame = frame
        self.opened = opened
        self.released = False

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        return True

    def read(self):
        return True, self.frame.copy()

    def release(self):
        self.released = True


class DelayedExposureCapture(FakeCapture):
    def __init__(self, bright_frame, black_reads):
        super().__init__(bright_frame)
        self.black_frame = np.zeros_like(bright_frame)
        self.black_reads = int(black_reads)
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.reads <= self.black_reads:
            return True, self.black_frame.copy()
        return super().read()


class SilentCapture(FakeCapture):
    """Открывается, но никогда не отдаёт кадр (нет полосы USB)."""

    def read(self):
        return False, None


class BlockingAfterWarmupCapture(FakeCapture):
    def __init__(self, frame, block=False):
        super().__init__(frame)
        self.block = block
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.block and self.reads > 5:
            time.sleep(0.2)
        return super().read()


class CameraAndConfigTests(unittest.TestCase):
    def mapping(self):
        return {role: index for index, role in enumerate(sorted(REQUIRED_ROLES))}

    def write_json(self, root, name, payload):
        path = Path(root) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_model_paths_and_expected_classes_match_current_weights(self):
        configured = {
            entry["path"]: tuple(entry["classes"])
            for entries in MODEL_GROUPS.values()
            for entry in entries
        }
        self.assertEqual(
            configured["weights/2,7/long_omission_v.1.2.pt"],
            ("omission-long",),
        )
        self.assertEqual(
            configured["weights/3,5/short_omission_v.1.2.pt"],
            ("omission-short",),
        )
        self.assertNotIn(
            "weights/3,5/short_omission_v.1.1.pt",
            configured,
        )
        self.assertEqual(configured["weights/4/sinks_v.1_m.pt"], ("shells",))
        self.assertEqual(configured["weights/4/glass_v.1.pt"], ("glass",))
        self.assertEqual(
            configured["weights/4/well_v.1.pt"],
            ("case", "case_central"),
        )
        self.assertNotIn("weights/2,7/long_omission.pt", configured)
        self.assertNotIn("weights/3,5/short_omission_v.1_m.pt", configured)
        self.assertNotIn("weights/4/sinks.pt", configured)
        self.assertNotIn("weights/4/glass.pt", configured)
        self.assertEqual(SpiderShortOmissionRule.TARGET_CLASS, "omission-short")
        self.assertEqual(SpiderContactsLongRule.OMISSION_CLASS, "omission-long")
        self.assertEqual(SpiderContactsShortRule.OMISSION_CLASS, "omission-short")
        self.assertEqual(TopSinksRule.SINK_CLASS, "shells")

    def test_input_thresholds_are_explicit_per_camera_and_use_pixels(self):
        root = Path(__file__).resolve().parents[1]
        thresholds = ThresholdLoader(root / "thresholds.json").get_all()
        for role in ("INPUT_LEFT", "INPUT_RIGHT"):
            for name in (
                *INPUT_PART_PRESENCE_PARAMETER_NAMES,
                *INPUT_WINDOW_GEOMETRY_PARAMETER_NAMES,
                *INPUT_WINDOW_SINK_PARAMETER_NAMES,
            ):
                self.assertIn(f"{role}.{name}", thresholds)
            self.assertEqual(
                thresholds[
                    f"{role}.input_part_presence_false_positive_max_count"
                ],
                2,
            )
            self.assertIn(
                f"{role}.input_window_geometry_top_px_min",
                thresholds,
            )
            self.assertIn(
                f"{role}.input_window_geometry_bottom_px_max",
                thresholds,
            )
            self.assertEqual(
                thresholds[f"{role}.input_window_geometry_top_px_min"], 20,
            )
            self.assertEqual(
                thresholds[f"{role}.input_window_geometry_top_px_max"], 40,
            )
            self.assertEqual(
                thresholds[f"{role}.input_window_geometry_bottom_px_min"], 20,
            )
            self.assertEqual(
                thresholds[f"{role}.input_window_geometry_bottom_px_max"], 40,
            )
        self.assertFalse(any("window_geometry_top_ratio" in key for key in thresholds))
        self.assertFalse(any("window_geometry_bottom_ratio" in key for key in thresholds))
        self.assertNotIn("input_window_geometry_min_confidence", thresholds)
        self.assertNotIn("input_window_sinks_min_confidence", thresholds)

    def test_threshold_file_is_grouped_clean_and_rejects_unknown_keys(self):
        root = Path(__file__).resolve().parents[1]
        threshold_path = root / "thresholds.json"
        source = threshold_path.read_text(encoding="utf-8")
        raw = json.loads(source)
        for role in REQUIRED_ROLES:
            self.assertIn(role, raw)
            self.assertIsInstance(raw[role], dict)
            self.assertFalse(any(
                key.startswith("_comment") for key in raw[role]
            ))
        for role in (
            "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
            "SPIDER_IN", "SPIDER_OUT", "TOP",
        ):
            self.assertIn(f'\n\n    "{role}"', source)
        self.assertFalse(any(key.startswith("_comment") for key in raw))
        loaded = ThresholdLoader(threshold_path).get_all()
        self.assertFalse(any(key.startswith("_comment") for key in loaded))
        self.assertFalse(any("inscribe_shrink" in key for key in loaded))
        self.assertFalse(any("contact_short_mm" in key for key in loaded))
        self.assertFalse(any("contact_long_mm" in key for key in loaded))

        with tempfile.TemporaryDirectory() as temp:
            invalid = dict(raw)
            invalid["TOP"] = dict(raw["TOP"])
            invalid["TOP"]["unused_parameter"] = 1
            path = self.write_json(temp, "thresholds.json", invalid)
            with self.assertRaisesRegex(ValueError, "Лишние или неизвестные"):
                ThresholdLoader(path)

            invalid_boundary = dict(raw)
            invalid_boundary["TOP"] = dict(raw["TOP"])
            invalid_boundary["TOP"][
                "top_platform_overlap_boundary_width_px"
            ] = 149
            path = self.write_json(temp, "invalid_boundary.json", invalid_boundary)
            with self.assertRaisesRegex(ValueError, "не может быть меньше"):
                ThresholdLoader(path)

            invalid_component = dict(raw)
            invalid_component["TOP"] = dict(raw["TOP"])
            invalid_component["TOP"][
                "top_platform_overlap_excess_component_min_px"
            ] = 0
            path = self.write_json(temp, "invalid_component.json", invalid_component)
            with self.assertRaisesRegex(ValueError, "целым числом >= 1"):
                ThresholdLoader(path)

            invalid_presence = dict(raw)
            invalid_presence["INPUT_LEFT"] = dict(raw["INPUT_LEFT"])
            invalid_presence["INPUT_LEFT"][
                "input_part_presence_false_positive_max_count"
            ] = 1.5
            path = self.write_json(
                temp, "invalid_part_presence.json", invalid_presence,
            )
            with self.assertRaisesRegex(ValueError, "целым числом >= 0"):
                ThresholdLoader(path)

    def test_spider_thresholds_are_explicit_for_each_camera(self):
        root = Path(__file__).resolve().parents[1]
        thresholds = ThresholdLoader(root / "thresholds.json").get_all()
        for role in ("SPIDER_LEFT", "SPIDER_RIGHT"):
            for name in LONG_CONTACT_PARAMETER_NAMES:
                self.assertIn(f"{role}.{name}", thresholds)
            self.assertIn(
                f"{role}.spider_long_omission_min_confidence",
                thresholds,
            )
            for suffix in OMISSION_BOUNDARY_SUFFIXES:
                self.assertIn(
                    f"{role}.spider_long_omission_{suffix}",
                    thresholds,
                )
        for role in ("SPIDER_IN", "SPIDER_OUT"):
            for name in SHORT_CONTACT_PARAMETER_NAMES:
                self.assertIn(f"{role}.{name}", thresholds)
            self.assertIn(
                f"{role}.spider_short_omission_min_confidence",
                thresholds,
            )
            for suffix in OMISSION_BOUNDARY_SUFFIXES:
                self.assertIn(
                    f"{role}.spider_short_omission_{suffix}",
                    thresholds,
                )
        for name in (*LONG_CONTACT_PARAMETER_NAMES, *SHORT_CONTACT_PARAMETER_NAMES):
            self.assertNotIn(name, thresholds)
        self.assertFalse(any("omission_max_area_px2" in key for key in thresholds))
        self.assertFalse(any("omission_profile_" in key for key in thresholds))

    def test_contact_and_platform_rectangles_have_independent_width_and_height(self):
        root = Path(__file__).resolve().parents[1]
        thresholds = ThresholdLoader(root / "thresholds.json").get_all()
        expected = {
            "SPIDER_LEFT.spider_contacts_long_inscribed_rect_width_mm": 0.48,
            "SPIDER_LEFT.spider_contacts_long_inscribed_rect_height_mm": 0.36,
            "SPIDER_IN.spider_contacts_short_inscribed_rect_width_mm": 1.74,
            "SPIDER_IN.spider_contacts_short_inscribed_rect_height_mm": 0.66,
            "TOP.top_contacts_side_rect_width_px": 28,
            "TOP.top_contacts_side_rect_height_px": 35,
            "TOP.top_contacts_edge_rect_width_px": 30,
            "TOP.top_contacts_edge_rect_height_px": 28,
            "TOP.top_platform_inscribed_rect_width_px": 260,
            "TOP.top_platform_inscribed_rect_height_px": 120,
            "TOP.top_platform_overlap_boundary_width_px": 305,
            "TOP.top_platform_overlap_boundary_height_px": 140,
            "TOP.top_platform_overlap_excess_component_min_px": 3,
        }
        for key, value in expected.items():
            self.assertAlmostEqual(thresholds[key], value)
        self.assertNotIn(
            "TOP.top_platform_overlap_contacts_min_confidence", thresholds,
        )
        self.assertNotIn("TOP.top_platform_overlap_min_px", thresholds)
        self.assertGreaterEqual(
            thresholds["TOP.top_platform_overlap_boundary_width_px"],
            thresholds["TOP.top_platform_inscribed_rect_width_px"],
        )
        self.assertGreaterEqual(
            thresholds["TOP.top_platform_overlap_boundary_height_px"],
            thresholds["TOP.top_platform_inscribed_rect_height_px"],
        )
        for key in (
            "TOP.top_glass_case_min_confidence",
            "TOP.top_glass_case_central_min_confidence",
            "TOP.top_glass_pin_min_confidence",
        ):
            self.assertIn(key, thresholds)

    def test_camera_manager_rejects_black_frame_before_use_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            black = np.zeros((720, 1280, 3), dtype=np.uint8)
            captures = []
            black_id = mapping["INPUT_RIGHT"]

            def factory(camera_id):
                capture = FakeCapture(black if camera_id == black_id else bright)
                captures.append(capture)
                return capture

            with (
                patch("vision.camera_manager._PREFLIGHT_TIMEOUT", 0.15),
                patch("vision.camera_manager._PREFLIGHT_READ_INTERVAL", 0.01),
                self.assertRaisesRegex(RuntimeError, "near-black"),
            ):
                CameraManager(mapping_path, capture_factory=factory)
            self.assertTrue(captures)
            self.assertTrue(all(capture.released for capture in captures))

    def test_cameras_open_in_parallel_instead_of_one_after_another(self):
        """Камеры открываются волнами, а не строго по очереди.

        Полностью одновременный старт семи камер перегружает USB, но и
        строго последовательный давал бы задержку, линейно растущую с
        числом камер.
        """
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            open_delay = 0.2
            concurrent = []
            in_flight = {"count": 0}
            lock = threading.Lock()

            def factory(camera_id):
                with lock:
                    in_flight["count"] += 1
                    concurrent.append(in_flight["count"])
                # Инициализация драйвера камеры заметно небыстрая.
                time.sleep(open_delay)
                with lock:
                    in_flight["count"] -= 1
                return FakeCapture(bright)

            started = time.monotonic()
            with patch("vision.camera_manager._PREFLIGHT_READ_INTERVAL", 0.001):
                manager = CameraManager(mapping_path, capture_factory=factory)
            elapsed = time.monotonic() - started
            try:
                self.assertEqual(set(manager.cameras), set(REQUIRED_ROLES))
                # Последовательное открытие заняло бы 7 * open_delay.
                self.assertLess(elapsed, open_delay * len(REQUIRED_ROLES) * 0.8)
                self.assertGreater(max(concurrent), 1)
            finally:
                manager.release()

    def test_open_is_throttled_so_usb_bus_is_not_flooded_by_seven_cameras(self):
        """Волны ограничивают число одновременно стартующих камер."""
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            peak = {"value": 0, "current": 0}
            lock = threading.Lock()

            def factory(camera_id):
                with lock:
                    peak["current"] += 1
                    peak["value"] = max(peak["value"], peak["current"])
                time.sleep(0.05)
                with lock:
                    peak["current"] -= 1
                return FakeCapture(bright)

            with (
                patch("vision.camera_manager._PREFLIGHT_READ_INTERVAL", 0.001),
                patch("vision.camera_manager._OPEN_CONCURRENCY", 3),
            ):
                manager = CameraManager(mapping_path, capture_factory=factory)
            try:
                self.assertEqual(set(manager.cameras), set(REQUIRED_ROLES))
                self.assertLessEqual(peak["value"], 3)
            finally:
                manager.release()

    def test_camera_that_needs_a_second_attempt_is_not_declared_broken(self):
        """Разовый отказ USB лечится повтором, а не остановкой запуска."""
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            flaky_id = mapping["TOP"]
            calls = {"count": 0}

            def factory(camera_id):
                if camera_id != flaky_id:
                    return FakeCapture(bright)
                calls["count"] += 1
                # Первая попытка: устройство занято и кадров не отдаёт.
                if calls["count"] == 1:
                    return FakeCapture(bright, opened=False)
                return FakeCapture(bright)

            with (
                patch("vision.camera_manager._PREFLIGHT_READ_INTERVAL", 0.001),
                patch("vision.camera_manager._OPEN_RETRY_DELAY", 0.0),
            ):
                manager = CameraManager(mapping_path, capture_factory=factory)
            try:
                self.assertEqual(set(manager.cameras), set(REQUIRED_ROLES))
                self.assertGreaterEqual(calls["count"], 2)
            finally:
                manager.release()

    def test_persistently_dead_camera_still_fails_startup_with_details(self):
        """Повторы не должны прятать действительно нерабочую камеру."""
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            dead_id = mapping["TOP"]
            captures = []

            def factory(camera_id):
                capture = FakeCapture(
                    bright, opened=camera_id != dead_id,
                )
                captures.append(capture)
                return capture

            with (
                patch("vision.camera_manager._PREFLIGHT_READ_INTERVAL", 0.001),
                patch("vision.camera_manager._OPEN_RETRY_DELAY", 0.0),
                self.assertRaises(RuntimeError) as error,
            ):
                CameraManager(mapping_path, capture_factory=factory)

            message = str(error.exception)
            self.assertIn("TOP", message)
            self.assertIn("попытка", message)
            self.assertTrue(all(capture.released for capture in captures))

    def test_preflight_failure_reports_actionable_diagnostics(self):
        """Сообщение обязано объяснять, почему камера не прошла старт."""
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            silent_id = mapping["TOP"]

            def factory(camera_id):
                if camera_id == silent_id:
                    return SilentCapture(bright)
                return FakeCapture(bright)

            with (
                patch("vision.camera_manager._PREFLIGHT_TIMEOUT", 0.1),
                patch("vision.camera_manager._PREFLIGHT_READ_INTERVAL", 0.01),
                patch("vision.camera_manager._OPEN_RETRY_DELAY", 0.0),
                self.assertRaises(RuntimeError) as error,
            ):
                CameraManager(mapping_path, capture_factory=factory)

            message = str(error.exception)
            self.assertIn("empty_reads=", message)
            self.assertIn("negotiated=", message)

    def test_windows_open_falls_back_to_a_second_backend(self):
        """Камера, молчащая под одним backend, пробуется под другим."""
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            picky_id = mapping["TOP"]
            used_backends = []

            def factory(camera_id, backend=None):
                used_backends.append((camera_id, backend))
                if camera_id == picky_id and backend == "dshow":
                    return SilentCapture(bright)
                return FakeCapture(bright)

            with (
                patch("vision.camera_manager._PREFLIGHT_TIMEOUT", 0.1),
                patch("vision.camera_manager._PREFLIGHT_READ_INTERVAL", 0.01),
                patch("vision.camera_manager._OPEN_RETRY_DELAY", 0.0),
                patch(
                    "vision.camera_manager._default_backends",
                    lambda: ("dshow", "msmf"),
                ),
            ):
                manager = CameraManager(mapping_path, capture_factory=factory)
            try:
                self.assertEqual(set(manager.cameras), set(REQUIRED_ROLES))
                self.assertIn((picky_id, "msmf"), used_backends)
            finally:
                manager.release()

    def test_parallel_open_preserves_mapping_role_order(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)

            def factory(camera_id):
                # Обратный порядок готовности не должен влиять на карту ролей.
                time.sleep(0.02 * (len(mapping) - camera_id))
                return FakeCapture(bright)

            with patch("vision.camera_manager._PREFLIGHT_READ_INTERVAL", 0.001):
                manager = CameraManager(mapping_path, capture_factory=factory)
            try:
                self.assertEqual(list(manager.cameras), list(mapping))
            finally:
                manager.release()

    def test_parallel_open_reports_every_failed_camera_and_releases_all(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            broken_ids = {mapping["TOP"], mapping["SPIDER_IN"]}
            captures = []

            def factory(camera_id):
                capture = FakeCapture(
                    bright, opened=camera_id not in broken_ids
                )
                captures.append(capture)
                return capture

            with (
                patch("vision.camera_manager._PREFLIGHT_READ_INTERVAL", 0.001),
                self.assertRaises(RuntimeError) as error,
            ):
                CameraManager(mapping_path, capture_factory=factory)

            message = str(error.exception)
            self.assertIn("TOP", message)
            self.assertIn("SPIDER_IN", message)
            self.assertTrue(all(capture.released for capture in captures))

    def test_camera_preflight_waits_for_delayed_exposure(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            delayed_id = mapping["TOP"]
            captures = {}

            def factory(camera_id):
                capture = (
                    DelayedExposureCapture(bright, black_reads=8)
                    if camera_id == delayed_id
                    else FakeCapture(bright)
                )
                captures[camera_id] = capture
                return capture

            with (
                patch("vision.camera_manager._PREFLIGHT_TIMEOUT", 1.0),
                patch("vision.camera_manager._PREFLIGHT_READ_INTERVAL", 0.01),
            ):
                manager = CameraManager(
                    mapping_path,
                    capture_factory=factory,
                )
            try:
                self.assertGreaterEqual(captures[delayed_id].reads, 13)
                self.assertEqual(set(manager.cameras), set(REQUIRED_ROLES))
            finally:
                manager.release()

    def test_camera_manager_returns_exact_seven_role_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            manager = CameraManager(
                mapping_path,
                capture_factory=lambda camera_id: FakeCapture(bright),
            )
            try:
                frames = manager.capture_all()
                self.assertEqual(set(frames), set(REQUIRED_ROLES))
                self.assertTrue(
                    all(frame.shape == (720, 1280, 3) for frame in frames.values())
                )
            finally:
                manager.release()

    def test_selected_camera_read_is_not_blocked_by_other_camera_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            slow_id = mapping["INPUT_LEFT"]
            manager = CameraManager(
                mapping_path,
                capture_factory=lambda camera_id: BlockingAfterWarmupCapture(
                    bright,
                    block=camera_id == slow_id,
                ),
            )
            errors = []

            def capture_slow_role():
                try:
                    manager.capture_roles(("INPUT_LEFT",))
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(
                target=capture_slow_role,
                daemon=True,
            )
            worker.start()
            time.sleep(0.02)
            started = time.monotonic()
            try:
                frame = manager.capture_single("TOP")
                self.assertEqual(frame.shape, (720, 1280, 3))
                self.assertLess(time.monotonic() - started, 0.1)
                worker.join(1.0)
                self.assertFalse(worker.is_alive())
            except Exception as exc:
                errors.append(exc)
            finally:
                manager.release()
            self.assertEqual(errors, [])

    def test_camera_timeout_latches_manager_and_blocks_overlapping_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            blocked_id = mapping["TOP"]
            manager = CameraManager(
                mapping_path,
                capture_factory=lambda camera_id: BlockingAfterWarmupCapture(
                    bright,
                    block=camera_id == blocked_id,
                ),
            )
            try:
                with (
                    patch("vision.camera_manager._CAPTURE_TIMEOUT", 0.05),
                    self.assertRaisesRegex(RuntimeError, "capture timeout"),
                ):
                    manager.capture_all()
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "заблокирован"):
                    manager.capture_all()
                self.assertLess(time.monotonic() - started, 0.05)
                time.sleep(0.2)
            finally:
                manager.release()

    def test_capture_timeout_does_not_race_with_late_worker_writes(self):
        """Итог захвата не должен зависеть от опоздавших воркеров.

        Раньше таймаут дописывал ошибки в общий словарь без блокировки,
        пока рабочие потоки продолжали писать туда же.
        """
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            manager = CameraManager(
                mapping_path,
                capture_factory=lambda camera_id: BlockingAfterWarmupCapture(
                    bright, block=True,
                ),
            )
            try:
                with (
                    patch("vision.camera_manager._CAPTURE_TIMEOUT", 0.05),
                    self.assertRaisesRegex(RuntimeError, "capture timeout"),
                ):
                    manager.capture_all()
                # Опоздавшие воркеры завершаются уже после исключения и не
                # должны ничего менять в уже отданном результате.
                time.sleep(0.4)
                with self.assertRaisesRegex(RuntimeError, "заблокирован"):
                    manager.capture_all()
            finally:
                manager.release()

    def test_release_does_not_block_on_a_stuck_camera_read(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping_path = self.write_json(temp, "camera_mapping.json", mapping)
            bright = np.full((720, 1280, 3), 100, dtype=np.uint8)
            manager = CameraManager(
                mapping_path,
                capture_factory=lambda camera_id: BlockingAfterWarmupCapture(
                    bright, block=True,
                ),
            )
            with (
                patch("vision.camera_manager._CAPTURE_TIMEOUT", 0.05),
                self.assertRaises(RuntimeError),
            ):
                manager.capture_all()

            started = time.monotonic()
            manager.release()
            self.assertLess(time.monotonic() - started, 0.5)

    def test_camera_mapping_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = self.mapping()
            mapping["TOP"] = mapping["INPUT_LEFT"]
            path = self.write_json(temp, "mapping.json", mapping)
            with self.assertRaisesRegex(ValueError, "уникальными"):
                load_camera_mapping(path)

    def test_calibration_never_falls_back_after_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "calibration.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Ошибка чтения"):
                load_calibration(path)
            path.write_text(json.dumps({"conveyor_speed": 1}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing"):
                load_calibration(path)

    def test_settle_time_is_optional_and_validated(self):
        """Старые calibration.json без settle_time должны читаться."""
        from config.calibration_loader import DEFAULTS

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "calibration.json"

            path.write_text(json.dumps(DEFAULTS), encoding="utf-8")
            self.assertEqual(load_calibration(path)["settle_time"], 0.5)
            self.assertEqual(load_calibration(path)["stage_trace_time"], 0.5)

            path.write_text(
                json.dumps({**DEFAULTS, "settle_time": 0.4}), encoding="utf-8"
            )
            self.assertEqual(load_calibration(path)["settle_time"], 0.4)

            path.write_text(
                json.dumps({**DEFAULTS, "settle_time": 99.0}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "settle_time"):
                load_calibration(path)


if __name__ == "__main__":
    unittest.main()
