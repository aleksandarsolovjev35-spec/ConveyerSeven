"""Тесты упрощённого PartArchive: кадры + meta.json."""

import json
import os
import tempfile
import unittest

import numpy as np

from inspection.part_archive import PartArchive


def _frame(value=0):
    return np.full((48, 64, 3), value, dtype=np.uint8)


class PartArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archive_test_")
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_store_and_finalize_single_run(self):
        archive = PartArchive(root_folder=self.tmp, enabled=True)
        frame = _frame(10)
        archive.store_frames(
            part_id=1, stage="input",
            raw_frames={"INPUT_LEFT": frame, "INPUT_RIGHT": frame},
            annotated_frames={"INPUT_LEFT": frame.copy()},
            raw_overlay_frames={"INPUT_LEFT": frame.copy()},
            run_frames=[{"INPUT_LEFT": frame}],
            run_rule_results=[[]],
            run_vision_results=[{"INPUT_LEFT": []}],
        )
        archive.finalize(
            part_id=1, category="BAD", decision="contacts_long",
            defects=["contacts_long"], step=1,
        )
        info = archive.get_part_info(1)
        self.assertIsNotNone(info)
        folder = info["folder"]
        self.assertTrue(os.path.isdir(folder))
        self.assertTrue(folder.endswith(os.path.join("BAD", "part_0001")))
        for name in ("INPUT_LEFT.jpg", "INPUT_RIGHT.jpg",
                     "INPUT_LEFT_raw.jpg", "meta.json"):
            self.assertTrue(os.path.exists(os.path.join(folder, name)), name)
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["part_id"], 1)
        self.assertEqual(meta["category"], "BAD")

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
