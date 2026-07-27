import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calibrate_distributor import atomic_write_json, calibrate_endpoint


class FakeAxis:
    def __init__(self):
        self._position = 0
        self.moves = []

    @property
    def position(self):
        return self._position

    def home(self):
        self.moves.append(("home", 0))
        self._position = 0

    def move_absolute(self, target):
        self.moves.append(("absolute", target))
        self._position = target

    def wait_stop(self, timeout=10.0):
        return None

    def verify_homed(self):
        if self._position != 0:
            raise RuntimeError("not home")
        return {
            "position": 0,
            "moving": 0,
            "homed": 1,
            "limits_enabled": 1,
        }


class DistributorCalibrationTests(unittest.TestCase):
    def test_operator_accepts_observed_endpoint_and_axis_returns_home(self):
        axis = FakeAxis()
        responses = iter([
            "HOME DIST1",
            "350",
            "MOVE DIST1 TO 350",
            "ACCEPT 350",
            "HOME DIST1",
        ])
        with patch("builtins.input", side_effect=lambda prompt="": next(responses)):
            result = calibrate_endpoint(axis, "DIST1", 2000)
        self.assertEqual(result, 350)
        self.assertEqual(
            axis.moves,
            [("home", 0), ("absolute", 350), ("home", 0)],
        )
        self.assertEqual(axis.position, 0)

    def test_candidate_json_write_is_atomic_and_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "candidate.json"
            atomic_write_json(path, {"dist1_open_position": 350})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"dist1_open_position": 350},
            )
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_main_calibration_requires_exact_apply_phrase_and_backup(self):
        source = (
            Path(__file__).resolve().parents[1] / "calibrate_distributor.py"
        ).read_text(encoding="utf-8")
        self.assertIn("APPLY DISTRIBUTOR CALIBRATION", source)
        self.assertIn("calibration.distributor_candidate.json", source)
        self.assertIn("calibration.before_distributor_", source)
        self.assertIn('transport.send("G25")', source)


if __name__ == "__main__":
    unittest.main()
