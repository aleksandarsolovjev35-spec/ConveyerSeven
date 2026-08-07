"""Сквозная проверка сброса детали на позиции сортировки (+7).

Физика линии: корпус приезжает на +7 и ПРИДЕРЖИВАЕТСЯ лепестком; падение
происходит на следующем шаге, когда лента несёт корпус дальше (между +7
и +8) — к этому моменту DIST2 уже в нужном канале, DIST1 открыт.

Прогоняет ProductionCycle с **настоящим** Distributor (фейковые оси) и
проверяет весь маршрут:

- BAD/CLEANUP: на шаге приезда на +7 распределитель НЕ двигается, корпус
  остаётся в очереди; на следующем шаге перед движением ленты DIST2 уезжает
  в нужный канал, DIST1 открывается, корпус падает и лопасть закрывается;
- GOOD: лопасть не двигается вообще (mark_pass через _pass_good_parts);
- пустая линия: распределитель не двигается (reset_target);
- деталь с категорией UNKNOWN на +7 принудительно сбрасывается как BAD.

Каждый тест проверяет не только счётчики, но и физические позиции осей
и порядок операций относительно движения ленты.
"""

import unittest
from types import SimpleNamespace

import numpy as np

from core.production_cycle import ProductionCycle
from domain.defect_rules.base import RuleResult
from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP, Part
from hardware.distributor import Distributor
from inspection.result import InspectionResult


ROLES = ("INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
         "SPIDER_IN", "SPIDER_OUT", "TOP")

DIST1_OPEN = 120
DIST2_BAD = 80
DIST2_CLEANUP = 200


def _frame(value=0):
    return np.full((240, 320, 3), value, dtype=np.uint8)


class FakeAxis:
    """Ось, фиксирующая каждую команду движения в общий лог."""

    def __init__(self, name, log, position=0):
        self.name = name
        self.log = log
        self.position = position
        self.calls = []
        self.transport = type("T", (), {"send": lambda self, cmd: None})()

    def move_absolute(self, position):
        self.calls.append(("move", position))
        self.log.append((self.name, "move", position))
        self.position = position

    def wait_stop(self, timeout=12.0, progress_callback=None):
        if progress_callback:
            progress_callback(self.position, 0)

    def home(self):
        self.position = 0

    def verify_homed(self):
        pass


class FakeCameras:
    mapping = {role: {"index": index} for index, role in enumerate(ROLES)}

    def capture_all(self):
        return {role: _frame(index) for index, role in enumerate(ROLES)}

    def drain_buffers(self, roles=None):
        pass


class FakeConveyor:
    speed = 20000
    steps_per_division = 19048

    def __init__(self, log):
        self.log = log
        self.moves = 0

    def move_step(self):
        self.moves += 1
        self.log.append(("conveyor", "move", None))

    def wait_stop(self, timeout=15.0, progress_callback=None):
        if progress_callback:
            progress_callback({
                "raw": "", "mov": 0, "wait": 0,
                "pos": 100, "tgt": 100, "lasterr": 0,
            })

    def emergency_stop(self):
        pass


class FakeMonitor:
    def __init__(self):
        self.server = SimpleNamespace(active_camera_role=None)
        self.calls = []
        self.snapshots = []

    def update(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("line_status"):
            self.snapshots.append(kwargs["line_status"])


def _presence(empty):
    return RuleResult(
        "part_presence", False,
        details={"empty_tray": empty, "flatness_left": 0 if empty else 5,
                 "flatness_right": 0 if empty else 6},
        drawings=[],
    )


class FakeInspector:
    """Первый вход — деталь; последующие входы — пустые лотки."""

    INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    SPIDER_ROLES = ("SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN",
                    "SPIDER_OUT", "TOP")

    def __init__(self, spider_defects=(), spider_rule_names=None):
        self.spider_defects = list(spider_defects)
        self.spider_rule_names = spider_rule_names
        self.input_calls = 0
        self.spider_calls = 0

    def _input_result(self, frames, part_present):
        if not part_present:
            return InspectionResult(
                stage="input",
                defects=[],
                vision_results={role: [] for role in self.INPUT_ROLES},
                rule_results=[_presence(True)],
                annotated={},
                raw_frames=frames,
                raw_overlay_frames={},
                is_empty_tray=True,
                consensus={"runs": 1, "required_votes": 1, "rules": {}},
                model_health=[],
                run_frames=[frames],
                run_rule_results=[[]],
            )
        rule = RuleResult(
            "window_geometry", False,
            details={"per_role": {
                "INPUT_LEFT": {"valid": True, "triggered": False},
                "INPUT_RIGHT": {"valid": True, "triggered": False},
            }},
            drawings=[],
        )
        return InspectionResult(
            stage="input",
            defects=[],
            vision_results={role: [] for role in self.INPUT_ROLES},
            rule_results=[_presence(False), rule],
            annotated={},
            raw_frames=frames,
            raw_overlay_frames={},
            consensus={"runs": 1, "required_votes": 1, "rules": {}},
            model_health=[],
            run_frames=[frames],
            run_rule_results=[[rule]],
        )

    def inspect_input_consensus(self, part_id, step, frame_runs,
                                force_bad=False):
        self.input_calls += 1
        frames = frame_runs[0]
        return self._input_result(frames, self.input_calls == 1)

    def inspect_spider_consensus(self, part_id, step, frame_runs,
                                 force_bad=False):
        self.spider_calls += 1
        frames = frame_runs[0]
        names = self.spider_rule_names or self.spider_defects
        rules = [
            RuleResult(
                name, True,
                details={"per_role": {
                    "SPIDER_LEFT": {"valid": True, "triggered": True},
                    "SPIDER_RIGHT": {"valid": True, "triggered": True},
                }},
                drawings=[],
            )
            for name in names
        ]
        return InspectionResult(
            stage="spider",
            defects=self.spider_defects,
            vision_results={role: [] for role in self.SPIDER_ROLES},
            rule_results=rules,
            annotated={},
            raw_frames=frames,
            raw_overlay_frames={},
            consensus={"runs": 1, "required_votes": 1, "rules": {}},
            model_health=[],
            run_frames=[frames],
            run_rule_results=[rules],
        )


class FakeArchive:
    """Записывает вызовы finalize, ничего не пишет на диск."""

    def __init__(self):
        self.finalized = []

    def store_frames(self, **kwargs):
        pass

    def finalize(self, **kwargs):
        self.finalized.append(kwargs)
        return None

    def get_part_info(self, part_id):
        return None


class DropFlowTest(unittest.TestCase):
    def make_cycle(self, spider_defects=(), archive=None):
        self.log = []
        dist = Distributor(
            FakeAxis("dist1", self.log),
            FakeAxis("dist2", self.log),
            dist1_open_position=DIST1_OPEN,
            dist2_bad_position=DIST2_BAD,
            dist2_cleanup_position=DIST2_CLEANUP,
            drop_time=0.0,
        )
        inspector = FakeInspector(spider_defects=spider_defects)
        cycle = ProductionCycle(
            conveyor=FakeConveyor(self.log),
            cameras=FakeCameras(),
            inspector=inspector,
            distributor=dist,
            monitor=FakeMonitor(),
            archive=archive,
            jog=None,
            settle_seconds=0.0,
            stage_trace_seconds=0.0,
            review_seconds=0.0,
        )
        cycle.sm.request_start()
        return cycle

    def test_part_held_at_reject_before_drop(self):
        """На шаге приезда на +7 корпус придержан: лепесток не двигается."""
        cycle = self.make_cycle(spider_defects=["contacts_long"])
        for _ in range(8):
            cycle._run_once_safe()

        # После 8 шагов корпус стоит на позиции 7 (придержан), а сброс
        # ещё НЕ выполнен: распределитель не двигался, деталь в очереди.
        self.assertEqual(cycle.current_step, 8)
        self.assertEqual(len(cycle.parts), 1)
        self.assertEqual(cycle.parts[0].id, 1)
        self.assertEqual(cycle.parts[0].route_category, CATEGORY_BAD)
        self.assertEqual(cycle.bad_count, 0)
        self.assertEqual(cycle.distributor.dist1.calls, [])
        self.assertEqual(cycle.distributor.dist2.calls, [])
        self.assertIsNone(cycle._pending_drop)

        # Следующий шаг: лента несёт корпус к лотку (7 -> 8) и он падает.
        cycle._run_once_safe()
        self.assertEqual(cycle.bad_count, 1)
        self.assertEqual(cycle.distributor.dist1.calls,
                         [("move", DIST1_OPEN), ("move", 0)])
        self.assertEqual(cycle.distributor.dist2.calls,
                         [("move", DIST2_BAD)])
        self.assertEqual(cycle.parts, [])

    def test_bad_part_dropped(self):
        archive = FakeArchive()
        cycle = self.make_cycle(spider_defects=["contacts_long"], archive=archive)
        for _ in range(9):
            cycle._run_once_safe()

        dist = cycle.distributor
        self.assertEqual(cycle.part_counter, 1)
        self.assertEqual(cycle.bad_count, 1)
        self.assertEqual(cycle.good_count, 0)
        self.assertEqual(cycle.cleanup_count, 0)
        # Сброшенная деталь финализирована в архиве как BAD на своём шаге.
        self.assertEqual(len(archive.finalized), 1)
        record = archive.finalized[0]
        self.assertEqual(record["part_id"], 1)
        self.assertEqual(record["category"], CATEGORY_BAD)
        self.assertEqual(record["step"], 1)
        self.assertEqual(record["defects"], ["contacts_long"])
        # Лопасть открылась перед проездом (+120) и закрылась после падения.
        self.assertEqual(
            dist.dist1.calls,
            [("move", DIST1_OPEN), ("move", 0)],
        )
        # Направляющая ушла в канал BAD и осталась там.
        self.assertEqual(dist.dist2.calls, [("move", DIST2_BAD)])
        # После сброса: лопасть IDLE в HOME, направляющая READY в канале BAD.
        self.assertEqual(dist.dist1_state, "IDLE")
        self.assertEqual(dist.dist1.position, 0)
        self.assertEqual(dist.dist2_state, "READY")
        self.assertEqual(dist.dist2.position, DIST2_BAD)
        self.assertIn("DONE", dist.last_action)
        # Деталь покинула линию, pending-сброс очищен.
        self.assertEqual(cycle.parts, [])
        self.assertIsNone(cycle._pending_drop)
        self.assertEqual(cycle.current_step, 9)
        self.assertEqual(cycle.conveyor.moves, 9)
        # Порядок операций на шаге сброса: DIST2 -> DIST1 OPEN -> движение
        # ленты -> DIST1 CLOSE (сброс после остановки ленты, до съёмки).
        # Всё, что до этого, — обычные шаги с пустым входом (только лента).
        self.assertEqual(
            self.log[-4:],
            [("dist2", "move", DIST2_BAD),
             ("dist1", "move", DIST1_OPEN),
             ("conveyor", "move", None),
             ("dist1", "move", 0)],
        )
        # Лента двигалась все 9 шагов; распределитель — только на сбросе.
        self.assertEqual(
            [e for e in self.log if e[0] == "conveyor"],
            [("conveyor", "move", None)] * 9,
        )
        # В последнем снимке UI деталь уже покинула линию, счётчик брака
        # вырос, а распределитель вернулся в рабочее состояние.
        snap = cycle.monitor.snapshots[-1]
        self.assertEqual(snap["in_line"], 0)
        self.assertEqual(snap["line_parts"], [])
        self.assertEqual(snap["rejected"], 1)
        self.assertEqual(snap["distributor_state"], "IDLE")
        self.assertEqual(snap["dist1_position"], 0)
        self.assertEqual(snap["dist2_position"], DIST2_BAD)

    def test_cleanup_part_dropped_to_cleanup_channel(self):
        archive = FakeArchive()
        cycle = self.make_cycle(spider_defects=["glass"], archive=archive)
        for _ in range(9):
            cycle._run_once_safe()

        dist = cycle.distributor
        self.assertEqual(cycle.cleanup_count, 1)
        self.assertEqual(cycle.bad_count, 0)
        self.assertEqual(dist.dist2.position, DIST2_CLEANUP)
        self.assertEqual(dist.dist2.calls, [("move", DIST2_CLEANUP)])
        self.assertEqual(dist.dist1.position, 0)
        self.assertEqual(dist.dist1_state, "IDLE")
        self.assertIn("DONE", dist.last_action)
        self.assertEqual(cycle.parts, [])
        # В архив ушла как CLEANUP.
        self.assertEqual(len(archive.finalized), 1)
        self.assertEqual(archive.finalized[0]["category"], CATEGORY_CLEANUP)
        self.assertEqual(archive.finalized[0]["defects"], ["glass"])

    def test_good_part_passes_without_distributor_motion(self):
        cycle = self.make_cycle(spider_defects=[])
        for _ in range(8):
            cycle._run_once_safe()

        dist = cycle.distributor
        self.assertEqual(cycle.good_count, 1)
        self.assertEqual(cycle.bad_count, 0)
        # Годная деталь не трогает распределитель вообще.
        self.assertEqual(dist.dist1.calls, [])
        self.assertEqual(dist.dist2.calls, [])
        self.assertIn("PASS", dist.last_action)
        self.assertEqual(cycle.parts, [])

    def test_empty_line_no_distributor_motion(self):
        cycle = self.make_cycle(spider_defects=["contacts_long"])
        cycle.inspector.input_calls = 1  # сдвигаем «деталь»: только пустые входы
        for _ in range(3):
            cycle._run_once_safe()

        dist = cycle.distributor
        self.assertEqual(cycle.part_counter, 0)
        self.assertEqual(cycle.bad_count, 0)
        self.assertEqual(cycle.empty_count, 3)
        self.assertEqual(dist.dist1.calls, [])
        self.assertEqual(dist.dist2.calls, [])
        self.assertEqual(cycle.parts, [])

    def test_drop_failure_faults_line_and_keeps_part(self):
        """Сбой физического сброса уводит линию в FAULT, деталь не теряется."""
        self.log = []
        dist = Distributor(
            FakeAxis("dist1", self.log),
            FakeAxis("dist2", self.log),
            dist1_open_position=DIST1_OPEN,
            dist2_bad_position=DIST2_BAD,
            dist2_cleanup_position=DIST2_CLEANUP,
            drop_time=0.0,
        )

        def fail_drop(*args, **kwargs):
            raise RuntimeError("flap jammed")

        dist.drop_and_close = fail_drop
        cycle = ProductionCycle(
            conveyor=FakeConveyor(self.log),
            cameras=FakeCameras(),
            inspector=FakeInspector(spider_defects=["contacts_long"]),
            distributor=dist,
            monitor=FakeMonitor(),
            archive=None,
            jog=None,
            settle_seconds=0.0,
            stage_trace_seconds=0.0,
            review_seconds=0.0,
        )
        cycle.sm.request_start()

        for _ in range(9):
            cycle._run_once_safe()

        # Ошибка сброса = FAULT, а не «тихий» пропуск детали.
        self.assertEqual(cycle.state, "FAULT")
        self.assertIn("flap jammed", cycle._fault_reason)
        # Деталь остаётся на учёте до вмешательства оператора.
        self.assertEqual(len(cycle.parts), 1)
        self.assertIsNotNone(cycle._pending_drop)
        # Счётчик брака не накручен: сброс физически не подтверждён.
        self.assertEqual(cycle.bad_count, 0)

    def test_unknown_part_forced_bad_at_reject(self):
        cycle = self.make_cycle(spider_defects=["contacts_long"])
        # Деталь без полной инспекции (категория UNKNOWN) доехала до +7.
        # Штатно это возможно только при сбое на стыке инспекций, поэтому
        # проверяем напрямую пару _prepare_drop/_execute_drop.
        part = Part(1, 0)
        cycle.parts.append(part)
        cycle._pending_drop = part

        cycle._prepare_drop()
        self.assertEqual(part.route_category, CATEGORY_BAD)
        self.assertEqual(part.final_decision, "incomplete_inspection")
        self.assertEqual(cycle.distributor.dist2_target, CATEGORY_BAD)
        self.assertEqual(cycle.distributor.dist2.position, DIST2_BAD)
        self.assertEqual(cycle.distributor.dist1.position, DIST1_OPEN)

        cycle._execute_drop()
        self.assertEqual(cycle.bad_count, 1)
        self.assertEqual(cycle.distributor.dist2.position, DIST2_BAD)
        self.assertEqual(cycle.distributor.dist1.position, 0)
        self.assertEqual(cycle.parts, [])
        self.assertIsNone(cycle._pending_drop)


if __name__ == "__main__":
    unittest.main()
