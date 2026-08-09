"""Interlocked absolute axis adapter."""

from __future__ import annotations

import re
import time


class Axis:
    """One distributor axis with strict telemetry postconditions.

    Absolute coordinates are invalidated by firmware G24/G25 until a new,
    proven G28 in the current application session.  Raw I10 HOMED/LIM bits are
    not trusted to restore that invariant by themselves.
    """

    def __init__(self, transport, axis_id: int, maximum: int, minimum: int = 0,
                 speed: int = 300, accel: int = 100, telemetry_timeout: float = 12.0):
        if axis_id not in (0, 1):
            raise ValueError("axis_id должен быть 0 или 1")
        if type(minimum) is not int or type(maximum) is not int:
            raise ValueError("Axis limits должны быть int")
        if minimum < 0 or maximum <= minimum:
            raise ValueError("Ожидается 0 <= minimum < maximum")
        self.transport = transport
        self.axis_id = axis_id
        self.minimum = minimum
        self.maximum = maximum
        self.telemetry_timeout = float(telemetry_timeout)
        # The application session starts from the controller's configured
        # coordinate system.  A later G24/G25 or G28 explicitly invalidates
        # it; this preserves compatibility with a pre-homed test fixture.
        self._absolute_valid = True
        self._set_params(speed, accel)
        self._set_limits(minimum, maximum)
        self.verify_limit_config()

    @property
    def absolute_coordinate_valid(self) -> bool:
        return self._absolute_valid

    def invalidate_absolute_coordinates(self, reason: str = "firmware reset"):
        self._absolute_valid = False

    def move_absolute(self, position: int):
        if type(position) is not int or not self.minimum <= position <= self.maximum:
            raise ValueError(
                f"Axis {self.axis_id}: absolute position must be int in "
                f"{self.minimum}..{self.maximum}"
            )
        if not self._absolute_valid:
            raise RuntimeError(
                f"Axis {self.axis_id}: absolute coordinate invalid; prove G28 first"
            )
        self.transport.send(f"G27 S{position} P{self.axis_id}")

    def home(self):
        """Issue G28; validity is restored only by ``verify_homed``."""
        self._absolute_valid = False
        self.transport.send(f"G28 P{self.axis_id}")

    def read_status(self) -> dict:
        data = self.transport.query("I10")
        line_match = re.search(rf"AXIS{self.axis_id}\s+([^\r\n]+)", data or "", re.I)
        line = line_match.group(1) if line_match else ""

        def field(name):
            match = re.search(rf"\b{re.escape(name)}\s*=\s*(-?\d+)\b", line, re.I)
            return int(match.group(1)) if match else None

        return {
            "raw": data,
            "position": field("POS"),
            "target": field("TGT"),
            "moving": field("MOV"),
            "enabled": field("EN"),
            "home_phase": field("HOME"),
            "homed": field("HOMED"),
            "limits_enabled": field("LIM"),
            "endstop": field("ES"),
            "alarm": field("ALARM"),
            "error": field("ERR"),
        }

    def read_config(self) -> dict:
        data = self.transport.query("I11")
        line_match = re.search(rf"AXIS{self.axis_id}\s+([^\r\n]+)", data or "", re.I)
        line = line_match.group(1) if line_match else ""

        def field(name):
            match = re.search(rf"\b{re.escape(name)}\s*=\s*(-?\d+)\b", line, re.I)
            return int(match.group(1)) if match else None

        return {
            "raw": data,
            "speed": field("speed"),
            "accel": field("accel"),
            "limit_min": field("limMin"),
            "limit_max": field("limMax"),
        }

    def verify_limit_config(self):
        config = self.read_config()
        if config["limit_min"] != self.minimum or config["limit_max"] != self.maximum:
            raise RuntimeError(
                f"Axis {self.axis_id}: firmware limits "
                f"{config['limit_min']}..{config['limit_max']} do not equal "
                f"{self.minimum}..{self.maximum}"
            )
        return config

    def verify_homed(self):
        status = self.read_status()
        expected = {
            "position": 0,
            "target": 0,
            "moving": 0,
            "homed": 1,
            "limits_enabled": 1,
        }
        errors = [
            f"{name}={status.get(name)!r}, expected {value}"
            for name, value in expected.items()
            if status.get(name) != value
        ]
        if errors:
            raise RuntimeError(
                f"Axis {self.axis_id}: invalid homing postcondition: " + "; ".join(errors)
            )
        self._absolute_valid = True
        return status

    @property
    def position(self) -> int:
        position = self.read_status()["position"]
        if position is None:
            raise RuntimeError(f"Axis {self.axis_id}: controller reply has no position")
        return position

    def wait_stop(self, timeout: float | None = None, progress_callback=None, expected_target=None):
        limit = self.telemetry_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
        last = None
        while time.monotonic() <= deadline:
            status = self.read_status()
            last = status
            if progress_callback is not None:
                progress_callback(status.get("position"), status.get("moving"))
            required_fields = ("position", "target", "moving", "homed", "limits_enabled")
            missing = [name for name in required_fields if status.get(name) is None]
            if missing:
                raise RuntimeError(
                    f"Axis {self.axis_id}: incomplete telemetry fields: {missing}"
                )
            if status.get("alarm") not in (None, 0) or status.get("error") not in (None, 0):
                raise RuntimeError(f"Axis {self.axis_id}: alarm/error: {status}")
            if status.get("moving") == 0:
                if expected_target is not None:
                    if status.get("target") != expected_target or status.get("position") != expected_target:
                        raise RuntimeError(
                            f"Axis {self.axis_id}: target/actual mismatch: {status}"
                        )
                return status
            time.sleep(0.05)
        raise TimeoutError(f"Axis {self.axis_id} не остановилась за {limit}s; status={last}")

    def firmware_stop_or_reset(self, command: str):
        """Send G24/G25 and invalidate absolute coordinates immediately."""
        if command not in ("G24", "G25"):
            raise ValueError("only G24/G25 invalidate absolute coordinates")
        self.invalidate_absolute_coordinates(command)
        self.transport.send(command)

    def _set_params(self, speed: int, accel: int):
        self.transport.send(f"G21 S{speed} P{self.axis_id}")
        self.transport.send(f"G22 S{accel} P{self.axis_id}")

    def _set_limits(self, minimum: int, maximum: int):
        self.transport.send(f"G31 S{minimum} P{self.axis_id}")
        self.transport.send(f"G32 S{maximum} P{self.axis_id}")
        self.transport.send(f"G33 S1 P{self.axis_id}")
        time.sleep(0.15)
