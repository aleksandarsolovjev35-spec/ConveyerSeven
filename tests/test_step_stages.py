"""Тесты StepSequencer: порядок фаз и передача камер инспекции."""

import unittest
from unittest.mock import Mock

from core.step_stages import (
    StageSequenceError,
    StepSequencer,
    StepStage,
)


class FakeLive:
    def __init__(self):
        self.paused = False
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self, timeout=5.0):
        self.pause_calls += 1
        self.paused = True
        return True

    def resume(self):
        self.resume_calls += 1
        self.paused = False


class StepSequencerTest(unittest.TestCase):
    def setUp(self):
        self.live = FakeLive()
        self.stages = StepSequencer(
            self.live,
            settle_seconds=0.0,
            trace_seconds=0.0,
            on_stage=None,
        )

    def test_stage_order(self):
        self.stages.enter_motion()
        self.assertEqual(self.stages.stage, StepStage.MOTION)
        self.stages.enter_settle()
        self.assertEqual(self.stages.stage, StepStage.SETTLE)
        self.stages.enter_capture()
        self.assertEqual(self.stages.stage, StepStage.CAPTURE)
        self.assertTrue(self.stages.static)
        self.stages.enter_analysis()
        self.assertEqual(self.stages.stage, StepStage.ANALYSIS)
        self.stages.enter_publish()
        self.assertEqual(self.stages.stage, StepStage.PUBLISH)

    def test_invalid_transition_raises(self):
        with self.assertRaises(StageSequenceError):
            self.stages.enter_capture()   # IDLE -> CAPTURE недопустим

        self.stages.enter_motion()
        with self.assertRaises(StageSequenceError):
            self.stages.enter_analysis()  # MOTION -> ANALYSIS недопустим

    def test_capture_acquires_and_release(self):
        self.stages.enter_motion()
        self.stages.enter_settle()
        self.stages.enter_capture()
        self.assertEqual(self.live.pause_calls, 1)
        self.assertTrue(self.stages.static)

        # Следующий шаг: PUBLISH -> MOTION освобождает камеры
        self.stages.enter_analysis()
        self.stages.enter_publish()
        self.stages.enter_motion()
        self.assertFalse(self.stages.static)
        self.assertEqual(self.live.resume_calls, 1)

    def test_cannot_skip_analysis_from_capture(self):
        self.stages.enter_motion()
        self.stages.enter_settle()
        self.stages.enter_capture()
        with self.assertRaises(StageSequenceError):
            self.stages.enter_motion()  # CAPTURE -> MOTION недопустим

    def test_reset_releases_static(self):
        self.stages.enter_motion()
        self.stages.enter_settle()
        self.stages.enter_capture()
        self.stages.reset()
        self.assertEqual(self.stages.stage, StepStage.IDLE)
        self.assertFalse(self.stages.static)
        self.assertEqual(self.live.resume_calls, 1)

    def test_live_pause_failure_raises(self):
        failing = Mock()
        failing.pause.return_value = False
        failing.resume = Mock()
        stages = StepSequencer(
            failing, settle_seconds=0.0, trace_seconds=0.0, on_stage=None,
        )
        stages.enter_motion()
        stages.enter_settle()
        with self.assertRaises(StageSequenceError):
            stages.enter_capture()


if __name__ == "__main__":
    unittest.main()
