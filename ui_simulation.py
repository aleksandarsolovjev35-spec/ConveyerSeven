"""Standalone, hardware-free simulator for the operator UI.

Run from the repository root:
    python ui_simulation.py --host 0.0.0.0 --port 8000

It serves the same FastAPI UI as production but never opens cameras, serial
ports, models, or pywebview. Use the UI buttons to start, pause, resume and
stop the simulated conveyor.
"""
from __future__ import annotations

import argparse
import signal
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

from domain.threshold_loader import ThresholdLoader
from inspection.part_archive import PartArchive
from vision.ui.server.server import CAMERA_ORDER, UIServer


@dataclass
class SimPart:
    id: int
    position: int = 0
    category: str = ""
    defects: list[str] | None = None
    inspected: bool = False


class LineSimulation:
    """Small deterministic conveyor model intended for UI development."""

    ROUTE_PREPARE_SECONDS = 0.24
    STEP_SECONDS = 0.72
    SETTLE_SECONDS = 0.28
    # Time with a stopped belt: the vertical input/output animation must
    # finish before another horizontal step starts.
    POST_STOP_SECONDS = 0.62
    CATEGORIES = ("GOOD", "BAD", "CLEANUP", "GOOD", "GOOD", "BAD")

    def __init__(self, server: UIServer):
        self.server = server
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.state = "IDLE"
        self.stop_requested = False
        self.dist1_position = 0
        self.dist2_position = 0
        self.dist1_state = "GOOD"
        self.dist2_state = "READY"
        self.last_distributor_action = "SIMULATION READY"
        self.jog_active = False
        self.jog_busy = False
        self.selected_role: str | None = None
        self.last_diagnostic = "SIMULATION READY"
        self.step = 0
        self.next_id = 1
        self.slot_counter = 0
        self.empty_count = 0
        self.parts: list[SimPart] = []
        self.egress: SimPart | None = None
        self.recent: list[dict] = []
        self.counts = {"total": 0, "good": 0, "bad": 0, "cleanup": 0}
        self.thread = threading.Thread(target=self._run, name="ui-simulation", daemon=True)

    def start(self) -> bool:
        with self._lock:
            if self.state in ("RUNNING", "PAUSED"):
                return False
            self.jog_active = False
            self.stop_requested = False
            self.state = "RUNNING"
            self._wake.set()
        return True

    def enter_jog(self) -> bool:
        with self._lock:
            if self.state not in ("IDLE", "STOPPED"):
                return False
            self.jog_active = True
        self._publish("IDLE")
        return True

    def exit_jog(self) -> bool:
        with self._lock:
            self.jog_active = False
            self.jog_busy = False
        self._publish("IDLE")
        return True

    def jog_hold_start(self, _direction: str) -> bool:
        with self._lock:
            if not self.jog_active or self.state not in ("IDLE", "STOPPED"):
                return False
            self.jog_busy = True
        self._publish("JOG")
        return True

    def jog_hold_release(self, _reason: str = "") -> bool:
        with self._lock:
            self.jog_busy = False
        self._publish("IDLE")
        return True

    def selected_analysis(self, role: str) -> bool:
        with self._lock:
            if self.state not in ("IDLE", "STOPPED") or role not in CAMERA_ORDER:
                return False
            self.selected_role = role
            self.last_diagnostic = f"ANALYSIS {role}"
        self._publish("SELECTED_ANALYSIS")
        return True

    def release_selected_analysis(self) -> bool:
        with self._lock:
            self.selected_role = None
            self.last_diagnostic = "LIVE STREAM RESTORED"
        self._publish("IDLE")
        return True

    def distributor_diagnostic(self, command: str) -> bool:
        with self._lock:
            if self.state not in ("IDLE", "STOPPED"):
                return False
            self.last_diagnostic = command
        self._publish("DISTRIBUTOR_DIAGNOSTIC")
        return True

    def camera_diagnostic(self) -> bool:
        with self._lock:
            self.last_diagnostic = "CAMERAS OK · 7 / 7"
        self._publish("CAMERA_DIAGNOSTIC")
        return True

    def vision_rule_diagnostic(self) -> bool:
        with self._lock:
            self.last_diagnostic = "MODELS AND RULES OK"
        self._publish("VISION_DIAGNOSTIC")
        return True

    def stop(self) -> bool:
        """Production-like stop: stop feeding, then drain bodies on line."""
        with self._lock:
            if self.state not in ("RUNNING", "PAUSED", "STOPPING"):
                return False
            self.stop_requested = True
            self.state = "STOPPING"
            self._wake.set()
        self._publish("STOPPING")
        return True

    def pause(self) -> bool:
        with self._lock:
            if self.state != "RUNNING":
                return False
            self.state = "PAUSED"
        self._publish("PAUSED")
        return True

    def resume(self) -> bool:
        with self._lock:
            if self.state != "PAUSED":
                return False
            self.state = "RUNNING"
            self._wake.set()
        return True

    def close(self) -> bool:
        self._stop.set()
        self._wake.set()
        return True

    def _new_part(self) -> SimPart | None:
        """Feed an empty tray periodically, just like a real input can."""
        self.slot_counter += 1
        if self.slot_counter % 9 == 0:
            self.empty_count += 1
            return None
        part = SimPart(self.next_id)
        self.next_id += 1
        self.counts["total"] += 1
        return part

    def _line_parts(self) -> list[dict]:
        result = []
        for part in self.parts:
            result.append({"id": part.id, "position": part.position, "category": part.category,
                           "held": False, "dropping": False})
        # The output body remains logically at +7 while it visibly reaches
        # +8. This mirrors the production status contract used by the UI.
        if self.egress:
            result.append({"id": self.egress.id, "position": 7, "category": self.egress.category,
                           "held": False, "dropping": True})
        return result

    def _prepare_distributor(self) -> None:
        part = next((item for item in self.parts if item.position == 7), None)
        category = (part.category if part else "GOOD") or "GOOD"
        self.last_distributor_action = f"ROUTE {category}"
        self.dist1_state = "MOVING_TO_GOOD" if category == "GOOD" else "MOVING_TO_DIST2"
        self.dist2_state = "MOVING" if category in ("BAD", "CLEANUP") else "READY"
        self._planned_route = category

    def _settle_distributor(self) -> None:
        category = getattr(self, "_planned_route", "GOOD")
        self.dist1_position = 0 if category == "GOOD" else 340
        self.dist2_position = 340 if category == "CLEANUP" else 0
        self.dist1_state = "GOOD" if category == "GOOD" else "TO_DIST2"
        self.dist2_state = "READY"

    def _frame_analysis_payload(self) -> dict:
        inspection = next((part for part in self.parts if part.position == 4), None)
        role = self.selected_role or ("TOP" if inspection else None)
        if not role:
            return {"available": False}
        category = inspection.category if inspection else "GOOD"
        defects = inspection.defects if inspection else []
        triggered = bool(defects)
        return {
            "available": True,
            "kind": "selected" if self.selected_role else "production",
            "active": True,
            "title": "СИМУЛИРОВАННЫЙ АНАЛИЗ КАДРА",
            "role": role,
            "part_id": inspection.id if inspection else None,
            "stage": "DIAGNOSTIC" if self.selected_role else "CONTROL",
            "verdict": category,
            "rules": [{
                "name": "part_presence", "title": "Наличие корпуса", "triggered": False,
                "run_cards": [[{"type": "metric", "metrics": [{"key": "simulated_confidence", "label": "Уверенность модели", "value": "0.99", "limit": "0.40", "ok": True}]}]],
            }, {
                "name": "sinks" if triggered else "window_geometry",
                "title": "Симулированная проверка",
                "triggered": triggered,
                "human_cause": ", ".join(defects) if defects else "Норма",
                "run_cards": [[{"type": "metric", "metrics": [{"key": "simulated_result", "label": "Результат правила", "value": "СРАБОТАЛО" if triggered else "НОРМА", "limit": "—", "ok": not triggered}]}]],
            }],
        }

    def _publish(self, phase: str) -> None:
        with self._lock:
            state = self.state
            line_parts = self._line_parts()
            position = 7 if self.egress else None
            active_roles = []
            if any(part.position == 0 for part in self.parts): active_roles = ["INPUT_LEFT", "INPUT_RIGHT"]
            elif any(part.position == 4 for part in self.parts): active_roles = ["SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP"]
            status = {
                "state": state,
                "exit_requested": False,
                "step": self.step,
                "in_line": len(line_parts),
                "line_parts": line_parts,
                "total": self.counts["total"],
                "good": self.counts["good"],
                "rejected": self.counts["bad"],
                "cleanup": self.counts["cleanup"],
                "empty": self.empty_count,
                "dist1_state": self.dist1_state,
                "dist1_position": self.dist1_position, "dist1_max": 340,
                "dist2_state": self.dist2_state, "dist2_position": self.dist2_position, "dist2_max": 340,
                "dist2_target": "CLEANUP" if self.dist2_position else "BAD",
                "last_distributor_action": self.last_distributor_action,
                # Production start is available while JOG is open: issuing
                # start closes JOG and transfers control to the cycle.
                "controls": {"start": state in ("IDLE", "STOPPED") and self.selected_role is None,
                             "stop": state in ("RUNNING", "PAUSED"), "pause": state == "RUNNING",
                             "resume": state == "PAUSED", "exit": True,
                             "jog_hold": self.jog_active and state in ("IDLE", "STOPPED") and self.selected_role is None,
                             "selected_model_analysis": state in ("IDLE", "STOPPED") and self.selected_role is None,
                             "selected_model_release": self.selected_role is not None,
                             "distributor_diagnostic": state in ("IDLE", "STOPPED") and self.selected_role is None,
                             "camera_diagnostic": state in ("IDLE", "STOPPED") and self.selected_role is None,
                             "vision_rule_diagnostic": state in ("IDLE", "STOPPED") and self.selected_role is None},
                "process": {"phase": phase, "positions": [position] if position is not None else [],
                            "part_id": self.egress.id if self.egress else None,
                            "inspection_roles": active_roles,
                            "conveyor": {"speed": 18000}},
                "selected_analysis": {"active": self.selected_role is not None, "role": self.selected_role},
                "diagnostic_allowed": state in ("IDLE", "STOPPED") and self.selected_role is None,
                "diagnostic_busy": False,
                "diagnostics": {
                    "cameras": [{"name": role, "ok": True, "message": "SIMULATED"} for role in CAMERA_ORDER],
                    "models": [{"name": "VISION", "ok": True, "message": "SIMULATED"}],
                    "rules": [{"name": "RULES", "ok": True, "message": self.last_diagnostic}],
                },
                "frame_analysis": self._frame_analysis_payload(),
                "live": {"running": True, "static": self.selected_role is not None, "streaming": self.selected_role is None,
                         "static_roles": [self.selected_role] if self.selected_role else [],
                         "all_roles_static": False, "fps": 25, "error": None},
                "jog": {"active": self.jog_active, "busy": self.jog_busy, "can_enter": state in ("IDLE", "STOPPED"),
                        "hold_steps": 0, "direction": None, "last_action": "SIMULATION", "live_fps": 25, "error": None},
            }
            recent = list(self.recent)
        # Publish fresh virtual camera frames on every production state change.
        # The regular UI therefore exercises its real frame versioning, RAW/
        # RULES source switching and thumbnail refresh paths.
        self.server.update(
            frames=demo_frames(self.step, phase, line_parts),
            line_status=status,
            recent_parts=recent,
        )

    def _inspect_part(self, part: SimPart) -> None:
        """Virtual inspection uses the same verdict vocabulary and archive flow."""
        if part.inspected:
            return
        category = self.CATEGORIES[(part.id - 1) % len(self.CATEGORIES)]
        part.category = category
        part.inspected = True
        part.defects = {
            "GOOD": [],
            "BAD": ["СИМУЛИРОВАННЫЙ ДЕФЕКТ ГЕОМЕТРИИ"],
            "CLEANUP": ["СИМУЛИРОВАННОЕ СТЕКЛО"],
        }[category]
        archive = self.server.archive
        if archive is not None:
            frames = dict(self.server.frames)
            archive.store_frames(part.id, "CONTROL", frames, frames)

    def _finish_egress(self) -> None:
        if not self.egress:
            return
        part = self.egress
        category = part.category or "GOOD"
        self.counts[category.lower() if category != "BAD" else "bad"] += 1
        decision = "SIMULATION · " + (", ".join(part.defects or []) or "НОРМА")
        self.recent.append({"id": part.id, "category": category, "decision": decision,
                            "human_cause": ", ".join(part.defects or [])})
        archive = self.server.archive
        if archive is not None:
            archive.finalize(part.id, category, decision, part.defects or [], self.step,
                             extra={"source": "ui_simulation"})
        self.recent = self.recent[-10:]
        self.egress = None

    def _run(self) -> None:
        self._publish("IDLE")
        while not self._stop.is_set():
            with self._lock:
                current = self.state
            if current not in ("RUNNING", "STOPPING"):
                self._wake.wait(0.2)
                self._wake.clear()
                continue
            if current == "STOPPING" and not self.parts and self.egress is None:
                with self._lock:
                    self.state = "STOPPED"
                    self.stop_requested = False
                self._publish("STOPPED")
                continue

            # Set the distributor first, then perform one synchronous step.
            # This mirrors the physical route preparation before the belt moves.
            with self._lock:
                self._prepare_distributor()
            self._publish("ROUTE_PREPARE")
            if self._stop.wait(self.ROUTE_PREPARE_SECONDS):
                break
            with self._lock:
                self._settle_distributor()
            # Horizontal step: every currently visible part advances together.
            self._publish("CONVEYOR_MOVING")
            if self._stop.wait(self.STEP_SECONDS):
                break
            with self._lock:
                if self.state not in ("RUNNING", "STOPPING"):
                    continue
                self.egress = next((p for p in self.parts if p.position == 7), None)
                self.parts = [p for p in self.parts if p is not self.egress]
                for part in self.parts:
                    part.position += 1
                self.step += 1
            self._publish("CONVEYOR_CONFIRMED")
            if self._stop.wait(self.SETTLE_SECONDS):
                break
            with self._lock:
                if self.state not in ("RUNNING", "STOPPING"):
                    continue
                self._finish_egress()          # falling from +8 starts now
                # A requested stop drains the existing queue without adding
                # another body at +0.
                arriving = self._new_part() if self.state == "RUNNING" else None
                if arriving is not None:
                    self.parts.insert(0, arriving)
                for part in self.parts:
                    if part.position >= 4:
                        self._inspect_part(part)
            self._publish("SETTLE")
            # New body falls into +0 and the output body falls from +8 while
            # the conveyor is stopped. Do not begin the next step yet.
            if self._stop.wait(self.POST_STOP_SECONDS):
                break


def configure_simulated_thresholds(server: UIServer) -> None:
    """Expose the real threshold editor without changing its source file.

    The simulator deliberately keeps edits in memory: an operator can verify
    every control and its lock state without accidentally modifying production
    calibration values in ``thresholds.json``.
    """
    loader = ThresholdLoader("thresholds.json")
    server.thresholds = dict(loader.thresholds)
    server.threshold_labels = dict(loader.labels)
    server.thresholds_revision = 1

    def apply(role: str, values: dict, labels: dict) -> dict:
        prefix = f"{role}."
        updated = dict(server.thresholds or {})
        for key, value in values.items():
            full_key = key if str(key).startswith(prefix) else prefix + str(key)
            if full_key not in updated:
                raise ValueError(f"Неизвестный порог: {key}")
            updated[full_key] = value
        # ``UIServer.apply_thresholds`` stores labels and increments revision.
        return updated

    server.on_thresholds_apply = apply


def demo_frames(step: int = 0, phase: str = "IDLE", line_parts: list[dict] | None = None) -> dict:
    """Generate changing camera feeds rather than static placeholder images."""
    frames = {}
    line_parts = line_parts or []
    moving_x = 105 + (step % 7) * 76
    for index, role in enumerate(CAMERA_ORDER):
        frame = np.zeros((480, 800, 3), dtype=np.uint8)
        frame[:] = (20 + index * 3, 27 + index * 2, 33 + index * 2)
        cv2.putText(frame, "UI SIMULATION · VIRTUAL CAMERA", (42, 54), cv2.FONT_HERSHEY_SIMPLEX, .65, (105, 205, 170), 2)
        cv2.putText(frame, role, (42, 90), cv2.FONT_HERSHEY_SIMPLEX, .68, (205, 214, 224), 2)
        cv2.putText(frame, f"STEP {step:04d} · {phase}", (42, 122), cv2.FONT_HERSHEY_SIMPLEX, .48, (142, 165, 185), 1)
        cv2.rectangle(frame, (42, 155), (758, 420), (72, 94, 110), 2)
        # A moving virtual case lets the preview strip and main viewport show
        # actual image updates during every conveyor step.
        category = next((p.get("category") for p in line_parts if p.get("category")), "")
        color = {"GOOD": (80, 205, 105), "BAD": (85, 85, 225), "CLEANUP": (70, 190, 230)}.get(category, (110, 125, 135))
        x = moving_x + (index % 3) * 12
        cv2.rectangle(frame, (x, 245), (x + 165, 340), color, -1)
        cv2.rectangle(frame, (x, 245), (x + 165, 340), (220, 230, 235), 2)
        cv2.putText(frame, "VIRTUAL PART", (x + 18, 298), cv2.FONT_HERSHEY_SIMPLEX, .48, (15, 20, 24), 1)
        frames[role] = frame
    return frames


def main() -> None:
    parser = argparse.ArgumentParser(description="Hardware-free Conveyor Seven UI simulator")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (0.0.0.0 for Arena preview)")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()

    server = UIServer()
    # The real archive implementation writes only into ignored sandbox data.
    # Its settings dialog and validation therefore behave exactly as in the app.
    server.archive = PartArchive(root_folder="archive/ui_simulation", enabled=True)
    simulation = LineSimulation(server)
    server.on_start = simulation.start
    server.on_stop = simulation.stop
    server.on_pause = simulation.pause
    server.on_resume = simulation.resume
    server.on_exit = simulation.close
    server.on_jog_enter = simulation.enter_jog
    server.on_jog_exit = simulation.exit_jog
    server.on_jog_hold_start = simulation.jog_hold_start
    server.on_jog_hold_heartbeat = simulation.jog_hold_start
    server.on_jog_hold_release = simulation.jog_hold_release
    server.on_distributor_diagnostic = simulation.distributor_diagnostic
    server.on_camera_diagnostic = simulation.camera_diagnostic
    server.on_vision_rule_diagnostic = simulation.vision_rule_diagnostic
    server.on_selected_model_analysis = simulation.selected_analysis
    server.on_selected_model_release = simulation.release_selected_analysis
    configure_simulated_thresholds(server)
    server.update(frames=demo_frames())
    server.set_active_camera_role(CAMERA_ORDER[0])
    for key, _ in server.BOOT_STEPS:
        server.boot_step_done(key, "Симулятор UI готов")
    server.boot_complete()
    simulation.thread.start()
    server.start_server(host=args.host, port=args.port)
    print(f"[SIMULATION] Open http://{args.host}:{args.port}; press ПУСК to begin")

    stopping = threading.Event()
    def stop_signal(*_args):
        stopping.set()
        simulation.close()
    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)
    try:
        while not stopping.wait(0.5):
            pass
    finally:
        simulation.close()
        server.stop_server()


if __name__ == "__main__":
    main()
