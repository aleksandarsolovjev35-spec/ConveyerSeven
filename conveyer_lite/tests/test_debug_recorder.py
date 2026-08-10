"""Тесты DebugRecorder: аннотация кадров и сохранение на диск."""

import os
import tempfile
import unittest

import numpy as np

from domain.defect_rules.base import RuleResult
from inspection.debug_recorder import DebugRecorder


def _frame(value=0):
    return np.full((48, 64, 3), value, dtype=np.uint8)


class DebugRecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="conveyer_debug_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))

    def test_process_returns_annotated_frames(self):
        recorder = DebugRecorder(folder=self.tmp, enabled=True,
                                 save_interval=1000)  # не сохраняем на диск
        frame = _frame()
        rule = RuleResult("window_geometry", False,
                          details={"per_role": {}}, drawings=[])
        annotated = recorder.process(
            part_id=1, step=2,
            frames={"INPUT_LEFT": frame, "INPUT_RIGHT": frame},
            rule_results=[rule],
        )
        self.assertEqual(set(annotated), {"INPUT_LEFT", "INPUT_RIGHT"})
        self.assertEqual(annotated["INPUT_LEFT"].shape, frame.shape)

    def test_save_interval(self):
        recorder = DebugRecorder(folder=self.tmp, enabled=True,
                                 save_interval=2)
        # process каждые 2 шага сохраняет
        frame = _frame()
        recorder.process(part_id=1, step=1,
                         frames={"A": frame}, rule_results=[])
        recorder.process(part_id=2, step=2,
                         frames={"A": frame}, rule_results=[])
        saved = [d for d in os.listdir(self.tmp)
                 if os.path.isdir(os.path.join(self.tmp, d))]
        self.assertEqual(len(saved), 1, saved)

    def test_disabled_recorder_noop(self):
        recorder = DebugRecorder(folder=self.tmp, enabled=False)
        frame = _frame()
        annotated = recorder.process(
            part_id=1, step=1, frames={"A": frame}, rule_results=[],
        )
        self.assertEqual(set(annotated), {"A"})
        self.assertEqual(os.listdir(self.tmp), [])


if __name__ == "__main__":
    unittest.main()
