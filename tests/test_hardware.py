import importlib
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from hardware.axis import Axis
from hardware.conveyor import Conveyor
from hardware.distributor import Distributor
from hardware.jog_controller import JogController
from hardware.port_discovery import is_controller_response
from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP


class FakeTransport:
    def __init__(self, replies=None):
        self.commands = []
        self.replies = list(replies or [])

    def send(self, command):
        self.commands.append(command)

    def query(self, command, delay=0.15):
        self.commands.append(command)
        return self.replies.pop(0) if self.replies else ""


class HoldTransport:
    def __init__(self):
        self.commands = []
        self.moving = False
        self.lock = threading.Lock()

    def send(self, command):
        with self.lock:
            self.commands.append(command)
            if command == "G3":
                self.moving = True
            elif command == "G1":
                self.moving = False

    def query(self, command, delay=0.15):
        with self.lock:
            self.commands.append(command)
            if command == "I1":
                return "1" if self.moving else "0"
            if command == "I2":
                return "MOV=0 WAIT=0 lastErr=0"
            return ""


class FakeAxis:
    def __init__(self, position=0):
        self.position_value = position
        self.transport = FakeTransport()
        self.moves = []

    @property
    def position(self):
        return self.position_value

    def move_absolute(self, target):
        self.moves.append(("absolute", target))
        self.position_value = target

    def move_relative(self, steps):
        self.moves.append(("relative", steps))
        self.position_value += steps

    def home(self):
        self.moves.append(("home", 0))
        self.position_value = 0

    def wait_stop(self, timeout=10.0, progress_callback=None):
        if progress_callback is not None:
            progress_callback(self.position_value, 0)
        return None

    def verify_homed(self):
        if self.position_value != 0:
            raise RuntimeError("fake axis is not home")
        return {
            "position": 0,
            "moving": 0,
            "homed": 1,
            "limits_enabled": 1,
        }


class ProgressiveAxis(FakeAxis):
    def wait_stop(self, timeout=10.0, progress_callback=None):
        if progress_callback is not None:
            target = self.position_value
            for position in (0, target // 2, target):
                progress_callback(position, int(position != target))


class HardwareTests(unittest.TestCase):
    def test_axis_overrides_firmware_default_300_limit_and_verifies_i11(self):
        transport = FakeTransport([
            "AXIS0 speed=300 accel=100 dir=1 en=1 "
            "homeSpd=400 backoff=200 limMin=0 limMax=340"
        ])
        with patch("hardware.axis.time.sleep", return_value=None):
            axis = Axis(
                transport,
                axis_id=0,
                minimum=0,
                maximum=340,
                speed=300,
                accel=100,
            )
        self.assertEqual(axis.minimum, 0)
        self.assertEqual(axis.maximum, 340)
        self.assertEqual(
            transport.commands,
            [
                "G21 S300 P0",
                "G22 S100 P0",
                "G31 S0 P0",
                "G32 S340 P0",
                "G33 S1 P0",
                "I11",
            ],
        )

    def test_axis_rejects_firmware_limit_readback_still_at_300(self):
        transport = FakeTransport([
            "AXIS0 speed=300 accel=100 dir=1 en=1 "
            "homeSpd=400 backoff=200 limMin=0 limMax=300"
        ])
        with (
            patch("hardware.axis.time.sleep", return_value=None),
            self.assertRaisesRegex(RuntimeError, "limMax=300, expected 340"),
        ):
            Axis(
                transport,
                axis_id=0,
                minimum=0,
                maximum=340,
            )

    def test_distributor_endpoints_have_no_hidden_300_or_200_defaults(self):
        with self.assertRaises(TypeError):
            Distributor(FakeAxis(), FakeAxis())
        distributor = Distributor(
            FakeAxis(),
            FakeAxis(),
            dist1_open_position=340,
            dist2_bad_position=0,
            dist2_cleanup_position=340,
        )
        self.assertEqual(distributor.status["dist1_max"], 340)
        self.assertEqual(distributor.status["dist2_max"], 340)

    def test_motion_write_is_never_retried_after_uncertain_error(self):
        fake_serial = types.ModuleType("serial")

        class SerialException(Exception):
            pass

        fake_serial.SerialException = SerialException
        fake_serial.Serial = object
        with patch.dict(sys.modules, {"serial": fake_serial}):
            module = importlib.import_module("hardware.serial_transport")

        class FailingSerial:
            is_open = True

            def __init__(self):
                self.writes = 0

            def write(self, data):
                self.writes += 1
                raise SerialException("uncertain write")

            def flush(self):
                return None

        transport = module.SerialTransport.__new__(module.SerialTransport)
        transport.lock = threading.Lock()
        transport.ser = FailingSerial()
        with self.assertRaises(SerialException):
            transport.send("G3")
        self.assertEqual(transport.ser.writes, 1)

    def test_jog_requires_explicit_hold_and_restore_geometry(self):
        with self.assertRaises(KeyError):
            JogController(HoldTransport(), {"normal_steps": 19048})
        with self.assertRaises(KeyError):
            JogController(HoldTransport(), {"jog_hold_steps": 1_000_000})

    # ─── Ограниченная коррекция ленты ────────────────────────────

    NUDGE_CALIB = {
        "micro_steps": 500,
        "nudge_limit_steps": 1000,
        "jog_hold_steps": 1_000_000,
        "normal_steps": 19048,
    }

    @staticmethod
    def _nudge_transport():
        class NudgeTransport(HoldTransport):
            def query(self, command, delay=0.15):
                with self.lock:
                    self.commands.append(command)
                    if command == "I1":
                        # Одна итерация движения, затем подтверждённый стоп.
                        if self.moving:
                            self.moving = False
                            return "1"
                        return "0"
                    if command == "I2":
                        return "MOV=0 WAIT=0 lastErr=0"
                    return ""

        return NudgeTransport()

    def test_nudge_accumulates_and_clamps_at_limit_in_both_directions(self):
        transport = self._nudge_transport()
        jog = JogController(transport, self.NUDGE_CALIB)

        self.assertEqual(jog.nudge("+"), 500)
        self.assertEqual(jog.nudge("+"), 500)
        self.assertEqual(jog.nudge_offset, 1000)
        # Накопленный лимит достигнут: дальнейшие нажатия не двигают ленту.
        self.assertEqual(jog.nudge("+"), 0)
        self.assertEqual(jog.nudge("+"), 0)
        self.assertEqual(jog.nudge_offset, 1000)

        for _ in range(4):
            jog.nudge("-")
        self.assertEqual(jog.nudge_offset, -1000)
        self.assertEqual(jog.nudge("-"), 0)
        self.assertEqual(jog.nudge_offset, -1000)

    def test_nudge_never_leaves_one_cell_even_after_many_presses(self):
        transport = self._nudge_transport()
        jog = JogController(transport, self.NUDGE_CALIB)
        cell_steps = self.NUDGE_CALIB["normal_steps"] * 2
        for _ in range(200):
            jog.nudge("+")
        self.assertEqual(jog.nudge_offset, 1000)
        self.assertLess(abs(jog.nudge_offset), cell_steps)

    def test_nudge_partial_move_is_clipped_to_remaining_budget(self):
        transport = self._nudge_transport()
        jog = JogController(transport, self.NUDGE_CALIB)
        jog.nudge("+", steps=800)
        # Остаток бюджета 200: запрос 500 обязан примениться частично.
        self.assertEqual(jog.nudge("+", steps=500), 200)
        self.assertEqual(jog.nudge_offset, 1000)
        self.assertIn("G7 S200", transport.commands)

    def test_nudge_confirms_autopause_before_moving_the_belt(self):
        # Firmware при autoPauseMode=0 сама повторяет ход с текущими G7/G6
        # через pause_between_movements. Во время коррекции это двигало бы
        # ленту под руками оператора, поэтому режим подтверждается явно.
        transport = self._nudge_transport()
        jog = JogController(transport, self.NUDGE_CALIB)
        jog.nudge("+")
        self.assertIn("G12 S1", transport.commands)
        self.assertLess(
            transport.commands.index("G12 S1"),
            transport.commands.index("G3"),
            "G12 S1 обязан быть отправлен до запуска хода",
        )

    def test_nudge_does_not_trust_status_polled_before_motion_starts(self):
        # MOV=1 появляется только на следующем проходе loop() после G3.
        # Немедленный опрос вернул бы «остановлен» от ещё не начатого хода.
        class LateStartTransport(HoldTransport):
            def __init__(self):
                super().__init__()
                self.polls_before_motion = 0
                self.g3_at = None

            def query(self, command, delay=0.15):
                with self.lock:
                    self.commands.append(command)
                    if command == "I1":
                        if self.moving:
                            self.moving = False
                            return "1"
                        if self.g3_at is not None:
                            self.polls_before_motion += 1
                        return "0"
                    if command == "I2":
                        return "MOV=0 WAIT=0 lastErr=0"
                    return ""

            def send(self, command):
                super().send(command)
                if command == "G3":
                    with self.lock:
                        self.g3_at = time.monotonic()

        transport = LateStartTransport()
        jog = JogController(transport, self.NUDGE_CALIB)
        started = time.monotonic()
        jog.nudge("+")
        elapsed = time.monotonic() - started
        # Между G3 и первым опросом обязана быть выдержка, как в move_step.
        self.assertGreaterEqual(elapsed, 0.3)

    def test_nudge_restores_production_geometry_after_each_move(self):
        transport = self._nudge_transport()
        jog = JogController(transport, self.NUDGE_CALIB)
        jog.nudge("+")
        self.assertIn("G7 S500", transport.commands)
        self.assertIn("G6 S1", transport.commands)
        self.assertIn("G3", transport.commands)
        # Производственная геометрия обязана вернуться немедленно.
        self.assertEqual(transport.commands[-2:], ["G7 S19048", "G6 S2"])

    def test_nudge_rejects_request_above_single_press_limit(self):
        jog = JogController(self._nudge_transport(), self.NUDGE_CALIB)
        with self.assertRaises(ValueError):
            jog.nudge("+", steps=5000)
        with self.assertRaises(ValueError):
            jog.nudge("*")
        self.assertEqual(jog.nudge_offset, 0)

    def test_nudge_offset_resets_between_pauses(self):
        transport = self._nudge_transport()
        jog = JogController(transport, self.NUDGE_CALIB)
        jog.nudge("+")
        self.assertEqual(jog.nudge_offset, 500)
        jog.reset_nudge_offset()
        self.assertEqual(jog.nudge_offset, 0)

    def test_nudge_is_rejected_while_hold_jog_is_running(self):
        transport = HoldTransport()
        jog = JogController(transport, self.NUDGE_CALIB, heartbeat_timeout=0.4)
        self.assertTrue(jog.start_hold("+"))
        self.assertTrue(jog.heartbeat("+"))
        try:
            with self.assertRaises(RuntimeError):
                jog.nudge("+")
        finally:
            jog.release("test cleanup")

    def test_nudge_failure_does_not_count_offset_and_raises(self):
        class FailingNudgeTransport:
            def __init__(self):
                self.commands = []

            def send(self, command):
                self.commands.append(command)
                if command == "G3":
                    raise RuntimeError("motion rejected")

            def query(self, command, delay=0.15):
                return "0" if command == "I1" else "MOV=0 WAIT=0 lastErr=0"

        transport = FailingNudgeTransport()
        jog = JogController(transport, self.NUDGE_CALIB)
        with self.assertRaises(RuntimeError):
            jog.nudge("+")
        # Позиция после сбоя неизвестна: смещение не засчитано, ошибка видна.
        self.assertEqual(jog.nudge_offset, 0)
        self.assertIsNotNone(jog.status["error"])
        self.assertEqual(transport.commands[-2:], ["G7 S19048", "G6 S2"])

    # ─── Удержание коррекции в паузе ─────────────────────────────

    @staticmethod
    def _convey15_transport(pause_between=2000):
        """Симулятор прошивки convey15 с её реальной семантикой POS.

        Прошивка обнуляет позицию и в `G1` (`brake(); reset()`), и при
        штатном завершении хода в `loop()`. Раньше фейк возвращал сквозной
        POS, из-за чего учёт удержания выглядел рабочим, хотя на железе
        всегда получал 0 и бюджет коррекции не расходовался вовсе.
        """

        class Convey15Transport:
            def __init__(self):
                self.commands = []
                self.lock = threading.RLock()
                self.pos = 0
                self.steps_per_division = 19048
                self.divisions = 2
                self.speed = 20000.0
                self.pause_between = pause_between
                self.auto_pause = True
                self.moving = False
                self.wait_active = False
                self.wait_until = 0.0
                self.target_steps = 0
                self.start_pos = 0
                self.travel_start = None
                # Реальный путь ленты: тест сверяет с ним учёт системы.
                self.true_travel = 0
                self.g1_while_moving = 0

            def _tick(self):
                now = time.monotonic()
                if self.moving:
                    done = min(
                        abs(self.target_steps),
                        int((now - self.travel_start) * self.speed),
                    )
                    sign = 1 if self.target_steps >= 0 else -1
                    self.pos = self.start_pos + sign * done
                    if done >= abs(self.target_steps):
                        self.true_travel += self.target_steps
                        self.moving = False
                        self.pos = 0            # loop(): stepper.reset()
                        self.wait_active = True
                        self.wait_until = now + self.pause_between / 1000.0
                if self.wait_active and now >= self.wait_until:
                    self.wait_active = False

            def send(self, command):
                with self.lock:
                    self.commands.append(command)
                    self._tick()
                    if command.startswith("G7 S"):
                        self.steps_per_division = int(command[4:])
                    elif command.startswith("G6 S"):
                        self.divisions = int(command[4:])
                    elif command.startswith("G5 S"):
                        self.speed = float(command[4:])
                    elif command.startswith("G9 S"):
                        self.pause_between = int(command[4:])
                    elif command.startswith("G12 S"):
                        self.auto_pause = bool(int(command[5:]))
                    elif command == "G3":
                        self.wait_active = False
                        self.target_steps = (
                            self.steps_per_division * self.divisions
                        )
                        self.start_pos = self.pos
                        self.travel_start = time.monotonic()
                        self.moving = True
                    elif command == "G1":
                        if self.moving:
                            self.g1_while_moving += 1
                            self.true_travel += self.pos - self.start_pos
                        self.moving = False
                        self.wait_active = False
                        self.pos = 0            # case 1: reset()

            def query(self, command, delay=0.15):
                with self.lock:
                    self.commands.append(command)
                    self._tick()
                    if command == "I1":
                        return "1" if self.moving else "0"
                    if command == "I2":
                        return (
                            f"MOV={1 if self.moving else 0} "
                            f"WAIT={1 if self.wait_active else 0} "
                            f"POS={self.pos} TGT={self.target_steps} "
                            "lastErr=0"
                        )
                    return ""

        return Convey15Transport()

    HOLD_CALIB = {
        "micro_steps": 500,
        "nudge_limit_steps": 5000,
        "jog_hold_steps": 1_000_000,
        "normal_steps": 19048,
        "conveyor_speed": 20000,
        "pause_hold_speed": 2000,
        "nudge_hold_chunk_steps": 100,
        "pause_between_movements": 2000,
    }

    def _hold_for(self, jog, direction, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            jog.heartbeat(direction, mode="nudge")
            time.sleep(0.01)

    def test_pause_hold_accounting_matches_real_belt_travel(self):
        # Ключевая проверка: учтённое смещение обязано совпасть с реальным
        # путём ленты в симуляторе прошивки, а не с желаемым числом.
        transport = self._convey15_transport()
        jog = JogController(transport, self.HOLD_CALIB, heartbeat_timeout=1.0)

        self.assertTrue(jog.start_nudge_hold("+"))
        self._hold_for(jog, "+", 0.3)
        offset = jog.release_nudge_hold("pointerup")

        self.assertGreater(offset, 0, "Удержание обязано двигать ленту")
        self.assertEqual(offset, transport.true_travel)
        self.assertEqual(jog.nudge_offset, transport.true_travel)

    def test_pause_hold_never_exceeds_budget_even_if_held_forever(self):
        transport = self._convey15_transport()
        jog = JogController(transport, self.HOLD_CALIB, heartbeat_timeout=2.0)
        limit = self.HOLD_CALIB["nudge_limit_steps"]

        self.assertTrue(jog.start_nudge_hold("+"))
        deadline = time.monotonic() + 6.0
        while jog.busy and time.monotonic() < deadline:
            jog.heartbeat("+", mode="nudge")
            time.sleep(0.01)

        self.assertFalse(jog.busy, "Лента обязана встать на границе бюджета")
        self.assertEqual(jog.nudge_offset, limit)
        self.assertEqual(transport.true_travel, limit)
        # Бюджет исчерпан: та же сторона запрещена, обратная доступна.
        self.assertFalse(jog.start_nudge_hold("+"))
        self.assertTrue(jog.start_nudge_hold("-"))
        jog.release_nudge_hold("cleanup")

    def test_pause_hold_accumulates_across_presses_within_budget(self):
        transport = self._convey15_transport()
        jog = JogController(transport, self.HOLD_CALIB, heartbeat_timeout=1.0)
        limit = self.HOLD_CALIB["nudge_limit_steps"]

        for _ in range(6):
            if not jog.start_nudge_hold("+"):
                break
            self._hold_for(jog, "+", 0.25)
            jog.release_nudge_hold("pointerup")

        self.assertEqual(jog.nudge_offset, transport.true_travel)
        self.assertLessEqual(jog.nudge_offset, limit)
        self.assertEqual(jog.nudge_offset, limit)

    def test_pause_hold_release_sends_g1_like_jog(self):
        transport = self._convey15_transport()
        jog = JogController(transport, self.HOLD_CALIB, heartbeat_timeout=1.0)

        self.assertTrue(jog.start_nudge_hold("+"))
        self._hold_for(jog, "+", 0.12)
        offset = jog.release_nudge_hold("pointerup")

        self.assertGreater(transport.g1_while_moving, 0)
        self.assertIn("G1", transport.commands)
        self.assertGreater(offset, 0)
        self.assertLess(offset, self.HOLD_CALIB["nudge_limit_steps"])
        self.assertLessEqual(offset, transport.true_travel)

    def test_pause_hold_uses_jog_profile_and_restores_geometry(self):
        transport = self._convey15_transport()
        jog = JogController(transport, self.HOLD_CALIB, heartbeat_timeout=1.0)

        self.assertTrue(jog.start_nudge_hold("+"))
        self._hold_for(jog, "+", 0.15)
        jog.release_nudge_hold("pointerup")

        self.assertNotIn("G5 S2000", transport.commands)
        self.assertNotIn("G9 S0", transport.commands)
        self.assertIn("G1", transport.commands)
        self.assertEqual(transport.commands[-2:], ["G7 S19048", "G6 S2"])

    def test_pause_hold_stops_on_heartbeat_timeout(self):
        transport = self._convey15_transport()
        jog = JogController(transport, self.HOLD_CALIB, heartbeat_timeout=0.2)

        self.assertTrue(jog.start_nudge_hold("+"))
        jog.heartbeat("+", mode="nudge")
        deadline = time.monotonic() + 4.0
        while jog.busy and time.monotonic() < deadline:
            time.sleep(0.02)

        self.assertFalse(jog.busy, "Пропажа heartbeat обязана остановить ленту")
        self.assertGreater(jog.nudge_offset, 0)
        self.assertLessEqual(jog.nudge_offset, transport.true_travel)
        self.assertIn("G1", transport.commands)
        self.assertEqual(transport.commands[-2:], ["G7 S19048", "G6 S2"])

    def test_pause_hold_heartbeat_timeout_sends_g1_without_chunk_error(self):
        transport = self._convey15_transport()
        jog = JogController(transport, self.HOLD_CALIB, heartbeat_timeout=0.2)

        self.assertTrue(jog.start_nudge_hold("+"))
        jog.heartbeat("+", mode="nudge")
        deadline = time.monotonic() + 2.0
        while jog.busy and time.monotonic() < deadline:
            time.sleep(0.02)

        self.assertFalse(jog.busy, "Потеря heartbeat обязана остановить JOG в паузе")
        self.assertIn("G1", transport.commands)
        self.assertIsNone(jog.status["error"])


    def test_pause_hold_and_jog_hold_never_run_together(self):
        transport = self._convey15_transport()
        jog = JogController(transport, self.HOLD_CALIB, heartbeat_timeout=0.5)

        self.assertTrue(jog.start_nudge_hold("+"))
        self.assertTrue(jog.heartbeat("+", mode="nudge"))
        try:
            self.assertFalse(jog.start_hold("+"))
            # Heartbeat чужого режима не должен продлевать удержание.
            self.assertFalse(jog.heartbeat("+", mode="jog"))
            with self.assertRaises(RuntimeError):
                jog.nudge("+")
        finally:
            jog.release_nudge_hold("cleanup")

    def test_jog_controller_rejects_nudge_limit_reaching_neighbour_cell(self):
        # ±limit обязан быть строго внутри одной ячейки.
        with self.assertRaises(ValueError):
            JogController(
                HoldTransport(),
                {
                    "micro_steps": 500,
                    "nudge_limit_steps": 1000,
                    "jog_hold_steps": 1_000_000,
                    "normal_steps": 500,
                },
            )

    def test_jog_hold_starts_on_press_and_g1_stops_on_release(self):
        transport = HoldTransport()
        jog = JogController(
            transport,
            {"micro_steps": 500, "jog_hold_steps": 1_000_000, "normal_steps": 19048},
            heartbeat_timeout=0.4,
        )
        self.assertTrue(jog.start_hold("+"))
        self.assertNotIn("G3", transport.commands)
        self.assertTrue(jog.heartbeat("+"))
        deadline = time.monotonic() + 1.0
        while "G3" not in transport.commands and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("G3", transport.commands)
        self.assertIn("G7 S1000000", transport.commands)
        self.assertNotIn("G7 S500", transport.commands)
        started = time.monotonic()
        self.assertTrue(jog.release("test pointerup"))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(jog.busy)
        self.assertIn("G1", transport.commands)
        stop_index = max(
            index for index, command in enumerate(transport.commands)
            if command == "G1"
        )
        self.assertNotIn("G3", transport.commands[stop_index + 1:])
        self.assertEqual(transport.commands[-2:], ["G7 S19048", "G6 S2"])

    def test_jog_worker_reports_when_g1_stop_command_also_fails(self):
        class FailingStopTransport:
            def send(self, command):
                if command.startswith("G7") or command == "G1":
                    raise RuntimeError(f"send failed: {command}")

            def query(self, command, delay=0.15):
                return ""

        jog = JogController(
            FailingStopTransport(),
            {"jog_hold_steps": 1_000_000, "normal_steps": 19048},
            heartbeat_timeout=0.4,
        )
        self.assertTrue(jog.start_hold("+"))
        self.assertTrue(jog.heartbeat("+"))
        deadline = time.monotonic() + 1.0
        while jog.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(jog.busy)
        self.assertIn("G1 не отправлена", jog.status["error"])

    def test_jog_hold_stops_automatically_when_heartbeat_is_lost(self):
        transport = HoldTransport()
        jog = JogController(
            transport,
            {"micro_steps": 500, "jog_hold_steps": 1_000_000, "normal_steps": 19048},
            heartbeat_timeout=0.15,
        )
        self.assertTrue(jog.start_hold("-"))
        deadline = time.monotonic() + 1.0
        while jog.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(jog.busy)
        self.assertIn("G1", transport.commands)
        self.assertNotIn("G3", transport.commands)
        self.assertIn("heartbeat timeout", jog.last_action)

    def test_port_discovery_rejects_arbitrary_nonempty_serial_reply(self):
        self.assertTrue(
            is_controller_response("MOV=0 WAIT=0 POS=0 lastErr=0")
        )
        self.assertFalse(is_controller_response("unknown command I2"))
        self.assertFalse(is_controller_response("some other serial device"))
        self.assertFalse(is_controller_response(""))

    def test_axis_home_uses_physical_homing_command(self):
        axis = Axis.__new__(Axis)
        axis.axis_id = 1
        axis.transport = FakeTransport()
        with patch("hardware.axis.time.sleep", return_value=None):
            axis.home()
        self.assertEqual(axis.transport.commands, ["G28 P1"])

    def test_axis_rejects_negative_absolute_target_before_serial_write(self):
        axis = Axis.__new__(Axis)
        axis.axis_id = 0
        axis.minimum = 0
        axis.maximum = 340
        axis.transport = FakeTransport()
        with self.assertRaisesRegex(ValueError, "0..340"):
            axis.move_absolute(-1)
        self.assertEqual(axis.transport.commands, [])

    def test_axis_never_substitutes_zero_for_malformed_reply(self):
        axis = Axis.__new__(Axis)
        axis.axis_id = 0
        axis.transport = FakeTransport(["garbage", "garbage"])
        with self.assertRaisesRegex(RuntimeError, "no position"):
            _ = axis.position
        with self.assertRaisesRegex(RuntimeError, "no motion state"):
            _ = axis.is_moving

    def test_conveyor_applies_full_geometry_on_startup(self):
        transport = FakeTransport()
        with patch("hardware.conveyor.time.sleep", return_value=None):
            Conveyor(
                transport,
                speed=20000,
                accel=6000,
                steps_per_division=19048,
                divisions_per_movement=2,
            )
        self.assertEqual(
            transport.commands,
            ["G5 S20000", "G4 S6000", "G7 S19048", "G6 S2"],
        )

    def test_conveyor_waits_past_firmware_inter_move_pause(self):
        transport = FakeTransport([
            "0",
            "MOV=0 WAIT=1 lastErr=0",
            "0",
            "MOV=0 WAIT=0 lastErr=0",
        ])
        conveyor = Conveyor.__new__(Conveyor)
        conveyor.transport = transport
        progress = []
        with patch("hardware.conveyor.time.sleep", return_value=None):
            conveyor.wait_stop(
                timeout=1.0,
                progress_callback=progress.append,
            )
        self.assertEqual(transport.commands, ["I1", "I2", "I1", "I2"])
        self.assertEqual(progress[0]["wait"], 1)
        self.assertEqual(progress[-1]["wait"], 0)

    def test_conveyor_requires_strict_i2_postcondition(self):
        self.assertTrue(
            Conveyor._strict_stop_confirmed("MOV=0 WAIT=0 lastErr=0")
        )
        self.assertFalse(
            Conveyor._strict_stop_confirmed("MOV=0 WAIT=1 lastErr=0")
        )
        self.assertFalse(
            Conveyor._strict_stop_confirmed("MOV=0 WAIT=0 lastErr=4")
        )
        self.assertFalse(Conveyor._strict_stop_confirmed("0"))

    def test_distributor_validates_category_positions_and_stops_both_axes(self):
        dist1 = FakeAxis()
        dist2 = FakeAxis()
        distributor = Distributor(
            dist1,
            dist2,
            dist1_open_position=340,
            dist2_bad_position=0,
            dist2_cleanup_position=340,
            drop_time=0.0,
        )
        distributor.initialize()
        self.assertEqual(dist1.moves[0], ("home", 0))
        self.assertEqual(dist2.moves[0], ("home", 0))
        self.assertEqual(distributor.status["dist2_target"], CATEGORY_BAD)
        with patch("hardware.distributor.time.sleep", return_value=None):
            distributor.prepare(CATEGORY_CLEANUP, 1)
            self.assertEqual(dist1.position, 340)
            self.assertEqual(dist2.position, 340)
            self.assertEqual(distributor.status["dist2_target"], CATEGORY_CLEANUP)
            distributor.drop_and_close(1, CATEGORY_CLEANUP)
            self.assertEqual(dist1.position, 0)
            distributor.mark_pass(2)
            self.assertEqual(distributor.status["dist2_target"], CATEGORY_CLEANUP)
            distributor.reset_target()
            self.assertEqual(distributor.status["dist2_target"], CATEGORY_CLEANUP)
            distributor.park_production()
            self.assertEqual(dist1.position, 0)
            self.assertEqual(dist2.position, 0)
            self.assertEqual(distributor.status["dist2_target"], CATEGORY_BAD)
        with self.assertRaises(ValueError):
            distributor.prepare("UNKNOWN", 2)
        distributor.emergency_stop()
        self.assertEqual(dist1.transport.commands[-1], "G25")
        self.assertEqual(distributor.dist1_state, "FAULT")
        self.assertEqual(distributor.dist2_state, "FAULT")

    def test_dist1_uses_direct_absolute_positions_after_startup_homing(self):
        dist1 = FakeAxis()
        distributor = Distributor(
            dist1,
            FakeAxis(),
            dist1_open_position=340,
            dist2_bad_position=0,
            dist2_cleanup_position=340,
            drop_time=0.0,
        )
        distributor.initialize()
        distributor.diagnostic_gate("OPEN")
        distributor.diagnostic_gate("HOME")
        self.assertEqual(
            dist1.moves,
            [("home", 0), ("absolute", 340), ("absolute", 0)],
        )

    def test_distributor_hides_negative_firmware_homing_sentinel_from_ui(self):
        distributor = Distributor(
            FakeAxis(),
            FakeAxis(),
            dist1_open_position=340,
            dist2_bad_position=0,
            dist2_cleanup_position=340,
        )
        distributor._update_dist1_position(-2_000_000, 0)
        distributor._update_dist2_position(-15, 1)
        self.assertEqual(distributor.status["dist1_position"], 0)
        self.assertEqual(distributor.status["dist2_position"], 0)

    def test_distributor_publishes_real_intermediate_blade_positions(self):
        dist1 = ProgressiveAxis()
        distributor = Distributor(
            dist1,
            FakeAxis(),
            dist1_open_position=340,
            dist2_bad_position=0,
            dist2_cleanup_position=340,
            drop_time=0.0,
        )
        positions = []
        distributor.on_state_changed = lambda: positions.append(
            distributor.status["dist1_position"]
        )
        distributor.diagnostic_gate("OPEN")
        self.assertIn(170, positions)
        self.assertEqual(positions[-1], 340)

    def test_distributor_cancellation_prevents_followup_motion(self):
        dist1 = FakeAxis()
        dist2 = FakeAxis()
        distributor = Distributor(
            dist1,
            dist2,
            dist1_open_position=340,
            dist2_bad_position=0,
            dist2_cleanup_position=340,
            drop_time=0.0,
        )
        distributor.cancel_check = lambda: True
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            distributor.diagnostic_gate("OPEN")
        self.assertEqual(dist1.moves, [])

    def test_distributor_detects_position_mismatch(self):
        class StuckAxis(FakeAxis):
            def move_absolute(self, target):
                self.moves.append(("absolute", target))

        distributor = Distributor(
            FakeAxis(),
            StuckAxis(position=7),
            dist1_open_position=340,
            dist2_bad_position=0,
            dist2_cleanup_position=340,
            drop_time=0.0,
        )
        with self.assertRaisesRegex(RuntimeError, "DIST2 target mismatch"):
            distributor.prepare(CATEGORY_BAD, 1)


if __name__ == "__main__":
    unittest.main()
