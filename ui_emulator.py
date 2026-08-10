#!/usr/bin/env python3
"""Formal production lifecycle running on deterministic simulated adapters.

The simulator replaces only camera/hardware/model ports.  Commands, reducer,
StepPhase sequence, pending intents, inspection aggregate, publication, drain,
fault handling and archive boundaries are the same ProductionCycle used by the
real executable.  The HMI is permanently marked ``SIMULATION``.
"""

from __future__ import annotations

import argparse
import random
import threading
import time
import uuid
from types import SimpleNamespace

import cv2
import numpy as np

from config import load_archive_config
from core.control_runtime import ControlRuntime
from core.production_cycle import ProductionCycle
from core.recovery_journal import RecoveryJournal
from domain.defect_rules.base import RuleResult
from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP, CATEGORY_GOOD
from domain.threshold_loader import ThresholdLoader
from inspection.part_archive import PartArchive
from inspection.result import InspectionResult
from vision.ui.live_monitor import LiveMonitor

ROLES = (
    "INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
    "SPIDER_IN", "SPIDER_OUT", "TOP",
)
INPUT_ROLES = ROLES[:2]
CONTROL_ROLES = ROLES[2:]


class SimulatedCameras:
    def __init__(self, frame_source):
        self.mapping = {role: {"serial": f"SIM-{index}"} for index, role in enumerate(ROLES)}
        self.serials = {role: f"SIM-{index}" for index, role in enumerate(ROLES)}
        self.cameras = dict(self.mapping)
        self._frame_source = frame_source
        self.live_running = True
        self.live_error = None
        self.live_generation = 1

    def require_live_health(self):
        if self.live_error:
            raise RuntimeError(self.live_error)
        return True

    def freshness_boundary(self):
        return time.monotonic()

    def capture_roles(self, roles, after=None, max_age=None):
        return {role: self._frame_source(role) for role in roles}

    def capture_single(self, role):
        return self._frame_source(role)

    def capture_all(self):
        return self.capture_roles(ROLES)

    def get_live_snapshot(self, roles=None, **_kwargs):
        return self.capture_roles(tuple(roles or ROLES))

    def drain_buffers(self, roles=None):
        return None

    def release(self):
        self.live_running = False


class SimulatedConveyor:
    speed = 20_000
    steps_per_division = 19_048
    production_target = 38_096

    def __init__(self, delay=0.03):
        self.delay = delay
        self.moves = 0
        self.cancel_check = None

    def verify_reset_state(self):
        return {
            "pos": 0, "tgt": 0, "mov": 0, "wait": 0, "lasterr": 0,
            "lastreadyms": self.moves,
        }

    def move_step(self):
        if self.cancel_check and self.cancel_check():
            raise RuntimeError("simulated conveyor cancelled")
        self.moves += 1

    def wait_stop(self, progress_callback=None, **_kwargs):
        if progress_callback:
            progress_callback({
                "pos": 10_000, "tgt": self.production_target,
                "mov": 1, "wait": 0, "lasterr": 0,
                "lastreadyms": self.moves - 1, "paused": 1, "auto": 1,
            })
        time.sleep(self.delay)
        final = {
            "pos": 0, "tgt": 0, "mov": 0, "wait": 0, "lasterr": 0,
            "lastreadyms": self.moves, "paused": 1, "auto": 1,
        }
        if progress_callback:
            progress_callback(final)
        return final

    def emergency_stop(self):
        return None


class SimulatedDistributor:
    dist1_open_position = 340
    dist2_bad_position = 0
    dist2_cleanup_position = 340

    def __init__(self):
        self.on_state_changed = None
        self.cancel_check = None
        self._dist1 = 0
        self._dist2 = 0
        self._target = "BAD"
        self.last_action = "SIMULATION READY"
        self.transfers = []

    @property
    def status(self):
        return {
            "dist1_state": "GOOD" if self._dist1 == 0 else "TO_DIST2",
            "dist1_position": self._dist1, "dist1_max": 340,
            "dist2_state": "IDLE", "dist2_position": self._dist2,
            "dist2_max": 340, "dist2_target": self._target,
            "last_distributor_action": self.last_action,
        }

    def _notify(self):
        if callable(self.on_state_changed):
            self.on_state_changed()

    def initialize(self):
        self.return_home()

    def verify_routes(self):
        for category in (CATEGORY_GOOD, CATEGORY_BAD, CATEGORY_CLEANUP):
            self.prepare_route(category)
        self.return_home()

    def park_production(self):
        self.return_home()

    def return_home(self):
        self._dist1 = self._dist2 = 0
        self._target = CATEGORY_BAD
        self.last_action = "HOME CONFIRMED"
        self._notify()

    def prepare_route(self, category, part_id=None):
        if category == CATEGORY_GOOD:
            self._dist1 = 0
        else:
            self._dist2 = 340 if category == CATEGORY_CLEANUP else 0
            self._dist1 = 340
        self._target = category
        self.last_action = f"ROUTE #{part_id} -> {category}"
        self._notify()

    def confirm_transfer(self, part_id, category):
        self.transfers.append((part_id, category))
        self.last_action = f"TRANSFER #{part_id} -> {category}"
        self._notify()

    def reset_target(self):
        return None

    def emergency_stop(self):
        self.last_action = "EMERGENCY STOP"

    def diagnostic_gate(self, command):
        self._dist1 = 0 if command == "HOME" else 340
        self._notify()

    def diagnostic_route(self, category):
        self.prepare_route(category, "DIAG")


class SimulatedJog:
    def __init__(self):
        self.busy = False
        self.direction = None
        self.error = None
        self.hold_steps = 100_000
        self.last_action = "-"
        self._motion = False

    @property
    def status(self):
        return {
            "busy": self.busy, "direction": self.direction,
            "error": self.error, "hold_steps": self.hold_steps,
            "last_action": self.last_action,
        }

    def start_hold(self, direction):
        self.busy = True
        self.direction = direction
        self.last_action = f"HOLD {direction}"
        return True

    def heartbeat(self, direction):
        if not self.busy or direction != self.direction:
            return False
        self._motion = True
        return True

    def release(self, reason="released"):
        self.busy = False
        self.direction = None
        self.last_action = f"STOP: {reason}"
        return True

    def consume_motion_happened(self):
        value = self._motion
        self._motion = False
        return value


class SimulatedInspector:
    INPUT_ROLES = INPUT_ROLES
    SPIDER_ROLES = CONTROL_ROLES

    def __init__(self, rng):
        self.rng = rng
        try:
            thresholds = ThresholdLoader().get_all()
        except Exception:
            thresholds = {"SIM.threshold": 1.0}
        self.decision = SimpleNamespace(thresholds=thresholds)
        self.vision = SimpleNamespace(worker_timeout=3.0, last_health=[])

    def inspect_input_consensus(
        self, part_id, step, frame_runs, force_bad=False, on_presence=None,
    ):
        frames = {role: frame_runs[0][role] for role in INPUT_ROLES}
        left = self.rng.random() < 0.90
        right = self.rng.random() < 0.90
        present = left or right
        presence = RuleResult(
            "part_presence", False,
            details={
                "empty_tray": not present,
                "presence_by_role": {"INPUT_LEFT": left, "INPUT_RIGHT": right},
                "count_by_role": {
                    "INPUT_LEFT": 4 if left else 0,
                    "INPUT_RIGHT": 4 if right else 0,
                },
                "presence_mismatch": left != right,
            },
        )
        if not present:
            return InspectionResult(
                stage="input", is_empty_tray=True,
                vision_results={role: [] for role in INPUT_ROLES},
                rule_results=[presence], raw_frames=frames,
                consensus={"runs": 1, "required_votes": 1},
                run_frames=[frames], run_rule_results=[[]],
            )
        if callable(on_presence):
            on_presence(presence)
        defects = []
        if force_bad or self.rng.random() < 0.08:
            defects.append("window_geometry")
        rule = RuleResult("window_geometry", bool(defects))
        return InspectionResult(
            stage="input", defects=defects,
            vision_results={role: [] for role in INPUT_ROLES},
            rule_results=[presence, rule], raw_frames=frames,
            raw_overlay_frames=frames, annotated=frames,
            consensus={"runs": 1, "required_votes": 1},
            run_frames=[frames], run_rule_results=[[rule]],
            run_vision_results=[{role: [] for role in INPUT_ROLES}],
        )

    def inspect_spider_consensus(self, part_id, step, frame_runs, force_bad=False):
        frames = {role: frame_runs[0][role] for role in CONTROL_ROLES}
        defects = []
        value = self.rng.random()
        if force_bad or value < 0.08:
            defects.append("contacts_long")
        elif value < 0.13:
            defects.append("glass")
        rule = RuleResult(defects[0] if defects else "contacts_long", bool(defects))
        return InspectionResult(
            stage="spider", defects=defects,
            vision_results={role: [] for role in CONTROL_ROLES},
            rule_results=[rule], raw_frames=frames,
            raw_overlay_frames=frames, annotated=frames,
            consensus={"runs": 1, "required_votes": 1},
            run_frames=[frames], run_rule_results=[[rule]],
            run_vision_results=[{role: [] for role in CONTROL_ROLES}],
        )


class Emulator:
    def __init__(
        self,
        seed=None,
        review_seconds=5.0,
        auto_start=False,
        archive_enabled=True,
        time_scale=1.0,
    ):
        self.rng = random.Random(seed)
        self.auto_start = bool(auto_start)
        self.time_scale = max(0.01, float(time_scale))
        self.monitor = LiveMonitor(fullscreen=False)
        self.server = self.monitor.server
        self.distributor = SimulatedDistributor()
        self.conveyor = SimulatedConveyor(delay=0.03 * self.time_scale)
        self.jog = SimulatedJog()
        self.cycle = None
        self.cameras = SimulatedCameras(self._frame)
        self.inspector = SimulatedInspector(self.rng)

        cfg = load_archive_config()
        self.archive = PartArchive(
            root_folder=cfg["root_path"], enabled=bool(archive_enabled),
            jpeg_quality=cfg["jpeg_quality"],
            zip_compression=cfg["zip_compression"],
            zip_level=cfg["zip_level"],
            compress_on_shutdown=False,
        )
        self.server.archive = self.archive
        self.journal = RecoveryJournal(
            str(self.archive.root_folder) + "/simulation_recovery_v2.jsonl"
        )
        self.server.thresholds = dict(self.inspector.decision.thresholds)
        self.server.thresholds_path = "thresholds.json"

        self.cycle = ProductionCycle(
            conveyor=self.conveyor,
            cameras=self.cameras,
            inspector=self.inspector,
            distributor=self.distributor,
            monitor=self.monitor,
            archive=self.archive,
            jog=self.jog,
            settle_seconds=0.03 * self.time_scale,
            stage_trace_seconds=0.0,
            review_seconds=float(review_seconds),
            journal=self.journal,
            initial_frame_max_age=5.0,
            manifest={"mode": "SIMULATION"},
        )
        self.cycle.simulation = True
        self.runtime = ControlRuntime(self.cycle)
        self.runtime.register_handler("DIST_DIAG", self.cycle.distributor_diagnostic)
        self.runtime.register_handler("CAM_DIAG", lambda: True)
        self.runtime.register_handler("VISION_DIAG", lambda: True)
        self.runtime.register_handler("SELECTED", lambda _role: True)
        self.runtime.register_handler("SELECTED_RELEASE", lambda: True)
        self.server.command_dispatcher = self.runtime.dispatch
        self.monitor.distributor_diagnostic_callback = lambda command: self.runtime.dispatch(
            uuid.uuid4().hex, "DIST_DIAG", command
        ).accepted
        self.monitor.camera_diagnostic_callback = lambda: self.runtime.dispatch(
            uuid.uuid4().hex, "CAM_DIAG"
        ).accepted
        self.monitor.vision_rule_diagnostic_callback = lambda: self.runtime.dispatch(
            uuid.uuid4().hex, "VISION_DIAG"
        ).accepted
        self.monitor.selected_model_analysis_callback = lambda role: self.runtime.dispatch(
            uuid.uuid4().hex, "SELECTED", role
        ).accepted
        self.monitor.selected_model_release_callback = lambda: self.runtime.dispatch(
            uuid.uuid4().hex, "SELECTED_RELEASE"
        ).accepted
        self._cycle_thread = None

    @property
    def sm(self):
        return self.cycle.sm

    @property
    def parts(self):
        return self.cycle.parts

    @property
    def current_step(self):
        return self.cycle.current_step

    def _frame(self, role):
        frame = np.full((720, 1280, 3), 28, dtype=np.uint8)
        cv2.putText(
            frame,
            f"SIMULATION  {role}  STEP {self.current_step if self.cycle else 0}",
            (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (180, 180, 180), 2,
        )
        return frame

    def _boot(self):
        for key, _label in self.server.BOOT_STEPS:
            self.server.boot_step_start(key)
            self.server.boot_step_done(key)
        self.server.boot_complete()

    def begin_simulation(self, start=True):
        self.monitor.update(
            frames=self.cameras.capture_all(),
            line_status=self.cycle._build_status(),
            recent_parts=[],
        )
        self.cycle.live.start()
        self._cycle_thread = threading.Thread(
            target=self.runtime.start,
            daemon=True,
            name="formal-simulation-control-core",
        )
        self._cycle_thread.start()
        if self.auto_start and start:
            self.runtime.dispatch(uuid.uuid4().hex, "START")

    def request_start(self):
        return self.runtime.dispatch(uuid.uuid4().hex, "START").accepted

    def request_stop(self):
        return self.runtime.dispatch(uuid.uuid4().hex, "STOP").accepted

    def request_pause(self):
        return self.runtime.dispatch(uuid.uuid4().hex, "PAUSE").accepted

    def request_resume(self):
        return self.runtime.dispatch(uuid.uuid4().hex, "RESUME").accepted

    def request_exit(self):
        return self.runtime.dispatch(uuid.uuid4().hex, "EXIT").accepted

    def request_force_exit(self):
        result = self.runtime.dispatch(uuid.uuid4().hex, "FORCE_EXIT")
        return result.accepted

    def run(self, host="0.0.0.0", port=8000, auto_start=None):
        self._boot()
        self.server.start_server(host=host, port=port)
        self.begin_simulation(
            start=self.auto_start if auto_start is None else bool(auto_start)
        )
        print(f"FORMAL SIMULATION UI: http://localhost:{port}")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.request_force_exit()
            self.server.stop_server()


def _parse_args():
    parser = argparse.ArgumentParser(description="Formal conveyor simulator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--review", type=float, default=5.0)
    parser.add_argument("--auto-start", action="store_true")
    parser.add_argument("--no-archive", action="store_true")
    parser.add_argument("--time-scale", type=float, default=1.0)
    return parser.parse_args()


def main():
    args = _parse_args()
    Emulator(
        seed=args.seed,
        review_seconds=args.review,
        auto_start=args.auto_start,
        archive_enabled=not args.no_archive,
        time_scale=args.time_scale,
    ).run(args.host, args.port)


if __name__ == "__main__":
    main()
