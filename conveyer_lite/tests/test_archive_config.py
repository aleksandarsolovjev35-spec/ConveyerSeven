"""Проверки упрощённой конфигурации архива партий."""

import json
import os
import tempfile
import unittest

from config.archive_config import (
    load_archive_config,
    normalise_archive_config,
    save_archive_config,
)


class ArchiveConfigTest(unittest.TestCase):
    def test_defaults_are_complete_and_safe(self):
        config = normalise_archive_config({})
        self.assertTrue(config["enabled"])
        self.assertEqual(config["jpeg_quality"], 92)
        self.assertTrue(config["compress_on_shutdown"])
        self.assertTrue(config["delete_original_after_zip"])
        self.assertEqual(config["root_path"], "archive")

    def test_values_are_clamped(self):
        config = normalise_archive_config({
            "root_path": "~/inspection",
            "jpeg_quality": 1,
        })
        self.assertEqual(config["jpeg_quality"], 70)
        # Качество выше максимума ограничивается 98.
        config = normalise_archive_config({"jpeg_quality": 999})
        self.assertEqual(config["jpeg_quality"], 98)

    def test_save_and_load_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "archive_config.json")
            saved = save_archive_config(path, {
                "root_path": os.path.join(root, "data"),
                "jpeg_quality": 88,
                "compress_on_shutdown": False,
            })
            loaded = load_archive_config(path)
            self.assertEqual(loaded, saved)
            with open(path, encoding="utf-8") as stream:
                self.assertEqual(json.load(stream), saved)
            self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
