"""Convey15 v2.4.0 production motion adapter.

The adapter deliberately proves one firmware STEP/DIR cycle.  It does not
pretend that POS is encoder feedback and it never retries a motion command
when acknowledgement is ambiguous.
"""

from __future__ import annotations

import re
import time


class MotionContractError(RuntimeError):
    """Telemetry cannot prove exactly one completed production step."""


class Conveyor:
    CELL_STEPS = 19_048 * 2
    PRODUCTION_TARGET = CELL_STEPS

    def __init__(
        self,
        transport,
        speed: int = 20000,
        accel: int = 6000,
        steps_per_division: int = 19048,
        divisions_per_movement: int = 2,
        telemetry_timeout: float = 15.0,
        telemetry_poll_interval: float = 0.05,
    ):
        self.transport = transport
        if steps_per_division <= 0 or divisions_per_movement <= 0:
            raise ValueError("Conveyor geometry must be positive")
        self._set_params(speed, accel, steps_per_division, divisions_per_movement)
        self.production_target = int(steps_per_division) * int(divisions_per_movement)
        self.telemetry_timeout = float(telemetry_timeout)
        self.telemetry_poll_interval = float(telemetry_poll_interval)
        if self.telemetry_timeout <= 0 or self.telemetry_poll_interval < 0:
            raise ValueError("Invalid conveyor telemetry timing")
        self._motion_started = False
        self._pre_motion_status = None
        self._armed_seen = False
        self._armed_position_seen = False
        self._ready_changed = False
        self._last_status = None
        self.cancel_check = None

    def move_step(self):
        """Issue exactly one forward production command.

        The command itself is intentionally not retried.  Call
        :meth:`wait_stop` once to prove its result.
        """
        if self._motion_started:
            raise MotionContractError("production movement is already awaiting acknowledgement")
        before = self.read_status()
        self._require_reset_state(before, "before G3")
        if self.cancel_check is not None and self.cancel_check():
            raise MotionContractError("conveyor motion cancelled before G3")
        self._pre_motion_status = before
        self._armed_seen = False
        self._armed_position_seen = False
        self._ready_changed = False
        self._last_status = before
        self.transport.send("G3")
        self._motion_started = True

    def execute_production_step(self, timeout: float | None = None, progress_callback=None):
        """Send and fully acknowledge one production cell."""
        self.move_step()
        return self.wait_stop(timeout=timeout, progress_callback=progress_callback)

    def verify_reset_state(self):
        """Prove the firmware reset state without moving the conveyor."""
        status = self.read_status()
        self._require_reset_state(status, "START preflight")
        return status

    def wait_stop(self, timeout: float | None = None, progress_callback=None):
        """Prove the v2.4.0 two-phase telemetry contract.

        Empty/malformed telemetry is treated as a missed read and retried
        within the same budget.  No G3 is sent again.  Any deadline or
        ambiguous end raises :class:`MotionContractError`.
        """
        if not self._motion_started:
            raise MotionContractError("wait_stop called without one issued G3")
        limit = self.telemetry_timeout if timeout is None else float(timeout)
        if limit <= 0:
            self._motion_started = False
            raise MotionContractError("invalid motion telemetry timeout")
        started = time.monotonic()
        last_i1 = ""
        last_i2 = ""
        try:
            while time.monotonic() - started <= limit:
                if self.cancel_check is not None and self.cancel_check():
                    raise MotionContractError("conveyor motion cancelled by FORCE EXIT")
                try:
                    last_i1 = self.transport.query("I1", delay=0.05)
                    last_i2 = self.transport.query("I2", delay=0.05)
                except Exception as exc:
                    # A transport exception is not evidence of a completed
                    # move; do not reconnect and do not issue another G3.
                    raise MotionContractError(f"conveyor telemetry link failed: {exc}") from exc

                status = self._parse_status(last_i2)
                self._last_status = status
                if progress_callback is not None:
                    progress_callback(status)

                if self._has_armed_target(last_i2, status):
                    self._armed_seen = True
                if self._armed_seen and status.get("pos") is not None:
                    position = status["pos"]
                    if not 0 <= position <= self.production_target:
                        raise MotionContractError(
                            f"POS={position} outside armed range 0..{self.production_target}"
                        )
                    self._armed_position_seen = True
                # ``lastReadyMs`` is the firmware edge proving that the
                # controller accepted the next cycle.  It must change only
                # after the armed target has actually been observed.
                before_ready = self._pre_motion_status.get("lastreadyms") if self._pre_motion_status else None
                if self._armed_seen and before_ready is not None and status.get("lastreadyms") is not None:
                    if status["lastreadyms"] != before_ready:
                        self._ready_changed = True

                if self._contract_complete(last_i1, status):
                    return status
                if status.get("lasterr") not in (None, 0):
                    raise MotionContractError(
                        f"firmware LASTERR={status.get('lasterr')}; I2={last_i2!r}"
                    )
                time.sleep(self.telemetry_poll_interval)

            raise MotionContractError(
                "ambiguous conveyor movement acknowledgement; "
                f"I1={last_i1!r}; I2={last_i2!r}; "
                f"armed={self._armed_seen}; position_seen={self._armed_position_seen}; "
                f"ready_changed={self._ready_changed}"
            )
        finally:
            # A caller may inspect the latched status, but a second wait must
            # never accidentally acknowledge the same command.
            self._motion_started = False

    def read_status(self) -> dict:
        try:
            raw = self.transport.query("I2", delay=0.05)
        except Exception as exc:
            raise MotionContractError(f"conveyor telemetry link failed: {exc}") from exc
        return self._parse_status(raw)

    def emergency_stop(self):
        """Best-effort hardware stop; caller must treat positions as unknown."""
        self.transport.send("G1")

    def _set_params(self, speed, accel, steps_per_division, divisions_per_movement):
        self.transport.send(f"G5 S{speed}")
        self.transport.send(f"G4 S{accel}")
        self.transport.send(f"G7 S{steps_per_division}")
        self.transport.send(f"G6 S{divisions_per_movement}")
        self.speed = int(speed)
        self.accel = int(accel)
        self.steps_per_division = int(steps_per_division)
        self.divisions_per_movement = int(divisions_per_movement)
        # Firmware needs a short settle after changing motion parameters.
        time.sleep(0.5)

    def _require_reset_state(self, status: dict, phase: str):
        required = {
            "pos": 0,
            "tgt": 0,
            "mov": 0,
            "wait": 0,
        }
        missing = [key for key, value in required.items() if status.get(key) != value]
        if status.get("lasterr") not in (0, None):
            missing.append(f"lasterr={status.get('lasterr')}")
        if missing:
            raise MotionContractError(
                f"{phase}: firmware reset state not proven ({', '.join(map(str, missing))}); "
                f"raw={status.get('raw')!r}"
            )
        if status.get("lastreadyms") is None:
            raise MotionContractError(f"{phase}: lastReadyMs is missing; raw={status.get('raw')!r}")

    def _has_armed_target(self, raw: str, status: dict) -> bool:
        target = status.get("tgt")
        # Require an observed target, not just a locally remembered command.
        return target == self.production_target or bool(
            re.search(rf"\bTGT\s*=\s*{self.production_target}\b", raw or "", re.I)
        )

    def _contract_complete(self, i1_raw: str, status: dict) -> bool:
        required_final = {
            "pos": 0,
            "tgt": 0,
            "mov": 0,
            "wait": 0,
            "paused": 1,
            "auto": 1,
            "lasterr": 0,
        }
        if not self._armed_seen or not self._armed_position_seen or not self._ready_changed:
            return False
        if any(status.get(key) != expected for key, expected in required_final.items()):
            return False
        stopped = self._parse_motion_reply(i1_raw)
        return stopped is True

    @staticmethod
    def _parse_status(data: str) -> dict:
        result = {"raw": data or ""}
        # Firmware has emitted both LASTERR and lastErr over its revisions;
        # all keys are normalised to lower case without punctuation.
        fields = {
            "mov": ("MOV",),
            "wait": ("WAIT",),
            "pos": ("POS",),
            "tgt": ("TGT",),
            "lasterr": ("LASTERR", "lastErr"),
            "lastreadyms": ("lastReadyMs", "LASTREADYMS", "lastReady"),
            "paused": ("PAUSED",),
            "auto": ("AUTO",),
        }
        for destination, names in fields.items():
            match = None
            for name in names:
                match = re.search(rf"\b{re.escape(name)}\s*=\s*(-?\d+)\b", data or "", re.I)
                if match:
                    break
            result[destination] = int(match.group(1)) if match else None
        # Compatibility aliases used by older UI callbacks.
        result.update({
            "moving": result["mov"],
            "target": result["tgt"],
            "lastErr": result["lasterr"],
        })
        return result

    @staticmethod
    def _strict_stop_confirmed(data: str) -> bool:
        status = Conveyor._parse_status(data)
        return all(status.get(key) == value for key, value in {
            "mov": 0, "wait": 0, "lasterr": 0,
        }.items())

    @staticmethod
    def _parse_motion_reply(data: str) -> bool | None:
        if not data:
            return None
        for line in reversed(str(data).splitlines()):
            stripped = line.strip()
            if stripped == "0":
                return True
            if stripped == "1":
                return False
        # Some firmware replies I1=MOV=0 instead of a bare line.
        match = re.search(r"\b(?:MOV|I1)\s*=\s*([01])\b", str(data), re.I)
        if match:
            return match.group(1) == "0"
        return None
