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
        self.labels = self.loader.labels

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
        # Необязательный ключ с нечисловым значением
        bad = dict(self.data)
        bad["INPUT_LEFT.extra_parameter"] = "abc"
        with self.assertRaises(ValueError):
            ThresholdLoader.validate(bad)

    def test_validate_accepts_extra_numeric(self):
        extra = dict(self.data)
        extra["INPUT_LEFT.extra_parameter"] = 1.5
        ThresholdLoader.validate(extra)  # не должно бросить

    def test_describe_role_parameters(self):
        groups = describe_role_parameters("INPUT_LEFT", self.data)
        self.assertTrue(groups)
        for group in groups:
            self.assertIn("rule", group)
            self.assertIn("label", group)
            self.assertIn("params", group)
            for param in group["params"]:
                self.assertIn("key", param)
                self.assertIn("value", param)
                self.assertIn("label", param)

    def test_describe_unknown_role_empty(self):
        groups = describe_role_parameters("NO_SUCH_ROLE", self.data)
        self.assertEqual(groups, [])

    def test_labels_loaded(self):
        # _label.* из thresholds.json попадают в loader.labels
        self.assertIsInstance(self.labels, dict)

    def test_missing_file_raises(self):
        with self.assertRaises(RuntimeError):
            ThresholdLoader("no_such_file.json")

    def test_save_and_reload_roundtrip(self):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        )
        tmp.close()
        self.addCleanup(lambda: __import__("os").unlink(tmp.name))

        labels = {"INPUT_LEFT.input_window_geometry_expected_count": "Окон, шт"}
        ThresholdLoader.save_file(tmp.name, self.data, labels)
        loader2 = ThresholdLoader(tmp.name)
        self.assertEqual(loader2.thresholds, self.data)
        self.assertEqual(
            loader2.labels.get("INPUT_LEFT.input_window_geometry_expected_count"),
            "Окон, шт",
        )


if __name__ == "__main__":
    unittest.main()
