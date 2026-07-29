import time
import threading
import traceback
from collections import deque

from core.live_preview import LivePreview
from core.rule_report import build_rule_report_row, build_rule_report_rows
from core.state_machine import StateMachine, State
from core.step_stages import (
    STAGE_SETTLE_SECONDS,
    STAGE_TRACE_SECONDS,
    StepSequencer,
)
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


class ProductionCycle:
    """
    Оркестратор производственной линии.
    """

    OFFSET_INPUT  = 0
    OFFSET_SPIDER = 4
    OFFSET_REJECT = 7

    JOG_ALLOWED_STATES = ("IDLE", "STOPPED", "PAUSED")

    def __init__(
        self,
        conveyor,
        cameras,
        inspector,
        distributor,
        monitor=None,
        archive=None,
        jog=None,
        settle_seconds=STAGE_SETTLE_SECONDS,
        stage_trace_seconds=STAGE_TRACE_SECONDS,
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

        # Живой просмотр: работает и в JOG, и во время движения ленты.
        self.live = LivePreview(
            cameras=cameras,
            monitor=monitor,
            get_active_role=self._get_active_camera_role,
        )

        # Фазы шага и передача камер между live-просмотром и инспекцией.
        self.stages = StepSequencer(
            self.live,
            settle_seconds=settle_seconds,
            trace_seconds=stage_trace_seconds,
            on_stage=self._on_stage_change,
        )

        # JOG
        self.jog_active: bool = False
        self._jog_lock = threading.Lock()
        self._selected_analysis_active = False
        self._selected_analysis_role = None
        self._shutdown = False

        # Пауза в рабочем цикле
        self._pause_requested = threading.Event()
        self._pause_frame_active = False

    # Process telemetry

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
        conveyor_info = dict(status or {})
        # Expose speed for frontend animation timing (higher = faster motion)
        try:
            conveyor_info["speed"] = int(getattr(self.conveyor, "speed", 20000))
            conveyor_info["normal_steps"] = int(getattr(self.conveyor, "steps_per_division", 19048))
        except Exception:
            conveyor_info["speed"] = 20000
        self._set_process(
            "CONVEYOR_MOVING",
            "Лента перемещает детали на следующую позицию",
            part_id=current.get("part_id"),
            positions=range(self.OFFSET_REJECT + 1),
            conveyor_status=conveyor_info,
        )

    # Public API

    def request_start(self):
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if self._selected_analysis_active:
                return False
            if self.live.error:
                return False
            if self.jog is not None and self.jog.status.get("error"):
                return False
            if self.jog_active:
                print("[JOG] auto-exit on START")
                self.exit_jog()
            # The frame thread may fail while START waits for JOG shutdown.
            if self.live.error:
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
                # Оператор видит поток всё время, пока линия работает;
                # на статических этапах шага он приостанавливается.
                self.live.start()
                self._set_process("READY", "Цикл запущен")
            return accepted
        finally:
            self._operation_lock.release()

    def request_stop(self):
        self._pause_requested.clear()
        if self._pause_frame_active:
            self._stop_pause_frame_loop()
        return self.sm.request_stop()

    def request_exit(self):
        self._pause_requested.clear()
        if self._pause_frame_active:
            self._stop_pause_frame_loop()
        return self.sm.request_exit()

    def request_pause(self) -> bool:
        """Запросить паузу после остановки шага и до работы нейронок."""
        if self.state != "RUNNING" or self.exit_requested:
            return False
        if self._pause_requested.is_set():
            return True
        self._pause_requested.set()
        self._set_process(
            "PAUSE_REQUESTED",
            "Пауза будет применена после остановки шага и до работы нейронок",
            positions=range(self.OFFSET_REJECT + 1),
        )
        self._refresh_monitor()
        return True

    def request_resume(self) -> bool:
        """Возобновить работу линии из паузы."""
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if self.state != "PAUSED" or self.exit_requested:
                return False
            if self.jog is not None and (self.jog.busy or self.jog.status.get("error")):
                return False
            self._pause_requested.clear()
            accepted = self.sm.request_resume()
            if not accepted:
                return False
            self._stop_pause_frame_loop()
            print("[PAUSE] resume; работа возобновлена")
            self._set_process(
                "RESUMED",
                "Работа возобновлена после паузы",
                positions=range(self.OFFSET_REJECT + 1),
            )
            self._refresh_monitor()
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
                "Камеры -> модели -> правила дефектов",
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
        return build_rule_report_row(result)

    @staticmethod
    def _rule_report_rows(results) -> list:
        return build_rule_report_rows(results)

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

            if not self.live.pause():
                raise RuntimeError(
                    "Live-просмотр не освободил камеры для анализа кадров"
                )
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
                    "summary_lines": [
                        "Не выполнено: для part_presence одновременно нужны "
                        "INPUT_LEFT и INPUT_RIGHT"
                    ],
                    "part_absent": False,
                    "decisive": True,
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
            self.live.resume()
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
            self.live.resume()
            self._set_process(
                "LIVE_SELECTED_CAMERA",
                f"Поток восстановлен: {role}",
            )
            return True
        finally:
            self._operation_lock.release()

    def request_force_exit(self):
        self._cancel_motion.set()
        self._pause_requested.clear()
        if self._pause_frame_active:
            self._stop_pause_frame_loop()
        self.stages.reset()
        self.live.reset_pause()
        accepted = self.sm.request_force_exit()
        if self.jog_active:
            try:
                self.exit_jog()
            except Exception as exc:
                print(f"[JOG] release during force exit failed: {exc}")
        self._safe_emergency_stop()
        return accepted

    # Properties для UI и main.py

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

    # Main loop

    def start(self):
        print("Система готова. Ожидание команды START.")

        try:
            while True:
                if self.sm.force_exit:
                    print("[EXIT] Force exit.")
                    break

                if self.sm.is_active:
                    # STOP on an already empty line must not advance Conveyor.
                    if self.sm.state == State.STOPPING and not self.parts:
                        self.sm.notify_line_empty()
                        self._refresh_monitor()
                        if self.sm.exit_requested:
                            print("[EXIT] Line empty -> exit.")
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
                            print("[EXIT] Line empty -> exit.")
                            break
                else:
                    if self.sm.exit_requested:
                        print("[EXIT] Not active -> exit.")
                        break

                    if self.live.error and self.sm.state != State.FAULT:
                        self._handle_fault(
                            f"Ошибка камеры в режиме ручного управления: {self.live.error}"
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
            self._pause_requested.clear()
            try:
                self._stop_pause_frame_loop()
            except Exception as e:
                print(f"[SHUTDOWN] stop pause frame loop failed: {e}")
            self.stages.reset()
            self.live.reset_pause()
            self.live.stop()
            try:
                self.exit_jog()
            except Exception as e:
                print(f"[SHUTDOWN] exit_jog failed: {e}")
            self._safe_emergency_stop()
            self._archive_inflight("runtime_shutdown")
            print("Цикл конвейера завершён.")

    # Fault

    def _handle_fault(self, reason: str):
        self._cancel_motion.set()
        self._pause_requested.clear()
        self._stop_pause_frame_loop()
        self._selected_analysis_active = False
        self._selected_analysis_role = None
        self.stages.reset()
        self.live.reset_pause()
        self.live.stop()
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

    # Safe run

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
        except Exception as e:
            # Повтор неудачного физического шага теряет соответствие
            # деталь/ячейка, поэтому падаем в FAULT на первой же ошибке.
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

    # Статическая фаза шага

    # Core step

    def _run_once(self):
        """Один шаг линии: движение, затухание, съёмка, анализ, публикация.

        Владелец камер меняется только на границах фаз, поэтому кадры для
        defect rules физически не могут быть сняты во время движения.
        """
        self._check_motion_cancelled()
        print(f"\nШАГ {self.current_step + 1}")

        # Право принять INPUT фиксируется до движения: если STOP придёт уже
        # во время проезда, вошедшая этим шагом деталь всё равно будет
        # проинспектирована и останется синхронной со своей ячейкой.
        accept_input_for_this_step = self.sm.accepts_new_parts

        self._last_vision_results = {}
        self._last_rule_results = []

        pending_id = self._stage_motion()
        self._stage_settle(pending_id)
        self._check_pause_barrier()
        frame_runs = self._stage_capture()
        display_frames = self._stage_analysis(
            frame_runs, accept_input_for_this_step,
        )
        self._stage_publish(display_frames)

    def _stage_motion(self):
        """MOTION: подготовить маршрут и переместить ленту на шаг."""
        self.stages.enter_motion()
        # Разметка прошлого шага построена по статичному кадру и на
        # движущемся изображении указывала бы мимо детали.
        self.live.clear_overlays()

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
        # Логическая позиция фиксируется только после подтверждения
        # физического завершения движения.
        self.current_step += 1
        return pending_id

    def _stage_settle(self, pending_id):
        """SETTLE: сброс детали и пауза на затухание вибрации."""
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

        self._set_process(
            "SETTLE",
            "Ожидание затухания вибрации перед съёмкой",
            positions=[self.OFFSET_INPUT, self.OFFSET_SPIDER],
        )
        self.stages.enter_settle()
        self._check_motion_cancelled()

    def _stage_capture(self):
        """CAPTURE: три синхронных набора кадров неподвижной детали."""
        self.stages.enter_capture()

        frame_runs = []
        for run_number in range(1, INSPECTION_RUNS + 1):
            self._set_process(
                "CAMERA_CAPTURE",
                f"Синхронный захват семи камер: прогон "
                f"{run_number}/{INSPECTION_RUNS}",
                positions=[self.OFFSET_INPUT, self.OFFSET_SPIDER],
            )
            frame_runs.append(self.cameras.capture_all())
            self._check_motion_cancelled()
        return frame_runs

    def _stage_analysis(self, frame_runs, accept_input_for_this_step):
        """ANALYSIS: модели и defect rules по уже снятым кадрам."""
        self.stages.enter_analysis()

        # По умолчанию UI получает самый свежий набор. Для каждой реально
        # выполненной стадии он заменяется evidence-кадрами, выбранными
        # majority-алгоритмом как наиболее согласованными с итогом.
        display_frames = dict(frame_runs[-1])

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
        return display_frames

    def _stage_publish(self, display_frames):
        """PUBLISH: маршрут годных деталей и вывод результата на экран."""
        self.stages.enter_publish()

        self._set_process(
            "ROUTE_CHECK",
            "Проверка годной детали на позиции +7",
            positions=[self.OFFSET_REJECT],
        )
        self._pass_good_parts()

        self._set_process("STEP_COMPLETE", "Шаг полностью завершён")
        self._refresh_monitor(display_frames)

    # Пауза в рабочем цикле

    def _check_pause_barrier(self):
        """Пауза после полной остановки шага и до работы нейронок.

        Оператор может поправить линию с помощью jog без ограничений.
        """
        if (
            self.sm.state == State.RUNNING
            and self._pause_requested.is_set()
            and not self.sm.exit_requested
        ):
            if self.sm.request_pause():
                self._enter_pause_frame()
            else:
                self._pause_requested.clear()

        while self.sm.state == State.PAUSED:
            if self.sm.exit_requested or self.sm.force_exit:
                self._pause_requested.clear()
                self._stop_pause_frame_loop()
                self.sm.request_stop()
                break
            if self.live.error:
                self._handle_fault(
                    "Ошибка камеры во время паузы: "
                    f"{self.live.error}"
                )
                break
            jog_error = (
                self.jog.status.get("error")
                if self.jog is not None else None
            )
            if jog_error:
                self._handle_fault(f"Ошибка ручного управления (JOG): {jog_error}")
                break
            self._refresh_monitor()
            time.sleep(0.05)

        if self._pause_frame_active:
            self._stop_pause_frame_loop()

    def _enter_pause_frame(self):
        """Включить режим JOG и отображение состояния паузы."""
        if not self._pause_frame_active:
            self._pause_frame_active = True
        self.enter_jog()
        print("[PAUSE] линия остановлена на границе шага после полной остановки")
        self._set_process(
            "PAUSED",
            "Пауза: доступна ручная коррекция ленты с помощью JOG",
            positions=range(self.OFFSET_REJECT + 1),
        )

    def _stop_pause_frame_loop(self):
        if not self._pause_frame_active:
            return
        self._pause_frame_active = False
        self.exit_jog()

    # Input stage

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

    # Inspection

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

    # Distributor flow

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
                f"инспекцию -> принудительно BAD"
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
                f"[REJECT] #{part.id} -> BAD "
                f"({self.bad_count})"
            )
        elif category == CATEGORY_CLEANUP:
            self.cleanup_count += 1
            print(
                f"[CLEANUP] #{part.id} -> CLEANUP "
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
                f"[PASS] #{part.id} -> GOOD "
                f"({self.good_count})"
            )

    # Archive

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

    # Helpers

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

    def _on_stage_change(self, previous, current, elapsed: float):
        """Печать границы фаз шага: видно, где именно проводится время."""
        print(
            f"[STAGE] {previous.value} -> {current.value} "
            f"(предыдущая фаза {elapsed:.2f} с)"
        )

    def _on_state_change(self, old, new, action: str):
        if new == State.STOPPING:
            self._set_process("DRAINING", "Завершение деталей на линии")
        elif new == State.STOPPED:
            # Линия пуста: последние кадры с разметкой остаются на экране,
            # пока оператор не войдёт в JOG или не запустит цикл заново.
            self.stages.reset()
            self.live.stop()
            self._set_process("STOPPED", "Линия остановлена и пуста")
        elif new == State.FAULT:
            self._set_process("FAULT", "Цикл остановлен из-за ошибки")
        else:
            self._refresh_monitor()

    # JOG mode

    def can_enter_jog(self) -> bool:
        if self.jog is None or self._shutdown:
            return False
        return (
            self.state in self.JOG_ALLOWED_STATES
            and not self.exit_requested
            and not self._operation_lock.locked()
            and not self.live.error
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
            self.live.start()
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
                if not self.sm.is_active:
                    self.live.stop()
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

    # Живой просмотр камер

    def _get_active_camera_role(self):
        server = getattr(self.monitor, "server", None)
        if server is None:
            return None
        return getattr(server, "active_camera_role", None)

    def _current_live_fps(self) -> float:
        return self.live.fps

    # Monitor

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
                "rules": self._rule_report_rows(
                    self._frame_analysis_rule_results
                ),
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

        # Статус собирается из потоков UI, пока цикл меняет линию. Снимок
        # списка и шага берётся один раз, иначе in_line и line_parts могли
        # бы описывать разные моменты времени.
        parts_snapshot = list(self.parts)
        step_snapshot = self.current_step

        line_parts = []
        for part in parts_snapshot:
            position = step_snapshot - part.step_created
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
        jog_error = jog_snapshot.get("error") or self.live.error
        diagnostic_allowed = (
            state_name in ("IDLE", "STOPPED")
            and not parts_snapshot
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
                and not parts_snapshot
                and not jog_busy
                and not jog_error
                and not operation_busy
                and not self._selected_analysis_active
                and not sm_snap["exit_requested"]
            ),
            "stop": state_name in ("RUNNING", "PAUSED") and not operation_busy,
            "pause": (
                state_name == "RUNNING"
                and not operation_busy
                and not sm_snap["exit_requested"]
            ),
            "resume": (
                state_name == "PAUSED"
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
            "step": step_snapshot,
            "in_line": len(parts_snapshot),
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
            "diagnostic_allowed": diagnostic_allowed,
            "diagnostic_busy": operation_busy,
            "controls": controls,
            "selected_analysis": {
                "active": self._selected_analysis_active,
                "role": self._selected_analysis_role,
            },
            # Живой просмотр: активен во время движения, приостановлен на
            # статических этапах, по которым считаются defect rules.
            "live": {
                "running": self.live.running,
                "streaming": self.live.running and not self.stages.static,
                "static": self.stages.static,
                "stage": self.stages.stage.value,
                "fps": self._current_live_fps(),
                "error": self.live.error,
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

    def _refresh_monitor(self, frames: dict | None = None):
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
