"""Serialized production control event loop and adapter composition."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from core.command_arbiter import PRIORITY, CommandArbiter, CommandResult
from core.control_core import StepExecutor


class CommandGate:
    """Reject physical commands after FAULT/FORCE EXIT."""

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
class ProductionStepAdapter:
    cycle: object

    def execute(self):
        return self.cycle._run_once()


@dataclass
class InspectionExecutor:
    cycle: object

    def execute(self, frame_runs, accept_input=True):
        return self.cycle._stage_analysis(frame_runs, accept_input)


@dataclass
class _CommandEnvelope:
    command_id: str
    command: str
    payload: dict[str, Any]
    completed: threading.Event = field(default_factory=threading.Event)
    result: CommandResult | None = None
    error: BaseException | None = None


class ControlRuntime:
    """One sequential event loop owns all state and hardware mutations.

    HMI/watchdog threads enqueue commands and wait for their memoized result.
    The cycle calls :meth:`process_pending` at safe polling points, including
    motion telemetry, worker waits, REVIEW and PAUSED loops. No HMI thread calls
    a ProductionCycle mutator directly.
    """

    def __init__(self, cycle, *, command_gate: CommandGate | None = None):
        self.cycle = cycle
        self.command_gate = command_gate or CommandGate()
        self.step_executor = ProductionStepAdapter(cycle)
        self.inspection_executor = InspectionExecutor(cycle)
        self.command_arbiter = CommandArbiter(
            self._handle_command,
            lambda: cycle.sm.get_snapshot(),
        )
        self._queue: queue.Queue[_CommandEnvelope] = queue.Queue()
        self._owner_thread_id: int | None = None
        self._stopped = threading.Event()
        self._service_handlers: dict[str, Callable[..., Any]] = {}
        cycle._command_pump = self.process_pending

    @property
    def state(self):
        return self.cycle.state

    def register_handler(self, command: str, handler: Callable[..., Any]):
        if not callable(handler):
            raise TypeError("service command handler must be callable")
        self._service_handlers[str(command).upper()] = handler

    def start(self):
        """Run the sole control loop on the current thread."""
        self._owner_thread_id = threading.get_ident()
        self._stopped.clear()
        try:
            return self.cycle.start()
        finally:
            self._stopped.set()
            self.process_pending(reject_reason="control runtime stopped")
            self._owner_thread_id = None

    def dispatch(
        self,
        command_id: str,
        command: str,
        *args,
        timeout: float = 180.0,
        **payload,
    ):
        if args:
            payload["args"] = args
        command = str(command).upper()
        # Test/offline integrations may dispatch before start(); there is no
        # competing owner in that case, so execute synchronously.
        if self._owner_thread_id is None or self._owner_thread_id == threading.get_ident():
            return self.command_arbiter.submit(command_id, command, **payload)
        envelope = _CommandEnvelope(command_id, command, dict(payload))
        self._queue.put(envelope)
        if not envelope.completed.wait(timeout):
            raise TimeoutError(f"control command {command} was not serviced in {timeout}s")
        if envelope.error is not None:
            raise envelope.error
        return envelope.result

    def process_pending(
        self,
        *,
        max_items: int | None = None,
        reject_reason: str | None = None,
    ) -> int:
        if (
            self._owner_thread_id is not None
            and self._owner_thread_id != threading.get_ident()
        ):
            return 0
        pending = []
        while max_items is None or len(pending) < max_items:
            try:
                pending.append(self._queue.get_nowait())
            except queue.Empty:
                break
        pending.sort(
            key=lambda item: PRIORITY.get(item.command, 3),
        )
        for envelope in pending:
            try:
                if reject_reason:
                    envelope.result = CommandResult(
                        command_id=envelope.command_id,
                        command=envelope.command,
                        accepted=False,
                        state=str(self.state),
                        reason=reject_reason,
                    )
                else:
                    envelope.result = self.command_arbiter.submit(
                        envelope.command_id,
                        envelope.command,
                        **envelope.payload,
                    )
            except BaseException as exc:
                envelope.error = exc
            finally:
                envelope.completed.set()
                self._queue.task_done()
        return len(pending)

    def _handle_command(self, command: str, **payload):
        args = tuple(payload.pop("args", ()))
        if command in {"START", "RESUME", "JOG"}:
            self.command_gate.check()
        if command == "JOG":
            direction = args[0] if args else payload.get("direction")
            return self.cycle.jog_hold_start(direction)
        if command == "JOG_ENTER":
            return self.cycle.enter_jog()
        if command == "JOG_EXIT":
            return self.cycle.exit_jog()
        if command == "JOG_HEARTBEAT":
            direction = args[0] if args else payload.get("direction")
            return self.cycle.jog_hold_heartbeat(direction)
        if command == "JOG_RELEASE":
            reason = args[0] if args else payload.get("reason", "button released")
            return self.cycle.jog_hold_release(reason)
        if command in self._service_handlers:
            return self._service_handlers[command](*args, **payload)
        handlers = {
            "START": self.cycle.request_start,
            "STOP": self.cycle.request_stop,
            "PAUSE": self.cycle.request_pause,
            "RESUME": self.cycle.request_resume,
            "FORCE_EXIT": self.cycle.request_force_exit,
            "EXIT": self.cycle.request_exit,
        }
        handler = handlers.get(command)
        if handler is None:
            raise ValueError(f"unsupported command: {command}")
        return handler()
