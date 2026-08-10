"""Интеграционный тест производственного цикла на фейковом «железе».

Прогоняет ProductionCycle._run_once несколько шагов с фейковыми камерами,
конвейером, распределителем и инспектором, проверяя полный путь:
захват → анализ → создание Part → публикация снимка в UI → spider → сброс.

Ключевые проверки:
- каждый шаг снимает кадры один раз и публикует единый снимок;
- деталь создаётся на входе, на +4 проверяется, на +7 ожидает следующего шага, затем проходит заслонки в выбранный канал;
- пустые лотки на входе считаются и не создают Part.
"""

import threading
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

    def prepare_route(self, category, part_id):
        self.status["dist2_target"] = category
        self.status["last_distributor_action"] = f"ROUTE #{part_id} -> {category}"

    def confirm_transfer(self, part_id, category):
        self.drops.append((part_id, category))
        self.status["last_distributor_action"] = f"TRANSFER #{part_id} -> {category}"

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
        self.input_threads = []
        self.control_threads = []

    def inspect_input_consensus(self, part_id, step, frame_runs,
                                force_bad=False):
        self.input_calls += 1
        self.input_threads.append(threading.get_ident())
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
        self.control_threads.append(threading.get_ident())
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
        # Прогоняем 9 шагов: вход -> +4 (spider) -> +7 (ожидание маршрута) -> +8 (BAD)
        for _ in range(9):
            self.cycle._run_once_safe()

        # Ровно одна деталь (первый шаг), остальные входы — пустые лотки
        self.assertEqual(self.cycle.part_counter, 1)
        self.assertEqual(self.cycle.empty_count, 8)
        # Деталь с дефектом контактов на +4 -> BAD, на +8 сброшена
        self.assertEqual(self.cycle.bad_count, 1)
        self.assertEqual(self.distributor.drops, [(1, "BAD")])
        self.assertEqual(self.cycle.parts, [])
        self.assertEqual(self.cycle.current_step, 9)
        self.assertEqual(self.conveyor.moves, 9)
        # Spider проверялся ровно один раз (деталь прошла +4)
        self.assertEqual(self.inspector.spider_calls, 1)
        # Input вызывался на каждом шаге (accepts_new_parts=True)
        self.assertEqual(self.inspector.input_calls, 9)

    def test_workers_compute_but_control_thread_commits(self):
        owner = threading.get_ident()
        commit_threads = []
        original = self.cycle._commit_input_result

        def recording_commit(*args, **kwargs):
            commit_threads.append(threading.get_ident())
            return original(*args, **kwargs)

        self.cycle._commit_input_result = recording_commit
        self.cycle._run_once_safe()
        self.assertTrue(self.inspector.input_threads)
        self.assertNotEqual(self.inspector.input_threads[0], owner)
        self.assertEqual(commit_threads, [owner])

    def test_formal_transaction_finishes_before_final_publication(self):
        self.cycle._run_once_safe()
        snapshot = self.cycle.control_core.snapshot
        self.assertEqual(snapshot.step_phase.value, "NONE")
        self.assertIsNone(snapshot.transaction)
        self.assertEqual(snapshot.current_step, 1)
        self.assertIsInstance(snapshot.run_id, int)
        result = self.cycle._last_inspection_execution
        self.assertEqual(result.publication_version, result.snapshot.state_version)
        self.assertEqual(result.snapshot.step_phase.value, "NONE")
        self.assertFalse(hasattr(self.cycle, "_pending_stop"))
        self.assertFalse(hasattr(self.cycle, "_pending_exit"))

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

    def test_pause_invalidates_cached_empty_input_result(self):
        # Фоновая проверка могла успеть решить, что вход пуст, до того как
        # оператор нажал паузу. После паузы INPUT обязан пройти свежий захват.
        self.cycle._background_presence_usable = True
        self.cycle._background_presence_result = {"is_empty": True}

        self.assertTrue(self.cycle.request_pause())
        self.assertFalse(self.cycle._background_presence_usable)
        self.assertIsNone(self.cycle._background_presence_result)

        roles = self.cycle._capture_roles_for_current_step()
        self.assertEqual(roles, ())

        # Отдельно проверяем защиту в INPUT-анализе: даже если гонка потока
        # успела оставить старый is_empty-кэш, свежие кадры не превращаются
        # в «пусто».
        self.assertTrue(self.cycle.sm.request_pause())
        self.assertTrue(self.cycle.request_resume())
        self.assertFalse(self.cycle._resume_inspection_only)
        self.cycle._background_presence_usable = False
        self.cycle._background_presence_result = {"is_empty": True}
        frames = [{
            role: _frame(index)
            for index, role in enumerate(self.inspector.INPUT_ROLES)
        }]
        result = self.cycle._inspect_input_worker(
            frames,
            self.cycle.part_counter + 1,
            lambda presence: self.cycle._commit_input_presence(
                self.cycle.part_counter + 1, presence,
            ),
        )
        self.cycle._commit_input_result(result)
        self.assertFalse(result.is_empty_tray)
        self.assertEqual(self.inspector.input_calls, 1)


if __name__ == "__main__":
    unittest.main()
