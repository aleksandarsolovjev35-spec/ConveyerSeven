"""Тесты domain.threshold_loader: загрузка, валидация, описание ролей."""

import tempfile
import unittest

from domain.threshold_loader import (
    ThresholdLoader,
    describe_role_parameters,
)


class ThresholdLoaderTest(unittest.TestCase):
    def setUp(self):
        self.loader = ThresholdLoader("thresholds.json")
        self.data = self.loader.thresholds

    def test_loads_all_required_keys(self):
        for key in ThresholdLoader.REQUIRED_KEYS:
            self.assertIn(key, self.data, key)

    def test_roles_present(self):
        roles = {key.split(".")[0] for key in self.data if "." in key}
        for role in ("INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT",
                     "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP"):
            self.assertIn(role, roles, role)

    def test_validate_rejects_bad(self):
        with self.assertRaises(ValueError):
            ThresholdLoader.validate({})


if __name__ == "__main__":
    unittest.main()
