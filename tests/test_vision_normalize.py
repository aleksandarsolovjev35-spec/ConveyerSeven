import os
import sys
import types
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from vision.normalize import (
    normalize_enabled,
    normalize_for_role,
    normalize_frame,
)


def _fake_vision_module():
    """Импортировать vision.vision_cluster с фейковым ultralytics."""
    fake_ultralytics = types.ModuleType("ultralytics")
    fake_ultralytics.YOLO = object
    with patch.dict(sys.modules, {"ultralytics": fake_ultralytics}):
        import vision.vision_cluster as module
    return module


def _low_contrast_frame():
    """Тёмный низкоконтрастный кадр — типичный результат смены света."""
    frame = np.full((720, 1280, 3), 70, dtype=np.uint8)
    ramp = (np.arange(1280, dtype=np.uint16) // 64).astype(np.uint8)
    frame[:] += ramp[:, None]
    return frame


class NormalizeFrameTests(unittest.TestCase):

    def test_preserves_dtype_and_shape(self):
        frame = _low_contrast_frame()
        out = normalize_frame(frame)
        self.assertEqual(out.dtype, np.uint8)
        self.assertEqual(out.shape, frame.shape)

    def test_increases_local_contrast(self):
        frame = _low_contrast_frame()
        out = normalize_frame(frame)
        lab_in = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        lab_out = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
        std_in = float(np.std(lab_in[:, :, 0]))
        std_out = float(np.std(lab_out[:, :, 0]))
        self.assertGreater(std_out, std_in)

    def test_constant_frame_returns_without_error(self):
        frame = np.full((720, 1280, 3), 100, dtype=np.uint8)
        out = normalize_frame(frame)
        self.assertEqual(out.shape, frame.shape)

    def test_bad_input_returns_frame_unchanged(self):
        frame = np.zeros((720, 1280, 3), dtype=np.float32)
        self.assertIs(normalize_frame(frame), frame)
        gray = np.zeros((720, 1280), dtype=np.uint8)
        self.assertIs(normalize_frame(gray), gray)


class NormalizeEnabledTests(unittest.TestCase):

    def tearDown(self):
        for key in (
            "VISION_NORMALIZE",
            "VISION_NORMALIZE_ROLES",
            "VISION_NORMALIZE_CLIP_LIMIT",
            "VISION_NORMALIZE_TILE",
        ):
            os.environ.pop(key, None)

    def test_disabled_by_default(self):
        os.environ.pop("VISION_NORMALIZE", None)
        frame = _low_contrast_frame()
        self.assertIs(normalize_for_role(frame, "SPIDER_IN"), frame)

    def test_enabled_applies_clahe(self):
        os.environ["VISION_NORMALIZE"] = "1"
        frame = _low_contrast_frame()
        out = normalize_for_role(frame, "SPIDER_IN")
        self.assertIsNot(out, frame)
        self.assertFalse(np.array_equal(out, frame))

    def test_roles_filter(self):
        os.environ["VISION_NORMALIZE"] = "1"
        os.environ["VISION_NORMALIZE_ROLES"] = "SPIDER_IN, SPIDER_OUT"
        frame = _low_contrast_frame()
        self.assertIsNot(normalize_for_role(frame, "SPIDER_IN"), frame)
        self.assertIsNot(normalize_for_role(frame, "SPIDER_OUT"), frame)
        self.assertIs(normalize_for_role(frame, "TOP"), frame)

    def test_flag_value_normalization(self):
        os.environ["VISION_NORMALIZE"] = "true"
        self.assertTrue(normalize_enabled("TOP"))
        os.environ["VISION_NORMALIZE"] = "0"
        self.assertFalse(normalize_enabled("TOP"))


class VisionClusterIntegrationTests(unittest.TestCase):

    def tearDown(self):
        for key in ("VISION_NORMALIZE", "VISION_CONF_omission-short"):
            os.environ.pop(key, None)

    def _cluster_with_recording_model(self):
        module = _fake_vision_module()

        class SimpleResult:
            names = {}
            boxes = None
            masks = None

        class RecordingModel:
            def __init__(self):
                self.calls = []

            def predict(self, frame, **kwargs):
                self.calls.append((frame, kwargs))
                return [SimpleResult()]

        model = RecordingModel()
        cluster = module.VisionCluster.__new__(module.VisionCluster)
        cluster.device = "cpu"
        cluster.verbose = False
        cluster.models = {"model.pt": model}
        cluster.last_health = []
        return module, cluster, model

    def test_normalization_is_not_applied_by_default(self):
        module, cluster, model = self._cluster_with_recording_model()
        frame = _low_contrast_frame()
        with (
            patch.object(
                module,
                "MODEL_GROUPS",
                {"GROUP": [{"path": "model.pt", "conf": 0.1}]},
            ),
            patch.object(module, "ROLE_TO_GROUP", {"SPIDER_IN": "GROUP"}),
        ):
            cluster.process_all({"SPIDER_IN": frame})
        seen_frame, _ = model.calls[0]
        self.assertIs(seen_frame, frame)

    def test_normalization_applied_when_enabled(self):
        os.environ["VISION_NORMALIZE"] = "1"
        module, cluster, model = self._cluster_with_recording_model()
        frame = _low_contrast_frame()
        with (
            patch.object(
                module,
                "MODEL_GROUPS",
                {"GROUP": [{"path": "model.pt", "conf": 0.1}]},
            ),
            patch.object(module, "ROLE_TO_GROUP", {"SPIDER_IN": "GROUP"}),
        ):
            cluster.process_all({"SPIDER_IN": frame})
        seen_frame, _ = model.calls[0]
        self.assertIsNot(seen_frame, frame)
        self.assertFalse(np.array_equal(seen_frame, frame))

    def test_conf_env_override(self):
        os.environ["VISION_CONF_omission-short"] = "0.2"
        module, cluster, model = self._cluster_with_recording_model()
        frame = np.ones((720, 1280, 3), dtype=np.uint8)
        with (
            patch.object(
                module,
                "MODEL_GROUPS",
                {"GROUP": [{
                    "path": "model.pt",
                    "conf": 0.4,
                    "classes": ("omission-short",),
                }]},
            ),
            patch.object(module, "ROLE_TO_GROUP", {"SPIDER_IN": "GROUP"}),
        ):
            cluster.process_all({"SPIDER_IN": frame})
        _, kwargs = model.calls[0]
        self.assertEqual(kwargs["conf"], 0.2)

    def test_conf_env_invalid_value_falls_back_to_base(self):
        os.environ["VISION_CONF_omission-short"] = "abc"
        module, cluster, model = self._cluster_with_recording_model()
        frame = np.ones((720, 1280, 3), dtype=np.uint8)
        with (
            patch.object(
                module,
                "MODEL_GROUPS",
                {"GROUP": [{
                    "path": "model.pt",
                    "conf": 0.4,
                    "classes": ("omission-short",),
                }]},
            ),
            patch.object(module, "ROLE_TO_GROUP", {"SPIDER_IN": "GROUP"}),
        ):
            cluster.process_all({"SPIDER_IN": frame})
        _, kwargs = model.calls[0]
        self.assertEqual(kwargs["conf"], 0.4)


if __name__ == "__main__":
    unittest.main()
