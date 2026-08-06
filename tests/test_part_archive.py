"""Тесты PartArchive: буферизация, финализация и zip-сжатие с одним прогоном."""

import json
import os
import tempfile
import unittest

import numpy as np

from domain.defect_rules.base import RuleResult
from inspection.part_archive import PartArchive


def _frame(value=0):
    return np.full((48, 64, 3), value, dtype=np.uint8)


class PartArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="conveyer_archive_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_store_and_finalize_single_run(self):
        archive = PartArchive(root_folder=self.tmp, enabled=True)
        frame = _frame(10)
        rule = RuleResult("window_geometry", False,
                          details={"per_role": {}}, drawings=[])

        archive.store_frames(
            part_id=1,
            stage="input",
            raw_frames={"INPUT_LEFT": frame, "INPUT_RIGHT": frame},
            annotated_frames={"INPUT_LEFT": frame.copy()},
            raw_overlay_frames={"INPUT_LEFT": frame.copy()},
            run_frames=[{"INPUT_LEFT": frame}],
            run_rule_results=[[rule]],
            run_vision_results=[{"INPUT_LEFT": [{"class": "flatness"}]}],
        )
        archive.store_frames(
            part_id=1,
            stage="spider",
            raw_frames={"SPIDER_LEFT": frame},
            annotated_frames={"SPIDER_LEFT": frame.copy()},
        )

        archive.finalize(
            part_id=1,
            category="BAD",
            decision="contacts_long",
            defects=["contacts_long"],
            step=1,
        )

        info = archive.get_part_info(1)
        self.assertIsNotNone(info)
        folder = info["folder"]
        self.assertTrue(os.path.isdir(folder))

        # Основные файлы
        for name in ("INPUT_LEFT.jpg", "INPUT_RIGHT.jpg", "SPIDER_LEFT.jpg",
                     "INPUT_LEFT_raw.jpg", "INPUT_LEFT_debug.jpg",
                     "meta.json"):
            self.assertTrue(
                os.path.exists(os.path.join(folder, name)), name,
            )

        # Прогон: run1-файлы существуют, run2/run3 — нет (один прогон)
        self.assertTrue(os.path.exists(
            os.path.join(folder, "INPUT_LEFT_run1.jpg")))
        self.assertTrue(os.path.exists(
            os.path.join(folder, "INPUT_LEFT_run1_debug.jpg")))
        self.assertFalse(os.path.exists(
            os.path.join(folder, "INPUT_LEFT_run2.jpg")))
        self.assertFalse(os.path.exists(
            os.path.join(folder, "INPUT_LEFT_run3.jpg")))

        # meta.json
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["part_id"], 1)
        self.assertEqual(meta["category"], "BAD")

        # get_part_images отдаёт основные и run1-изображения
        images = archive.get_part_images(1)
        self.assertIn("raw", images["INPUT_LEFT"])
        self.assertIn("debug", images["INPUT_LEFT"])
        self.assertIn("raw_run1", images["INPUT_LEFT"])
        self.assertNotIn("raw_run2", images["INPUT_LEFT"])

    def test_compress_creates_zip(self):
        archive = PartArchive(root_folder=self.tmp, enabled=True)
        frame = _frame(5)
        archive.store_frames(
            part_id=1, stage="input",
            raw_frames={"INPUT_LEFT": frame},
            annotated_frames={"INPUT_LEFT": frame.copy()},
        )
        archive.finalize(
            part_id=1, category="GOOD", decision="none",
            defects=[], step=1,
        )

        zip_path = archive.compress(delete_original=True)
        self.assertIsNotNone(zip_path)
        self.assertTrue(os.path.exists(zip_path))
        self.assertTrue(zip_path.endswith(".zip"))

    def test_disabled_archive_is_noop(self):
        archive = PartArchive(root_folder=self.tmp, enabled=False)
        archive.store_frames(
            part_id=1, stage="input",
            raw_frames={"INPUT_LEFT": _frame()},
            annotated_frames={"INPUT_LEFT": _frame()},
        )
        archive.finalize(
            part_id=1, category="GOOD", decision="none",
            defects=[], step=1,
        )
        self.assertIsNone(archive.get_part_info(1))


if __name__ == "__main__":
    unittest.main()
