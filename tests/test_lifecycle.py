import time
import unittest
from types import SimpleNamespace

from core.production_cycle import ProductionCycle
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
    """Камеры с полным API CameraManager.

    ``captures`` считает только синхронные наборы ``capture_all`` для
    инспекции; фоновый live-просмотр читает камеры через ``capture_single``
    и ``capture_roles`` и на этот счётчик не влияет.
    """

    def __init__(self):
        self.captures = 0
        self.live_reads = 0
        self.mapping = {role: index for index, role in enumerate(ROLES)}

    def capture_all(self):
        self.captures += 1
        return {role: object() for role in ROLES}

    def capture_single(self, role):
        self.live_reads += 1
        return object()

    def capture_roles(self, roles):
        self.live_reads += 1
        return {role: object() for role in roles}


class ScriptedInspector:
    INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    SPIDER_ROLES = (
        "SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP",
    )

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
    def test_part_under_cameras_at_start_is_counted_before_first_move(self):
        events = []
        inspector = ScriptedInspector([[]], [[]])
        cycle = ProductionCycle(
            FakeConveyor(events),
            FakeCameras(),
            inspector,
            FakeDistributor(events),
            archive=FakeArchive(),
            review_seconds=0,
        )
        phases = []
        original_set_process = cycle._set_process

        def record_process(phase, label, **kwargs):
            phases.append(phase)
            return original_set_process(phase, label, **kwargs)

        cycle._set_process = record_process
        self.assertTrue(cycle.request_start())

        # Первый шаг обязан пройти без движения ленты: деталь, которая
        # уже стоит под входными камерами, попадает в учёт на шаге 0.
        cycle._run_once()
        self.assertNotIn("move", events)
        self.assertEqual(cycle.current_step, 0)
        self.assertEqual(len(cycle.parts), 1)
        self.assertEqual(cycle.parts[0].step_created, 0)
        self.assertTrue(cycle.parts[0].input_inspected)
        self.assertEqual(inspector.input_calls, [(1, 0)])
        self.assertIn("INITIAL_INSPECTION", phases)
        line_parts = cycle._build_status()["line_parts"]
        self.assertEqual(line_parts, [{
            "id": 1, "position": 0, "category": "UNKNOWN",
        }])

        # Со второго шага лента едет обычным графиком.
        cycle._run_once()
        self.assertEqual(events.count("move"), 1)
        self.assertEqual(cycle.current_step, 1)

    def test_review_pause_holds_step_after_analysis(self):
        events = []
        inspector = ScriptedInspector([[]], [[]])
        cycle = ProductionCycle(
            FakeConveyor(events),
            FakeCameras(),
            inspector,
            FakeDistributor(events),
            archive=FakeArchive(),
            review_seconds=0.3,
        )
        phases = []
        original_set_process = cycle._set_process

        def record_process(phase, label, **kwargs):
            phases.append(phase)
            return original_set_process(phase, label, **kwargs)

        cycle._set_process = record_process
        self.assertTrue(cycle.request_start())
        started = time.monotonic()
        cycle._run_once()
        elapsed = time.monotonic() - started
        self.assertIn("ANALYSIS_REVIEW", phases)
        # Пауза просмотра реально удерживает шаг: оператор успевает
        # отсмотреть результат нейросетей до следующего движения.
        self.assertGreaterEqual(elapsed, 0.25)
        # После паузы шаг публикуется штатно.
        self.assertIn("STEP_COMPLETE", phases)

    def test_default_review_pause_is_at_least_five_seconds(self):
        cycle = ProductionCycle(
            FakeConveyor([]),
            FakeCameras(),
            ScriptedInspector([[]], [[]]),
            FakeDistributor([]),
            archive=FakeArchive(),
        )
        self.assertGreaterEqual(cycle.review_seconds, 5.0)

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
            # Паузу на просмотр результатов в тестах не держим: шаги
            # прогоняются синхронно, без ожидания в реальном времени.
            review_seconds=0,
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
            review_seconds=0,
        )
        conveyor.cycle = cycle
        self.assertTrue(cycle.request_start())
        # Первый шаг — контроль уже стоящей под камерами детали: лента
        # не двигается, принудительный выход из move_step пока не случается.
        cycle._run_once_safe()
        self.assertEqual(events.count("move"), 0)
        self.assertEqual(cameras.captures, 3)
        self.assertEqual(len(cycle.parts), 1)
        self.assertEqual(cycle.parts[0].step_created, 0)
        self.assertEqual(cycle.state, "RUNNING")

        # Второй шаг двигает ленту; FORCE EXIT внутри move_step обязан
        # прервать шаг до съёмки и публикации.
        cycle._run_once_safe()
        self.assertEqual(cycle.current_step, 0)
        self.assertEqual(cameras.captures, 3)
        self.assertEqual(len(cycle.parts), 1)
        self.assertEqual(cycle.parts[0].step_created, 0)
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
            review_seconds=0,
        )
        conveyor.cycle = cycle
        self.assertTrue(cycle.request_start())
        # Первый шаг — контроль без движения: STOP внутри move_step пока
        # не вызывается, деталь под камерами инспектируется на шаге 0.
        cycle._run_once()
        self.assertEqual(events.count("move"), 0)
        self.assertEqual(cycle.state, "RUNNING")
        self.assertEqual(len(cycle.parts), 1)
        self.assertEqual(cycle.parts[0].step_created, 0)
        self.assertTrue(cycle.parts[0].input_inspected)

        # Второй шаг: STOP приходит во время проезда — вошедшая этим
        # шагом позиция всё равно инспектируется (пустой лоток).
        cycle._run_once()
        self.assertEqual(cycle.state, "STOPPING")
        self.assertEqual(len(cycle.parts), 1)
        self.assertTrue(cycle.parts[0].input_inspected)

    def test_good_part_uses_exact_input_plus4_plus7_lifecycle(self):
        cycle, inspector, archive, events = self.run_one_part([], [])
        # Деталь под камерами в момент пуска инспектируется на шаге 0 без
        # движения ленты, поэтому дальнейшие позиции на 1 меньше старого
        # графика: вход 0, контроль +4, сортировка +7.
        self.assertEqual(inspector.input_calls[0], (1, 0))
        self.assertEqual(inspector.spider_calls, [(1, 4)])
        self.assertEqual(cycle.current_step, 7)
        self.assertEqual(cycle.good_count, 1)
        self.assertEqual(cycle.bad_count, 0)
        self.assertEqual(cycle.cameras.captures, 8 * 3)
        for phase in (
            "INITIAL_INSPECTION",
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
            review_seconds=0,
        )
        self.assertTrue(cycle.request_start())
        for _ in range(10):
            cycle._run_once()

        # Стартовый контроль без движения сдвигает весь график на шаг:
        # первая деталь проходит вход уже на шаге 0.
        self.assertEqual(inspector.input_calls[:3], [(1, 0), (2, 1), (3, 2)])
        self.assertEqual(
            inspector.spider_calls,
            [(1, 4), (2, 5), (3, 6)],
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


if __name__ == "__main__":
    unittest.main()
