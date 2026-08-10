"""Atomic readiness gate for mandatory production dependencies."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from core.control_model import HealthState


CAMERA_ROLES = frozenset({
    "INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
    "SPIDER_IN", "SPIDER_OUT", "TOP",
})


class HealthGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class HealthReport:
    state: HealthState
    cameras: Mapping[str, bool]
    controller_links: bool
    conveyor_telemetry: bool
    distributor_telemetry: bool
    workers_ready: bool
    journal_writable: bool
    archive_writable: bool
    disk_reserve: bool
    no_root_fault: bool
    hardware_idle: bool
    reasons: tuple[str, ...]
    revision: int

    @property
    def ready(self) -> bool:
        return self.state is HealthState.READY


class HealthSupervisor:
    """Continuously updated health with one lock-protected gate snapshot."""

    def __init__(self, *, archive_enabled: bool = True):
        self.archive_enabled = bool(archive_enabled)
        self._lock = threading.RLock()
        self._revision = 0
        self._cameras = {role: False for role in CAMERA_ROLES}
        self._checks = {
            "controller_links": False,
            "conveyor_telemetry": False,
            "distributor_telemetry": False,
            "workers_ready": False,
            "journal_writable": False,
            "archive_writable": not self.archive_enabled,
            "disk_reserve": False,
            "no_root_fault": True,
            "hardware_idle": True,
        }

    def update_camera(self, role: str, healthy: bool):
        if role not in CAMERA_ROLES:
            raise ValueError(f"unknown camera role: {role}")
        with self._lock:
            self._cameras[role] = bool(healthy)
            self._revision += 1

    def update(self, **checks: bool):
        unknown = set(checks) - set(self._checks)
        if unknown:
            raise ValueError(f"unknown health checks: {sorted(unknown)}")
        with self._lock:
            for name, value in checks.items():
                self._checks[name] = bool(value)
            if checks:
                self._revision += 1

    def report(self) -> HealthReport:
        with self._lock:
            cameras = dict(self._cameras)
            checks = dict(self._checks)
            revision = self._revision
        reasons = [
            f"camera {role} is not healthy"
            for role in sorted(CAMERA_ROLES)
            if not cameras[role]
        ]
        labels = {
            "controller_links": "controller link is not open",
            "conveyor_telemetry": "conveyor telemetry is invalid",
            "distributor_telemetry": "distributor telemetry is invalid",
            "workers_ready": "model workers are not alive and warm",
            "journal_writable": "journal is not writable",
            "archive_writable": "archive is not writable",
            "disk_reserve": "disk reserve is insufficient",
            "no_root_fault": "root fault is latched",
            "hardware_idle": "conflicting hardware operation is active",
        }
        reasons.extend(label for key, label in labels.items() if not checks[key])
        state = HealthState.READY if not reasons else HealthState.NOT_READY
        return HealthReport(
            state=state,
            cameras=MappingProxyType(cameras),
            reasons=tuple(reasons),
            revision=revision,
            **checks,
        )

    def gate(self) -> HealthReport:
        report = self.report()
        if not report.ready:
            raise HealthGateError("; ".join(report.reasons))
        return report
