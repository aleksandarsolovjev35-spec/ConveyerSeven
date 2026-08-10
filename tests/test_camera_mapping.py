"""Тесты загрузки и валидации camera_mapping.json."""

import unittest

from config.camera_mapping import (
    REQUIRED_ROLES,
    load_camera_mapping,
)


class CameraMappingTest(unittest.TestCase):
    def test_required_roles_are_seven(self):
        self.assertEqual(len(REQUIRED_ROLES), 7)

    def test_load_real_mapping(self):
        mapping = load_camera_mapping("camera_mapping.json")
        self.assertEqual(set(mapping), REQUIRED_ROLES)


if __name__ == "__main__":
    unittest.main()
