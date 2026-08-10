import json
import tempfile
import unittest
from pathlib import Path

from inspection.recovery import mark_batch_aborted


class RecoveryTest(unittest.TestCase):
    def test_previous_batch_is_durably_marked_aborted(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "2026-08-09" / "batch-old"
            (folder / "parts").mkdir(parents=True)
            payload = {"batch_id": "batch-old", "status": "OPEN", "parts": []}
            for path in (folder / "batch.json", folder / "parts" / "batch.json"):
                path.write_text(json.dumps(payload), encoding="utf-8")
            result = mark_batch_aborted(root, "batch-old")
            self.assertEqual(result, folder)
            for path in (folder / "batch.json", folder / "parts" / "batch.json"):
                row = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(row["status"], "ABORTED")
                self.assertIn("aborted_at", row)


if __name__ == "__main__":
    unittest.main()
