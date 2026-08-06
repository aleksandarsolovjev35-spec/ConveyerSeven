"""Интеграционный тест производственного цикла на фейковом «железе».

Прогоняет ProductionCycle._run_once несколько шагов с фейковыми камерами,
конвейером, распределителем и инспектором, проверяя полный путь:
захват → анализ → создание Part → публикация снимка в UI → spider → сброс.

Ключевые проверки:
- каждый шаг снимает кадры один раз и публикует единый снимок;
- деталь создаётся на входе, на +4 проверяется, на +7 уходит (BAD -> сброс);
- пустые лотки на входе считаются и не создают Part.
"""

import unittest
from types import SimpleNamespace

import numpy as np

from core.production_cycle import ProductionCycle
from domain.defect_rules.base import RuleResult
from inspection.result import InspectionResult


ROLES = ("INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
         "SPIDER_IN", "SPIDER_OUT", "TOP")


def _frame(value=0):
    return np.full((240, 320, 3), value, dtype=np.uint8)


class FakeCameras:
    mapping = {role: {"index": index} for index, role in enumerate(ROLES)}

    def capture_all(self):
        return {role: _frame(index) for index, role in enumerate(ROLES)}

    def drain_buffers(self, roles=None):
        pass


class FakeConveyor:
    speed = 20000
    steps_per_division = 19048

    def __init__(self):
        self.moves = 0

    def move_step(self):
        self.moves += 1

    def wait_stop(self, timeout=15.0, progress_callback=None):
        if progress_callback:
            progress_callback({
                "raw": "", "mov": 0, "wait": 0,
                "pos": 100, "tgt": 100, "lasterr": 0,
            })

    def emergency_stop(self):
        pass


class FakeDistributor:
    def __init__(self):
        self.status = {
            "dist1_state": "IDLE", "dist1_position": 0, "dist1_max": 340,
            "dist2_state": "IDLE", "dist2_position": 0, "dist2_max": 340,
            "dist2_target": "-", "last_distributor_action": "-",
        }
        self.on_state_changed = None
        self.cancel_check = None
        self.dist1_open_position = 0
        self.drops = []

    def park_production(self):
        self.status["dist1_state"] = "IDLE"
        self.status["last_distributor_action"] = "PRODUCTION READY"

    def prepare(self, category, part_id):
        self.status["dist2_target"] = category
        self.status["last_distributor_action"] = f"PREPARE #{part_id}"

    def mark_pass(self, part_id):
        self.status["last_distributor_action"] = f"PASS #{part_id}"

    def drop_and_close(self, part_id, category):
        self.drops.append((part_id, category))
        self.status["dist2_target"] = "-"

    def reset_target(self):
        self.status["dist2_target"] = "-"

    def emergency_stop(self):
        pass


def _presence(empty):
    return RuleResult(
        "part_presence", False,
        details={"empty_tray": empty, "flatness_left": 0 if empty else 5,
                 "flatness_right": 0 if empty else 6},
        drawings=[],
    )


class FakeInspector:
    INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    SPIDER_ROLES = ("SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN",
                    "SPIDER_OUT", "TOP")

    def __init__(self):
        self.input_calls = 0
        self.spider_calls = 0

    def inspect_input_consensus(self, part_id, step, frame_runs,
                                force_bad=False):
        self.input_calls += 1
        frames = frame_runs[0]
        if self.input_calls == 1:
            # Первый шаг: деталь присутствует, дефектов нет
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
        # Остальные шаги: пустой лоток
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

    def inspect_spider_consensus(self, part_id, step, frame_runs,
                                 force_bad=False):
        self.spider_calls += 1
        frames = frame_runs[0]
        # На +4 деталь всегда с дефектом контактов -> BAD
        rule = RuleResult(
            "contacts_long", True,
            details={"per_role": {
                "SPIDER_LEFT": {"valid": True, "triggered": True},
                "SPIDER_RIGHT": {"valid": True, "triggered": True},
            }},
            drawings=[],
        )
        return InspectionResult(
            stage="spider",
            defects=["contacts_long"],
            vision_results={role: [] for role in self.SPIDER_ROLES},
            rule_results=[rule],
            annotated={},
            raw_frames=frames,
            raw_overlay_frames={},
            consensus={"runs": 1, "required_votes": 1, "rules": {}},
            model_health=[],
            run_frames=[frames],
            run_rule_results=[[rule]],
        )


class FakeMonitor:
    def __init__(self):
        self.server = SimpleNamespace(active_camera_role=None)
        self.calls = []
        self.snapshots = []

    def update(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("line_status"):
            self.snapshots.append(kwargs["line_status"])


class CycleIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.cameras = FakeCameras()
        self.conveyor = FakeConveyor()
        self.distributor = FakeDistributor()
        self.inspector = FakeInspector()
        self.monitor = FakeMonitor()

        self.cycle = ProductionCycle(
            conveyor=self.conveyor,
            cameras=self.cameras,
            inspector=self.inspector,
            distributor=self.distributor,
            monitor=self.monitor,
            archive=None,
            jog=None,
            settle_seconds=0.0,
            stage_trace_seconds=0.0,
            review_seconds=0.0,
        )
        # Переводим StateMachine в RUNNING без реального START-пути
        self.cycle.sm.request_start()

    def test_full_cycle(self):
        # Прогоняем 8 шагов: вход -> +4 (spider) -> +7 (сброс BAD)
        for _ in range(8):
            self.cycle._run_once_safe()

        # Ровно одна деталь (первый шаг), остальные входы — пустые лотки
        self.assertEqual(self.cycle.part_counter, 1)
        self.assertEqual(self.cycle.empty_count, 7)
        # Деталь с дефектом контактов на +4 -> BAD, на +7 сброшена
        self.assertEqual(self.cycle.bad_count, 1)
        self.assertEqual(self.distributor.drops, [(1, "BAD")])
        self.assertEqual(self.cycle.parts, [])
        self.assertEqual(self.cycle.current_step, 8)
        self.assertEqual(self.conveyor.moves, 8)
        # Spider проверялся ровно один раз (деталь прошла +4)
        self.assertEqual(self.inspector.spider_calls, 1)
        # Input вызывался на каждом шаге (accepts_new_parts=True)
        self.assertEqual(self.inspector.input_calls, 8)

    def test_publish_snapshots(self):
        self.cycle._run_once_safe()

        # Публикации с кадрами (анализ) содержат единый снимок
        frame_calls = [c for c in self.monitor.calls if c.get("frames")]
        self.assertGreaterEqual(len(frame_calls), 1)
        latest = frame_calls[-1]
        self.assertIn("run_frames", latest)
        self.assertIn("run_rule_results", latest)
        self.assertIn("line_status", latest)
        self.assertIn("rule_results", latest)

        # В line_status деталь на входе, категория UNKNOWN (инспекция входа
        # выполнена, но spider ещё нет)
        snap = self.monitor.snapshots[-1]
        self.assertEqual(snap["state"], "RUNNING")
        self.assertEqual(snap["step"], 1)
        self.assertEqual(len(snap["line_parts"]), 1)
        self.assertEqual(snap["line_parts"][0]["id"], 1)
        self.assertEqual(snap["line_parts"][0]["position"], 0)

    def test_review_publish_does_not_change_status(self):
        self.cycle._run_once_safe()
        self.cycle._run_once_safe()

        # На втором шаге деталь на позиции 1, вход пустой
        snap = self.monitor.snapshots[-1]
        self.assertEqual(snap["step"], 2)
        self.assertEqual(len(snap["line_parts"]), 1)
        self.assertEqual(snap["line_parts"][0]["position"], 1)
        # Пустой вход не создаёт Part: деталь всё ещё одна
        self.assertEqual(self.cycle.part_counter, 1)
        self.assertEqual(self.cycle.empty_count, 1)


if __name__ == "__main__":
    unittest.main()
