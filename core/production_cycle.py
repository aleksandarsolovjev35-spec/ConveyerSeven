# core/production_cycle.py

import time
import threading
import traceback
from collections import deque

from core.state_machine import StateMachine, State
from core.cycle import PauseLogic, JogMode, InspectionStage, MonitorBuilders
from domain.defect_rules import InputPartPresenceRule
from inspection.consensus import (
    INSPECTION_RUNS,
    combine_rule_results,
    summarize_model_health,
)
from domain.part import (
    Part,
    CATEGORY_GOOD,
    CATEGORY_BAD,
    CATEGORY_CLEANUP,
    CATEGORY_UNKNOWN,
)

RECENT_PARTS_LIMIT = 10
DRAIN_TIMEOUT = 120.0

LIVE_TARGET_FPS          = 30.0
JOG_FRAME_INTERVAL       = 1.0 / LIVE_TARGET_FPS
JOG_AUX_BATCH_INTERVAL   = 0.20   # refresh all auxiliary previews as one batch
JOG_THREAD_JOIN_TIMEOUT  = 6.0


class ProductionCycle:
    """
    Оркестратор производственной линии.
    """

    OFFSET_INPUT  = 0
    OFFSET_SPIDER = 4
    OFFSET_REJECT = 7

    JOG_ALLOWED_STATES = ("IDLE", "STOPPED")
    # Ограниченная коррекция ленты доступна только в паузе внутри цикла.
    NUDGE_ALLOWED_STATES = ("PAUSED",)

    def __init__(
        self,
        conveyor,
        cameras,
        inspector,
        distributor,
        monitor=None,
        archive=None,
        jog=None,
    ):
        self.conveyor     = conveyor
        self.cameras      = cameras
        self.inspector    = inspector
        self.distributor  = distributor
        self.monitor      = monitor
        self.archive      = archive
        self.jog          = jog

        self.distributor.on_state_changed = self._refresh_monitor

        self.sm = StateMachine(on_transition=self._on_state_change)

        self.parts: list = []
        self.part_counter = 0
        self.current_step = 0

        self.good_count    = 0
        self.bad_count     = 0
        self.cleanup_count = 0
        self.empty_count   = 0   # счётчик пустых лотков

        self.recent_parts = deque(maxlen=RECENT_PARTS_LIMIT)

        self.force_all_bad = False
        self._pending_drop = None

        self._last_vision_results: dict = {}
        self._last_rule_results: list = []
        self._last_model_health: list = []
        self._frame_analysis_rule_results: list = []
        self._frame_analysis_updated_at = None

        self._drain_start_time: float = 0
        self._consecutive_errors: int = 0
        self._fault_reason = None
        self._operation_lock = threading.Lock()
        self._cancel_motion = threading.Event()
        self.distributor.cancel_check = self._cancel_motion.is_set
        self._process_revision = 0
        self._diagnostics = {
            "status": "NOT_RUN",
            "kind": None,
            "message": "Проверки ещё не запускались",
            "cameras": [],
            "models": [],
            "rules": [],
            "updated_at": None,
        }
        self._process = {
            "phase": "IDLE",
            "label": "Система готова к пуску",
            "step": 0,
            "part_id": None,
            "positions": [],
            "conveyor": {},
            "revision": 0,
            "updated_at": time.time(),
        }

        # ── JOG ────────────────────────────────────────────────
        self.jog_active: bool = False
        self._jog_lock = threading.Lock()
        self._jog_thread = None
        self._jog_aux_thread = None
        self._jog_stop_event = threading.Event()
        self._jog_frame_error = None
        self._jog_frame_times = deque(maxlen=240)
        self._live_capture_pause = threading.Event()
        self._selected_analysis_active = False
        self._selected_analysis_role = None
        self._shutdown = False

        # ── Пауза внутри цикла ─────────────────────────────────
        # Запрос ставится в любой момент, но применяется только на границе
        # шага: физический шаг никогда не прерывается на середине.
        self._pause_requested = threading.Event()
        self._pause_frame_active = False

    # ─── Process telemetry ───────────────────────────────────────

    def _set_process(
        self,
        phase: str,
        label: str,
        *,
        part_id=None,
        positions=None,
        conveyor_status=None,
    ):
        self._process_revision += 1
        self._process = {
            "phase": phase,
            "label": label,
            "step": self.current_step,
            "part_id": part_id,
            "positions": list(positions or []),
            "conveyor": dict(conveyor_status or {}),
            "revision": self._process_revision,
            "updated_at": time.time(),
        }
        self._refresh_monitor()

    def _on_conveyor_progress(self, status: dict):
        current = self._process
        self._set_process(
            "CONVEYOR_MOVING",
            "Лента перемещает детали на следующую позицию",
            part_id=current.get("part_id"),
            positions=range(self.OFFSET_REJECT + 1),
            conveyor_status=status,
        )

    # ─── Public API ──────────────────────────────────────────────

    def request_start(self):
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if self._selected_analysis_active:
                return False
            if self._jog_frame_error:
                return False
            if self.jog is not None and self.jog.status.get("error"):
                return False
            if self.jog_active:
                print("[JOG] auto-exit on START")
                self.exit_jog()
            # The frame thread may fail while START waits for JOG shutdown.
            if self._jog_frame_error:
                return False
            if self.jog is not None and self.jog.status.get("error"):
                return False
            if self.state not in ("IDLE", "STOPPED"):
                return False
            self._cancel_motion.clear()
            self._set_process(
                "START_POSITIONING",
                "Возврат распределителя в рабочее положение",
            )
            try:
                self.distributor.park_production()
            except Exception as exc:
                self._handle_fault(f"Не удалось установить распределитель в рабочее положение: {exc}")
                raise
            accepted = self.sm.request_start()
            if accepted:
                self._drain_start_time = 0
                self._consecutive_errors = 0
                self._fault_reason = None
                self._last_model_health = []
                self._frame_analysis_rule_results = []
                self._frame_analysis_updated_at = None
                if self._diagnostics.get("kind") == "SELECTED_MODEL":
                    self._diagnostics = {
                        "status": "NOT_RUN",
                        "kind": None,
                        "message": "Анализ трёх кадров ещё не выполнялся",
                        "cameras": [],
                        "models": [],
                        "rules": [],
                        "updated_at": None,
                    }
                self._set_process("READY", "Цикл запущен")
            return accepted
        finally:
            self._operation_lock.release()

    def request_stop(self):
        # STOP из паузы должен снять паузу, иначе дренаж линии не начнётся.
        self._pause_requested.clear()
        return self.sm.request_stop()

    def request_exit(self):
        self._pause_requested.clear()
        return self.sm.request_exit()

    # ─── Пауза внутри производственного цикла ────────────────────

    def request_pause(self) -> bool:
        """Запросить остановку на ближайшей границе шага."""
        if self.state != "RUNNING" or self.exit_requested:
            return False
        if self._pause_requested.is_set():
            return True
        self._pause_requested.set()
        self._set_process(
            "PAUSE_REQUESTED",
            "Пауза будет применена после завершения текущего шага",
            positions=range(self.OFFSET_REJECT + 1),
        )
        return True

    def request_resume(self) -> bool:
        """Вернуть линию в работу после паузы и коррекции."""
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if self.state != "PAUSED" or self.exit_requested:
                return False
            if self.jog is not None and self.jog.status.get("error"):
                return False
            offset = self.jog.nudge_offset if self.jog is not None else 0
            self._pause_requested.clear()
            accepted = self.sm.request_resume()
            if not accepted:
                return False
            self._stop_pause_frame_loop()
            print(f"[PAUSE] resume; накопленная коррекция {offset:+d} микрошагов")
            self._set_process(
                "RESUMED",
                (
                    "Работа возобновлена; коррекция "
                    f"{offset:+d} микрошагов внутри ячейки"
                ),
                positions=range(self.OFFSET_REJECT + 1),
            )
            return True
        finally:
            self._operation_lock.release()

    def nudge_belt(self, direction: str, steps: int = None) -> bool:
        """Ограниченная ручная коррекция ленты в паузе."""
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if (
                self.jog is None
                or self.state not in self.NUDGE_ALLOWED_STATES
                or self.exit_requested
                or self._selected_analysis_active
                or self.jog_active
            ):
                return False
            if self.jog.status.get("error") or self._jog_frame_error:
                return False
            if self._cancel_motion.is_set():
                return False
            try:
                applied = self.jog.nudge(direction, steps)
            except ValueError:
                # Некорректный запрос оператора не является отказом железа.
                return False
            except Exception as exc:
                self._handle_fault(f"Ошибка коррекции ленты: {exc}")
                return False

            status = self.jog.status
            if applied == 0:
                self._set_process(
                    "PAUSE_NUDGE_LIMIT",
                    (
                        "Достигнут предел коррекции "
                        f"±{status['nudge_limit_steps']} микрошагов"
                    ),
                    positions=range(self.OFFSET_REJECT + 1),
                )
            else:
                self._set_process(
                    "PAUSE_NUDGE",
                    (
                        f"Коррекция ленты {applied:+d}; "
                        f"сумма {status['nudge_offset']:+d} микрошагов"
                    ),
                    positions=range(self.OFFSET_REJECT + 1),
                )
            return True
        finally:
            self._operation_lock.release()

    def distributor_diagnostic(self, command: str) -> bool:
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if (
                self.state not in ("IDLE", "STOPPED")
                or self.parts
                or self._selected_analysis_active
            ):
                return False
            if self.jog is not None and self.jog.busy:
                return False
            self._set_process(
                "DISTRIBUTOR_DIAGNOSTIC",
                f"Проверка распределителя: {command}",
            )
            if command == "DIST1_HOME":
                self.distributor.diagnostic_gate("HOME")
            elif command == "DIST1_OPEN":
                self.distributor.diagnostic_gate("OPEN")
            elif command == "DIST2_BAD":
                self.distributor.diagnostic_route(CATEGORY_BAD)
            elif command == "DIST2_CLEANUP":
                self.distributor.diagnostic_route(CATEGORY_CLEANUP)
            else:
                raise ValueError(f"Unknown distributor diagnostic: {command}")
            self._set_process(
                "DIAGNOSTIC_DONE",
                f"Положение распределителя подтверждено: {command}",
            )
            return True
        except Exception as exc:
            self._handle_fault(f"Ошибка проверки распределителя: {exc}")
            raise
        finally:
            self._operation_lock.release()

    def diagnostic_check_cameras(self) -> bool:
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if not self._prestart_diagnostic_allowed():
                return False
            self._set_diagnostic_running("CAMERAS", "Проверка семи камер")
            self._set_process("CAMERA_DIAGNOSTIC", "Проверка семи камер")
            frames = self.cameras.capture_all()
            camera_rows = []
            for role, frame in frames.items():
                height, width = frame.shape[:2]
                camera_rows.append({
                    "role": role,
                    "ok": True,
                    "width": int(width),
                    "height": int(height),
                })
            self._last_vision_results = {}
            self._last_rule_results = []
            self._diagnostics = {
                "status": "PASSED",
                "kind": "CAMERAS",
                "message": f"Камеры: {len(camera_rows)}/{len(camera_rows)} OK",
                "cameras": camera_rows,
                "models": [],
                "rules": [],
                "updated_at": time.time(),
            }
            self._set_process("DIAGNOSTIC_DONE", "Семь камер проверены")
            self._refresh_monitor(frames)
            return True
        except Exception as exc:
            self._set_diagnostic_error("CAMERAS", exc)
            self._handle_fault(f"Ошибка проверки камер: {exc}")
            raise
        finally:
            self._operation_lock.release()

    def diagnostic_check_vision_rules(self) -> bool:
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if not self._prestart_diagnostic_allowed():
                return False
            self._set_diagnostic_running(
                "VISION_RULES",
                "Камеры → модели → правила дефектов",
            )
            self._set_process(
                "VISION_RULE_DIAGNOSTIC",
                "Запуск всех моделей и правил дефектов без движения линии",
                positions=[self.OFFSET_INPUT, self.OFFSET_SPIDER],
            )
            frames = self.cameras.capture_all()
            vision_results = self.inspector.vision.process_all(frames)
            presence_rule = InputPartPresenceRule(
                self.inspector.decision.thresholds
            )
            if not presence_rule.enabled:
                raise RuntimeError("part_presence rule is disabled")
            presence_result = presence_rule.check(vision_results)
            rule_results = [presence_result]
            rule_results.extend(
                self.inspector.decision.evaluate_all_detailed(
                    vision_results,
                    frames=frames,
                )
            )
            model_rows = [dict(item) for item in self.inspector.vision.last_health]
            rule_rows = [
                self._rule_report_row(result)
                for result in rule_results
            ]
            camera_rows = []
            for role, frame in frames.items():
                height, width = frame.shape[:2]
                camera_rows.append({
                    "role": role,
                    "ok": True,
                    "width": int(width),
                    "height": int(height),
                    "detections": len(vision_results.get(role, [])),
                })
            self._last_vision_results = vision_results
            self._last_rule_results = rule_results
            triggered = sum(row["triggered"] for row in rule_rows)
            self._diagnostics = {
                "status": "PASSED",
                "kind": "VISION_RULES",
                "message": (
                    f"Модели: {len(model_rows)} исправны; "
                    f"правил: {len(rule_rows)}, сработало: {triggered}"
                ),
                "cameras": camera_rows,
                "models": model_rows,
                "rules": rule_rows,
                "updated_at": time.time(),
            }
            self._set_process(
                "DIAGNOSTIC_DONE",
                "Модели и правила дефектов выполнены",
            )
            self._refresh_monitor(frames)
            return True
        except Exception as exc:
            self._set_diagnostic_error("VISION_RULES", exc)
            self._handle_fault(f"Ошибка проверки моделей и правил: {exc}")
            raise
        finally:
            self._operation_lock.release()

    def _prestart_diagnostic_allowed(self) -> bool:
        return (
            self.state in ("IDLE", "STOPPED")
            and not self.parts
            and not self.exit_requested
            and not self._cancel_motion.is_set()
            and not self._selected_analysis_active
            and not (self.jog is not None and self.jog.busy)
        )

    def _set_diagnostic_running(self, kind: str, message: str):
        self._diagnostics = {
            "status": "RUNNING",
            "kind": kind,
            "message": message,
            "cameras": [],
            "models": [],
            "rules": [],
            "updated_at": time.time(),
        }
        self._refresh_monitor()

    def _set_diagnostic_error(self, kind: str, exc: Exception):
        self._diagnostics = {
            "status": "ERROR",
            "kind": kind,
            "message": f"{type(exc).__name__}: {exc}",
            "cameras": [],
            "models": [],
            "rules": [],
            "updated_at": time.time(),
        }
        self._refresh_monitor()

    @staticmethod
    def _rule_report_row(result) -> dict:
        details = getattr(result, "details", {}) or {}
        detail = details.get("reason") or details.get("status")
        detail_lines = []
        skipped = False
        per_role = details.get("per_role")
        if isinstance(per_role, dict) and per_role:
            skipped_rows = [
                (role, role_details)
                for role, role_details in per_role.items()
                if isinstance(role_details, dict) and role_details.get("skipped")
            ]
            if len(skipped_rows) == len(per_role):
                skipped = True
                reasons = [
                    f"{role}: {row.get('reason', 'нет измерения')}"
                    for role, row in skipped_rows
                ]
                detail = "Не выполнено: " + "; ".join(reasons)
            elif skipped_rows:
                detail = "Частично выполнено: " + "; ".join(
                    f"{role}: {row.get('reason', 'нет измерения')}"
                    for role, row in skipped_rows
                )
        rule_name = getattr(result, "rule_name", "")
        if rule_name == "window_geometry" and isinstance(per_role, dict):
            for role, role_details in per_role.items():
                if not isinstance(role_details, dict):
                    continue
                reason = role_details.get("reason")
                if reason:
                    detail_lines.append(
                        f"{role}: найдено {int(role_details.get('found') or 0)}/"
                        f"{int(role_details.get('expected_count') or 0)}"
                    )
                    continue
                top_limits = role_details.get("top_limits_px") or [0, 0]
                bottom_limits = role_details.get("bottom_limits_px") or [0, 0]
                detail_lines.append(
                    f"{role}: T {float(top_limits[0]):g}…"
                    f"{float(top_limits[1]):g} px; B "
                    f"{float(bottom_limits[0]):g}…"
                    f"{float(bottom_limits[1]):g} px"
                )
                ignored = int(role_details.get("ignored") or 0)
                if ignored:
                    detail_lines.append(
                        f"{role}: лишних detections показано серым: {ignored}"
                    )
                for item in role_details.get("items") or []:
                    index = int(item.get("index") or 0)
                    if not item.get("valid"):
                        detail_lines.append(
                            f"{role} #{index}: нет измерения T/B"
                        )
                        continue
                    suffix = []
                    if item.get("top_fail"):
                        suffix.append("T вне допуска")
                    if item.get("bottom_fail"):
                        suffix.append("B вне допуска")
                    text = (
                        f"{role} #{index}: "
                        f"T={float(item.get('top_px') or 0):.1f} px; "
                        f"B={float(item.get('bottom_px') or 0):.1f} px"
                    )
                    if suffix:
                        text += "; " + ", ".join(suffix)
                    detail_lines.append(text)
            if detail_lines:
                detail = "; ".join(detail_lines)

        if rule_name == "contacts_long" and isinstance(per_role, dict):
            for role, role_details in per_role.items():
                if not isinstance(role_details, dict):
                    continue
                reason = role_details.get("reason")
                if reason and str(reason).startswith("wrong_count"):
                    detail_lines.append(
                        f"{role}: найдено {int(role_details.get('found') or 0)}/5"
                    )
                    continue
                if reason == "invalid_contact_masks":
                    indices = ", ".join(
                        f"#{index}"
                        for index in role_details.get("invalid_mask_indices", [])
                    )
                    detail_lines.append(
                        f"{role}: нет segmentation mask контакта: {indices}"
                    )
                    continue
                if reason == "no_scale":
                    detail_lines.append(
                        f"{role}: невозможно вычислить scale по шагу 1.25 mm"
                    )
                    continue

                tolerance = float(role_details.get("line_tolerance_px") or 0)
                inscribe = role_details.get("inscribe_check") or {}
                scale = inscribe.get("scale_px_per_mm")
                detail_lines.append(
                    f"{role}: допуск линий {tolerance:.1f} px; "
                    f"rectangle {float(role_details.get('rect_width_mm') or 0):g}x"
                    f"{float(role_details.get('rect_height_mm') or 0):g} mm; "
                    f"scale {scale if scale is not None else '—'} px/mm"
                )
                ignored = int(role_details.get("ignored") or 0)
                if ignored:
                    detail_lines.append(
                        f"{role}: лишних contacts показано серым: {ignored}"
                    )
                omission = role_details.get("omission_tilt_check") or {}
                if omission.get("status") == "error":
                    detail_lines.append(
                        f"{role}: нет valid reference omission-long"
                    )
                else:
                    detail_lines.append(
                        f"{role}: omission tilt "
                        f"{float(omission.get('distance_trend_ratio') or 0):.3f}/"
                        f"предел {float(role_details.get('omission_tilt_ratio_max') or 0):.3f}"
                    )
                for item in role_details.get("items") or []:
                    index = int(item.get("index") or 0)
                    distance = item.get("omission_distance_px")
                    distance_text = (
                        f"{float(distance):.1f} px"
                        if distance is not None else "—"
                    )
                    text = (
                        f"{role} #{index}: верх "
                        f"{float(item.get('dev_top_px') or 0):.1f}/{tolerance:.1f} px; "
                        f"низ {float(item.get('dev_bottom_px') or 0):.1f}/{tolerance:.1f} px; "
                        f"rect {'OK' if item.get('rect_fits') else 'FAIL'}; "
                        f"d omission {distance_text}"
                    )
                    detail_lines.append(text)
            if detail_lines:
                detail = "; ".join(detail_lines)

        if rule_name == "contacts_short" and isinstance(per_role, dict):
            for role, role_details in per_role.items():
                if not isinstance(role_details, dict):
                    continue
                reason = role_details.get("reason")
                if reason and str(reason).startswith("wrong_count"):
                    detail_lines.append(
                        f"{role}: найдено {int(role_details.get('found') or 0)}/2; "
                        f"area min "
                        f"{float(role_details.get('area_absolute_min_px2') or 0):g} px²"
                    )
                    invalid_indices = role_details.get(
                        "invalid_mask_indices", []
                    )
                    if invalid_indices:
                        detail_lines.append(
                            f"{role}: нет segmentation mask контакта: "
                            + ", ".join(
                                f"#{index}" for index in invalid_indices
                            )
                        )
                    continue
                if reason == "invalid_contact_masks":
                    indices = ", ".join(
                        f"#{index}"
                        for index in role_details.get("invalid_mask_indices", [])
                    )
                    detail_lines.append(
                        f"{role}: нет segmentation mask контакта: {indices}"
                    )
                    continue
                tolerance = float(role_details.get("tolerance") or 0)
                detail_lines.append(
                    f"{role}: area min "
                    f"{float(role_details.get('area_absolute_min_px2') or 0):g} px²; "
                    f"Δtop {float(role_details.get('delta_top') or 0):.1f}/"
                    f"{tolerance:.1f} px; Δbottom "
                    f"{float(role_details.get('delta_bottom') or 0):.1f}/"
                    f"{tolerance:.1f} px; Δheight "
                    f"{float(role_details.get('delta_height') or 0):.1f}/"
                    f"{tolerance:.1f} px"
                )
                inscribe = role_details.get("inscribe_check") or {}
                scale = inscribe.get("scale_px_per_mm")
                detail_lines.append(
                    f"{role}: rectangle "
                    f"{float(role_details.get('rect_width_mm') or 0):g}x"
                    f"{float(role_details.get('rect_height_mm') or 0):g} mm; "
                    f"scale {scale if scale is not None else '—'} px/mm"
                )
                ignored = int(role_details.get("ignored") or 0)
                if ignored:
                    detail_lines.append(
                        f"{role}: лишних contacts показано серым: {ignored}"
                    )
                omission = role_details.get("omission_tilt_check") or {}
                if omission.get("status") == "error":
                    detail_lines.append(
                        f"{role}: нет valid reference omission-short"
                    )
                else:
                    detail_lines.append(
                        f"{role}: omission tilt "
                        f"{float(omission.get('distance_delta_ratio') or 0):.3f}/"
                        f"предел {float(role_details.get('omission_tilt_ratio_max') or 0):.3f}"
                    )
                for item in role_details.get("items") or []:
                    distance = item.get("omission_distance_px")
                    distance_text = (
                        f"{float(distance):.1f} px"
                        if distance is not None else "—"
                    )
                    detail_lines.append(
                        f"{role} #{int(item.get('index') or 0)}: "
                        f"top={float(item.get('top_y') or 0):.1f}; "
                        f"bottom={float(item.get('bottom_y') or 0):.1f}; "
                        f"height={float(item.get('height_px') or 0):.1f} px; "
                        f"rect {'OK' if item.get('rect_fits') else 'FAIL'}; "
                        f"d omission {distance_text}"
                    )
            if detail_lines:
                detail = "; ".join(detail_lines)

        if rule_name == "top_contacts" and isinstance(per_role, dict):
            for role, role_details in per_role.items():
                if not isinstance(role_details, dict):
                    continue
                reason = role_details.get("reason")
                if reason and str(reason).startswith("wrong_count"):
                    detail_lines.append(
                        f"{role}: найдено {int(role_details.get('found_raw') or 0)}/14"
                    )
                    continue
                if reason == "insufficient_valid_contact_masks":
                    detail_lines.append(
                        f"{role}: valid contact masks "
                        f"{int(role_details.get('found') or 0)}/14"
                    )
                    indices = role_details.get("invalid_mask_indices", [])
                    if indices:
                        detail_lines.append(
                            f"{role}: нет segmentation mask: "
                            + ", ".join(f"#{index}" for index in indices)
                        )
                    continue
                if reason == "no_valid_platform":
                    detail_lines.append(f"{role}: нет valid platform mask")
                    continue
                if reason == "invalid_platform_bbox":
                    detail_lines.append(f"{role}: нет valid platform bbox")
                    continue
                if reason == "layout_groups_failed":
                    counts = role_details.get("group_counts") or {}
                    detail_lines.append(
                        f"{role}: layout "
                        + ", ".join(
                            f"{group}={int(counts.get(group) or 0)}/"
                            f"{TopContactsRuleCount}"
                            for group, TopContactsRuleCount in (
                                ("L", 5), ("R", 5), ("T", 2), ("B", 2)
                            )
                        )
                    )
                    continue
                ignored = int(role_details.get("ignored") or 0)
                if ignored:
                    detail_lines.append(
                        f"{role}: лишних contacts показано серым: {ignored}"
                    )
                for group in ("L", "R", "T", "B"):
                    check = (role_details.get("group_checks") or {}).get(group) or {}
                    detail_lines.append(
                        f"{role} {group}: distance median "
                        f"{float(check.get('median_distance_px') or 0):.1f} px; "
                        f"max deviation "
                        f"{float(check.get('max_deviation_px') or 0):.1f}/"
                        f"{float(check.get('allowed_deviation_px') or 0):.1f} px"
                    )
                for item in role_details.get("items") or []:
                    detail_lines.append(
                        f"{role} #{int(item.get('index') or 0)} {item.get('group')}: "
                        f"distance {float(item.get('distance_px') or 0):.1f} px; "
                        f"deviation {float(item.get('deviation_px') or 0):.1f}/"
                        f"{float(item.get('allowed_deviation_px') or 0):.1f} px; "
                        f"rect {float(item.get('rect_width_px') or 0):g}x"
                        f"{float(item.get('rect_height_px') or 0):g} px "
                        f"{'OK' if item.get('rect_fits') else 'FAIL'}"
                    )
            if detail_lines:
                detail = "; ".join(detail_lines)

        if rule_name == "top_platform" and isinstance(per_role, dict):
            for role, role_details in per_role.items():
                if not isinstance(role_details, dict):
                    continue
                reason = role_details.get("reason")
                if reason == "no_valid_platform":
                    detail_lines.append(f"{role}: нет valid platform mask")
                    continue
                if reason == "invalid_platform_orientation":
                    detail_lines.append(f"{role}: не построена orientation platform")
                    continue
                placement = role_details.get("placement") or "not_fitted"
                placement_text = {
                    "centered": "по центру",
                    "shifted": "сдвинут",
                    "not_fitted": "не вписался",
                }.get(placement, str(placement))
                detail_lines.append(
                    f"{role}: rectangle "
                    f"{float(role_details.get('rect_width_px') or 0):g}x"
                    f"{float(role_details.get('rect_height_px') or 0):g} px; "
                    f"angle {float(role_details.get('angle_deg') or 0):.1f}°"
                )
                detail_lines.append(
                    f"{role}: {placement_text}; shift "
                    f"{float(role_details.get('shift_distance_px') or 0):.1f} px"
                )
            if detail_lines:
                detail = "; ".join(detail_lines)

        if rule_name == "platform_contacts_overlap" and isinstance(per_role, dict):
            for role, role_details in per_role.items():
                if not isinstance(role_details, dict):
                    continue
                reason = role_details.get("reason")
                if reason == "no_valid_platform":
                    detail_lines.append(f"{role}: нет valid platform mask")
                    continue
                if reason == "invalid_platform_orientation":
                    detail_lines.append(f"{role}: не построена orientation platform")
                    continue
                if reason == "inner_platform_reference_not_fitted":
                    detail_lines.append(
                        f"{role}: не построен inner rectangle 260x120 px"
                    )
                    continue
                detail_lines.append(
                    f"{role}: boundary "
                    f"{float(role_details.get('boundary_width_px') or 0):g}x"
                    f"{float(role_details.get('boundary_height_px') or 0):g} px; "
                    f"component min "
                    f"{int(role_details.get('excess_component_min_px') or 0)} px"
                )
                detail_lines.append(
                    f"{role}: largest component "
                    f"{int(role_details.get('largest_component_pixels') or 0)} px; "
                    f"confirmed "
                    f"{int(role_details.get('excess_pixels') or 0)} px"
                )
            if detail_lines:
                detail = "; ".join(detail_lines)

        if (
            rule_name in ("long_omission", "short_omission")
            and isinstance(per_role, dict)
        ):
            for role, role_details in per_role.items():
                if not isinstance(role_details, dict):
                    continue
                reason = role_details.get("reason")
                if reason:
                    detail_lines.append(
                        f"{role}: нет valid omission reference ({reason})"
                    )
                    continue
                detail_lines.append(
                    f"{role}: толщина "
                    f"{float(role_details.get('allowed_thickness_px') or 0):.1f} px; "
                    f"component min "
                    f"{int(role_details.get('excess_component_min_px') or 0)} px; "
                    f"residual "
                    f"{float(role_details.get('top_line_actual_max_residual_px') or 0):.1f}/"
                    f"{float(role_details.get('top_line_max_residual_px') or 0):.1f} px"
                )
                detail_lines.append(
                    f"{role}: largest component "
                    f"{int(role_details.get('largest_component_pixels') or 0)} px; "
                    f"confirmed "
                    f"{int(role_details.get('excess_pixels') or 0)} px; "
                    f"max depth "
                    f"{float(role_details.get('max_excess_depth_px') or 0):.1f} px"
                )
            if detail_lines:
                detail = "; ".join(detail_lines)

        if (
            result.triggered
            and rule_name not in (
                "window_geometry",
                "contacts_long",
                "contacts_short",
                "top_contacts",
                "top_platform",
                "platform_contacts_overlap",
                "long_omission",
                "short_omission",
            )
            and isinstance(per_role, dict)
        ):
            failure_rows = []
            for role, role_details in per_role.items():
                if (
                    not isinstance(role_details, dict)
                    or not role_details.get("triggered")
                ):
                    continue
                failures = []
                reason = role_details.get("reason")
                if reason:
                    failures.append(str(reason))

                if rule_name == "window_sinks":
                    failures = []
                    if reason and str(reason).startswith(
                        "invalid_window_reference_count"
                    ):
                        failures.append(
                            "нет семи mask окон: "
                            f"{int(role_details.get('selected_windows') or 0)}/7"
                        )
                    elif reason == "invalid_window_masks":
                        failures.append(
                            "нет segmentation mask окна: "
                            + ", ".join(
                                f"#{index}"
                                for index in role_details.get(
                                    "invalid_window_indices", []
                                )
                            )
                        )
                    elif reason == "invalid_sink_masks":
                        failures.append(
                            "нет segmentation mask раковины: "
                            + ", ".join(
                                f"#{index}"
                                for index in role_details.get(
                                    "invalid_sink_indices", []
                                )
                            )
                        )
                    elif not reason:
                        threshold = int(
                            role_details.get("overlap_min_px") or 0
                        )
                        for hit in role_details.get("hits") or []:
                            failures.append(
                                f"раковина #{hit.get('sink_index')} → "
                                f"окно #{hit.get('window_index')}: "
                                f"overlap {hit.get('overlap_px')} px "
                                f">= {threshold} px"
                            )

                elif rule_name == "sinks":
                    failures = []
                    if reason == "invalid_sink_masks":
                        failures.append(
                            "нет segmentation mask shell: "
                            + ", ".join(
                                f"#{index}"
                                for index in role_details.get(
                                    "invalid_sink_indices", []
                                )
                            )
                        )
                    elif reason == "invalid_case_central_reference":
                        failures.append(
                            "case_central reference: "
                            f"{int(role_details.get('case_central_found') or 0)}/1"
                        )
                    elif reason == "no_valid_platform":
                        failures.append("нет valid platform mask")
                    elif reason == "invalid_platform_bbox":
                        failures.append("нет valid platform bbox")
                    elif reason == "insufficient_valid_contacts":
                        failures.append(
                            "valid contact masks: "
                            f"{int(role_details.get('valid_contacts') or 0)}/14"
                        )
                    elif reason == "invalid_contact_layout":
                        counts = role_details.get("contact_group_counts") or {}
                        failures.append(
                            "contact layout: "
                            + ", ".join(
                                f"{group}={int(counts.get(group) or 0)}/{expected}"
                                for group, expected in (
                                    ("L", 5), ("R", 5),
                                    ("T", 2), ("B", 2),
                                )
                            )
                        )
                    elif not reason:
                        for hit in role_details.get("hits") or []:
                            failures.append(
                                f"shell #{hit.get('sink_index')}: forbidden "
                                f"{hit.get('forbidden_pixels')} px; "
                                f"central {hit.get('central_overlap_px')} px; "
                                f"platform {hit.get('platform_overlap_px')} px; "
                                f"contacts {hit.get('contacts_overlap_px')} px"
                            )

                elif rule_name == "glass":
                    failures = []
                    for hit in role_details.get("hits") or []:
                        failures.append(
                            f"glass #{hit.get('glass_index')} → ОЧИСТКА: "
                            f"platform {hit.get('platform_overlap_px')} px; "
                            f"pin {hit.get('pin_overlap_px')} px; "
                            f"ring {hit.get('ring_overlap_px')} px; "
                            f"union {hit.get('cleanup_overlap_px')} px"
                        )

                elif rule_name == "glass_on_contacts":
                    failures = []
                    if reason == "missing_glass_mask":
                        failures.append(
                            "нет segmentation mask glass: "
                            + ", ".join(
                                f"#{index}"
                                for index in role_details.get(
                                    "invalid_glass_indices", []
                                )
                            )
                        )
                    elif reason == "no_valid_platform":
                        failures.append("нет valid platform mask")
                    elif reason == "invalid_platform_bbox":
                        failures.append("нет valid platform bbox")
                    elif reason == "insufficient_valid_contacts":
                        failures.append(
                            "valid contact masks: "
                            f"{int(role_details.get('valid_contacts') or 0)}/14"
                        )
                    elif reason == "invalid_contact_layout":
                        counts = role_details.get("contact_group_counts") or {}
                        failures.append(
                            "contact layout: "
                            + ", ".join(
                                f"{group}={int(counts.get(group) or 0)}/{expected}"
                                for group, expected in (
                                    ("L", 5), ("R", 5),
                                    ("T", 2), ("B", 2),
                                )
                            )
                        )
                    elif reason and str(reason).startswith("wrong_pin_count"):
                        failures.append(
                            f"pins: {int(role_details.get('pins_found') or 0)}/14"
                        )
                    elif reason == "missing_pin_mask":
                        failures.append(
                            "нет pin mask: "
                            + ", ".join(
                                f"#{index}"
                                for index in role_details.get(
                                    "invalid_pin_indices", []
                                )
                            )
                        )
                    elif reason and str(reason).startswith("invalid_case_count"):
                        failures.append(
                            f"case: {int(role_details.get('case_found') or 0)}/1"
                        )
                    elif reason and str(reason).startswith(
                        "invalid_case_central_count"
                    ):
                        failures.append(
                            "case_central: "
                            f"{int(role_details.get('case_central_found') or 0)}/1"
                        )
                    elif reason == "case_central_not_inside_case":
                        failures.append("invalid case ring")
                    elif reason == "empty_case_ring":
                        failures.append("empty case ring")
                    elif not reason:
                        for pair in role_details.get("pairs") or []:
                            failures.append(
                                f"glass #{pair.get('glass_index')} → "
                                f"contact #{pair.get('contact_index')}: "
                                f"overlap {pair.get('overlap_pixels')} px → БРАК"
                            )


                if failures:
                    failure_rows.append(f"{role}: " + "; ".join(failures))
            if failure_rows:
                detail = "; ".join(failure_rows)

        consensus = details.get("consensus")
        if not isinstance(consensus, dict):
            consensus = {}

        if rule_name == "part_presence":
            detail = (
                "ДЕТАЛЬ НЕ ОБНАРУЖЕНА"
                if details.get("empty_tray")
                else "Деталь обнаружена"
            )
        if not detail:
            detail = "Сработало" if result.triggered else "Норма"

        status_label = None
        neutral = False
        if rule_name == "part_presence" and details.get("empty_tray"):
            status_label = "ДЕТАЛЬ НЕ ОБНАРУЖЕНА"
            neutral = True
            if consensus:
                status_label += (
                    f" · {int(consensus.get('empty_votes') or 0)}/"
                    f"{int(consensus.get('runs') or 0)}"
                )
        elif rule_name == "part_presence" and consensus:
            status_label = (
                "ДЕТАЛЬ ОБНАРУЖЕНА · "
                f"{int(consensus.get('present_votes') or 0)}/"
                f"{int(consensus.get('runs') or 0)}"
            )
        elif consensus:
            votes_key = "triggered_votes" if result.triggered else "normal_votes"
            status_label = (
                ("СРАБОТАЛО" if result.triggered else "НОРМА")
                + f" · {int(consensus.get(votes_key) or 0)}/"
                f"{int(consensus.get('runs') or 0)}"
            )

        return {
            "name": result.rule_name,
            "triggered": bool(result.triggered),
            "skipped": skipped,
            "status_label": status_label,
            "neutral": neutral,
            "show_detail": rule_name in (
                "window_geometry",
                "contacts_long",
                "contacts_short",
                "top_contacts",
                "top_platform",
                "platform_contacts_overlap",
                "long_omission",
                "short_omission",
            ),
            "detail": str(detail),
            "detail_lines": detail_lines,
            "consensus": dict(consensus),
        }

    def diagnostic_analyze_selected_camera(self, role: str) -> bool:
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if not self._prestart_diagnostic_allowed():
                return False
            available_roles = set(getattr(self.cameras, "mapping", {}))
            if not available_roles:
                available_roles = set(
                    self.inspector.INPUT_ROLES + self.inspector.SPIDER_ROLES
                )
            if role not in available_roles:
                raise ValueError(f"Неизвестная роль камеры: {role}")

            self._live_capture_pause.set()
            self._selected_analysis_active = True
            self._selected_analysis_role = role
            self._set_diagnostic_running(
                "SELECTED_MODEL",
                f"Анализ трёх кадров выбранной камеры: {role}",
            )
            self._set_process(
                "SELECTED_MODEL_ANALYSIS",
                f"Анализ 3 кадров {role}",
            )

            decision = self.inspector.decision
            decision_rules = decision.rules_for_role(role)
            if not decision_rules:
                raise RuntimeError(
                    f"Для камеры {role} нет активных правил анализа"
                )

            frame_runs = []
            vision_runs = []
            rule_results_by_run = []
            raw_model_health = []
            detection_counts = []

            for run_number in range(1, INSPECTION_RUNS + 1):
                self._set_process(
                    "SELECTED_MODEL_ANALYSIS",
                    f"{role}: свежий кадр {run_number}/{INSPECTION_RUNS}",
                )
                frame = self.cameras.capture_single(role)
                stage_frames = {role: frame}
                vision_results = self.inspector.vision.process_all(stage_frames)
                if role not in vision_results:
                    raise RuntimeError(
                        f"Модели не вернули результат камеры {role} "
                        f"в прогоне {run_number}"
                    )

                selected_rule_results = decision.evaluate_rules_detailed(
                    decision_rules,
                    vision_results,
                    frames=stage_frames,
                )
                frame_runs.append(frame)
                vision_runs.append(vision_results)
                rule_results_by_run.append(selected_rule_results)
                detection_counts.append(len(vision_results.get(role, [])))

                health_rows = getattr(
                    self.inspector.vision,
                    "last_health",
                    None,
                )
                if isinstance(health_rows, list):
                    raw_model_health.extend(
                        {**item, "run": run_number}
                        for item in health_rows
                        if isinstance(item, dict)
                    )

            rule_results, consensus, evidence_index = combine_rule_results(
                rule_results_by_run
            )
            evidence_frame = frame_runs[evidence_index]
            vision_results = vision_runs[evidence_index]
            stage_frames = {role: evidence_frame}
            model_rows = summarize_model_health(raw_model_health)
            if not model_rows or any(not row.get("ok") for row in model_rows):
                raise RuntimeError(
                    f"Нет полного комплекта model health 3/3 для камеры {role}"
                )

            rule_rows = []
            if role in self.inspector.INPUT_ROLES:
                rule_rows.append({
                    "name": "part_presence",
                    "triggered": False,
                    "skipped": True,
                    "status_label": None,
                    "neutral": False,
                    "show_detail": False,
                    "detail": (
                        "Не выполнено: для part_presence одновременно нужны "
                        "INPUT_LEFT и INPUT_RIGHT"
                    ),
                    "detail_lines": [],
                    "consensus": {},
                })
            rule_rows.extend(
                self._rule_report_row(result)
                for result in rule_results
            )

            height, width = evidence_frame.shape[:2]
            camera_rows = [{
                "role": role,
                "selected": True,
                "ok": True,
                "width": int(width),
                "height": int(height),
                "runs": INSPECTION_RUNS,
                "detections": int(detection_counts[evidence_index]),
                "detections_by_run": list(detection_counts),
            }]

            self._last_vision_results = vision_results
            self._last_rule_results = rule_results
            self._last_model_health = model_rows
            self._diagnostics = {
                "status": "PASSED",
                "kind": "SELECTED_MODEL",
                "message": (
                    f"{role}: 3 свежих кадра; моделей {len(model_rows)}; "
                    f"правил {len(rule_rows)}; объекты "
                    + "/".join(str(value) for value in detection_counts)
                ),
                "selected_role": role,
                "cameras": camera_rows,
                "models": model_rows,
                "rules": rule_rows,
                "consensus": consensus,
                "updated_at": time.time(),
            }
            self._set_process(
                "SELECTED_MODEL_READY",
                f"Анализ 3 кадров {role} завершён; поток приостановлен",
            )
            self._refresh_monitor(stage_frames)
            return True
        except Exception as exc:
            self._selected_analysis_active = False
            self._selected_analysis_role = None
            self._live_capture_pause.clear()
            self._set_diagnostic_error("SELECTED_MODEL", exc)
            self._handle_fault(f"Ошибка анализа выбранного кадра: {exc}")
            raise
        finally:
            self._operation_lock.release()

    def diagnostic_release_selected_camera(self) -> bool:
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if not self._selected_analysis_active:
                return False
            role = self._selected_analysis_role
            self._selected_analysis_active = False
            self._selected_analysis_role = None
            self._last_vision_results = {}
            self._last_rule_results = []
            self._last_model_health = []
            self._frame_analysis_rule_results = []
            self._frame_analysis_updated_at = None
            self._diagnostics = {
                "status": "NOT_RUN",
                "kind": None,
                "message": "Анализ трёх кадров не выполнялся",
                "cameras": [],
                "models": [],
                "rules": [],
                "updated_at": None,
            }
            self._live_capture_pause.clear()
            self._set_process(
                "LIVE_SELECTED_CAMERA",
                f"Поток восстановлен: {role}",
            )
            return True
        finally:
            self._operation_lock.release()

    def request_force_exit(self):
        self._cancel_motion.set()
        self._live_capture_pause.clear()
        accepted = self.sm.request_force_exit()
        if self.jog_active:
            try:
                self.exit_jog()
            except Exception as exc:
                print(f"[JOG] release during force exit failed: {exc}")
        self._safe_emergency_stop()
        return accepted

    # ─── Properties для UI и main.py ─────────────────────────────

    @property
    def state(self) -> str:
        return self.sm.state.value

    @property
    def exit_requested(self) -> bool:
        return self.sm.exit_requested

    @property
    def force_exit_requested(self) -> bool:
        return self.sm.force_exit

    @property
    def dist1_open_position(self) -> int:
        return self.distributor.dist1_open_position

    # ─── Main loop ───────────────────────────────────────────────

    def start(self):
        print("Система готова. Ожидание команды START.")

        try:
            while True:
                if self.sm.force_exit:
                    print("[EXIT] Force exit.")
                    break

                # Пауза применяется строго между шагами: _run_once никогда
                # не прерывается, поэтому детали остаются в своих ячейках.
                if (
                    self.sm.state == State.RUNNING
                    and self._pause_requested.is_set()
                    and not self.sm.exit_requested
                ):
                    if self.sm.request_pause():
                        self._enter_pause_frame()
                    else:
                        self._pause_requested.clear()
                    continue

                if self.sm.state == State.PAUSED:
                    if self.sm.exit_requested or self.sm.force_exit:
                        # Выход из паузы не должен оставлять линию в PAUSED.
                        self._pause_requested.clear()
                        self._stop_pause_frame_loop()
                        self.sm.request_stop()
                        continue
                    if self._jog_frame_error:
                        self._handle_fault(
                            "Ошибка камеры во время паузы: "
                            f"{self._jog_frame_error}"
                        )
                        continue
                    jog_error = (
                        self.jog.status.get("error")
                        if self.jog is not None else None
                    )
                    if jog_error:
                        self._handle_fault(f"Ошибка коррекции ленты: {jog_error}")
                        continue
                    self._refresh_monitor()
                    time.sleep(0.1)
                    continue

                if self.sm.is_active:
                    # STOP on an already empty line must not advance Conveyor.
                    if self.sm.state == State.STOPPING and not self.parts:
                        self.sm.notify_line_empty()
                        self._refresh_monitor()
                        if self.sm.exit_requested:
                            print("[EXIT] Line empty → exit.")
                            break
                        continue

                    self._run_once_safe()

                    if (
                        self.sm.state == State.STOPPING
                        and not self.parts
                    ):
                        self.sm.notify_line_empty()
                        self._refresh_monitor()

                        if self.sm.exit_requested:
                            print("[EXIT] Line empty → exit.")
                            break
                else:
                    if self.sm.exit_requested:
                        print("[EXIT] Not active → exit.")
                        break

                    if self._jog_frame_error and self.sm.state != State.FAULT:
                        self._handle_fault(
                            f"Ошибка камеры в режиме ручного управления: {self._jog_frame_error}"
                        )
                        continue
                    if self.jog is not None:
                        jog_status = self.jog.status
                        jog_error = jog_status.get("error")
                        if jog_error and self.sm.state != State.FAULT:
                            self._handle_fault(f"Ошибка ручного управления лентой: {jog_error}")
                            continue
                        if (
                            self._process.get("phase") == "JOG_HOLD"
                            and not jog_status.get("busy")
                        ):
                            self._set_process(
                                "JOG_STOPPED",
                                f"JOG остановлен: {jog_status.get('last_action', '-')}",
                            )
                            continue
                    self._refresh_monitor()
                    time.sleep(0.1)

        except Exception as e:
            print(f"[CYCLE] Critical error: {e}")
            traceback.print_exc()
            self._handle_fault(f"Критическая ошибка цикла: {e}")
        finally:
            self._shutdown = True
            self._cancel_motion.set()
            self._live_capture_pause.clear()
            self._pause_requested.clear()
            try:
                self._stop_pause_frame_loop()
            except Exception as e:
                print(f"[SHUTDOWN] stop pause frame loop failed: {e}")
            try:
                self.exit_jog()
            except Exception as e:
                print(f"[SHUTDOWN] exit_jog failed: {e}")
            self._safe_emergency_stop()
            self._archive_inflight("runtime_shutdown")
            print("Цикл конвейера завершён.")

    # ─── Fault ───────────────────────────────────────────────────

    def _handle_fault(self, reason: str):
        self._cancel_motion.set()
        self._selected_analysis_active = False
        self._selected_analysis_role = None
        self._live_capture_pause.clear()
        self._pause_requested.clear()
        self._stop_pause_frame_loop()
        self._fault_reason = reason
        print(f"[FAULT] {reason}")
        print(
            f"[FAULT] В очереди осталось "
            f"{len(self.parts)} деталей"
        )
        self.sm.notify_fault()
        if self.jog_active:
            try:
                self.exit_jog()
            except Exception as exc:
                print(f"[JOG] release during fault failed: {exc}")
        self._set_process("FAULT", reason)
        self._safe_emergency_stop()
        self._refresh_monitor()

    # ─── Safe run ────────────────────────────────────────────────

    def _run_once_safe(self):
        if self.sm.state == State.STOPPING and self.parts:
            if self._drain_start_time == 0:
                self._drain_start_time = time.time()
            elif time.time() - self._drain_start_time > DRAIN_TIMEOUT:
                self._handle_fault(
                    f"Превышено время штатной остановки {DRAIN_TIMEOUT} с; "
                    f"на линии осталось деталей: {len(self.parts)}"
                )
                return

        try:
            self._run_once()
            self._consecutive_errors = 0
        except Exception as e:
            # Repeating a failed physical step loses part-to-cell alignment.
            # Fail closed on the first incomplete cycle instead.
            self._consecutive_errors += 1
            print(f"[CYCLE] Error in _run_once: {e}")
            traceback.print_exc()
            self._handle_fault(f"Ошибка производственного шага: {e}")

    def _safe_emergency_stop(self):
        errors = []
        try:
            self.conveyor.emergency_stop()
        except Exception as e:
            errors.append(f"conveyor: {e}")
        try:
            stop_distributor = getattr(self.distributor, "emergency_stop", None)
            if stop_distributor is not None:
                stop_distributor()
        except Exception as e:
            errors.append(f"distributor: {e}")
        if errors:
            print(f"[SHUTDOWN] Emergency stop errors: {'; '.join(errors)}")

    def _check_motion_cancelled(self):
        if self._cancel_motion.is_set() or self.sm.force_exit:
            raise RuntimeError("physical operation cancelled")

    # ─── Core step ───────────────────────────────────────────────

    def _run_once(self):
        self._check_motion_cancelled()
        print(f"\nШАГ {self.current_step + 1}")

        # Фиксируем право принять INPUT в начале физического шага. Если STOP
        # придёт уже во время движения, вошедшая этим шагом деталь всё равно
        # будет проинспектирована и останется синхронизированной с ячейкой.
        accept_input_for_this_step = self.sm.accepts_new_parts

        self._last_vision_results = {}
        self._last_rule_results = []

        self._pending_drop = self._find_pending_drop()
        pending_id = self._pending_drop.id if self._pending_drop else None
        self._set_process(
            "ROUTE_PREPARE",
            "Подготовка маршрута распределителя",
            part_id=pending_id,
            positions=[self.OFFSET_REJECT] if pending_id else [],
        )
        self._prepare_drop()
        self._check_motion_cancelled()

        self._set_process(
            "CONVEYOR_COMMAND",
            "Команда движения ленты отправлена",
            part_id=pending_id,
            positions=range(self.OFFSET_REJECT + 1),
        )
        self.conveyor.move_step()
        self.conveyor.wait_stop(progress_callback=self._on_conveyor_progress)
        self._check_motion_cancelled()
        # Commit the logical position only after confirmed physical completion.
        self.current_step += 1
        self._set_process(
            "CONVEYOR_CONFIRMED",
            "Позиции деталей подтверждены контроллером",
            part_id=pending_id,
            positions=range(self.OFFSET_REJECT + 1),
        )

        if self._pending_drop is not None:
            self._set_process(
                "PART_DROP",
                "Сброс детали и возврат лопасти",
                part_id=pending_id,
                positions=[self.OFFSET_REJECT],
            )
        self._execute_drop()
        self._check_motion_cancelled()

        frame_runs = []
        for run_number in range(1, INSPECTION_RUNS + 1):
            self._set_process(
                "CAMERA_CAPTURE",
                f"Синхронный захват семи камер: прогон {run_number}/{INSPECTION_RUNS}",
                positions=[self.OFFSET_INPUT, self.OFFSET_SPIDER],
            )
            frame_runs.append(self.cameras.capture_all())
            self._check_motion_cancelled()

        # По умолчанию UI получает самый свежий набор. Для каждой реально
        # выполненной стадии он заменяется evidence-кадрами, выбранными
        # majority-алгоритмом как наиболее согласованными с итогом.
        display_frames = dict(frame_runs[-1])

        # Input inspection: part_presence и каждое defect rule голосуют 2/3.
        if accept_input_for_this_step:
            self._set_process(
                "INPUT_ANALYSIS",
                "Вход: три прогона моделей и голосование правил 2 из 3",
                positions=[self.OFFSET_INPUT],
            )
            input_result = self._process_input_stage(frame_runs)
            if input_result is not None:
                display_frames.update(input_result.raw_frames)
            self._check_motion_cancelled()

        self._set_process(
            "SPIDER_CHECK",
            "Проверка детали на +4: три прогона, голосование 2 из 3",
            positions=[self.OFFSET_SPIDER],
        )
        spider_result = self._run_spider_inspection(frame_runs)
        if spider_result is not None:
            display_frames.update(spider_result.raw_frames)
        self._check_motion_cancelled()

        self._set_process(
            "ROUTE_CHECK",
            "Проверка годной детали на позиции +7",
            positions=[self.OFFSET_REJECT],
        )
        self._pass_good_parts()

        self._set_process("STEP_COMPLETE", "Шаг полностью завершён")
        self._refresh_monitor(display_frames)

    # ─── Input stage ─────────────────────────────────────────────

    def _process_input_stage(self, frame_runs):
        """Обработать INPUT по трём свежим кадрам и majority 2 из 3."""

        candidate_id = self.part_counter + 1
        self._set_process(
            "INPUT_ANALYSIS",
            f"Вход: голосование 2 из 3 для кандидата №{candidate_id}",
            part_id=candidate_id,
            positions=[self.OFFSET_INPUT],
        )

        inspect_consensus = getattr(
            self.inspector,
            "inspect_input_consensus",
            None,
        )
        if not callable(inspect_consensus):
            raise RuntimeError(
                "Inspector не поддерживает обязательный INPUT consensus 2 из 3"
            )
        result = inspect_consensus(
            part_id=candidate_id,
            step=self.current_step,
            frame_runs=frame_runs,
            force_bad=self.force_all_bad,
        )
        self._append_latest_model_health(result)
        self._frame_analysis_rule_results = list(result.rule_results)
        self._frame_analysis_updated_at = time.time()

        if result.is_empty_tray:
            self.empty_count += 1
            self._last_vision_results.update(result.vision_results)
            self._last_rule_results.extend(result.rule_results)
            print(
                f"[EMPTY] Пустой лоток на step {self.current_step} "
                f"по majority 2/3 (total empty: {self.empty_count})"
            )
            # Пустой лоток остаётся нейтральным: Part и архив не создаются.
            return result

        self.part_counter += 1
        part = Part(self.part_counter, self.current_step)
        part.inspection_consensus["input"] = dict(result.consensus)
        self.parts.append(part)
        print(f"[INPUT] Деталь #{part.id}")

        for defect in result.defects:
            part.add_input_defect(defect)
        part.mark_input_done()

        self._last_vision_results.update(result.vision_results)
        self._last_rule_results.extend(result.rule_results)

        if self.archive:
            self.archive.store_frames(
                part_id=part.id,
                stage="input",
                raw_frames=result.raw_frames,
                annotated_frames=result.annotated,
                raw_overlay_frames=result.raw_overlay_frames,
            )

        print(
            f"[INPUT] Деталь #{part.id} "
            f"дефекты majority: {result.defects or ['none']}"
        )
        return result

    # ─── Inspection ──────────────────────────────────────────────

    def _run_spider_inspection(self, frame_runs):
        for part in self.parts:
            if (part.step_created + self.OFFSET_SPIDER
                    != self.current_step):
                continue

            self._set_process(
                "SPIDER_ANALYSIS",
                f"Контроль 2 из 3 для детали №{part.id}",
                part_id=part.id,
                positions=[self.OFFSET_SPIDER],
            )
            inspect_consensus = getattr(
                self.inspector,
                "inspect_spider_consensus",
                None,
            )
            if not callable(inspect_consensus):
                raise RuntimeError(
                    "Inspector не поддерживает обязательный SPIDER consensus 2 из 3"
                )
            result = inspect_consensus(
                part_id=part.id,
                step=self.current_step,
                frame_runs=frame_runs,
                force_bad=self.force_all_bad,
            )
            self._append_latest_model_health(result)
            self._frame_analysis_rule_results = list(result.rule_results)
            self._frame_analysis_updated_at = time.time()
            part.inspection_consensus["spider"] = dict(result.consensus)

            for defect in result.defects:
                part.add_spider_defect(defect)

            part.mark_spider_done()

            self._last_vision_results.update(result.vision_results)
            self._last_rule_results.extend(result.rule_results)

            if self.archive:
                self.archive.store_frames(
                    part_id=part.id,
                    stage="spider",
                    raw_frames=result.raw_frames,
                    annotated_frames=result.annotated,
                    raw_overlay_frames=result.raw_overlay_frames,
                )

            print(
                f"[SPIDER] Деталь #{part.id} "
                f"дефекты majority: {result.defects or ['none']} "
                f"категория={part.route_category}"
            )
            return result
        return None

    # ─── Distributor flow ────────────────────────────────────────

    def _find_pending_drop(self):
        next_step = self.current_step + 1
        for part in self.parts:
            if part.step_created + self.OFFSET_REJECT == next_step:
                return part
        return None

    def _prepare_drop(self):
        part = self._pending_drop
        if part is None:
            self.distributor.reset_target()
            return

        category = part.route_category

        if category == CATEGORY_UNKNOWN:
            print(
                f"[WARN] Деталь #{part.id} не прошла полную "
                f"инспекцию → принудительно BAD"
            )
            part.route_category = CATEGORY_BAD
            part.final_decision = "incomplete_inspection"
            category = CATEGORY_BAD

        if category in (CATEGORY_BAD, CATEGORY_CLEANUP):
            print(
                f"[PRE-OPEN] Деталь #{part.id} "
                f"категория={category}"
            )
            self.distributor.prepare(category, part.id)
        else:
            self.distributor.mark_pass(part.id)
            self._pending_drop = None

    def _execute_drop(self):
        part = self._pending_drop
        if part is None:
            return

        category = part.route_category
        self.distributor.drop_and_close(part.id, category)

        if category == CATEGORY_BAD:
            self.bad_count += 1
            print(
                f"[REJECT] #{part.id} → BAD "
                f"({self.bad_count})"
            )
        elif category == CATEGORY_CLEANUP:
            self.cleanup_count += 1
            print(
                f"[CLEANUP] #{part.id} → CLEANUP "
                f"({self.cleanup_count})"
            )

        self._archive_part(part)
        self._register_finished(part)
        self._remove_part(part)
        self._pending_drop = None

    def _pass_good_parts(self):
        for part in list(self.parts):
            if (part.step_created + self.OFFSET_REJECT
                    != self.current_step):
                continue
            if part.route_category != CATEGORY_GOOD:
                continue

            self._set_process(
                "ROUTE_FINALIZE",
                f"GOOD: завершение детали №{part.id}",
                part_id=part.id,
                positions=[self.OFFSET_REJECT],
            )
            self.good_count += 1
            self._archive_part(part)
            self._register_finished(part)
            self._remove_part(part)
            print(
                f"[PASS] #{part.id} → GOOD "
                f"({self.good_count})"
            )

    # ─── Archive ─────────────────────────────────────────────────

    def _archive_part(self, part, extra=None):
        if not self.archive:
            return
        kwargs = {
            "part_id": part.id,
            "category": part.route_category,
            "decision": part.final_decision,
            "defects": part.get_all_defects(),
            "step": part.step_created,
        }
        archive_extra = {}
        consensus = getattr(part, "inspection_consensus", None)
        if consensus:
            archive_extra["inspection_consensus"] = consensus
        if extra:
            archive_extra.update(extra)
        if archive_extra:
            kwargs["extra"] = archive_extra
        self.archive.finalize(**kwargs)

    def _archive_inflight(self, reason: str):
        for part in list(self.parts):
            if part.route_category == CATEGORY_UNKNOWN:
                part.route_category = CATEGORY_BAD
            part.final_decision = f"aborted_{reason}"
            try:
                self._archive_part(
                    part,
                    extra={"aborted": True, "abort_reason": reason},
                )
            except Exception as e:
                print(f"[ARCHIVE] Failed to archive aborted part #{part.id}: {e}")
            self._remove_part(part)
        self._pending_drop = None

    # ─── Helpers ─────────────────────────────────────────────────

    def _remove_part(self, part):
        if part in self.parts:
            self.parts.remove(part)

    def _register_finished(self, part):
        self.recent_parts.append({
            "id":       part.id,
            "decision": part.final_decision,
            "category": part.route_category,
            "time":     time.time(),
        })

    def _append_latest_model_health(self, inspection_result=None):
        result_rows = getattr(inspection_result, "model_health", None)
        if isinstance(result_rows, list) and result_rows:
            self._last_model_health = [dict(item) for item in result_rows]
            return
        vision = getattr(self.inspector, "vision", None)
        rows = getattr(vision, "last_health", None)
        if not isinstance(rows, list):
            return
        self._last_model_health = [dict(item) for item in rows]

    def _on_state_change(self, old, new, action: str):
        if new == State.STOPPING:
            self._set_process("DRAINING", "Завершение деталей на линии")
        elif new == State.STOPPED:
            self._set_process("STOPPED", "Линия остановлена и пуста")
        elif new == State.FAULT:
            self._set_process("FAULT", "Цикл остановлен из-за ошибки")
        else:
            self._refresh_monitor()

    # ─── JOG mode ────────────────────────────────────────────────

    def can_enter_jog(self) -> bool:
        if self.jog is None or self._shutdown:
            return False
        return (
            self.state in self.JOG_ALLOWED_STATES
            and not self.exit_requested
            and not self._operation_lock.locked()
            and not self._jog_frame_error
            and not self.jog.status.get("error")
        )

    def enter_jog(self) -> bool:
        with self._jog_lock:
            if self._shutdown:
                return False
            if self.jog is None:
                return False
            if self.jog_active:
                return True
            if not self.can_enter_jog():
                print(f"[JOG] Cannot enter (state={self.state})")
                return False

            self.jog_active = True
            self._jog_frame_times.clear()
            self._start_jog_frame_loop()
            print("[JOG] entered")

        self._refresh_monitor()
        return True

    def exit_jog(self):
        with self._jog_lock:
            if not self.jog_active:
                return True
            release_error = None
            try:
                if self.jog is not None:
                    self.jog.release("leaving JOG mode")
            except Exception as exc:
                release_error = exc
            finally:
                self.jog_active = False
                self._stop_jog_frame_loop()
                print("[JOG] exited")

        self._refresh_monitor()
        if release_error is not None:
            raise release_error
        return True

    def jog_hold_start(self, direction: str) -> bool:
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if (
                not self.jog_active
                or self.jog is None
                or self.state not in self.JOG_ALLOWED_STATES
                or self.exit_requested
                or self._selected_analysis_active
            ):
                return False
            accepted = self.jog.start_hold(direction)
            if accepted:
                label = "Ручное движение ленты вправо" if direction == "+" else "Ручное движение ленты влево"
                self._set_process(
                    "JOG_HOLD",
                    label,
                    positions=range(self.OFFSET_REJECT + 1),
                )
            else:
                self._refresh_monitor()
            return accepted
        finally:
            self._operation_lock.release()

    def jog_hold_heartbeat(self, direction: str) -> bool:
        if (
            not self.jog_active
            or self.jog is None
            or self.state not in self.JOG_ALLOWED_STATES
        ):
            return False
        return self.jog.heartbeat(direction)

    def jog_hold_release(self, reason: str = "button released") -> bool:
        # A delayed UI release must never stop a production Conveyor after START.
        if (
            self.jog is None
            or not self.jog_active
            or self.state not in self.JOG_ALLOWED_STATES
        ):
            return False
        accepted = self.jog.release(reason)
        if accepted:
            self._set_process("JOG_STOPPED", f"Ручное движение остановлено: {reason}")
        else:
            self._refresh_monitor()
        return accepted

    # ─── Пауза: живой кадр для наведения ленты ───────────────────

    def _enter_pause_frame(self):
        """Включить LIVE-поток, чтобы оператор видел лентy во время правки."""
        if self.jog is not None:
            # Каждая пауза начинается с чистого накопителя коррекции.
            self.jog.reset_nudge_offset()
        offset_limit = (
            self.jog.status.get("nudge_limit_steps") if self.jog is not None else 0
        )
        if not self._pause_frame_active:
            self._jog_frame_times.clear()
            self._start_jog_frame_loop()
            self._pause_frame_active = True
        print("[PAUSE] линия остановлена на границе шага")
        self._set_process(
            "PAUSED",
            (
                "Пауза: доступна коррекция ленты "
                f"±{offset_limit} микрошагов"
            ),
            positions=range(self.OFFSET_REJECT + 1),
        )

    def _stop_pause_frame_loop(self):
        if not self._pause_frame_active:
            return
        self._pause_frame_active = False
        self._stop_jog_frame_loop()

    # ─── JOG live frame loop ─────────────────────────────────────

    def _start_jog_frame_loop(self):
        self._jog_stop_event.clear()
        self._jog_thread = threading.Thread(
            target=self._jog_frame_loop,
            daemon=True,
            name="jog-selected-camera",
        )
        self._jog_aux_thread = threading.Thread(
            target=self._jog_aux_frame_loop,
            daemon=True,
            name="jog-aux-cameras",
        )
        self._jog_thread.start()
        self._jog_aux_thread.start()

    def _stop_jog_frame_loop(self):
        self._jog_stop_event.set()
        deadline = time.monotonic() + JOG_THREAD_JOIN_TIMEOUT
        for label, thread in (
            ("selected", self._jog_thread),
            ("auxiliary", self._jog_aux_thread),
        ):
            if thread and thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    print(
                        f"[JOG] {label} frame thread не остановился за "
                        f"{JOG_THREAD_JOIN_TIMEOUT}s"
                    )
        self._jog_thread = None
        self._jog_aux_thread = None

    def _get_active_camera_role(self):
        try:
            server = getattr(self.monitor, "server", None)
            if server is None:
                return None
            return getattr(server, "active_camera_role", None)
        except Exception:
            return None

    def _jog_frame_loop(self):
        print("[JOG] live frame loop started")
        while not self._jog_stop_event.is_set():
            if self._live_capture_pause.is_set():
                self._jog_stop_event.wait(0.02)
                continue
            iteration_started = time.monotonic()
            try:
                available_roles = list(getattr(self.cameras, "mapping", {}))
                active_role = self._get_active_camera_role()
                if active_role is None:
                    active_role = available_roles[0] if available_roles else None
                elif available_roles and active_role not in available_roles:
                    active_role = available_roles[0]

                if active_role is None:
                    # Compatibility path for a camera provider without role map.
                    frames = self.cameras.capture_all()
                    self._jog_frame_times.append(time.monotonic())
                    if self.monitor:
                        self.monitor.update(
                            frames=frames,
                            vision_results={},
                            rule_results=[],
                        )
                else:
                    # Выбранная камера имеет приоритет, но публикация жёстко
                    # ограничена частотой LIVE_TARGET_FPS.
                    frame = self.cameras.capture_single(active_role)
                    self._jog_frame_times.append(time.monotonic())
                    if self.monitor:
                        self.monitor.update(
                            frames={active_role: frame},
                            vision_results={},
                            rule_results=[],
                        )
            except Exception as e:
                self._jog_frame_error = f"{type(e).__name__}: {e}"
                print(f"[JOG] frame loop error: {self._jog_frame_error}")
                self._jog_stop_event.set()
                break
            elapsed = time.monotonic() - iteration_started
            self._jog_stop_event.wait(max(0.0, JOG_FRAME_INTERVAL - elapsed))
        print("[JOG] live frame loop stopped")

    def _jog_aux_frame_loop(self):
        print("[JOG] auxiliary frame loop started")
        while not self._jog_stop_event.is_set():
            if self._live_capture_pause.is_set():
                self._jog_stop_event.wait(0.02)
                continue
            iteration_started = time.monotonic()
            try:
                available_roles = list(getattr(self.cameras, "mapping", {}))
                active_role = self._get_active_camera_role()
                auxiliary_roles = [
                    role for role in available_roles
                    if role != active_role
                ]
                if auxiliary_roles:
                    capture_roles = getattr(self.cameras, "capture_roles", None)
                    if callable(capture_roles):
                        frames = capture_roles(auxiliary_roles)
                    else:
                        frames = {
                            role: frame
                            for role, frame in self.cameras.capture_all().items()
                            if role in auxiliary_roles
                        }
                    if self.monitor and frames:
                        self.monitor.update(frames=frames)
            except Exception as exc:
                if self._jog_stop_event.is_set():
                    break
                self._jog_frame_error = f"{type(exc).__name__}: {exc}"
                print(f"[JOG] auxiliary frame loop error: {self._jog_frame_error}")
                self._jog_stop_event.set()
                break
            elapsed = time.monotonic() - iteration_started
            self._jog_stop_event.wait(
                max(0.0, JOG_AUX_BATCH_INTERVAL - elapsed)
            )
        print("[JOG] auxiliary frame loop stopped")

    def _current_live_fps(self) -> float:
        now = time.monotonic()
        recent = [value for value in list(self._jog_frame_times) if now - value <= 2.0]
        if len(recent) < 2:
            return 0.0
        elapsed = recent[-1] - recent[0]
        measured = 0.0 if elapsed <= 0 else (len(recent) - 1) / elapsed
        return min(LIVE_TARGET_FPS, measured)

    # ─── Monitor ─────────────────────────────────────────────────

    def _build_frame_analysis(self, state_name: str) -> dict:
        report = self._diagnostics
        selected_report = report.get("kind") == "SELECTED_MODEL"

        if state_name in ("RUNNING", "STOPPING"):
            return {
                "available": True,
                "kind": "CYCLE",
                "active": True,
                "title": "АНАЛИЗ ТЕКУЩЕГО КАДРА",
                "role": None,
                "part_id": self._process.get("part_id"),
                "message": (
                    "Ожидание результатов анализа"
                    if not self._last_model_health
                    and not self._frame_analysis_rule_results
                    else "Итог трёх свежих кадров: голосование каждого правила 2 из 3"
                ),
                "models": [dict(item) for item in self._last_model_health],
                "rules": [
                    self._rule_report_row(result)
                    for result in self._frame_analysis_rule_results
                ],
                "updated_at": self._frame_analysis_updated_at,
            }

        if selected_report:
            return {
                "available": True,
                "kind": "SELECTED",
                "active": self._selected_analysis_active,
                "title": "АНАЛИЗ 3 КАДРОВ",
                "role": (
                    report.get("selected_role")
                    or self._selected_analysis_role
                ),
                "part_id": None,
                "message": report.get("message") or "Анализ трёх кадров",
                "status": report.get("status"),
                "cameras": [dict(item) for item in report.get("cameras", [])],
                "models": [dict(item) for item in report.get("models", [])],
                "rules": [dict(item) for item in report.get("rules", [])],
                "updated_at": report.get("updated_at"),
            }

        return {
            "available": False,
            "kind": None,
            "active": False,
            "title": None,
            "role": None,
            "part_id": None,
            "message": None,
            "models": [],
            "rules": [],
            "updated_at": None,
        }

    def _build_status(self) -> dict:
        dist = self.distributor.status

        sm_snap = self.sm.get_snapshot()

        line_parts = []
        for part in self.parts:
            position = self.current_step - part.step_created
            position = max(0, min(position, self.OFFSET_REJECT))
            line_parts.append({
                "id": part.id,
                "position": position,
                "category": part.route_category,
            })

        state_name = sm_snap["state"]
        operation_busy = self._operation_lock.locked()
        jog_snapshot = self.jog.status if self.jog is not None else {}
        jog_busy = bool(jog_snapshot.get("busy", False))
        jog_error = jog_snapshot.get("error") or self._jog_frame_error
        diagnostic_allowed = (
            state_name in ("IDLE", "STOPPED")
            and not self.parts
            and not jog_busy
            and not jog_error
            and not operation_busy
            and not self._cancel_motion.is_set()
            and not self._selected_analysis_active
            and not sm_snap["exit_requested"]
        )
        controls = {
            "start": (
                state_name in ("IDLE", "STOPPED")
                and not self.parts
                and not jog_busy
                and not jog_error
                and not operation_busy
                and not self._selected_analysis_active
                and not sm_snap["exit_requested"]
            ),
            "stop": (
                state_name in ("RUNNING", "PAUSED")
                and not operation_busy
            ),
            "pause": (
                state_name == "RUNNING"
                and not self._pause_requested.is_set()
                and not operation_busy
                and not sm_snap["exit_requested"]
            ),
            "resume": (
                state_name == "PAUSED"
                and not operation_busy
                and not jog_error
                and not sm_snap["exit_requested"]
            ),
            "nudge": (
                state_name in self.NUDGE_ALLOWED_STATES
                and self.jog is not None
                and not operation_busy
                and not jog_busy
                and not jog_error
                and not sm_snap["exit_requested"]
            ),
            "exit": (
                not self._shutdown
                and not operation_busy
                and not jog_busy
            ),
            "jog_hold": (
                state_name in self.JOG_ALLOWED_STATES
                and self.jog_active
                and not jog_error
                and not operation_busy
                and not self._selected_analysis_active
                and not sm_snap["exit_requested"]
            ),
            "selected_model_analysis": diagnostic_allowed,
            "selected_model_release": (
                self._selected_analysis_active
                and state_name in ("IDLE", "STOPPED")
                and not operation_busy
            ),
            "distributor_diagnostic": diagnostic_allowed,
            "camera_diagnostic": diagnostic_allowed,
            "vision_rule_diagnostic": diagnostic_allowed,
        }

        status = {
            "state": state_name,
            "exit_requested": sm_snap["exit_requested"],
            "fault_reason": self._fault_reason,
            "step": self.current_step,
            "in_line": len(self.parts),
            "line_parts": line_parts,
            "total": self.part_counter,
            "good": self.good_count,
            "rejected": self.bad_count,
            "cleanup": self.cleanup_count,
            "empty": self.empty_count,
            **dist,
            "axis_position": dist["dist1_position"],
            "axis_max": dist["dist1_max"],
            "distributor_state": dist["dist1_state"],
            "process": dict(self._process),
            "pause": {
                "requested": self._pause_requested.is_set(),
                "active": state_name == "PAUSED",
                "nudge_offset": jog_snapshot.get("nudge_offset", 0),
                "nudge_limit_steps": jog_snapshot.get("nudge_limit_steps", 0),
                "micro_steps": jog_snapshot.get("micro_steps", 0),
                "remaining_forward": jog_snapshot.get(
                    "nudge_remaining_forward", 0,
                ),
                "remaining_backward": jog_snapshot.get(
                    "nudge_remaining_backward", 0,
                ),
            },
            "diagnostic_allowed": diagnostic_allowed,
            "diagnostic_busy": operation_busy,
            "controls": controls,
            "selected_analysis": {
                "active": self._selected_analysis_active,
                "role": self._selected_analysis_role,
            },
            "frame_analysis": self._build_frame_analysis(state_name),
            "diagnostics": {
                **self._diagnostics,
                "cameras": [dict(item) for item in self._diagnostics["cameras"]],
                "models": [dict(item) for item in self._diagnostics["models"]],
                "rules": [dict(item) for item in self._diagnostics["rules"]],
            },
        }

        if self.jog is not None:
            state_ok = (
                sm_snap["state"] in self.JOG_ALLOWED_STATES
            )
            jog_status = self.jog.status
            status["jog"] = {
                "active":      bool(self.jog_active and state_ok),
                "can_enter":   self.can_enter_jog(),
                "hold_steps":  jog_status["hold_steps"],
                "last_action": jog_status["last_action"],
                "busy":        jog_status["busy"],
                "direction":   jog_status["direction"],
                "error":       jog_error,
                "live_fps":    self._current_live_fps(),
            }
        else:
            status["jog"] = {
                "active":      False,
                "can_enter":   False,
                "hold_steps":  0,
                "last_action": "-",
                "busy":        False,
                "direction":   None,
                "error":       None,
            }

        return status

    def _refresh_monitor(self, frames: dict = None):
        if not self.monitor:
            return
        status = self._build_status()
        if frames:
            self.monitor.update(
                frames=frames,
                vision_results=self._last_vision_results,
                rule_results=self._last_rule_results,
                line_status=status,
                recent_parts=list(self.recent_parts),
            )
        else:
            self.monitor.update(
                line_status=status,
                recent_parts=list(self.recent_parts),
            )