"""Барьеры между этапами шага: движение, затухание, съёмка, анализ."""

import threading
import time
import unittest

from core.live_preview import LiveCaptureGate, LivePreview
from core.step_stages import (
    StageSequenceError,
    StepSequencer,
    StepStage,
)


class RecordingLive:
    """Заглушка live-просмотра, фиксирующая передачу камер."""

    def __init__(self, pause_ok=True):
        self.pause_ok = pause_ok
        self.events = []
        self.paused_depth = 0

    def pause(self, timeout=None):
        self.events.append("pause")
        if not self.pause_ok:
            return False
        self.paused_depth += 1
        return True

    def resume(self):
        self.events.append("resume")
        self.paused_depth -= 1


class StepSequencerOrderTests(unittest.TestCase):
    def make(self, **kwargs):
        live = kwargs.pop("live", None) or RecordingLive()
        sleeps = []
        sequencer = StepSequencer(
            live,
            sleep=sleeps.append,
            **kwargs,
        )
        return sequencer, live, sleeps

    def test_full_step_follows_the_declared_order(self):
        sequencer, live, _ = self.make()
        self.assertEqual(sequencer.stage, StepStage.IDLE)

        sequencer.enter_motion()
        self.assertEqual(sequencer.stage, StepStage.MOTION)
        sequencer.enter_settle()
        sequencer.enter_capture()
        self.assertEqual(sequencer.stage, StepStage.CAPTURE)
        sequencer.enter_analysis()
        sequencer.enter_publish()

        # Следующий шаг начинается снова с движения.
        sequencer.enter_motion()
        self.assertEqual(sequencer.stage, StepStage.MOTION)
        self.assertEqual(live.paused_depth, 0)

    def test_capture_without_settle_is_rejected(self):
        """Съёмка сразу после движения означала бы смазанный кадр."""
        sequencer, _, _ = self.make()
        sequencer.enter_motion()
        with self.assertRaises(StageSequenceError):
            sequencer.enter_capture()

    def test_motion_cannot_start_while_frames_are_being_captured(self):
        sequencer, _, _ = self.make()
        sequencer.enter_motion()
        sequencer.enter_settle()
        sequencer.enter_capture()
        with self.assertRaises(StageSequenceError):
            sequencer.enter_motion()

    def test_analysis_cannot_run_before_capture(self):
        sequencer, _, _ = self.make()
        sequencer.enter_motion()
        sequencer.enter_settle()
        with self.assertRaises(StageSequenceError):
            sequencer.enter_analysis()

    def test_settle_waits_before_capture(self):
        sequencer, _, sleeps = self.make(settle_seconds=0.25)
        sequencer.enter_motion()
        sequencer.enter_settle()
        self.assertEqual(sleeps, [0.25])

    def test_cameras_belong_to_inspection_only_from_capture(self):
        sequencer, live, _ = self.make()
        sequencer.enter_motion()
        self.assertFalse(sequencer.static)
        sequencer.enter_settle()
        # На затухании камеры ещё у live-просмотра.
        self.assertFalse(sequencer.static)
        self.assertNotIn("pause", live.events)

        sequencer.enter_capture()
        self.assertTrue(sequencer.static)
        self.assertEqual(live.events, ["pause"])

    def test_static_hold_survives_analysis_and_publish(self):
        """Разметка обязана остаться на тех же кадрах до конца шага."""
        sequencer, _, _ = self.make()
        sequencer.enter_motion()
        sequencer.enter_settle()
        sequencer.enter_capture()
        sequencer.enter_analysis()
        self.assertTrue(sequencer.static)
        sequencer.enter_publish()
        self.assertTrue(sequencer.static)

        sequencer.enter_motion()
        self.assertFalse(sequencer.static)

    def test_failed_handover_stops_the_step(self):
        sequencer, _, _ = self.make(live=RecordingLive(pause_ok=False))
        sequencer.enter_motion()
        sequencer.enter_settle()
        with self.assertRaises(StageSequenceError):
            sequencer.enter_capture()
        self.assertFalse(sequencer.static)

    def test_reset_returns_cameras_and_clears_stage(self):
        sequencer, live, _ = self.make()
        sequencer.enter_motion()
        sequencer.enter_settle()
        sequencer.enter_capture()
        sequencer.reset()
        self.assertEqual(sequencer.stage, StepStage.IDLE)
        self.assertFalse(sequencer.static)
        self.assertEqual(live.paused_depth, 0)

    def test_reset_is_idempotent_and_does_not_double_resume(self):
        sequencer, live, _ = self.make()
        sequencer.enter_motion()
        sequencer.enter_settle()
        sequencer.enter_capture()
        sequencer.reset()
        sequencer.reset()
        self.assertEqual(live.paused_depth, 0)
        self.assertEqual(live.events.count("resume"), 1)


class SlowCameras:
    mapping = {"TOP": 0, "INPUT_LEFT": 1}

    def __init__(self, read_seconds=0.02):
        self.read_seconds = read_seconds
        self.lock = threading.Lock()
        self.in_flight = set()
        self.overlaps = []

    def _read(self, source):
        with self.lock:
            if self.in_flight - {source}:
                self.overlaps.append(source)
            self.in_flight.add(source)
        try:
            time.sleep(self.read_seconds)
        finally:
            with self.lock:
                self.in_flight.discard(source)

    def capture_single(self, role):
        self._read("live")
        return object()

    def capture_roles(self, roles):
        self._read("live")
        return {role: object() for role in roles}

    def capture_all(self):
        self._read("inspection")
        return {role: object() for role in self.mapping}


class Monitor:
    def update(self, **kwargs):
        return None


class StepSequencerAgainstLiveTests(unittest.TestCase):
    """Проверка барьера на настоящем LivePreview, а не на заглушке."""

    def test_capture_never_overlaps_running_live_preview(self):
        cameras = SlowCameras()
        preview = LivePreview(cameras, Monitor(), lambda: "TOP")
        sequencer = StepSequencer(preview, settle_seconds=0.0)
        preview.start()
        try:
            for _ in range(15):
                sequencer.enter_motion()
                time.sleep(0.01)
                sequencer.enter_settle()
                sequencer.enter_capture()
                for _ in range(3):
                    cameras.capture_all()
                sequencer.enter_analysis()
                sequencer.enter_publish()
        finally:
            sequencer.reset()
            preview.stop()

        self.assertEqual(cameras.overlaps, [])

    def test_gate_is_left_clean_after_many_steps(self):
        preview = LivePreview(SlowCameras(), Monitor(), lambda: "TOP")
        sequencer = StepSequencer(preview, settle_seconds=0.0)
        preview.start()
        try:
            for _ in range(30):
                sequencer.enter_motion()
                sequencer.enter_settle()
                sequencer.enter_capture()
                sequencer.enter_analysis()
                sequencer.enter_publish()
            sequencer.reset()
        finally:
            preview.stop()

        self.assertFalse(preview.gate.paused)
        with preview.gate.live_read() as allowed:
            self.assertTrue(allowed)


class LiveCaptureGateNestingTests(unittest.TestCase):
    def test_nested_pause_requires_matching_resume(self):
        gate = LiveCaptureGate()
        self.assertTrue(gate.pause())
        self.assertTrue(gate.pause())
        gate.resume()
        self.assertTrue(gate.paused)
        gate.resume()
        self.assertFalse(gate.paused)

    def test_resume_without_pause_does_not_go_negative(self):
        gate = LiveCaptureGate()
        gate.resume()
        gate.resume()
        self.assertFalse(gate.paused)
        self.assertTrue(gate.pause())
        self.assertTrue(gate.paused)
        gate.resume()
        self.assertFalse(gate.paused)


if __name__ == "__main__":
    unittest.main()
