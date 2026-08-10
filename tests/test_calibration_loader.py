"""Тесты config.calibration_loader: валидация calibration.json."""

import json
import tempfile
import unittest

from config.calibration_loader import DEFAULTS, load_calibration


class CalibrationLoaderTest(unittest.TestCase):
    def _write(self, data):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        )
        json.dump(data, tmp)
        tmp.close()
        self.addCleanup(lambda: __import__("os").unlink(tmp.name))
        return tmp.name

    def test_load_real_calibration(self):
        cal = load_calibration("calibration.json")
        for key in DEFAULTS:
            self.assertIn(key, cal, key)
        self.assertGreater(cal["conveyor_speed"], 0)
        self.assertNotEqual(cal["dist2_bad_position"],
                            cal["dist2_cleanup_position"])

    def test_optional_defaults_applied(self):
        data = dict(DEFAULTS)
        data["dist2_bad_position"] = 40
        path = self._write(data)
        cal = load_calibration(path)
        self.assertEqual(cal["settle_time"], 0.5)
        self.assertEqual(cal["review_time"], 5.0)
        self.assertEqual(cal["stage_trace_time"], 0.5)

    def test_missing_file_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            load_calibration("no_such_calibration.json")


if __name__ == "__main__":
    unittest.main()
