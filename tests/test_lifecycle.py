import unittest
from types import SimpleNamespace

from core.production_cycle import ProductionCycle
from core.state_machine import State
from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP, CATEGORY_GOOD


ROLES = (
    "INPUT_LEFT",
    "INPUT_RIGHT",
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
)


class FakeConveyor:
    def __init__(self, events):
        self.events = events

    def move_step(self):
        self.events.append("move")

    def wait_stop(self, progress_callback=None):
        if progress_callback is not None:
            progress_callback({"mov": 0, "wait": 0, "lasterr": 0})
        return None

    def emergency_stop(self):
        self.events.append("stop")


class ForceExitDuringMoveConveyor(FakeConveyor):
    def __init__(self, events):
        super().__init__(events)
        self.cycle = None

    def move_step(self):
        super().move_step()
        self.cycle.request_force_exit()


class StopDuringMoveConveyor(FakeConveyor):
    def __init__(self, events):
        super().__init__(events)
        self.cycle = None

    def move_step(self):
        super().move_step()
        self.cycle.request_stop()


class FakeCameras:
    def __init__(self):
        self.captures = 0

    def capture_all(self):
        self.captures += 1
        return {role: object() for role in ROLES}


class NudgeJog:
    """JOG-заглушка с ограниченной коррекцией и накопителем смещения."""

    def __init__(self, micro_steps=500, nudge_limit_steps=1000):
        self.micro_steps = micro_steps
        self.nudge_limit_steps = nudge_limit_steps
        self._offset = 0
        self.applied = []

    @property
    def nudge_offset(self):
        return self._offset

    def reset_nudge_offset(self):
        self._offset = 0

    def nudge(self, direction, steps=None):
        if direction not in ("+", "-"):
            raise ValueError("bad direction")
        requested = self.micro_steps if steps is None else int(steps)
        signed = requested if direction == "+" else -requested
        clamped = max(
            -self.nudge_limit_steps,
            min(self.nudge_limit_steps, self._offset + signed),
        )
        applied = clamped - self._offset
        self._offset = clamped
        self.applied.append(applied)
        return applied

    def release(self, reason="released"):
        return True

    @property
    def busy(self):
        return False

    @property
    def status(self):
        return {
            "hold_steps": 1000000,
            "last_action": "-",
            "busy": False,
            "direction": None,
            "error": None,
            "micro_steps": self.micro_steps,
            "nudge_limit_steps": self.nudge_limit_steps,
            "nudge_offset": self._offset,
            "nudge_remaining_forward": self.nudge_limit_steps - self._offset,
            "nudge_remaining_backward": self.nudge_limit_steps + self._offset,
        }


class ScriptedInspector:
    def __init__(self, input_defects, spider_defects):
        self.input_defects = list(input_defects)
        self.spider_defects = list(spider_defects)
        self.input_calls = []
        self.spider_calls = []

    @staticmethod
    def result(stage, defects=None, empty=False):
        return SimpleNamespace(
            stage=stage,
            defects=list(defects or []),
            vision_results={},
            rule_results=[],
            annotated={},
            raw_frames={},
            raw_overlay_frames={},
            is_empty_tray=empty,
            consensus={},
            model_health=[],
        )

    def inspect_input(self, part_id, step, frames, force_bad=False):
        self.input_calls.append((part_id, step))
        if self.input_defects:
            return self.result("input", self.input_defects.pop(0))
        return self.result("input", empty=True)

    def inspect_spider(self, part_id, step, frames, force_bad=False):
        self.spider_calls.append((part_id, step))
        defects = self.spider_defects.pop(0) if self.spider_defects else []
        return self.result("spider", defects)

    def inspect_input_consensus(
        self, part_id, step, frame_runs, force_bad=False,
    ):
        result = self.inspect_input(
            part_id, step, frame_runs[-1], force_bad=force_bad,
        )
        result.consensus = {"runs": 3, "required_votes": 2, "rules": {}}
        return result

    def inspect_spider_consensus(
        self, part_id, step, frame_runs, force_bad=False,
    ):
        result = self.inspect_spider(
            part_id, step, frame_runs[-1], force_bad=force_bad,
        )
        result.consensus = {"runs": 3, "required_votes": 2, "rules": {}}
        return result


class FakeDistributor:
    def __init__(self, events):
        self.events = events
        self.on_state_changed = None
        self.dist1_open_position = 340

    @property
    def status(self):
        return {
            "dist1_position": 0,
            "dist1_max": 340,
            "dist1_state": "IDLE",
            "dist2_position": 0,
            "dist2_state": "IDLE",
            "dist2_target": "-",
            "last_distributor_action": "-",
        }

    def reset_target(self):
        return None

    def park_production(self):
        self.events.append(("park", "production"))

    def mark_pass(self, part_id):
        self.events.append(("pass", part_id))

    def prepare(self, category, part_id):
        self.events.append(("prepare", part_id, category))

    def drop_and_close(self, part_id, category):
        self.events.append(("drop", part_id, category))

    def emergency_stop(self):
        self.events.append("dist-stop")


class FakeArchive:
    def __init__(self):
        self.parts = []

    def store_frames(self, **kwargs):
        return None

    def finalize(self, **kwargs):
        self.parts.append(kwargs)


class LifecycleTests(unittest.TestCase):
    def run_one_part(self, input_defects, spider_defects):
        events = []
        inspector = ScriptedInspector([input_defects], [spider_defects])
        archive = FakeArchive()
        cycle = ProductionCycle(
            FakeConveyor(events),
            FakeCameras(),
            inspector,
            FakeDistributor(events),
            archive=archive,
        )
        phases = []
        original_set_process = cycle._set_process

        def record_process(phase, label, **kwargs):
            phases.append(phase)
            return original_set_process(phase, label, **kwargs)

        cycle._set_process = record_process
        self.assertTrue(cycle.request_start())
        for _ in range(8):
            cycle._run_once()
        cycle.recorded_phases = phases
        return cycle, inspector, archive, events

    def test_force_exit_during_motion_cannot_continue_to_camera_or_next_phase(self):
        events = []
        conveyor = ForceExitDuringMoveConveyor(events)
        cameras = FakeCameras()
        cycle = ProductionCycle(
            conveyor,
            cameras,
            ScriptedInspector([[]], [[]]),
            FakeDistributor(events),
            archive=FakeArchive(),
        )
        conveyor.cycle = cycle
        self.assertTrue(cycle.request_start())
        cycle._run_once_safe()
        self.assertEqual(cycle.current_step, 0)
        self.assertEqual(cameras.captures, 0)
        self.assertEqual(cycle.parts, [])
        self.assertEqual(cycle.state, "FAULT")

    def test_stop_during_started_step_still_tracks_entering_input_part(self):
        events = []
        conveyor = StopDuringMoveConveyor(events)
        inspector = ScriptedInspector([[]], [[]])
        cycle = ProductionCycle(
            conveyor,
            FakeCameras(),
            inspector,
            FakeDistributor(events),
            archive=FakeArchive(),
        )
        conveyor.cycle = cycle
        self.assertTrue(cycle.request_start())
        cycle._run_once()
        self.assertEqual(cycle.state, "STOPPING")
        self.assertEqual(len(cycle.parts), 1)
        self.assertEqual(cycle.parts[0].step_created, 1)
        self.assertTrue(cycle.parts[0].input_inspected)

    def test_good_part_uses_exact_input_plus4_plus7_lifecycle(self):
        cycle, inspector, archive, events = self.run_one_part([], [])
        self.assertEqual(inspector.input_calls[0], (1, 1))
        self.assertEqual(inspector.spider_calls, [(1, 5)])
        self.assertEqual(cycle.current_step, 8)
        self.assertEqual(cycle.good_count, 1)
        self.assertEqual(cycle.bad_count, 0)
        self.assertEqual(cycle.cameras.captures, 8 * 3)
        for phase in (
            "ROUTE_PREPARE",
            "CONVEYOR_COMMAND",
            "CONVEYOR_MOVING",
            "CONVEYOR_CONFIRMED",
            "CAMERA_CAPTURE",
            "INPUT_ANALYSIS",
            "SPIDER_ANALYSIS",
            "ROUTE_FINALIZE",
            "STEP_COMPLETE",
        ):
            self.assertIn(phase, cycle.recorded_phases)
        self.assertEqual(archive.parts[0]["category"], CATEGORY_GOOD)
        self.assertEqual(
            archive.parts[0]["extra"]["inspection_consensus"]["input"]["runs"],
            3,
        )
        self.assertEqual(
            archive.parts[0]["extra"]["inspection_consensus"]["spider"]["required_votes"],
            2,
        )
        pass_index = events.index(("pass", 1))
        self.assertEqual(events[pass_index + 1], "move")

    def test_bad_part_is_prepared_before_plus7_move_and_dropped_after(self):
        cycle, inspector, archive, events = self.run_one_part([], ["contacts"])
        self.assertEqual(cycle.bad_count, 1)
        self.assertEqual(archive.parts[0]["category"], CATEGORY_BAD)
        prepare = events.index(("prepare", 1, CATEGORY_BAD))
        move = events.index("move", prepare)
        drop = events.index(("drop", 1, CATEGORY_BAD))
        self.assertLess(prepare, move)
        self.assertLess(move, drop)

    def test_cleanup_only_defect_routes_cleanup(self):
        cycle, inspector, archive, events = self.run_one_part(["glass"], [])
        self.assertEqual(cycle.cleanup_count, 1)
        self.assertEqual(archive.parts[0]["category"], CATEGORY_CLEANUP)
        self.assertIn(("prepare", 1, CATEGORY_CLEANUP), events)
        self.assertIn(("drop", 1, CATEGORY_CLEANUP), events)

    def test_three_overlapping_parts_keep_ids_stages_and_routes_aligned(self):
        events = []
        inspector = ScriptedInspector(
            [[], ["glass"], []],
            [[], [], ["contacts"]],
        )
        archive = FakeArchive()
        cycle = ProductionCycle(
            FakeConveyor(events),
            FakeCameras(),
            inspector,
            FakeDistributor(events),
            archive=archive,
        )
        self.assertTrue(cycle.request_start())
        for _ in range(10):
            cycle._run_once()

        self.assertEqual(inspector.input_calls[:3], [(1, 1), (2, 2), (3, 3)])
        self.assertEqual(
            inspector.spider_calls,
            [(1, 5), (2, 6), (3, 7)],
        )
        self.assertEqual(cycle.parts, [])
        self.assertEqual(cycle.part_counter, 3)
        self.assertEqual(cycle.good_count, 1)
        self.assertEqual(cycle.cleanup_count, 1)
        self.assertEqual(cycle.bad_count, 1)
        self.assertEqual(
            [(item["part_id"], item["category"]) for item in archive.parts],
            [
                (1, CATEGORY_GOOD),
                (2, CATEGORY_CLEANUP),
                (3, CATEGORY_BAD),
            ],
        )
        self.assertIn(("prepare", 2, CATEGORY_CLEANUP), events)
        self.assertIn(("drop", 2, CATEGORY_CLEANUP), events)
        self.assertIn(("prepare", 3, CATEGORY_BAD), events)
        self.assertIn(("drop", 3, CATEGORY_BAD), events)

    def test_pause_and_nudge_preserve_part_cell_alignment_end_to_end(self):
        """Пауза с коррекцией не должна разрушать соответствие деталь↔ячейка."""
        events = []
        inspector = ScriptedInspector([[]], [[]])
        cycle = ProductionCycle(
            FakeConveyor(events),
            FakeCameras(),
            inspector,
            FakeDistributor(events),
            archive=FakeArchive(),
            jog=NudgeJog(),
        )
        self.assertTrue(cycle.request_start())

        # Несколько штатных шагов, деталь входит на линию.
        for _ in range(3):
            cycle._run_once()
        step_before = cycle.current_step
        parts_before = [(part.id, part.step_created) for part in cycle.parts]
        moves_before = events.count("move")

        # Пауза на границе шага и коррекция до упора в обе стороны.
        self.assertTrue(cycle.request_pause())
        self.assertTrue(cycle.sm.request_pause())
        cycle._enter_pause_frame()
        for _ in range(5):
            cycle.nudge_belt("+")
        for _ in range(10):
            cycle.nudge_belt("-")
        self.assertEqual(cycle.jog.nudge_offset, -1000)

        # Коррекция не выполнила ни одного производственного шага.
        self.assertEqual(events.count("move"), moves_before)
        self.assertEqual(cycle.current_step, step_before)
        self.assertEqual(
            [(part.id, part.step_created) for part in cycle.parts],
            parts_before,
        )

        # После возобновления цикл продолжается с той же нумерации шагов.
        self.assertTrue(cycle.request_resume())
        self.assertEqual(cycle.sm.state, State.RUNNING)
        cycle._run_once()
        self.assertEqual(cycle.current_step, step_before + 1)
        # Уже находившиеся на линии детали сохранили свои step_created,
        # то есть их позиции пересчитываются от прежней точки отсчёта.
        surviving = {
            part.id: part.step_created
            for part in cycle.parts
            if part.id in {pid for pid, _ in parts_before}
        }
        for part_id, created in parts_before:
            if part_id in surviving:
                self.assertEqual(surviving[part_id], created)


if __name__ == "__main__":
    unittest.main()
