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
from vision.ui.server.server import CAMERA_ORDER, UIServer


@dataclass
class SimPart:
    id: int
    position: int = 0
    category: str = ""


class LineSimulation:
    """Small deterministic conveyor model intended for UI development."""

    STEP_SECONDS = 0.72
    SETTLE_SECONDS = 0.28
    CATEGORIES = ("GOOD", "BAD", "CLEANUP", "GOOD", "GOOD", "BAD")

    def __init__(self, server: UIServer):
        self.server = server
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.state = "IDLE"
        self.step = 0
        self.next_id = 1
        self.parts: list[SimPart] = []
        self.egress: SimPart | None = None
        self.recent: list[dict] = []
        self.counts = {"total": 0, "good": 0, "bad": 0, "cleanup": 0}
        self.thread = threading.Thread(target=self._run, name="ui-simulation", daemon=True)

    def start(self) -> bool:
        with self._lock:
            if self.state in ("RUNNING", "PAUSED"):
                return False
            self.state = "RUNNING"
            self._wake.set()
        return True

    def stop(self) -> bool:
        with self._lock:
            self.state = "IDLE"
            self._wake.set()
        self._publish("IDLE")
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

    def _new_part(self) -> SimPart:
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

    def _publish(self, phase: str) -> None:
        with self._lock:
            state = self.state
            line_parts = self._line_parts()
            position = 7 if self.egress else None
            category = self.egress.category if self.egress else ""
            d1 = 0 if category == "GOOD" else 340
            d2 = 340 if category == "CLEANUP" else 0
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
                "empty": 0,
                "dist1_state": "GOOD" if d1 == 0 else "TO_DIST2",
                "dist1_position": d1, "dist1_max": 340,
                "dist2_state": "READY", "dist2_position": d2, "dist2_max": 340,
                "dist2_target": "CLEANUP" if d2 else "BAD",
                "last_distributor_action": "SIMULATION",
                "controls": {"start": state in ("IDLE", "STOPPED"), "stop": state in ("RUNNING", "PAUSED"),
                             "pause": state == "RUNNING", "resume": state == "PAUSED", "exit": True,
                             "jog_hold": False, "selected_model_analysis": False,
                             "selected_model_release": False, "distributor_diagnostic": False,
                             "camera_diagnostic": False, "vision_rule_diagnostic": False},
                "process": {"phase": phase, "positions": [position] if position is not None else [],
                            "part_id": self.egress.id if self.egress else None,
                            "conveyor": {"speed": 18000}},
                "live": {"static": False, "streaming": True, "static_roles": [], "all_roles_static": False},
                "jog": {"active": False, "busy": False, "error": None},
            }
            recent = list(self.recent)
        self.server.update(line_status=status, recent_parts=recent)

    def _finish_egress(self) -> None:
        if not self.egress:
            return
        part = self.egress
        category = part.category or "GOOD"
        self.counts[category.lower() if category != "BAD" else "bad"] += 1
        self.recent.append({"id": part.id, "category": category, "decision": "SIMULATION"})
        self.recent = self.recent[-10:]
        self.egress = None

    def _run(self) -> None:
        self._publish("IDLE")
        while not self._stop.is_set():
            with self._lock:
                current = self.state
            if current != "RUNNING":
                self._wake.wait(0.2)
                self._wake.clear()
                continue

            # Horizontal step: every currently visible part advances together.
            self._publish("CONVEYOR_MOVING")
            if self._stop.wait(self.STEP_SECONDS):
                break
            with self._lock:
                if self.state != "RUNNING":
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
                if self.state != "RUNNING":
                    continue
                self._finish_egress()          # falling from +8 starts now
                arriving = self._new_part()    # falling into +0 starts now
                self.parts.insert(0, arriving)
                for part in self.parts:
                    if part.position >= 4 and not part.category:
                        part.category = self.CATEGORIES[(part.id - 1) % len(self.CATEGORIES)]
            self._publish("SETTLE")


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


def demo_frames() -> dict:
    frames = {}
    for index, role in enumerate(CAMERA_ORDER):
        frame = np.zeros((480, 800, 3), dtype=np.uint8)
        frame[:] = (20 + index * 3, 27 + index * 2, 33 + index * 2)
        cv2.putText(frame, "UI SIMULATION", (42, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (105, 205, 170), 2)
        cv2.putText(frame, role, (42, 132), cv2.FONT_HERSHEY_SIMPLEX, .75, (205, 214, 224), 2)
        cv2.rectangle(frame, (42, 175), (758, 405), (72, 94, 110), 2)
        frames[role] = frame
    return frames


def main() -> None:
    parser = argparse.ArgumentParser(description="Hardware-free Conveyor Seven UI simulator")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (0.0.0.0 for Arena preview)")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()

    server = UIServer()
    simulation = LineSimulation(server)
    server.on_start = simulation.start
    server.on_stop = simulation.stop
    server.on_pause = simulation.pause
    server.on_resume = simulation.resume
    server.on_exit = simulation.close
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
