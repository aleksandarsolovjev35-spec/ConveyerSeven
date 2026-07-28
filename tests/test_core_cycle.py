import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.production_cycle import ProductionCycle
from core.state_machine import State
from domain.part import (
    CATEGORY_BAD,
    CATEGORY_CLEANUP,
    CATEGORY_GOOD,
    Part,
)


class FakeConveyor:
    def __init__(self, fail_move=False):
        self.fail_move = fail_move
        self.moves = 0
        self.stops = 0

    def move_step(self):
        self.moves += 1
        if self.fail_move:
            raise RuntimeError("move failed")

    def wait_stop(self, progress_callback=None):
        if progress_callback is not None:
            progress_callback({"mov": 0, "wait": 0, "lasterr": 0})
        return None

    def emergency_stop(self):
        self.stops += 1


class FakeCameras:
    def __init__(self):
        self.single_captures = 0

    def capture_all(self):
        return {}

    def capture_single(self, role):
        self.single_captures += 1
        return object()


class FailingCameras:
    def capture_all(self):
        raise RuntimeError("camera read failed")

    def capture_single(self, role):
        raise RuntimeError("camera read failed")


class RoleTrackingCameras:
    ROLES = (
        "INPUT_LEFT",
        "INPUT_RIGHT",
        "SPIDER_LEFT",
        "SPIDER_RIGHT",
        "SPIDER_IN",
        "SPIDER_OUT",
        "TOP",
    )

    def __init__(self):
        self.mapping = {role: index for index, role in enumerate(self.ROLES)}
        self.capture_counts = {role: 0 for role in self.ROLES}
        self.lock = threading.Lock()

    def capture_single(self, role):
        with self.lock:
            self.capture_counts[role] += 1
        return object()

    def capture_roles(self, roles):
        frames = {}
        with self.lock:
            for role in roles:
                self.capture_counts[role] += 1
                frames[role] = object()
        return frames

    def capture_all(self):
        return self.capture_roles(self.ROLES)


class FakeInspector:
    pass


class ActiveCameraMonitor:
    def __init__(self):
        self.server = SimpleNamespace(active_camera_role="TOP")
        self.updates = 0
        self.updated_frame_roles = []

    def update(self, **kwargs):
        self.updates += 1
        self.updated_frame_roles.extend((kwargs.get("frames") or {}).keys())


class FakeDistributor:
    def __init__(self):
        self.on_state_changed = None
        self.dist1_open_position = 340
        self.stops = 0
        self.diagnostics = []

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
        self.diagnostics.append(("park", "production"))

    def diagnostic_gate(self, position):
        self.diagnostics.append(("gate", position))

    def diagnostic_route(self, category):
        self.diagnostics.append(("route", category))

    def emergency_stop(self):
        self.stops += 1


class FakeArchive:
    def __init__(self):
        self.records = []

    def finalize(self, **kwargs):
        self.records.append(kwargs)


class FakeJog:
    def __init__(self):
        self.releases = []
        self.starts = []
        self.heartbeats = []
        self._busy = False
        self._error = None
        self.micro_steps = 500
        self.nudge_limit_steps = 1000
        self._nudge_offset = 0
        self.nudges = []
        self.resets = 0
        self.nudge_exception = None

    def start_hold(self, direction):
        self.starts.append(direction)
        self._busy = True
        return True

    def heartbeat(self, direction):
        self.heartbeats.append(direction)
        return True

    def release(self, reason="released"):
        self.releases.append(reason)
        self._busy = False
        return True

    @property
    def nudge_offset(self):
        return self._nudge_offset

    def reset_nudge_offset(self):
        self.resets += 1
        self._nudge_offset = 0

    def nudge(self, direction, steps=None):
        if self.nudge_exception is not None:
            raise self.nudge_exception
        if direction not in ("+", "-"):
            raise ValueError("bad direction")
        requested = self.micro_steps if steps is None else int(steps)
        if requested > self.nudge_limit_steps:
            raise ValueError("above single press limit")
        signed = requested if direction == "+" else -requested
        clamped = max(
            -self.nudge_limit_steps,
            min(self.nudge_limit_steps, self._nudge_offset + signed),
        )
        applied = clamped - self._nudge_offset
        self._nudge_offset = clamped
        self.nudges.append(applied)
        return applied

    @property
    def busy(self):
        return self._busy

    @property
    def status(self):
        return {
            "hold_steps": 1000000,
            "last_action": "-",
            "busy": self._busy,
            "direction": None,
            "error": self._error,
            "micro_steps": self.micro_steps,
            "nudge_limit_steps": self.nudge_limit_steps,
            "nudge_offset": self._nudge_offset,
            "nudge_remaining_forward": (
                self.nudge_limit_steps - self._nudge_offset
            ),
            "nudge_remaining_backward": (
                self.nudge_limit_steps + self._nudge_offset
            ),
        }


class CoreCycleTests(unittest.TestCase):
    def make_cycle(self, conveyor=None, jog=None, archive=None):
        return ProductionCycle(
            conveyor or FakeConveyor(),
            FakeCameras(),
            FakeInspector(),
            FakeDistributor(),
            archive=archive,
            jog=jog,
        )

    def test_fault_transition_is_latched_from_running(self):
        cycle = self.make_cycle(FakeConveyor(fail_move=True))
        self.assertTrue(cycle.request_start())
        cycle._run_once_safe()
        self.assertEqual(cycle.sm.state, State.FAULT)
        self.assertEqual(cycle.current_step, 0)
        self.assertGreaterEqual(cycle.conveyor.stops, 1)
        self.assertGreaterEqual(cycle.distributor.stops, 1)
        self.assertFalse(cycle.request_start())

    # ─── Пауза внутри цикла и коррекция ленты ────────────────────

    def test_pause_is_rejected_outside_running(self):
        cycle = self.make_cycle(jog=FakeJog())
        self.assertFalse(cycle.request_pause())
        self.assertEqual(cycle.sm.state, State.IDLE)

    def test_pause_request_does_not_interrupt_running_state_immediately(self):
        cycle = self.make_cycle(jog=FakeJog())
        self.assertTrue(cycle.request_start())

        # Запрос паузы не меняет состояние сразу: шаг обязан завершиться.
        self.assertTrue(cycle.request_pause())
        self.assertEqual(cycle.sm.state, State.RUNNING)
        self.assertEqual(cycle._process["phase"], "PAUSE_REQUESTED")
        self.assertTrue(cycle._pause_requested.is_set())
        # Повторный запрос идемпотентен.
        self.assertTrue(cycle.request_pause())

    def test_nudge_never_moves_conveyor_or_shifts_part_map(self):
        conveyor = FakeConveyor()
        cycle = self.make_cycle(conveyor, jog=FakeJog())
        self.assertTrue(cycle.request_start())
        # Имитируем деталь, уже стоящую в своей ячейке.
        cycle.current_step = 3
        part = Part(1, 0)
        cycle.parts.append(part)

        self.assertTrue(cycle.sm.request_pause())
        cycle._enter_pause_frame()
        self.assertEqual(cycle.sm.state, State.PAUSED)

        moves_before = conveyor.moves
        position_before = cycle._build_status()["line_parts"][0]["position"]
        for _ in range(6):
            cycle.nudge_belt("+")

        # Коррекция не выполняет шаг ленты и не сдвигает логическую карту.
        self.assertEqual(conveyor.moves, moves_before)
        self.assertEqual(cycle.current_step, 3)
        self.assertEqual(
            cycle._build_status()["line_parts"][0]["position"],
            position_before,
        )
        cycle._stop_pause_frame_loop()

    def test_nudge_requires_paused_state(self):
        jog = FakeJog()
        cycle = self.make_cycle(jog=jog)
        # IDLE
        self.assertFalse(cycle.nudge_belt("+"))
        self.assertTrue(cycle.request_start())
        # RUNNING
        self.assertFalse(cycle.nudge_belt("+"))
        self.assertEqual(jog.nudges, [])

    def test_nudge_clamps_accumulated_offset_and_reports_limit(self):
        jog = FakeJog()
        cycle = self.make_cycle(jog=jog)
        self.assertTrue(cycle.request_start())
        self.assertTrue(cycle.sm.request_pause())
        cycle._enter_pause_frame()

        self.assertTrue(cycle.nudge_belt("+"))
        self.assertTrue(cycle.nudge_belt("+"))
        self.assertEqual(jog.nudge_offset, 1000)
        self.assertEqual(cycle._process["phase"], "PAUSE_NUDGE")

        # Сверх лимита лента не двигается, оператор получает явный статус.
        self.assertTrue(cycle.nudge_belt("+"))
        self.assertEqual(jog.nudge_offset, 1000)
        self.assertEqual(cycle._process["phase"], "PAUSE_NUDGE_LIMIT")
        cycle._stop_pause_frame_loop()

    def test_pause_resets_nudge_offset_on_every_entry(self):
        jog = FakeJog()
        cycle = self.make_cycle(jog=jog)
        self.assertTrue(cycle.request_start())
        self.assertTrue(cycle.sm.request_pause())
        cycle._enter_pause_frame()
        cycle.nudge_belt("+")
        self.assertEqual(jog.nudge_offset, 500)

        self.assertTrue(cycle.request_resume())
        self.assertEqual(cycle.sm.state, State.RUNNING)

        self.assertTrue(cycle.sm.request_pause())
        cycle._enter_pause_frame()
        # Новая пауза обязана начинаться с нулевого накопителя.
        self.assertEqual(jog.nudge_offset, 0)
        self.assertGreaterEqual(jog.resets, 2)
        cycle._stop_pause_frame_loop()

    def test_hardware_failure_during_nudge_faults_the_line(self):
        jog = FakeJog()
        cycle = self.make_cycle(jog=jog)
        self.assertTrue(cycle.request_start())
        self.assertTrue(cycle.sm.request_pause())
        cycle._enter_pause_frame()

        jog.nudge_exception = RuntimeError("motion rejected")
        self.assertFalse(cycle.nudge_belt("+"))
        # Неизвестная позиция ленты обязана останавливать линию.
        self.assertEqual(cycle.sm.state, State.FAULT)
        self.assertGreaterEqual(cycle.conveyor.stops, 1)

    def test_invalid_nudge_request_does_not_fault_the_line(self):
        jog = FakeJog()
        cycle = self.make_cycle(jog=jog)
        self.assertTrue(cycle.request_start())
        self.assertTrue(cycle.sm.request_pause())
        cycle._enter_pause_frame()

        # Некорректный ввод оператора — не отказ железа.
        self.assertFalse(cycle.nudge_belt("*"))
        self.assertEqual(cycle.sm.state, State.PAUSED)
        cycle._stop_pause_frame_loop()

    def test_stop_from_pause_drains_line_normally(self):
        cycle = self.make_cycle(jog=FakeJog())
        self.assertTrue(cycle.request_start())
        self.assertTrue(cycle.sm.request_pause())
        cycle._enter_pause_frame()

        self.assertTrue(cycle.request_stop())
        self.assertEqual(cycle.sm.state, State.STOPPING)
        self.assertFalse(cycle._pause_requested.is_set())
        cycle._stop_pause_frame_loop()

    def test_resume_returns_line_to_running(self):
        cycle = self.make_cycle(jog=FakeJog())
        self.assertTrue(cycle.request_start())
        self.assertTrue(cycle.sm.request_pause())
        cycle._enter_pause_frame()
        self.assertTrue(cycle.request_resume())
        self.assertEqual(cycle.sm.state, State.RUNNING)
        self.assertFalse(cycle._pause_frame_active)
        self.assertFalse(cycle.request_resume())

    def test_status_exposes_pause_budget_and_controls(self):
        jog = FakeJog()
        cycle = self.make_cycle(jog=jog)
        status = cycle._build_status()
        self.assertFalse(status["pause"]["active"])
        self.assertFalse(status["controls"]["pause"])
        self.assertFalse(status["controls"]["nudge"])

        self.assertTrue(cycle.request_start())
        status = cycle._build_status()
        self.assertTrue(status["controls"]["pause"])
        self.assertFalse(status["controls"]["nudge"])

        self.assertTrue(cycle.sm.request_pause())
        cycle._enter_pause_frame()
        cycle.nudge_belt("-")
        status = cycle._build_status()
        self.assertTrue(status["pause"]["active"])
        self.assertTrue(status["controls"]["nudge"])
        self.assertTrue(status["controls"]["resume"])
        self.assertTrue(status["controls"]["stop"])
        self.assertEqual(status["pause"]["nudge_offset"], -500)
        self.assertEqual(status["pause"]["nudge_limit_steps"], 1000)
        self.assertEqual(status["pause"]["remaining_backward"], 500)
        self.assertEqual(status["pause"]["remaining_forward"], 1500)
        cycle._stop_pause_frame_loop()

    def test_fault_clears_pause_request(self):
        cycle = self.make_cycle(FakeConveyor(fail_move=True), jog=FakeJog())
        self.assertTrue(cycle.request_start())
        self.assertTrue(cycle.request_pause())
        cycle._run_once_safe()
        self.assertEqual(cycle.sm.state, State.FAULT)
        self.assertFalse(cycle._pause_requested.is_set())
        self.assertFalse(cycle._pause_frame_active)

    def test_stop_on_empty_line_does_not_move_conveyor(self):
        cycle = self.make_cycle()
        self.assertTrue(cycle.request_start())
        self.assertTrue(cycle.request_stop())
        thread = threading.Thread(target=cycle.start)
        thread.start()
        deadline = time.monotonic() + 1.0
        while cycle.sm.state != State.STOPPED and time.monotonic() < deadline:
            time.sleep(0.01)
        cycle.request_force_exit()
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cycle.sm.state, State.STOPPED)
        self.assertEqual(cycle.conveyor.moves, 0)

    def test_selected_live_camera_is_capped_at_thirty_fps(self):
        jog = FakeJog()
        cameras = FakeCameras()
        monitor = ActiveCameraMonitor()
        cycle = ProductionCycle(
            FakeConveyor(),
            cameras,
            FakeInspector(),
            FakeDistributor(),
            monitor=monitor,
            jog=jog,
        )
        self.assertTrue(cycle.enter_jog())
        time.sleep(0.14)
        self.assertTrue(cycle.exit_jog())
        self.assertGreaterEqual(cameras.single_captures, 3)
        self.assertGreater(cycle._current_live_fps(), 20.0)
        self.assertLessEqual(cycle._current_live_fps(), 30.0)
        self.assertGreaterEqual(monitor.updates, cameras.single_captures)

    def test_jog_keeps_selected_camera_fast_and_refreshes_all_aux_previews(self):
        jog = FakeJog()
        cameras = RoleTrackingCameras()
        monitor = ActiveCameraMonitor()
        cycle = ProductionCycle(
            FakeConveyor(),
            cameras,
            FakeInspector(),
            FakeDistributor(),
            monitor=monitor,
            jog=jog,
        )
        with patch("core.production_cycle.JOG_AUX_BATCH_INTERVAL", 0.04):
            self.assertTrue(cycle.enter_jog())
            time.sleep(0.16)
            self.assertTrue(cycle.exit_jog())

        self.assertTrue(all(cameras.capture_counts.values()))
        self.assertGreaterEqual(cameras.capture_counts["TOP"], 3)
        self.assertTrue(
            all(
                count >= 1
                for role, count in cameras.capture_counts.items()
                if role != "TOP"
            )
        )
        self.assertLessEqual(cycle._current_live_fps(), 30.0)
        self.assertEqual(
            set(monitor.updated_frame_roles),
            set(RoleTrackingCameras.ROLES),
        )

    def test_start_is_rejected_if_jog_camera_failed_during_shutdown(self):
        jog = FakeJog()
        cycle = ProductionCycle(
            FakeConveyor(),
            FailingCameras(),
            FakeInspector(),
            FakeDistributor(),
            jog=jog,
        )
        self.assertTrue(cycle.enter_jog())
        deadline = time.monotonic() + 1.0
        while not cycle._jog_frame_error and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(cycle._jog_frame_error)
        self.assertFalse(cycle.request_start())
        self.assertNotIn(("park", "production"), cycle.distributor.diagnostics)
        self.assertTrue(cycle.exit_jog())

    def test_jog_camera_failure_faults_line_and_blocks_start(self):
        jog = FakeJog()
        cycle = ProductionCycle(
            FakeConveyor(),
            FailingCameras(),
            FakeInspector(),
            FakeDistributor(),
            jog=jog,
        )
        self.assertTrue(cycle.enter_jog())
        thread = threading.Thread(target=cycle.start)
        thread.start()
        deadline = time.monotonic() + 1.0
        while cycle.state != "FAULT" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(cycle.state, "FAULT")
        self.assertIn(
            "Ошибка камеры в режиме ручного управления",
            cycle._build_status()["fault_reason"],
        )
        self.assertFalse(cycle._build_status()["controls"]["start"])
        cycle.request_force_exit()
        thread.join(1.0)
        self.assertFalse(thread.is_alive())

    def test_fault_forces_jog_release_and_exposes_reason(self):
        jog = FakeJog()
        cycle = self.make_cycle(jog=jog)
        self.assertTrue(cycle.enter_jog())
        cycle._handle_fault("camera disconnected")
        self.assertFalse(cycle.jog_active)
        self.assertIn("leaving JOG mode", jog.releases)
        status = cycle._build_status()
        self.assertEqual(status["fault_reason"], "camera disconnected")
        self.assertEqual(status["process"]["phase"], "FAULT")

    def test_jog_is_forbidden_in_fault(self):
        cycle = self.make_cycle(jog=FakeJog())
        self.assertTrue(cycle.sm.notify_fault())
        self.assertFalse(cycle.can_enter_jog())
        self.assertFalse(cycle.enter_jog())

    def test_jog_hold_callbacks_and_exit_release(self):
        jog = FakeJog()
        cycle = self.make_cycle(jog=jog)
        self.assertTrue(cycle.enter_jog())
        self.assertTrue(cycle.jog_hold_start("+"))
        self.assertTrue(cycle.jog_hold_heartbeat("+"))
        self.assertTrue(cycle.exit_jog())
        self.assertEqual(jog.starts, ["+"])
        self.assertEqual(jog.heartbeats, ["+"])
        self.assertEqual(jog.releases, ["leaving JOG mode"])
        self.assertFalse(cycle.jog_active)
        self.assertFalse(cycle.jog_hold_release("stale release"))
        self.assertEqual(jog.releases, ["leaving JOG mode"])

    def test_ui_rule_report_contains_only_rejection_reason(self):
        omission = SimpleNamespace(
            rule_name="long_omission",
            triggered=True,
            details={"per_role": {"SPIDER_LEFT": {
                "triggered": True,
                "reason": None,
                "allowed_thickness_px": 20.0,
                "excess_pixels": 340,
                "largest_component_pixels": 340,
                "excess_component_min_px": 3,
                "max_excess_depth_px": 18.0,
                "top_line_actual_max_residual_px": 1.2,
                "top_line_max_residual_px": 3.0,
                "max_consecutive_columns": 24,
            }}},
        )
        row = ProductionCycle._rule_report_row(omission)
        self.assertTrue(row["show_detail"])
        self.assertIn("толщина 20.0 px", row["detail"])
        self.assertIn("component min 3 px", row["detail"])
        self.assertIn("residual 1.2/3.0 px", row["detail"])
        self.assertIn("largest component 340 px", row["detail"])
        self.assertIn("confirmed 340 px", row["detail"])
        self.assertIn("max depth 18.0 px", row["detail"])
        self.assertNotIn("столб", row["detail"])
        self.assertNotIn("area", row["detail"].lower())

        contact = SimpleNamespace(
            rule_name="contacts_short",
            triggered=True,
            details={"per_role": {"SPIDER_IN": {
                "triggered": True,
                "reason": None,
                "ignored": 0,
                "area_absolute_min_px2": 400,
                "tolerance": 10.0,
                "delta_top": 0.0,
                "delta_bottom": 0.0,
                "delta_height": 0.0,
                "rect_width_mm": 1.74,
                "rect_height_mm": 0.66,
                "omission_tilt_ratio_max": 0.2,
                "omission_tilt_check": {
                    "status": "error",
                    "reason": "no_valid_omission_top_line",
                },
                "inscribe_check": {
                    "status": "ok",
                    "scale_px_per_mm": 14.5,
                },
                "items": [
                    {
                        "index": 1, "top_y": 100, "bottom_y": 130,
                        "height_px": 30, "rect_fits": True,
                        "omission_distance_px": None,
                    },
                    {
                        "index": 2, "top_y": 100, "bottom_y": 130,
                        "height_px": 30, "rect_fits": True,
                        "omission_distance_px": None,
                    },
                ],
            }}},
        )
        row = ProductionCycle._rule_report_row(contact)
        self.assertTrue(row["show_detail"])
        self.assertIn("нет valid reference omission-short", row["detail"])
        self.assertIn("SPIDER_IN #1", row["detail"])
        self.assertIn("SPIDER_IN #2", row["detail"])
        self.assertIn("rect OK", row["detail"])

        presence = SimpleNamespace(
            rule_name="part_presence",
            triggered=False,
            details={
                "empty_tray": True,
                "flatness_left": 2,
                "flatness_right": 1,
                "false_positive_ignored_left": 2,
                "false_positive_ignored_right": 1,
                "false_positive_max_count_by_role": {
                    "INPUT_LEFT": 2,
                    "INPUT_RIGHT": 2,
                },
            },
        )
        row = ProductionCycle._rule_report_row(presence)
        self.assertEqual(row["detail"], "ДЕТАЛЬ НЕ ОБНАРУЖЕНА")
        self.assertEqual(row["status_label"], "ДЕТАЛЬ НЕ ОБНАРУЖЕНА")
        self.assertTrue(row["neutral"])
        self.assertFalse(row["show_detail"])
        self.assertFalse(row["triggered"])

        sinks = SimpleNamespace(
            rule_name="window_sinks",
            triggered=True,
            details={"per_role": {"INPUT_LEFT": {
                "triggered": True,
                "reason": None,
                "overlap_min_px": 5,
                "hits": [
                    {"sink_index": 1, "window_index": 2, "overlap_px": 8},
                    {"sink_index": 2, "window_index": 5, "overlap_px": 13},
                ],
            }}},
        )
        row = ProductionCycle._rule_report_row(sinks)
        self.assertIn("раковина #1 → окно #2: overlap 8 px >= 5 px", row["detail"])
        self.assertIn("раковина #2 → окно #5: overlap 13 px >= 5 px", row["detail"])
        self.assertNotIn("снаружи", row["detail"])

    def test_window_geometry_report_exposes_all_seven_rows(self):
        items = [
            {
                "index": index,
                "valid": True,
                "top_px": 30 + index / 10,
                "bottom_px": 29 + index / 10,
                "top_fail": index == 4,
                "bottom_fail": False,
            }
            for index in range(1, 8)
        ]
        result = SimpleNamespace(
            rule_name="window_geometry",
            triggered=True,
            details={"per_role": {"INPUT_LEFT": {
                "triggered": True,
                "reason": None,
                "found": 7,
                "expected_count": 7,
                "ignored": 1,
                "top_limits_px": [20, 40],
                "bottom_limits_px": [20, 40],
                "items": items,
            }}},
        )
        row = ProductionCycle._rule_report_row(result)
        self.assertTrue(row["show_detail"])
        self.assertEqual(len(row["detail_lines"]), 9)
        for index in range(1, 8):
            self.assertTrue(any(
                f"INPUT_LEFT #{index}" in line
                for line in row["detail_lines"]
            ))
        self.assertIn("T вне допуска", row["detail"])
        self.assertIn("лишних detections", row["detail"])

    def test_contacts_long_report_exposes_all_five_rows(self):
        items = [
            {
                "index": index,
                "dev_top_px": float(index),
                "dev_bottom_px": float(index) / 2,
                "rect_fits": index != 4,
                "omission_distance_px": 90 + index * 3,
            }
            for index in range(1, 6)
        ]
        result = SimpleNamespace(
            rule_name="contacts_long",
            triggered=True,
            details={"per_role": {"SPIDER_LEFT": {
                "triggered": True,
                "reason": None,
                "ignored": 0,
                "line_tolerance_px": 7.0,
                "rect_width_mm": 0.48,
                "rect_height_mm": 0.36,
                "omission_tilt_ratio_max": 0.2,
                "omission_tilt_check": {
                    "status": "fail",
                    "distance_trend_ratio": 0.31,
                },
                "inscribe_check": {
                    "status": "fail",
                    "scale_px_per_mm": 24.0,
                },
                "items": items,
            }}},
        )
        row = ProductionCycle._rule_report_row(result)
        self.assertTrue(row["show_detail"])
        self.assertEqual(len(row["detail_lines"]), 7)
        for index in range(1, 6):
            self.assertTrue(any(
                f"SPIDER_LEFT #{index}" in line
                for line in row["detail_lines"]
            ))
        self.assertIn("omission tilt 0.310/предел 0.200", row["detail"])
        self.assertIn("rect FAIL", row["detail"])

    def test_top_rejection_reasons_are_concise(self):
        group_checks = {
            group: {
                "median_distance_px": 50.0,
                "max_deviation_px": 15.0 if group == "L" else 2.0,
                "allowed_deviation_px": 10.0,
            }
            for group in ("L", "R", "T", "B")
        }
        groups = ["L"] * 5 + ["R"] * 5 + ["T"] * 2 + ["B"] * 2
        items = [
            {
                "index": index,
                "group": group,
                "distance_px": 50.0,
                "deviation_px": 15.0 if index == 1 else 2.0,
                "allowed_deviation_px": 10.0,
                "rect_width_px": 28 if group in ("L", "R") else 30,
                "rect_height_px": 35 if group in ("L", "R") else 28,
                "rect_fits": index != 3,
            }
            for index, group in enumerate(groups, start=1)
        ]
        contacts = SimpleNamespace(
            rule_name="top_contacts",
            triggered=True,
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": None,
                "ignored": 0,
                "group_checks": group_checks,
                "items": items,
            }}},
        )
        row = ProductionCycle._rule_report_row(contacts)
        self.assertTrue(row["show_detail"])
        self.assertEqual(len(row["detail_lines"]), 18)
        self.assertIn("TOP L: distance median 50.0 px", row["detail"])
        self.assertIn("max deviation 15.0/10.0 px", row["detail"])
        self.assertIn("TOP #3 L", row["detail"])
        self.assertIn("rect 28x35 px FAIL", row["detail"])

        platform = SimpleNamespace(
            rule_name="top_platform",
            triggered=True,
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": None,
                "inscribe_fail": True,
                "rect_width_px": 260,
                "rect_height_px": 120,
                "angle_deg": 2.5,
                "placement": "not_fitted",
                "shift_distance_px": 0.0,
            }}},
        )
        row = ProductionCycle._rule_report_row(platform)
        self.assertTrue(row["show_detail"])
        self.assertIn("rectangle 260x120 px", row["detail"])
        self.assertIn("angle 2.5°", row["detail"])
        self.assertIn("не вписался", row["detail"])
        self.assertIn("shift 0.0 px", row["detail"])

        overflow = SimpleNamespace(
            rule_name="platform_contacts_overlap",
            triggered=True,
            details={"per_role": {"TOP": {
                "triggered": True,
                "boundary_width_px": 275,
                "boundary_height_px": 135,
                "largest_component_pixels": 17,
                "excess_component_min_px": 3,
                "excess_pixels": 21,
            }}},
        )
        row = ProductionCycle._rule_report_row(overflow)
        self.assertTrue(row["show_detail"])
        self.assertIn("boundary 275x135 px", row["detail"])
        self.assertIn("component min 3 px", row["detail"])
        self.assertIn("largest component 17 px", row["detail"])
        self.assertIn("confirmed 21 px", row["detail"])

        sinks = SimpleNamespace(
            rule_name="sinks",
            triggered=True,
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": None,
                "hits": [{
                    "sink_index": 1,
                    "forbidden_pixels": 12,
                    "central_overlap_px": 20,
                    "platform_overlap_px": 5,
                    "contacts_overlap_px": 3,
                }],
            }}},
        )
        row = ProductionCycle._rule_report_row(sinks)
        self.assertIn("shell #1: forbidden 12 px", row["detail"])
        self.assertIn("central 20 px", row["detail"])
        self.assertIn("platform 5 px", row["detail"])
        self.assertIn("contacts 3 px", row["detail"])

        glass = SimpleNamespace(
            rule_name="glass",
            triggered=True,
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": None,
                "hits": [{
                    "glass_index": 1,
                    "platform_overlap_px": 12,
                    "pin_overlap_px": 3,
                    "ring_overlap_px": 4,
                    "cleanup_overlap_px": 19,
                }],
            }}},
        )
        row = ProductionCycle._rule_report_row(glass)
        self.assertIn("glass #1 → ОЧИСТКА", row["detail"])
        self.assertIn("platform 12 px", row["detail"])
        self.assertIn("pin 3 px", row["detail"])
        self.assertIn("ring 4 px", row["detail"])
        self.assertIn("union 19 px", row["detail"])

        reference = SimpleNamespace(
            rule_name="glass_on_contacts",
            triggered=True,
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": "wrong_pin_count: 12/14",
                "pins_found": 12,
                "hits": 0,
                "pairs": [],
            }}},
        )
        row = ProductionCycle._rule_report_row(reference)
        self.assertEqual(row["detail"], "TOP: pins: 12/14")

        bad_glass = SimpleNamespace(
            rule_name="glass_on_contacts",
            triggered=True,
            details={"per_role": {"TOP": {
                "triggered": True,
                "reason": None,
                "pairs": [{
                    "glass_index": 1,
                    "contact_index": 4,
                    "overlap_pixels": 9,
                }],
            }}},
        )
        row = ProductionCycle._rule_report_row(bad_glass)
        self.assertIn("glass #1 → contact #4", row["detail"])
        self.assertIn("overlap 9 px → БРАК", row["detail"])

    def test_running_status_exposes_current_models_and_rules(self):
        cycle = self.make_cycle()
        self.assertTrue(cycle.request_start())
        cycle._last_model_health = [{
            "role": "TOP",
            "model": "weights/top.pt",
            "ok": True,
            "elapsed_ms": 12,
            "detections": 2,
        }]
        cycle._frame_analysis_rule_results = [SimpleNamespace(
            rule_name="top_rule",
            triggered=False,
            details={},
        )]
        report = cycle._build_status()["frame_analysis"]
        self.assertTrue(report["available"])
        self.assertEqual(report["kind"], "CYCLE")
        self.assertEqual(len(report["models"]), 1)
        self.assertEqual(len(report["rules"]), 1)

    def test_backend_control_permissions_match_every_line_state(self):
        cycle = self.make_cycle()
        controls = cycle._build_status()["controls"]
        self.assertTrue(controls["start"])
        self.assertFalse(controls["stop"])
        self.assertTrue(controls["distributor_diagnostic"])
        self.assertTrue(cycle._operation_lock.acquire(blocking=False))
        try:
            busy_controls = cycle._build_status()["controls"]
            self.assertFalse(busy_controls["start"])
            self.assertFalse(busy_controls["exit"])
            self.assertFalse(busy_controls["distributor_diagnostic"])
        finally:
            cycle._operation_lock.release()

        self.assertTrue(cycle.request_start())
        controls = cycle._build_status()["controls"]
        self.assertFalse(controls["start"])
        self.assertTrue(controls["stop"])
        self.assertFalse(controls["distributor_diagnostic"])
        self.assertFalse(controls["jog_hold"])

        self.assertTrue(cycle.request_stop())
        controls = cycle._build_status()["controls"]
        self.assertFalse(controls["start"])
        self.assertFalse(controls["stop"])
        self.assertTrue(cycle.sm.notify_line_empty())
        controls = cycle._build_status()["controls"]
        self.assertTrue(controls["start"])
        self.assertTrue(controls["distributor_diagnostic"])

        faulted = self.make_cycle()
        self.assertTrue(faulted.sm.notify_fault())
        controls = faulted._build_status()["controls"]
        self.assertFalse(controls["start"])
        self.assertFalse(controls["stop"])
        self.assertFalse(controls["jog_hold"])
        self.assertFalse(controls["distributor_diagnostic"])
        self.assertTrue(controls["exit"])

        exiting = self.make_cycle()
        self.assertTrue(exiting.request_exit())
        controls = exiting._build_status()["controls"]
        self.assertFalse(controls["start"])
        self.assertFalse(controls["jog_hold"])
        self.assertFalse(controls["distributor_diagnostic"])
        self.assertTrue(controls["exit"])

    def test_latched_jog_error_blocks_every_new_motion_command(self):
        jog = FakeJog()
        jog._error = "serial failure"
        cycle = self.make_cycle(jog=jog)
        controls = cycle._build_status()["controls"]
        self.assertFalse(controls["start"])
        self.assertFalse(controls["jog_hold"])
        self.assertFalse(controls["distributor_diagnostic"])
        self.assertFalse(cycle.can_enter_jog())

    def test_jog_hold_disables_start_and_distributor_diagnostics(self):
        jog = FakeJog()
        cycle = self.make_cycle(jog=jog)
        self.assertTrue(cycle.enter_jog())
        controls = cycle._build_status()["controls"]
        self.assertTrue(controls["start"])
        self.assertTrue(controls["jog_hold"])
        self.assertTrue(controls["distributor_diagnostic"])
        self.assertTrue(cycle.jog_hold_start("+"))
        self.assertEqual(cycle._build_status()["process"]["phase"], "JOG_HOLD")
        controls = cycle._build_status()["controls"]
        self.assertFalse(controls["start"])
        self.assertFalse(controls["distributor_diagnostic"])
        self.assertFalse(controls["exit"])
        self.assertTrue(cycle.jog_hold_release("test"))
        self.assertEqual(cycle._build_status()["process"]["phase"], "JOG_STOPPED")

    def test_distributor_diagnostics_only_before_start(self):
        cycle = self.make_cycle()
        self.assertTrue(cycle.distributor_diagnostic("DIST1_OPEN"))
        self.assertTrue(cycle.distributor_diagnostic("DIST2_CLEANUP"))
        self.assertEqual(
            cycle.distributor.diagnostics,
            [("gate", "OPEN"), ("route", CATEGORY_CLEANUP)],
        )
        self.assertTrue(cycle.request_start())
        self.assertIn(("park", "production"), cycle.distributor.diagnostics)
        self.assertFalse(cycle.distributor_diagnostic("DIST1_HOME"))

    def test_delayed_jog_release_cannot_stop_running_cycle(self):
        jog = FakeJog()
        cycle = self.make_cycle(jog=jog)
        self.assertTrue(cycle.enter_jog())
        self.assertTrue(cycle.request_start())
        releases_after_start = list(jog.releases)
        self.assertFalse(cycle.jog_hold_release("delayed pointerup"))
        self.assertEqual(jog.releases, releases_after_start)
        self.assertEqual(cycle.state, "RUNNING")

    def test_inflight_part_is_archived_as_aborted_on_shutdown(self):
        archive = FakeArchive()
        cycle = self.make_cycle(archive=archive)
        part = Part(7, 3)
        part.mark_input_done()
        cycle.parts.append(part)
        cycle._archive_inflight("test_shutdown")
        self.assertEqual(cycle.parts, [])
        self.assertEqual(len(archive.records), 1)
        record = archive.records[0]
        self.assertEqual(record["category"], CATEGORY_BAD)
        self.assertTrue(record["extra"]["aborted"])
        self.assertEqual(record["extra"]["abort_reason"], "test_shutdown")

    def test_part_category_semantics(self):
        part = Part(1, 0)
        part.mark_input_done()
        self.assertNotEqual(part.route_category, CATEGORY_GOOD)
        part.mark_spider_done()
        self.assertEqual(part.route_category, CATEGORY_GOOD)

        cleanup = Part(2, 0)
        cleanup.add_input_defect("glass")
        cleanup.mark_input_done()
        cleanup.mark_spider_done()
        self.assertEqual(cleanup.route_category, CATEGORY_CLEANUP)

        bad = Part(3, 0)
        bad.add_input_defect("glass")
        bad.add_spider_defect("contacts")
        bad.mark_input_done()
        bad.mark_spider_done()
        self.assertEqual(bad.route_category, CATEGORY_BAD)


if __name__ == "__main__":
    unittest.main()
