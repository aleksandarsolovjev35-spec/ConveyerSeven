import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from inspection.inspector import Inspector
from inspection.part_archive import PartArchive


class FakeDecision:
    thresholds = {
        "INPUT_LEFT.input_window_geometry_min_confidence": 0.4,
        "INPUT_RIGHT.input_window_geometry_min_confidence": 0.4,
        "INPUT_LEFT.input_part_presence_false_positive_max_count": 2,
        "INPUT_RIGHT.input_part_presence_false_positive_max_count": 2,
    }

    def evaluate_all_detailed(self, vision_results, frames=None):
        return []


class FakeVision:
    def process_all(self, frames):
        return {role: [] for role in frames}


class FakeRecorder:
    def process(self, **kwargs):
        return {}


class InspectionVisionArchiveTests(unittest.TestCase):
    def test_inspector_rejects_missing_mandatory_camera_role(self):
        inspector = Inspector(FakeVision(), FakeDecision(), FakeRecorder())
        with self.assertRaisesRegex(RuntimeError, "INPUT_RIGHT"):
            inspector.inspect_input(
                part_id=1,
                step=0,
                frames={"INPUT_LEFT": np.ones((10, 10, 3), dtype=np.uint8)},
            )

    def test_inspector_requires_three_hits_on_both_input_cameras(self):
        class PresenceVision:
            def __init__(self, left_count, right_count):
                self.left_count = left_count
                self.right_count = right_count

            @staticmethod
            def _detections(count):
                return [
                    {
                        "class": "flatness",
                        "confidence": 0.9,
                        "bbox": [1 + index, 1, 5 + index, 5],
                        "mask": [
                            [1 + index, 1], [5 + index, 1],
                            [5 + index, 5], [1 + index, 5],
                        ],
                    }
                    for index in range(count)
                ]

            def process_all(self, frames):
                return {
                    "INPUT_LEFT": self._detections(self.left_count),
                    "INPUT_RIGHT": self._detections(self.right_count),
                }

        frames = {
            "INPUT_LEFT": np.ones((20, 20, 3), dtype=np.uint8) * 100,
            "INPUT_RIGHT": np.ones((20, 20, 3), dtype=np.uint8) * 100,
        }
        empty = Inspector(
            PresenceVision(2, 2), FakeDecision(), FakeRecorder(),
        ).inspect_input(part_id=1, step=1, frames=frames)
        self.assertTrue(empty.is_empty_tray)
        self.assertEqual(len(empty.rule_results), 1)
        self.assertEqual(empty.rule_results[0].rule_name, "part_presence")
        self.assertEqual(
            empty.rule_results[0].details["false_positive_ignored_left"],
            2,
        )

        one_camera = Inspector(
            PresenceVision(3, 0), FakeDecision(), FakeRecorder(),
        ).inspect_input(part_id=2, step=2, frames=frames)
        self.assertTrue(one_camera.is_empty_tray)

        present = Inspector(
            PresenceVision(3, 3), FakeDecision(), FakeRecorder(),
        ).inspect_input(part_id=3, step=3, frames=frames)
        self.assertFalse(present.is_empty_tray)

    def test_model_class_contract_rejects_wrong_weight_file(self):
        fake_ultralytics = types.ModuleType("ultralytics")
        fake_ultralytics.YOLO = object
        with patch.dict(sys.modules, {"ultralytics": fake_ultralytics}):
            import vision.vision_cluster as module

        good = types.SimpleNamespace(names={0: "shells"})
        module.VisionCluster._verify_model_classes(
            "model.pt", good, ("shells",),
        )
        wrong = types.SimpleNamespace(names={0: "sinks"})
        with self.assertRaisesRegex(RuntimeError, "Model class mismatch"):
            module.VisionCluster._verify_model_classes(
                "model.pt", wrong, ("shells",),
            )

    def test_model_error_is_not_converted_to_empty_detections(self):
        fake_ultralytics = types.ModuleType("ultralytics")
        fake_ultralytics.YOLO = object
        with patch.dict(sys.modules, {"ultralytics": fake_ultralytics}):
            import vision.vision_cluster as module

        class FailingModel:
            def predict(self, *args, **kwargs):
                raise RuntimeError("inference failed")

        cluster = module.VisionCluster.__new__(module.VisionCluster)
        cluster.device = "cpu"
        cluster.verbose = False
        cluster.models = {"model.pt": FailingModel()}
        frame = np.ones((720, 1280, 3), dtype=np.uint8)
        with (
            patch.object(
                module,
                "MODEL_GROUPS",
                {"GROUP": [{"path": "model.pt", "conf": 0.1}]},
            ),
            patch.object(module, "ROLE_TO_GROUP", {"TOP": "GROUP"}),
            self.assertRaisesRegex(RuntimeError, "Model inference failed"),
        ):
            cluster.process_all({"TOP": frame})
        self.assertEqual(len(cluster.last_health), 1)
        self.assertFalse(cluster.last_health[0]["ok"])
        self.assertIn("inference failed", cluster.last_health[0]["error"])

    def test_model_health_records_success_latency_and_zero_detections(self):
        fake_ultralytics = types.ModuleType("ultralytics")
        fake_ultralytics.YOLO = object
        with patch.dict(sys.modules, {"ultralytics": fake_ultralytics}):
            import vision.vision_cluster as module

        class SuccessfulModel:
            def predict(self, *args, **kwargs):
                return [SimpleResult()]

        class SimpleResult:
            names = {}
            boxes = None
            masks = None

        cluster = module.VisionCluster.__new__(module.VisionCluster)
        cluster.device = "cpu"
        cluster.verbose = False
        cluster.models = {"model.pt": SuccessfulModel()}
        cluster.last_health = []
        frame = np.ones((720, 1280, 3), dtype=np.uint8)
        with (
            patch.object(
                module,
                "MODEL_GROUPS",
                {"GROUP": [{"path": "model.pt", "conf": 0.1}]},
            ),
            patch.object(module, "ROLE_TO_GROUP", {"TOP": "GROUP"}),
        ):
            result = cluster.process_all({"TOP": frame})
        self.assertEqual(result, {"TOP": []})
        self.assertEqual(len(cluster.last_health), 1)
        self.assertTrue(cluster.last_health[0]["ok"])
        self.assertEqual(cluster.last_health[0]["detections"], 0)
        self.assertGreaterEqual(cluster.last_health[0]["elapsed_ms"], 0)

    def test_warmup_failure_is_fatal(self):
        fake_ultralytics = types.ModuleType("ultralytics")
        fake_ultralytics.YOLO = object
        with patch.dict(sys.modules, {"ultralytics": fake_ultralytics}):
            import vision.vision_cluster as module

        class FailingModel:
            def predict(self, *args, **kwargs):
                raise RuntimeError("warmup failed")

        cluster = module.VisionCluster.__new__(module.VisionCluster)
        cluster.device = "cpu"
        cluster.verbose = False
        cluster.models = {"model.pt": FailingModel()}
        with self.assertRaisesRegex(RuntimeError, "Model warmup failed"):
            cluster.warmup()

    def test_archive_keeps_buffer_when_image_write_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = PartArchive(temp, batch_id="batch", enabled=True)
            frame = np.ones((20, 20, 3), dtype=np.uint8)
            archive.store_frames(1, "input", {"TOP": frame}, {}, {})
            self.assertIsInstance(archive._buffers[1]["TOP"]["raw"], bytes)
            with (
                patch(
                    "inspection.part_archive.os.replace",
                    side_effect=OSError("disk failure"),
                ),
                self.assertRaisesRegex(OSError, "disk failure"),
            ):
                archive.finalize(1, "BAD", "defect", ["defect"], 0)
            self.assertIn(1, archive._buffers)

    def test_archive_zip_is_crc_verified_before_source_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = PartArchive(temp, batch_id="batch", enabled=True)
            frame = np.ones((20, 20, 3), dtype=np.uint8) * 100
            archive.store_frames(1, "input", {"TOP": frame}, {}, {})
            archive.finalize(1, "GOOD", "none", [], 0)
            source = Path(archive.archive_base_path)
            zip_path = archive.compress(delete_original=True)
            self.assertIsNotNone(zip_path)
            self.assertFalse(source.exists())
            with zipfile.ZipFile(zip_path) as compressed:
                self.assertIsNone(compressed.testzip())
                self.assertIn("part_0001_GOOD_none/meta.json", compressed.namelist())

    def test_archive_stores_three_runs_correctly(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = PartArchive(temp, batch_id="batch_3runs", enabled=True)
            frame1 = np.ones((20, 20, 3), dtype=np.uint8) * 50
            frame2 = np.ones((20, 20, 3), dtype=np.uint8) * 100
            frame3 = np.ones((20, 20, 3), dtype=np.uint8) * 150
            
            run_frames = [
                {"INPUT_LEFT": frame1},
                {"INPUT_LEFT": frame2},
                {"INPUT_LEFT": frame3},
            ]
            run_rule_results = [[], [], []]
            run_vision_results = [
                {"INPUT_LEFT": []},
                {"INPUT_LEFT": []},
                {"INPUT_LEFT": []},
            ]

            archive.store_frames(
                part_id=2,
                stage="input",
                raw_frames={"INPUT_LEFT": frame1},
                annotated_frames={"INPUT_LEFT": frame1},
                raw_overlay_frames={"INPUT_LEFT": frame1},
                run_frames=run_frames,
                run_rule_results=run_rule_results,
                run_vision_results=run_vision_results,
            )

            # finalize to disk
            folder = archive.finalize(2, "BAD", "defect", ["some_defect"], 0)
            self.assertIsNotNone(folder)
            folder_path = Path(folder)

            # Check that fallback files exist
            self.assertTrue((folder_path / "INPUT_LEFT.jpg").exists())
            self.assertTrue((folder_path / "INPUT_LEFT_raw.jpg").exists())
            self.assertTrue((folder_path / "INPUT_LEFT_debug.jpg").exists())

            # Check that run-specific files exist
            for r in (1, 2, 3):
                self.assertTrue((folder_path / f"INPUT_LEFT_run{r}.jpg").exists())
                self.assertTrue((folder_path / f"INPUT_LEFT_run{r}_raw.jpg").exists())
                self.assertTrue((folder_path / f"INPUT_LEFT_run{r}_debug.jpg").exists())

            # Check get_part_images returns correct keys
            images = archive.get_part_images(2)
            self.assertIn("INPUT_LEFT", images)
            paths = images["INPUT_LEFT"]
            self.assertIn("raw", paths)
            self.assertIn("raw_overlay", paths)
            self.assertIn("debug", paths)
            for r in (1, 2, 3):
                self.assertIn(f"raw_run{r}", paths)
                self.assertIn(f"raw_overlay_run{r}", paths)
                self.assertIn(f"debug_run{r}", paths)


if __name__ == "__main__":
    unittest.main()
