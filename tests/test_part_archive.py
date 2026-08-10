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
        self.assertTrue(folder.endswith(os.path.join("BAD", "part_0001")))
        self.assertTrue(os.path.exists(os.path.join(
            os.path.dirname(os.path.dirname(folder)), "batch.json",
        )))

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
        self.assertTrue(os.path.exists(
            os.path.join(folder, "input_INPUT_LEFT_run1_detections.json")))
        self.assertTrue(os.path.exists(
            os.path.join(os.path.dirname(os.path.dirname(folder)), "samples.jsonl")))
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

    def test_stats_by_parts(self):
        """Статистика по корпусам: годные / брак / очистка ведётся и
        восстанавливается между запусками из stats.json."""
        archive = PartArchive(root_folder=self.tmp, enabled=True)
        self.assertEqual(
            archive.get_stats(),
            {"total": 0, "good": 0, "bad": 0, "cleanup": 0},
        )
        archive.finalize(1, "GOOD", "none", [], step=0)
        archive.finalize(2, "BAD", "contacts_long", ["contacts_long"], step=1)
        archive.finalize(3, "CLEANUP", "glass", ["glass"], step=2)
        archive.finalize(4, "BAD", "window_geometry", ["window_geometry"], step=3)
        self.assertEqual(
            archive.get_stats(),
            {"total": 4, "good": 1, "bad": 2, "cleanup": 1},
        )

        # Файл статистики записан
        stats_path = os.path.join(self.tmp, PartArchive.STATS_FILE)
        self.assertTrue(os.path.exists(stats_path))
        with open(stats_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(
            data, {"total": 4, "good": 1, "bad": 2, "cleanup": 1},
        )

        # Новый архив (новый запуск программы) восстанавливает статистику
        reopened = PartArchive(root_folder=self.tmp, enabled=True)
        self.assertEqual(
            reopened.get_stats(),
            {"total": 4, "good": 1, "bad": 2, "cleanup": 1},
        )

        # Повреждённый файл — безопасный сброс в ноль
        with open(stats_path, "w", encoding="utf-8") as f:
            f.write("{broken")
        broken = PartArchive(root_folder=self.tmp, enabled=True)
        self.assertEqual(
            broken.get_stats(),
            {"total": 0, "good": 0, "bad": 0, "cleanup": 0},
        )


if __name__ == "__main__":
    unittest.main()
