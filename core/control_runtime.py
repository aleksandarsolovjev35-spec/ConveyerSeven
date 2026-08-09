"""Explicit control-core composition used by production integrations.

The existing :class:`ProductionCycle` remains the detailed line algorithm;
this façade makes ownership and command serialization explicit for launchers,
test harnesses and HMI adapters.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from core.command_arbiter import CommandArbiter


class CommandGate:
    """Reject physical commands unless the runtime is in a permitted phase."""

    def __init__(self):
        self._lock = threading.RLock()
        self._blocked = False
        self.reason = None

    def block(self, reason: str):
        with self._lock:
            self._blocked = True
            self.reason = str(reason)

    def unblock(self):
        with self._lock:
            self._blocked = False
            self.reason = None

    def check(self):
        with self._lock:
            if self._blocked:
                raise RuntimeError(f"physical command gate blocked: {self.reason}")


@dataclass
class StepExecutor:
    cycle: object

    def execute(self):
        return self.cycle._run_once()


@dataclass
class InspectionExecutor:
    cycle: object

    def execute(self, frame_runs, accept_input=True):
        return self.cycle._stage_analysis(frame_runs, accept_input)


class ControlRuntime:
    """One process/owner for state machine, journal and hardware adapters."""

    def __init__(self, cycle, *, command_gate: CommandGate | None = None):
        self.cycle = cycle
        self.command_gate = command_gate or CommandGate()
        self.step_executor = StepExecutor(cycle)
        self.inspection_executor = InspectionExecutor(cycle)
        self.command_arbiter = CommandArbiter(
            self._handle_command,
            lambda: cycle.sm.get_snapshot(),
        )

    @property
    def state(self):
        return self.cycle.state

    def start(self):
        """Run the single control-core loop; HMI never owns this thread."""
        return self.cycle.start()

    def dispatch(self, command_id: str, command: str, **payload):
        return self.command_arbiter.submit(command_id, command, **payload)

    def _handle_command(self, command: str, **payload):
        self.command_gate.check() if command in {"START", "RESUME", "JOG"} else None
        handlers = {
            "START": self.cycle.request_start,
            "STOP": self.cycle.request_stop,
            "PAUSE": self.cycle.request_pause,
            "RESUME": self.cycle.request_resume,
            "FORCE_EXIT": self.cycle.request_force_exit,
            "EXIT": self.cycle.request_exit,
        }
        if command == "JOG":
            return self.cycle.jog_hold_start(payload.get("direction"))
        if command == "JOG_ENTER":
            return self.cycle.enter_jog()
        if command == "JOG_EXIT":
            return self.cycle.exit_jog()
        if command == "JOG_HEARTBEAT":
            return self.cycle.jog_hold_heartbeat(payload.get("direction"))
        if command == "JOG_RELEASE":
            return self.cycle.jog_hold_release(payload.get("reason", "button released"))
        handler = handlers.get(command)
        if handler is None:
            raise ValueError(f"unsupported command: {command}")
        return handler()
