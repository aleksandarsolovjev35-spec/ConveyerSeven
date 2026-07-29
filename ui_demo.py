from __future__ import annotations

import json
import random
import tempfile
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace
import time

import cv2
import numpy as np
import webview

from core.rule_report import build_rule_report_rows
from vision.ui import LiveMonitor


def demo_rule_rows():
    """Демонстрационные правила в продакшн-формате отчёта."""
    return build_rule_report_rows([
        SimpleNamespace(
            rule_name="part_presence",
            triggered=False,
            details={
                "empty_tray": False,
                "flatness_left": 6,
                "flatness_right": 5,
                "effective_flatness_left": 6,
                "effective_flatness_right": 5,
                "false_positive_max_count_by_role": {
                    "INPUT_LEFT": 2, "INPUT_RIGHT": 2,
                },
            },
        ),
        SimpleNamespace(
            rule_name="long_omission",
            triggered=True,
            details={"per_role": {
                "SPIDER_LEFT": {
                    "triggered": True, "reason": None,
                    "allowed_thickness_px": 20.0, "excess_pixels": 340,
                    "largest_component_pixels": 340,
                    "excess_component_min_px": 3,
                    "max_excess_depth_px": 18.0,
                    "top_line_actual_max_residual_px": 1.2,
                    "top_line_max_residual_px": 3.0,
                    "found": 5, "expected_count": 5,
                },
                "SPIDER_RIGHT": {
                    "triggered": False, "reason": None,
                    "allowed_thickness_px": 20.0, "excess_pixels": 0,
                    "excess_component_min_px": 3,
                    "max_excess_depth_px": 0.0,
                    "top_line_actual_max_residual_px": 0.4,
                    "top_line_max_residual_px": 3.0,
                    "found": 5, "expected_count": 5,
                },
            }},
        ),
        SimpleNamespace(
            rule_name="top_platform",
            triggered=False,
            details={"per_role": {"TOP": {
                "triggered": False, "reason": None,
                "placement": "centered", "shift_distance_px": 1.8,
                "angle_deg": 0.4, "rect_width_px": 262,
                "rect_height_px": 121, "found": 1, "expected_count": 1,
            }}},
        ),
    ])

ROLES = (
    "INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
    "SPIDER_IN", "SPIDER_OUT", "TOP",
)

# Keep the demo clock aligned with the production cycle.  These are elapsed
# times, not ``sleep`` durations: a delayed UI/webview must never make the
# simulator run faster (or execute several steps in a burst).
STEP_SECONDS = 2.0
REVIEW_SECONDS = 5.0
TICK_SECONDS = 0.05

COLORS = (
    (55, 75, 95), (65, 95, 70), (90, 65, 75), (80, 80, 45),
    (70, 55, 100), (45, 90, 95), (95, 70, 45),
)


class DemoArchive:
    """Small file-backed archive so demo history cards are actually usable."""

    def __init__(self, frames):
        self._tempdir = tempfile.TemporaryDirectory(prefix="conveyer-seven-demo-")
        self.root = Path(self._tempdir.name)
        self.records = {}
        for role, frame in frames.items():
            path = self.root / f"{role}.jpg"
            cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 82])

    def add(self, part_id, category, decision):
        part_id = int(part_id)
        record = {
            "category": category,
            "decision": decision,
            "time": time.time(),
        }
        self.records[part_id] = record
        folder = self.root / str(part_id)
        folder.mkdir(exist_ok=True)
        (folder / "meta.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )

    def get_part_info(self, part_id):
        part_id = int(part_id)
        if part_id not in self.records:
            return None
        return {"folder": str(self.root / str(part_id))}

    def get_part_images(self, part_id):
        if int(part_id) not in self.records:
            return {}
        return {
            role: {
                "raw": str(self.root / f"{role}.jpg"),
                "debug": str(self.root / f"{role}.jpg"),
            }
            for role in ROLES
        }

    def metadata(self, part_id):
        return dict(self.records.get(int(part_id), {}))

    def close(self):
        self._tempdir.cleanup()


class UiDemo:
    def __init__(self):
        self.monitor = LiveMonitor(
            window_name="МОНИТОР ЛИНИИ — ДЕМО",
            fullscreen=False,
        )
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.state = "IDLE"
        self.exit_requested = False
        self.step = 0
        self.parts = []
        self.recent_parts = deque(maxlen=10)
        self.next_part = 1
        self.good = 0
        self.bad = 0
        self.cleanup = 0
        self.empty = 0
        self.dist1 = 0
        self.dist2 = 0
        self.dist1_state = "IDLE"
        self.dist2_state = "IDLE"
        self.dist_target = "BAD"
        self.dist_action = "ДЕМО-РЕЖИМ"
        self.jog_active = False
        self.jog_busy = False
        self.jog_direction = None
        self.selected_analysis = False
        self.selected_role = None
        self.process = self._process("IDLE", "Демо-режим: оборудование отключено")
        self.diagnostics = {
            "status": "NOT_RUN", "kind": None,
            "message": "Синтетические данные демо-режима",
            "cameras": [], "models": [], "rules": [],
            "updated_at": None,
        }
        self.frames = self._make_frames()
        self.archive = DemoArchive(self.frames)
        self._next_step_at = 0.0
        self._review_until = 0.0
        self._paused_step_remaining = None
        self._paused_review_remaining = None
        self.thread = threading.Thread(target=self._loop, daemon=True)

    @staticmethod
    def _process(phase, label, positions=None, part_id=None):
        return {
            "phase": phase, "label": label, "step": 0,
            "part_id": part_id, "positions": list(positions or []),
            "conveyor": {}, "revision": int(time.time() * 1000),
            "updated_at": time.time(),
        }

    def _make_frames(self):
        frames = {}
        for role, color in zip(ROLES, COLORS, strict=True):
            frame = np.full((720, 1280, 3), color, dtype=np.uint8)
            cv2.putText(frame, "UI ONLY", (55, 95), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (230, 235, 240), 3)
            cv2.putText(frame, role, (55, 175), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
            cv2.putText(frame, "NO COM / NO CAMERAS / NO MODELS / NO MOTION", (55, 650), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (220, 220, 220), 2)
            frames[role] = frame
        return frames

    def controls(self):
        prestart = self.state in {"IDLE", "STOPPED"} and not self.parts and not self.jog_busy and not self.selected_analysis
        return {
            "start": prestart,
            "stop": self.state in {"RUNNING", "PAUSED"},
            "pause": self.state == "RUNNING",
            "resume": self.state == "PAUSED",
            "exit": not self.jog_busy,
            "jog_hold": self.jog_active and (prestart or self.state == "PAUSED"),
            "selected_model_analysis": prestart,
            "selected_model_release": self.selected_analysis,
            "distributor_diagnostic": prestart,
            "camera_diagnostic": prestart,
            "vision_rule_diagnostic": prestart,
        }

    def line_status(self):
        # Демо-строки правил собираются реальным построителем отчёта,
        # поэтому витрина совпадает с продакшн-форматом сводки.
        prestart = self.controls()["distributor_diagnostic"]
        cycle_analysis = self.state in {"RUNNING", "STOPPING"}
        selected_report = self.diagnostics.get("kind") == "SELECTED_MODEL"
        if cycle_analysis:
            # Как и в рабочем цикле, панель следует за камерой, которую
            # оператор выбрал в UI: ВХОД для входных камер, КОНТРОЛЬ +4
            # для остальных.
            role = self.monitor.server.active_camera_role
            group = "INPUT" if role in ("INPUT_LEFT", "INPUT_RIGHT") else "SPIDER"
            stage_label = "ВХОД" if group == "INPUT" else "КОНТРОЛЬ +4"
            frame_analysis = {
                "available": True,
                "kind": "CYCLE",
                "active": True,
                "title": "АНАЛИЗ ТЕКУЩЕГО КАДРА",
                "role": role,
                "group": group,
                "stage": stage_label,
                "part_id": self.process.get("part_id"),
                "message": f"Демонстрационный результат: {stage_label}",
                "models": [
                    {
                        "role": role or "TOP",
                        "model": "demo/model.pt",
                        "ok": True,
                        "elapsed_ms": 12,
                        "detections": 2,
                    }
                ],
                "rules": demo_rule_rows(),
                "updated_at": time.time(),
            }
        elif selected_report:
            frame_analysis = {
                "available": True,
                "kind": "SELECTED",
                "active": self.selected_analysis,
                "title": "АНАЛИЗ ВЫБРАННОГО КАДРА",
                "role": self.diagnostics.get("selected_role") or self.selected_role,
                "message": self.diagnostics.get("message"),
                "models": list(self.diagnostics.get("models", [])),
                "rules": list(self.diagnostics.get("rules", [])),
            }
        else:
            frame_analysis = {
                "available": False,
                "kind": None,
                "active": False,
                "models": [],
                "rules": [],
            }
        return {
            "state": self.state,
            "exit_requested": self.exit_requested,
            "fault_reason": None,
            "step": self.step,
            "in_line": len(self.parts),
            "line_parts": [dict(item) for item in self.parts],
            "total": self.next_part - 1,
            "good": self.good,
            "rejected": self.bad,
            "cleanup": self.cleanup,
            "empty": self.empty,
            "dist1_position": self.dist1,
            "dist1_max": 340,
            "dist1_state": self.dist1_state,
            "dist2_position": self.dist2,
            "dist2_max": 340,
            "dist2_state": self.dist2_state,
            "dist2_target": self.dist_target,
            "last_distributor_action": self.dist_action,
            "axis_position": self.dist1,
            "axis_max": 340,
            "distributor_state": self.dist1_state,
            "process": dict(self.process),
            "diagnostic_allowed": prestart,
            "diagnostic_busy": False,
            "controls": self.controls(),
            "selected_analysis": {
                "active": self.selected_analysis,
                "role": self.selected_role,
            },
            "frame_analysis": frame_analysis,
            "diagnostics": dict(self.diagnostics),
            "jog": {
                "active": self.jog_active,
                "can_enter": self.state in {"IDLE", "STOPPED", "PAUSED"},
                "busy": self.jog_busy,
                "hold_steps": 1000000,
                "last_action": "ДЕМО-РЕЖИМ",
                "direction": self.jog_direction,
                "error": None,
                "live_fps": 30.0 if self.jog_active and not self.selected_analysis else 0.0,
            },
        }

    def publish(self, frames=False):
        self.process["step"] = self.step
        self.monitor.update(
            frames=self.frames if frames else None,
            vision_results={},
            rule_results=[],
            line_status=self.line_status(),
            # The real cycle keeps the last ten completed parts.  Passing an
            # empty list here made the demo history stay permanently blank,
            # hiding whether sorting had actually happened.
            recent_parts=list(self.recent_parts),
        )

    def start(self):
        with self.lock:
            if not self.controls()["start"]:
                return False
            self.jog_active = False
            self.state = "RUNNING"
            # Start a fresh monotonic schedule.  Using a deadline instead of
            # resetting a counter in the worker prevents a slow status/render
            # callback from causing an immediate double step.
            self._next_step_at = time.monotonic() + STEP_SECONDS
            self._review_until = 0.0
            self._paused_step_remaining = None
            self._paused_review_remaining = None
            if self.diagnostics.get("kind") == "SELECTED_MODEL":
                self.diagnostics = {
                    "status": "NOT_RUN",
                    "kind": None,
                    "message": "Анализ кадра ещё не выполнялся",
                    "cameras": [],
                    "models": [],
                    "rules": [],
                    "updated_at": None,
                }
            self.process = self._process("READY", "Демонстрационный цикл запущен")
            self.publish()
        return True

    def stop(self):
        with self.lock:
            if self.state not in {"RUNNING", "PAUSED"}:
                return False
            self.state = "STOPPING"
            # Production exits REVIEW immediately on STOP and drains only
            # already present parts.  Do the same instead of keeping the UI
            # stuck on a five-second review countdown.
            self._review_until = 0.0
            self.process = self._process("DRAINING", "Опорожнение демонстрационной линии")
            self.publish()
        return True

    def pause(self):
        with self.lock:
            if self.state != "RUNNING":
                return False
            now = time.monotonic()
            self._paused_step_remaining = max(0.0, self._next_step_at - now)
            self._paused_review_remaining = (
                max(0.0, self._review_until - now)
                if self._review_until else None
            )
            self._next_step_at = 0.0
            self._review_until = 0.0
            self.state = "PAUSED"
            self.process = self._process(
                "PAUSED",
                "Пауза после остановки шага: доступна ручная коррекция ленты",
                range(8),
            )
            self.publish()
        return True

    def resume(self):
        with self.lock:
            if self.state != "PAUSED":
                return False
            now = time.monotonic()
            if self._paused_review_remaining is not None:
                self._review_until = now + self._paused_review_remaining
            else:
                self._next_step_at = now + max(
                    STEP_SECONDS,
                    self._paused_step_remaining or 0.0,
                )
            self._paused_step_remaining = None
            self._paused_review_remaining = None
            self.state = "RUNNING"
            self.process = self._process(
                "RESUMED",
                "Работа демонстрационной линии возобновлена",
                range(8),
            )
            self.publish()
        return True

    def exit(self):
        self.stop_event.set()
        self.monitor.close_window()
        return True

    def jog_enter(self):
        with self.lock:
            if (
                self.state not in {"IDLE", "STOPPED", "PAUSED"}
                or self.selected_analysis
            ):
                return False
            self.jog_active = True
            self.publish()
        return True

    def jog_exit(self):
        with self.lock:
            self.jog_active = False
            self.jog_busy = False
            self.jog_direction = None
            self.publish()
        return True

    def jog_start(self, direction):
        with self.lock:
            if not self.controls()["jog_hold"]:
                return False
            self.jog_busy = True
            self.jog_direction = direction
            self.process = self._process("JOG_HOLD", f"Ручное движение {direction}", range(8))
            self.publish()
        return True

    def jog_heartbeat(self, direction):
        return self.jog_busy and self.jog_direction == direction

    def jog_release(self, reason="released"):
        with self.lock:
            self.jog_busy = False
            self.jog_direction = None
            self.process = self._process("JOG_STOPPED", f"Демо-режим: {reason}")
            self.publish()
        return True

    def distributor(self, command):
        targets = {
            "DIST1_HOME": ("dist1", 0), "DIST1_OPEN": ("dist1", 340),
            "DIST2_BAD": ("dist2", 0), "DIST2_CLEANUP": ("dist2", 340),
        }
        if command not in targets or not self.controls()["distributor_diagnostic"]:
            return False
        name, target = targets[command]
        with self.lock:
            # A second diagnostic command must start from the position that
            # was actually reached by the previous command.
            start_position = self.dist1 if name == "dist1" else self.dist2
            if command == "DIST2_BAD":
                self.dist_target = "BAD"
            elif command == "DIST2_CLEANUP":
                self.dist_target = "CLEANUP"
        for value in range(0, 11):
            # Interpolate from a stable origin.  The old code used the
            # current position for every iteration, so it only travelled
            # ~65% of the route and visibly stopped short of the marker.
            position = round(start_position + (target - start_position) * value / 10)
            with self.lock:
                if name == "dist1":
                    self.dist1 = position
                    self.dist1_state = "MOVING" if value < 10 else "IDLE"
                else:
                    self.dist2 = position
                    self.dist2_state = "MOVING" if value < 10 else "IDLE"
                self.dist_action = f"ДЕМО {command}"
                self.publish()
            if value < 10:
                time.sleep(0.03)
        return True

    def check_cameras(self):
        with self.lock:
            if not self.controls()["camera_diagnostic"]:
                return False
            self.diagnostics = {
                "status": "PASSED", "kind": "CAMERAS",
                "message": "Демо-режим: семь синтетических камер",
                "cameras": [{"role": role, "ok": True, "width": 1280, "height": 720} for role in ROLES],
                "models": [], "rules": [], "updated_at": time.time(),
            }
            self.publish(frames=True)
        return True

    def check_vision_rules(self):
        with self.lock:
            if not self.controls()["vision_rule_diagnostic"]:
                return False
            self.diagnostics = {
                "status": "PASSED", "kind": "VISION_RULES",
                "message": "Демонстрационный отчёт моделей и правил",
                "cameras": [{"role": role, "ok": True, "width": 1280, "height": 720, "detections": 1} for role in ROLES],
                "models": [{"role": role, "model": f"demo/{role}.pt", "ok": True, "elapsed_ms": 10, "detections": 1} for role in ROLES],
                "rules": [{"name": "part_presence", "triggered": False, "detail": "PRESENT"}, {"name": "demo_rule", "triggered": False, "detail": None}],
                "updated_at": time.time(),
            }
            self.publish(frames=True)
        return True

    def analyze_selected(self, role):
        with self.lock:
            if role not in ROLES or not self.controls()["selected_model_analysis"]:
                return False
            self.selected_analysis = True
            self.selected_role = role
            self.diagnostics = {
                "status": "PASSED", "kind": "SELECTED_MODEL",
                "message": f"Демонстрационный анализ камеры: {role}",
                "selected_role": role,
                "cameras": [{"role": role, "ok": True, "width": 1280, "height": 720, "detections": 2}],
                "models": [{"role": role, "model": f"demo/{role}.pt", "ok": True, "elapsed_ms": 12, "detections": 2}],
                "rules": [{
                    "name": "demo_rule",
                    "triggered": False,
                    "skipped": False,
                    "detail": "Норма",
                }],
                "updated_at": time.time(),
            }
            self.publish(frames=True)
        return True

    def release_selected(self):
        with self.lock:
            self.selected_analysis = False
            self.selected_role = None
            self.diagnostics = {
                "status": "NOT_RUN",
                "kind": None,
                "message": "Анализ кадра не выполнялся",
                "cameras": [],
                "models": [],
                "rules": [],
                "updated_at": None,
            }
            self.publish()
        return True

    def _loop(self):
        while not self.stop_event.wait(TICK_SECONDS):
            with self.lock:
                if self.state not in {"RUNNING", "STOPPING"}:
                    continue
                now = time.monotonic()

                # REVIEW is a real barrier in ProductionCycle.  Publish the
                # countdown when its displayed second changes; the previous
                # demo updated the object but never published it, leaving the
                # browser on STEP_COMPLETE for the whole five seconds.
                if self._review_until and now < self._review_until:
                    left = int(self._review_until - now + 0.999)
                    if self.state == "STOPPING":
                        self._review_until = 0.0
                    else:
                        active_cam_pos = [
                            p["position"] for p in self.parts
                            if p["position"] in {0, 4}
                        ]
                        label = f"Просмотр результатов анализа: {left} с до следующего шага"
                        if self.process.get("label") != label:
                            self.process = self._process(
                                "ANALYSIS_REVIEW", label, active_cam_pos
                            )
                            self.publish()
                        continue
                self._review_until = 0.0

                # Production stops an already empty line without inventing a
                # dummy conveyor step.  This also makes STOP responsive while
                # the demo is between parts.
                if self.state == "STOPPING" and not self.parts:
                    self.state = "STOPPED"
                    self.process = self._process("STOPPED", "Демонстрационная линия остановлена")
                    self.publish()
                    continue

                if now < self._next_step_at:
                    continue

                # Advance by one deadline, never by a while-loop catch-up.
                # If the process/UI was paused for a while, real hardware has
                # not performed the missed cycles either.
                self._next_step_at = now + STEP_SECONDS
                self.step += 1
                for part in self.parts:
                    part["position"] += 1
                # Обновление категории после SPIDER-контроля (позиция ≥ 4)
                for part in self.parts:
                    if part["position"] >= 4 and part["category"] == "UNKNOWN":
                        part["category"] = part.get("target_category", "GOOD")
                finished = [part for part in self.parts if part["position"] > 7]
                self.parts = [part for part in self.parts if part["position"] <= 7]
                for part in finished:
                    cat = part.get("target_category", "GOOD")
                    if cat == "GOOD":
                        self.good += 1
                        decision = "none"
                    elif cat == "BAD":
                        self.bad += 1
                        decision = "demo_defect"
                    else:
                        self.cleanup += 1
                        decision = "cleanup"
                    self.archive.add(part["id"], cat, decision)
                    self.recent_parts.append({
                        "id": part["id"],
                        "category": cat,
                        "decision": decision,
                        "time": time.time(),
                    })

                # Симуляция распределителей: подсветка при сортировке.
                parts_at_reject = [p for p in self.parts if p["position"] == 7]
                if parts_at_reject:
                    part = parts_at_reject[0]
                    if part["category"] == "BAD":
                        self.dist1, self.dist2, self.dist_target = 340, 0, "BAD"
                        self.dist_action = f"СБРОС #{part['id']} → БРАК"
                    elif part["category"] == "CLEANUP":
                        self.dist1, self.dist2, self.dist_target = 340, 340, "CLEANUP"
                        self.dist_action = f"СБРОС #{part['id']} → ОЧИСТКА"
                    else:
                        self.dist1 = self.dist2 = 0
                        self.dist_target = "BAD"
                        self.dist_action = f"ПРОХОД #{part['id']}"
                    self.dist1_state = self.dist2_state = "IDLE"
                else:
                    self.dist1 = self.dist2 = 0
                    self.dist1_state = self.dist2_state = "IDLE"
                    self.dist_target = "BAD"
                    self.dist_action = "—"

                if self.state == "RUNNING" and self.step % 2 == 1:
                    target = random.choices(
                        ["GOOD", "BAD", "CLEANUP"], weights=[40, 35, 25]
                    )[0]
                    self.parts.append({
                        "id": self.next_part, "position": 0,
                        "category": "UNKNOWN", "target_category": target,
                    })
                    self.next_part += 1
                drained = self.state == "STOPPING" and not self.parts
                if drained:
                    # Do not leave a completed drain behind a fake review
                    # phase. The final status must visibly be STOPPED.
                    self.state = "STOPPED"
                    self._review_until = 0.0
                    self.process = self._process(
                        "STOPPED", "Демонстрационная линия остановлена"
                    )
                else:
                    self._review_until = now + REVIEW_SECONDS
                    # STEP_COMPLETE is a stable state, not an active camera
                    # or motion phase. Highlighting all cells here made the
                    # whole line look permanently selected.
                    self.process = self._process(
                        "STEP_COMPLETE", "Демонстрационный шаг завершён"
                    )
                self.publish(frames=True)


def main():
    demo = UiDemo()
    monitor = demo.monitor
    # History cards must open the same archive endpoint as production, even
    # though demo images are synthetic and stored in a temporary directory.
    monitor.server.archive = demo.archive
    monitor.start_callback = demo.start
    monitor.stop_callback = demo.stop
    monitor.pause_callback = demo.pause
    monitor.resume_callback = demo.resume
    monitor.exit_callback = demo.exit
    monitor.distributor_diagnostic_callback = demo.distributor
    monitor.camera_diagnostic_callback = demo.check_cameras
    monitor.vision_rule_diagnostic_callback = demo.check_vision_rules
    monitor.selected_model_analysis_callback = demo.analyze_selected
    monitor.selected_model_release_callback = demo.release_selected
    monitor.jog_enter_callback = demo.jog_enter
    monitor.jog_exit_callback = demo.jog_exit
    monitor.jog_hold_start_callback = demo.jog_start
    monitor.jog_hold_heartbeat_callback = demo.jog_heartbeat
    monitor.jog_hold_release_callback = demo.jog_release

    monitor.server.start_server(host=monitor.host, port=monitor.port)
    for key, _ in monitor.server.BOOT_STEPS:
        monitor.boot_step_done(key)
    demo.publish(frames=True)
    monitor.boot_complete()
    demo.thread.start()

    try:
        window = webview.create_window(
            title=monitor.window_name,
            url=f"http://{monitor.host}:{monitor.port}/",
            width=1280,
            height=720,
            background_color="#0b0f13",
        )
        monitor._webview_window = window
        webview.start()
    finally:
        demo.stop_event.set()
        demo.thread.join(2.0)
        monitor.stop_server()
        demo.archive.close()


if __name__ == "__main__":
    main()
