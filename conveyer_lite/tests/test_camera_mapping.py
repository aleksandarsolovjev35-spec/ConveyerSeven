"""Тесты загрузки и валидации camera_mapping.json."""

import unittest

from config.camera_mapping import (
    REQUIRED_ROLES,
    load_camera_mapping,
    validate_camera_mapping,
)


class CameraMappingTest(unittest.TestCase):
    def test_required_roles_are_seven(self):
        self.assertEqual(
            REQUIRED_ROLES,
            {"INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
             "SPIDER_IN", "SPIDER_OUT", "TOP"},
        )

    def test_load_real_mapping(self):
        mapping = load_camera_mapping("camera_mapping.json")
        self.assertEqual(set(mapping), REQUIRED_ROLES)
        self.assertEqual(len(set(mapping.values())), 7)
        for camera_id in mapping.values():
            self.assertIsInstance(camera_id, int)
            self.assertGreaterEqual(camera_id, 0)

    def test_validate_rejects_bad_mappings(self):
        with self.assertRaises(ValueError):
            validate_camera_mapping({})
        with self.assertRaises(ValueError):
            validate_camera_mapping({"A": 1})
        # Дубликат ID
        bad = {
            "INPUT_LEFT": 0, "INPUT_RIGHT": 0,
            "SPIDER_LEFT": 2, "SPIDER_RIGHT": 3,
            "SPIDER_IN": 4, "SPIDER_OUT": 5, "TOP": 6,
        }
        with self.assertRaises(ValueError):
            validate_camera_mapping(bad)
        # Отрицательный ID
        bad_neg = dict(bad, INPUT_RIGHT=-1)
        with self.assertRaises(ValueError):
            validate_camera_mapping(bad_neg)

    def test_missing_file_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            load_camera_mapping("no_such_file.json")


if __name__ == "__main__":
    unittest.main()
