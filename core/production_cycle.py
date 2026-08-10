import time
import hashlib
import json
import threading
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from core.live_preview import LivePreview
from core.command_arbiter import CommandArbiter
from core.rule_report import build_rule_report_row, build_rule_report_rows
from core.state_machine import StateMachine, State
from core.step_stages import (
    STAGE_SETTLE_SECONDS,
    STAGE_TRACE_SECONDS,
    StepSequencer,
)
from domain.defect_rules import InputPartPresenceRule
from inspection.consensus import (
    CONSENSUS_MIN_VOTES,
    INSPECTION_RUNS,
    combine_presence_results,
    combine_rule_results,
    describe_picture_run,
    select_picture_run,
    summarize_model_health,
)
from inspection.result import InspectionResult
from core.identity import IdentityCounters
from core.fault import FaultLatch
from inspection.model_worker import terminate_all_workers

from domain.part import (
    Part,
    CATEGORY_GOOD,
    CATEGORY_BAD,
    CATEGORY_CLEANUP,
    CATEGORY_UNKNOWN,
)

RECENT_PARTS_LIMIT = 10
DRAIN_TIMEOUT = 120.0

# Пауза после обработки кадров нейросетями: оператор успевает отсмотреть
# результат анализа до начала следующего шага.
REVIEW_SECONDS = 5.0


class ProductionCycle:
    """
    Оркестратор производственной линии.
    """

    OFFSET_INPUT  = 0
    OFFSET_SPIDER = 4
    # Позиция сортировки: на следующем шаге корпус проходит распределитель.
    # До движения DIST1 выбирает GOOD (0) или передачу на DIST2 (340);
    # DIST2 выбирает BAD (0) или CLEANUP (340).
    OFFSET_REJECT = 7

    JOG_ALLOWED_STATES = ("IDLE", "STOPPED", "PAUSED")

    FRAME_ANALYSIS_GROUPS = ("INPUT", "SPIDER")

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
        review_seconds=REVIEW_SECONDS,
        journal=None,
        initial_frame_max_age: float | None = 5.0,
        manifest: dict | None = None,
        threshold_revision: str | None = None,
    ):
        self.conveyor     = conveyor
        self.cameras      = cameras
        self.inspector    = inspector
        self.distributor  = distributor
        self.monitor      = monitor
        self.archive      = archive
        self.jog          = jog
        self.journal = journal
        self.review_seconds = max(0.0, float(review_seconds))
        self.initial_frame_max_age = (
            None if initial_frame_max_age is None else max(0.0, float(initial_frame_max_age))
        )

        self.distributor.on_state_changed = self._refresh_monitor

        self.sm = StateMachine(on_transition=self._on_state_change)
        self.command_arbiter = CommandArbiter(
            self._handle_command,
            lambda: self.sm.get_snapshot(),
        )

        self.parts: list = []
        # One process owns one batch.  The archive created by main carries the
        # same batch id; a test/offline cycle creates its own identity.
        self.identity = IdentityCounters(
            getattr(archive, "batch_id", None) or None
        )
        self.batch_id = self.identity.batch_id
        self.run_id = None
        self.config_revision = threshold_revision
        self.manifest = dict(manifest or {})
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
        self._frame_analysis_groups = self._empty_frame_analysis_groups()

        self._drain_start_time: float = 0
        self._fault_reason = None
        self._fault_latch = FaultLatch()
        self._operation_lock = threading.Lock()
        self._cancel_motion = threading.Event()
        self.distributor.cancel_check = self._cancel_motion.is_set
        if hasattr(self.conveyor, "cancel_check"):
            self.conveyor.cancel_check = self._cancel_motion.is_set
        self._process_revision = 0
        self._analysis_batch_active = False
        self._review_published = False
        self._review_active = False
        # Compatibility flags for old adapters; production never runs a
        # background presence inference from these fields.
        self._background_presence_thread = None
        self._background_presence_result = None
        self._background_presence_usable = False
        self._background_presence_generation = 0
        # Снимки inspection остаются операторским стоп-кадром до следующего
        # движения, хотя физические камеры уже вернулись в live.
        self._inspection_display_roles = ()
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
        # STOP/EXIT are pending while a movement, inference, persistence or
        # five-second review transaction is active.  They are applied only at
        # the safe boundary after that transaction.
        self._pending_stop = False
        self._pending_exit = False

        # Первый шаг после пуска: сначала контроль того, что уже стоит под
        # камерами, и только потом движение ленты.
        self._await_initial_inspection = False
        self._resume_inspection_only = False
        self._jog_moved = False
        self._snapshot_after = None
        self._snapshot_generation = None
        self._pause_before_settle = False

    def _journal_append(self, event: str, **fields):
        journal = getattr(self, "journal", None)
        if journal is None:
            return
        journal.append(
            event,
            batch_id=self.batch_id,
            run_id=self.run_id,
            current_step=self.current_step,
            **fields,
        )

    # Process telemetry

    def _set_process(
        self,
        phase: str,
        label: str,
        *,
        part_id=None,
        positions=None,
        conveyor_status=None,
        capture_roles=None,
    ):
        self._process_revision += 1
        self._process = {
            "phase": phase,
            "label": label,
            "step": self.current_step,
            "part_id": part_id,
            "positions": list(positions or []),
            "conveyor": dict(conveyor_status or {}),
            # Роли только что захваченных камер. UI использует это, чтобы
            # оператор видел, какая стадия Part действительно снималась.
            "capture_roles": list(capture_roles or []),
            "inspection_roles": list(self._inspection_display_roles),
            "revision": self._process_revision,
            "updated_at": time.time(),
        }
        if not getattr(self, "_analysis_batch_active", False):
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
            "Лента перемещает корпуса на следующую позицию",
            part_id=current.get("part_id"),
            positions=range(self.OFFSET_REJECT + 1),
            conveyor_status=conveyor_info,
        )

    # Public API

    def dispatch_command(self, command_id: str, command: str, *args, **payload):
        """HMI entry point; all mutating commands pass one arbiter."""
        normalized = {
            "ПУСК": "START", "СТОП": "STOP", "ПАУЗА": "PAUSE",
            "ПРОДОЛЖИТЬ": "RESUME", "ВЫХОД": "EXIT",
            "FORCE_EXIT": "FORCE_EXIT",
        }.get(str(command).upper(), str(command).upper())
        if args:
            payload["args"] = args
        return self.command_arbiter.submit(command_id, normalized, **payload)

    def _handle_command(self, command: str, **payload):
        args = tuple(payload.pop("args", ()))
        handlers = {
            "START": self.request_start, "STOP": self.request_stop,
            "PAUSE": self.request_pause, "RESUME": self.request_resume,
            "EXIT": self.request_exit, "FORCE_EXIT": self.request_force_exit,
        }
        if command == "JOG":
            return self.jog_hold_start(*args)
        handler = handlers.get(command)
        if handler is None:
            raise ValueError(f"unsupported command: {command}")
        return handler()

    def _current_threshold_revision(self) -> str | None:
        thresholds = getattr(getattr(self.inspector, "decision", None), "thresholds", None)
        if not isinstance(thresholds, dict):
            return self.config_revision
        encoded = json.dumps(thresholds, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def request_start(self):
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if self._selected_analysis_active:
                return False
            if self.live.error or getattr(getattr(self, "cameras", None), "live_error", None):
                return False
            if self.archive is not None and getattr(self.archive, "enabled", False):
                require_space = getattr(self.archive, "require_space", None)
                if callable(require_space):
                    try:
                        require_space()
                    except Exception as exc:
                        self._handle_fault(f"archive storage gate failed: {exc}")
                        return False
            if self.jog is not None and self.jog.status.get("error"):
                return False
            if self.jog_active:
                print("[JOG] auto-exit on START")
                self.exit_jog()
            # The frame thread may fail while START waits for JOG shutdown.
            if self.live.error or getattr(getattr(self, "cameras", None), "live_error", None):
                return False
            if self.jog is not None and self.jog.status.get("error"):
                return False
            require_camera_health = getattr(getattr(self, "cameras", None), "require_live_health", None)
            if callable(require_camera_health):
                try:
                    require_camera_health()
                except Exception as exc:
                    self._handle_fault(f"camera health gate failed at START: {exc}")
                    return False
            if self.state not in ("IDLE", "STOPPED") or self.parts:
                return False
            self._cancel_motion.clear()
            self._pending_stop = False
            self._pending_exit = False
            # A new run always starts from logical step zero.  It is not a
            # reset of the batch/part counter.
            self.current_step = 0
            self.config_revision = self._current_threshold_revision()
            self.run_id = self.identity.new_run().run_id
            verify_reset = getattr(self.conveyor, "verify_reset_state", None)
            if callable(verify_reset):
                verify_reset()
            self._set_process(
                "START_POSITIONING",
                "Возврат распределителя в рабочее положение",
            )
            try:
                self.distributor.park_production()
            except Exception as exc:
                self._handle_fault(f"Не удалось установить распределитель в рабочее положение: {exc}")
                raise
            accepted = self.sm.request_start(run_id=self.run_id)
            if accepted:
                try:
                    self._journal_append("run_started", threshold_revision=self.config_revision)
                except Exception as exc:
                    self._handle_fault(f"run start journal failed: {exc}")
                    raise
                register_run = getattr(self.archive, "register_run", None)
                if callable(register_run):
                    try:
                        register_run(self.run_id, self.config_revision)
                    except Exception as exc:
                        self._handle_fault(f"batch run manifest failed: {exc}")
                        raise
                self._drain_start_time = 0
                self._fault_reason = None
                self._reset_frame_analysis()
                # Деталь могла остаться под входными камерами ещё до пуска:
                # первый шаг выполняется без движения ленты, чтобы она
                # попала в учёт, а не уехала дальше непроверенной.
                self._await_initial_inspection = True
                if self._diagnostics.get("kind") == "SELECTED_MODEL":
                    self._diagnostics = {
                        "status": "NOT_RUN",
                        "kind": None,
                        "message": "Анализ кадра ещё не выполнялся",
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
        if self.state not in ("RUNNING", "PAUSED"):
            return False
        if self.state == "PAUSED":
            if self._pause_frame_active:
                self._stop_pause_frame_loop()
            return self.sm.request_stop()
        # During a step/review this is a pending command.  It never cancels
        # inference, durable save, or REVIEW.
        if self.stages.stage.value in {"MOTION", "SETTLE", "CAPTURE", "ANALYSIS", "REVIEW", "PUBLISH"}:
            self._pending_stop = True
            return True
        if self._pause_frame_active:
            self._stop_pause_frame_loop()
        return self.sm.request_stop()

    def request_exit(self):
        self._pause_requested.clear()
        if self.state == "FAULT":
            # The HMI labels the same physical button FORCE EXIT in FAULT;
            # no graceful/production exit path exists from a latched fault.
            return self.request_force_exit()
        if self.state == "PAUSED":

            if self._pause_frame_active:
                self._stop_pause_frame_loop()
            self.sm.request_exit()
            if self.sm.state == State.PAUSED:
                self.sm.request_stop()
            return True
        if self.state == "RUNNING" and self.stages.stage.value != "IDLE":
            self._pending_exit = True
            self._pending_stop = True
            return True
        if self._pause_frame_active:
            self._stop_pause_frame_loop()
        return self.sm.request_exit()

    def request_pause(self) -> bool:
        """Запросить паузу после остановки шага и до работы нейронок."""
        if self.state != "RUNNING" or self.exit_requested:
            return False
        if self._pause_requested.is_set():
            return True
        self._invalidate_background_presence()
        # Presence is always recomputed from the post-motion snapshot.
        # После PAUSED этот результат больше не описывает текущую позицию:
        # при продолжении INPUT нужно проверить заново.
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
            # Даже если пауза была включена без JOG, к моменту продолжения
            # live-кадр уже мог устареть. INPUT будет снят заново после
            # возобновления, а не решён по результату до паузы.
            self._invalidate_background_presence()
            self._pause_requested.clear()
            if not getattr(self, "_pause_before_settle", False):
                self._resume_inspection_only = True
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
            # Сброс буфера драйвера: в IDLE/STOPPED после JOG или прогрева
            # cap.read() может вернуть устаревший кадр. См. комментарий
            # в _stage_capture().
            drain = getattr(getattr(self, "cameras", None), "drain_buffers", None)
            if callable(drain) and not getattr(getattr(self, "cameras", None), "live_running", False):
                drain()
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
            # Сброс буфера драйвера: в IDLE/STOPPED после JOG или прогрева
            # cap.read() может вернуть устаревший кадр. См. комментарий
            # в _stage_capture().
            drain = getattr(getattr(self, "cameras", None), "drain_buffers", None)
            if callable(drain) and not getattr(getattr(self, "cameras", None), "live_running", False):
                drain()
            frames = self.cameras.capture_all()
            vision_results = self.inspector.vision.process_all(frames)
            presence_rule = InputPartPresenceRule(
                self.inspector.decision.thresholds
            )
            if not presence_rule.enabled:
                raise RuntimeError("part_presence rule is disabled")
            presence_result = presence_rule.check(vision_results)
            rule_results = [presence_result]
            if not presence_result.details.get("empty_tray"):
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

    def _invalidate_background_presence(self):
        """Clear legacy cache flags; no background production inference exists."""
        self._background_presence_generation += 1
        self._background_presence_usable = False
        self._background_presence_result = None

    def _manual_presence_result(self, vision_results):
        checker = getattr(self.inspector, "_evaluate_part_presence")
        try:
            return checker(vision_results, production=False)
        except TypeError as exc:
            # Offline/test adapters may predate the explicit manual-mode flag;
            # their single-role check is already diagnostic-only.
            if "production" not in str(exc):
                raise
            # Legacy inspector wrappers may call the rule with its production
            # default.  Manual diagnostic mode must still be explicitly
            # single-camera and must not be allowed to fake production data.
            thresholds = getattr(getattr(self.inspector, "decision", None), "thresholds", {})
            return InputPartPresenceRule(thresholds).check(
                vision_results, production=False
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
    def _rule_report_rows(results, role: str | None = None) -> list:
        return build_rule_report_rows(results, role=role)

    def diagnostic_analyze_selected_camera(self, role: str) -> bool:
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if not self._prestart_diagnostic_allowed():
                return False
            available_roles = set(getattr(getattr(self, "cameras", None), "mapping", {}))
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
            # Сброс буфера драйвера: после паузы live cap.read()
            # может вернуть устаревший кадр. См. комментарий
            # в _stage_capture().
            drain = getattr(getattr(self, "cameras", None), "drain_buffers", None)
            if callable(drain) and not getattr(getattr(self, "cameras", None), "live_running", False):
                drain((role,))

            self._selected_analysis_active = True
            self._selected_analysis_role = role
            self._set_diagnostic_running(
                "SELECTED_MODEL",
                f"Анализ кадра выбранной камеры: {role}",
            )
            self._set_process(
                "SELECTED_MODEL_ANALYSIS",
                f"Анализ кадра {role}",
            )

            decision = self.inspector.decision
            decision_rules = decision.rules_for_role(role)
            if not decision_rules:
                raise RuntimeError(
                    f"Для камеры {role} нет активных правил анализа"
                )

            frame_runs = []
            vision_runs = []
            presence_runs = []
            rule_results_by_run = []
            raw_model_health = []
            detection_counts = []

            is_input = role in self.inspector.INPUT_ROLES

            for run_number in range(1, INSPECTION_RUNS + 1):
                self._set_process(
                    "SELECTED_MODEL_ANALYSIS",
                    f"{role}: свежий кадр",
                )
                frame = self.cameras.capture_single(role)
                stage_frames = {role: frame}
                vision_results = self.inspector.vision.process_all(stage_frames)
                if role not in vision_results:
                    raise RuntimeError(
                        f"Модели не вернули результат камеры {role}"
                    )

                frame_runs.append(frame)
                vision_runs.append(vision_results)
                detection_counts.append(len(vision_results.get(role, [])))

                if is_input:
                    presence_runs.append(
                        self._manual_presence_result(vision_results)
                    )

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

            if is_input:
                presence_result, presence_vote, presence_evidence = (
                    combine_presence_results(presence_runs)
                )

                if presence_result.details.get("empty_tray"):
                    # Пустой лоток: defect-правила не выполняются (проверять
                    # нечего), итог — только по part_presence. Важно:
                    # combine_rule_results здесь вызывать нельзя — прогонов
                    # defect-правил нет (0).
                    rule_results = []
                    evidence_index = presence_evidence
                    consensus = {
                        "runs": INSPECTION_RUNS,
                        "required_votes": CONSENSUS_MIN_VOTES,
                        "evidence_run": presence_evidence + 1,
                        "part_presence": presence_vote,
                        "rules": {},
                    }
                    # Пустой лоток: картинка по самому пограничному flatness
                    # (ближайший к порогу ложных срабатываний замер).
                    picture_index = select_picture_run([presence_result])
                    if picture_index is None:
                        picture_index = evidence_index
                    consensus["picture_run"] = picture_index + 1
                    consensus["picture_reason"] = describe_picture_run(
                        [presence_result], picture_index,
                    )
                    evidence_index = picture_index
                else:
                    for v_res, frame in zip(vision_runs, frame_runs):
                        rule_results_by_run.append(
                            decision.evaluate_rules_detailed(
                                decision_rules,
                                v_res,
                                frames={role: frame},
                            )
                        )
                    rule_results, consensus, evidence_index = (
                        combine_rule_results(rule_results_by_run)
                    )
                    consensus["part_presence"] = presence_vote
            else:
                presence_result = None
                for v_res, frame in zip(vision_runs, frame_runs):
                    rule_results_by_run.append(
                        decision.evaluate_rules_detailed(
                            decision_rules,
                            v_res,
                            frames={role: frame},
                        )
                    )
                rule_results, consensus, evidence_index = (
                    combine_rule_results(rule_results_by_run)
                )

            # Картинка — по замеру, ближе всего к порогу (в норме), либо
            # ближайшему к порогу браку.
            if is_input:
                picture_candidates = [presence_result] + list(rule_results)
            else:
                picture_candidates = rule_results
            picture_index = select_picture_run(picture_candidates)
            if picture_index is None:
                picture_index = evidence_index
            consensus["picture_run"] = picture_index + 1
            consensus["picture_reason"] = describe_picture_run(
                picture_candidates, picture_index,
            )
            evidence_index = picture_index

            evidence_frame = frame_runs[evidence_index]
            vision_results = vision_runs[evidence_index]
            stage_frames = {role: evidence_frame}
            model_rows = summarize_model_health(raw_model_health)
            if not model_rows or any(not row.get("ok") for row in model_rows):
                raise RuntimeError(
                    f"Нет полного комплекта model health "
                    f"{INSPECTION_RUNS}/{INSPECTION_RUNS} для камеры {role}"
                )

            rule_rows = []
            if is_input:
                rule_rows.append(self._rule_report_row(presence_result))

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
            self._diagnostics = {
                "status": "PASSED",
                "kind": "SELECTED_MODEL",
                "message": (
                    f"{role}: свежий кадр; моделей {len(model_rows)}; "
                    f"правил {len(rule_rows)}; объекты "
                    + "/".join(str(value) for value in detection_counts)
                ),
                "selected_role": role,
                "cameras": camera_rows,
                "models": model_rows,
                "rules": rule_rows,
                "consensus": consensus,
                "picture_run": (
                    int(consensus.get("picture_run"))
                    if consensus and consensus.get("picture_run") else None
                ),
                "picture_reason": (
                    str(consensus.get("picture_reason"))
                    if consensus and consensus.get("picture_reason") else None
                ),
                "updated_at": time.time(),
            }
            self._set_process(
                "SELECTED_MODEL_READY",
                f"Анализ кадра {role} завершён; поток приостановлен",
            )
            # Разметка кадра — по правилам этого кадра. Публикуется тем же
            # вызовом, что и кадры: единый снимок.
            self._refresh_monitor(
                stage_frames,
                run_frames=[{role: frame_runs[index]}
                            for index in range(INSPECTION_RUNS)],
                run_rule_results=(
                    rule_results_by_run
                    if rule_results_by_run
                    else [[]]
                ),
            )
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
            self._reset_frame_analysis()
            self._diagnostics = {
                "status": "NOT_RUN",
                "kind": None,
                "message": "Анализ кадра не выполнялся",
                "cameras": [],
                "models": [],
                "rules": [],
                "updated_at": None,
            }
            self.live.resume()
            # Убрать геометрию анализа с экрана: разметка построена по
            # статичному кадру и на движущемся изображении указывала бы
            # мимо детали (эффект маркера на лобовом стекле).
            self.live.clear_overlays()
            try:
                fresh_frames = self.cameras.capture_all()
                # Публикуем свежие кадры без оверлеев — возврат к живому виду.
                # Три кадра анализа больше неактуальны: очищаем.
                self._refresh_monitor(fresh_frames, run_frames=[])
            except Exception:
                # Если захват недоступен (камеры заняты / ошибка), хотя бы
                # гарантируем очистку оверлеев и обновление статуса.
                self._refresh_monitor(run_frames=[])
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
        terminate_all_workers()
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

                    if (self.live.error or getattr(getattr(self, "cameras", None), "live_error", None)) and self.sm.state != State.FAULT:
                        self._handle_fault(
                            f"Ошибка камеры в режиме ручного управления: {self.live.error or getattr(getattr(self, 'cameras', None), 'live_error', None)}"
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
        terminate_all_workers()
        self._stop_pause_frame_loop()
        self._selected_analysis_active = False
        self._selected_analysis_role = None
        self._analysis_batch_active = False
        self.stages.reset()
        self.live.reset_pause()
        # Physical CameraManager live readers remain active until application
        # close, including FAULT.  HMI may continue showing healthy roles.
        latch = getattr(self, "_fault_latch", None)
        if latch is None:
            latch = FaultLatch()
            self._fault_latch = latch
        if latch.root is None:
            lower_reason = str(reason).lower()
            code = "PRODUCTION_FAULT"
            for marker, candidate in (
                ("camera", "CAMERA_HEALTH"),
                ("model", "MODEL_WORKER"),
                ("rule", "RULE_EXECUTION"),
                ("archive", "ARCHIVE_TRACEABILITY"),
                ("journal", "OPERATIONAL_LOG"),
                ("conveyor", "CONVEYOR_TELEMETRY"),
                ("distributor", "DISTRIBUTOR_TELEMETRY"),
                ("storage", "DISK_RESERVE"),
            ):
                if marker in lower_reason:
                    code = candidate
                    break
            root = latch.latch(
                code,
                reason,
                phase=self._process.get("phase") if hasattr(self, "_process") else None,
                details={"traceback": traceback.format_exc()},
            )
            self._fault_reason = root.message
        else:
            latch.add_secondary("SECONDARY_FAULT", reason, phase=self._process.get("phase"))
        print(f"[FAULT] {self._fault_reason}")
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
        self._set_process("FAULT", self._fault_reason)
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
                    f"на линии осталось корпусов: {len(self.parts)}"
                )
                return

        try:
            if self.archive is not None and getattr(self.archive, "enabled", False):
                require_space = getattr(self.archive, "require_space", None)
                if callable(require_space):
                    require_space()
            camera_health = getattr(getattr(self, "cameras", None), "require_live_health", None)
            if callable(camera_health):
                camera_health()
            if getattr(getattr(self, "cameras", None), "live_error", None):
                raise RuntimeError(
                    f"camera health failed: {getattr(getattr(self, 'cameras', None), 'live_error', None)}"
                )
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
        # A watchdog/UI pause that arrives between steps must be applied
        # before the next physical command, not after it.
        if self._pause_requested.is_set() and self.sm.state == State.RUNNING:
            self._check_pause_barrier()
        print(f"\nШАГ {self.current_step + 1}")

        # Право принять INPUT фиксируется до движения: если STOP придёт уже
        # во время проезда, вошедшая этим шагом деталь всё равно будет
        # проинспектирована и останется синхронной со своей ячейкой.
        accept_input_for_this_step = self.sm.accepts_new_parts

        self._last_vision_results = {}
        self._last_rule_results = []

        # A resume after PAUSED is a stationary re-inspection of the same
        # logical step.  It must not issue another production G3.
        stationary_resume = bool(getattr(self, "_resume_inspection_only", False))
        self._resume_inspection_only = False
        if stationary_resume:
            self.stages.reset()
            self.stages.enter_motion()
            self._pending_drop = None
            self._initial_inspection_active = False
            pending_id = None
        else:
            initial_inspection = self._await_initial_inspection
            self._initial_inspection_active = bool(initial_inspection)
            pending_id = self._stage_motion()

        # Presence is evaluated from the immutable post-motion snapshot.
        # A pre-settle/live optimisation is forbidden: it could create an
        # empty decision from a different frame than the other INPUT rules.
        # A PAUSE requested during movement is applied before SETTLE and
        # snapshots.  STOP remains pending and the already issued movement is
        # still completed/confirmed.
        self._pause_before_settle = True
        self._check_pause_barrier()
        self._pause_before_settle = False
        self._stage_settle(
            pending_id,
            settle=(not stationary_resume or bool(getattr(self, "_jog_moved", False))),
        )
        self._jog_moved = False
        frame_runs = self._stage_capture()
        display_frames = self._stage_analysis(
            frame_runs, accept_input_for_this_step,
        )
        self._stage_review(display_frames)
        self._stage_publish(display_frames)
        self._apply_pending_at_boundary()

    def _apply_pending_at_boundary(self):
        """Apply STOP/PAUSE only after analysis, persistence and REVIEW."""
        if self._pending_stop or self._pending_exit:
            self._pending_stop = False
            if self._pending_exit:
                self._pending_exit = False
                self.sm.request_exit()
            else:
                self.sm.request_stop()
            self._pause_requested.clear()
            return
        if self._pause_requested.is_set() and self.sm.state == State.RUNNING:
            if self.sm.request_pause():
                self._enter_pause_frame()
            else:
                self._pause_requested.clear()

    def _stage_motion(self):
        """MOTION: подготовить маршрут и переместить ленту на шаг."""
        self.stages.enter_motion()
        self._inspection_display_roles = ()
        self._review_published = False
        # Разметка прошлого шага построена по статичному кадру и на
        # движущемся изображении указывала бы мимо детали.
        self.live.clear_overlays()

        if self._await_initial_inspection:
            # Деталь уже стоит под входными камерами: сначала её контроль,
            # движение ленты начнётся со следующего шага. Счётчик шагов не
            # увеличивается — физическая позиция не изменилась.
            self._await_initial_inspection = False
            self._set_process(
                "INITIAL_INSPECTION",
                "Корпус уже под камерами: контроль без движения ленты",
                positions=[self.OFFSET_INPUT, self.OFFSET_SPIDER],
            )
            self._check_motion_cancelled()
            return None

        verify_reset = getattr(self.conveyor, "verify_reset_state", None)
        if callable(verify_reset):
            verify_reset()
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
        # The journal boundary is before the physical command.  If it fails,
        # no G3 is allowed to leave the process.
        self._journal_append(
            "motion_intent",
            part_id=pending_id,
            route=(self._pending_drop.route_category if self._pending_drop else None),
            target=getattr(self.conveyor, "production_target", 38096),
        )
        self.conveyor.move_step()
        self.conveyor.wait_stop(progress_callback=self._on_conveyor_progress)
        self._check_motion_cancelled()
        self._journal_append(
            "motion_confirmed",
            part_id=pending_id,
            target=getattr(self.conveyor, "production_target", 38096),
        )
        # Логическая позиция фиксируется только после полного telemetry
        # confirmation, exactly once.
        self.current_step += 1
        return pending_id

    def _stage_settle(self, pending_id, settle: bool = True):
        """SETTLE: confirm transfer and optionally wait for vibration."""
        self._set_process(
            "CONVEYOR_CONFIRMED", "Позиции корпусов подтверждены контроллером",
            part_id=pending_id, positions=range(self.OFFSET_REJECT + 1),
        )
        if self._pending_drop is not None:
            self._set_process(
                "PART_TRANSFER", "Корпус прошёл распределитель",
                part_id=pending_id, positions=[self.OFFSET_REJECT],
            )
        self._execute_drop()
        self._check_motion_cancelled()
        active_cam_positions = []
        if self.sm.accepts_new_parts: active_cam_positions.append(self.OFFSET_INPUT)
        if any(p.step_created + self.OFFSET_SPIDER == self.current_step for p in self.parts):
            active_cam_positions.append(self.OFFSET_SPIDER)
        self._set_process("SETTLE", "Ожидание затухания вибрации перед съёмкой", positions=active_cam_positions)
        self.stages.enter_settle(wait=settle)
        # Freshness starts only after confirmed stop and complete SETTLE.  The
        # initial START inspection may use the last live frame within its age
        # limit, while every later snapshot must cross this boundary.
        if getattr(self, "_initial_inspection_active", False):
            # A pre-START JOG may have invalidated the previous live frame;
            # otherwise the last frame is allowed within initial_frame_max_age.
            if getattr(self, "_snapshot_after", None) is None:
                self._snapshot_after = None
        else:
            boundary = getattr(getattr(self, "cameras", None), "freshness_boundary", None)
            self._snapshot_after = boundary() if callable(boundary) else None
        self._check_motion_cancelled()

    def _capture_roles_for_current_step(self) -> tuple[str, ...]:
        """Вернуть только камеры, под которыми сейчас есть корпус.

        Один Part собирается в два разных момента: INPUT получает 2 кадра
        на +0, SPIDER/TOP — 5 кадров того же Part на +4. Захватывать все
        семь камер без корпуса под соответствующей группой нельзя: это
        расходует USB-полосу и создаёт ложные точки отказа.
        """
        roles = []

        input_needed = self.sm.accepts_new_parts
        if input_needed:
            roles.extend(self.inspector.INPUT_ROLES)
        if any(
            part.step_created + self.OFFSET_SPIDER == self.current_step
            for part in self.parts
        ):
            roles.extend(self.inspector.SPIDER_ROLES)
        return tuple(roles)

    def _stage_capture(self):
        """CAPTURE: свежие кадры только для занятых инспекционных позиций."""
        roles = self._capture_roles_for_current_step()
        self._inspection_display_roles = roles
        # Пауза только у ролей, которые сейчас дают inspection-кадр.
        # Остальные камеры продолжают live-поток для оператора.
        self.stages.enter_capture(roles)
        active_cam_positions = []
        if self.sm.accepts_new_parts:
            active_cam_positions.append(self.OFFSET_INPUT)
        if any(part.step_created + self.OFFSET_SPIDER == self.current_step for part in self.parts):
            active_cam_positions.append(self.OFFSET_SPIDER)

        self._set_process(
            "CAMERA_CAPTURE",
            (f"Синхронный захват камер: {', '.join(roles)}" if roles
             else "Нет корпуса под инспекционными камерами"),
            positions=active_cam_positions,
            capture_roles=roles,
        )
        if not roles:
            return [{}]

        # Драйвер может отдать старый кадр из буфера после движения. Дренируем
        # только нужные роли, затем получаем один свежий набор именно для
        # соответствующей стадии Part.
        drain = getattr(getattr(self, "cameras", None), "drain_buffers", None)
        if callable(drain) and not getattr(getattr(self, "cameras", None), "live_running", False):
            # Legacy/offline adapters without a continuous buffer need a
            # driver drain.  Production CameraManager never steals a frame
            # from the live reader.
            drain(roles=roles)
        capture_roles = getattr(getattr(self, "cameras", None), "capture_roles", None)
        if callable(capture_roles):
            try:
                after = getattr(self, "_snapshot_after", None)
                max_age = (
                    getattr(self, "initial_frame_max_age", None)
                    if getattr(self, "_initial_inspection_active", False) else None
                )
                if after is None and max_age is None:
                    frames = capture_roles(roles)
                else:
                    frames = capture_roles(roles, after=after, max_age=max_age)
            except TypeError:
                # Test doubles/legacy adapters expose the old positional API;
                # they still receive the one immutable capture set.
                frames = capture_roles(roles)
        else:
            # Совместимость со старыми test doubles; production CameraManager
            # всегда предоставляет capture_roles().
            frames = self.cameras.capture_all()
            frames = {role: frames[role] for role in roles}
        if set(frames) != set(roles):
            raise RuntimeError(
                f"Неполный набор кадров для инспекции: ожидались {sorted(roles)}, "
                f"получены {sorted(frames)}"
            )
        self._snapshot_generation = getattr(getattr(self, "cameras", None), "live_generation", None)
        self._check_motion_cancelled()
        # Нейросети используют только frames в памяти.  Physical CameraManager
        # continues reading in the background; the UI gate remains held until
        # REVIEW/PUBLISH has finished so the inspected roles stay frozen.
        # Do not publish a stop-frame here.  During capture and inference HMI
        # remains live; the immutable snapshot becomes visible only in the
        # atomic analysis publication below.
        return [frames]

    def _stage_analysis(self, frame_runs, accept_input_for_this_step):
        """Run independent INPUT and CONTROL transactions in parallel.

        The frame set is copied once after the freshness boundary.  Both
        workers receive that immutable-by-convention set; neither worker may
        capture another frame or retry inference.  CONTROL is submitted even
        when INPUT later reports defects.
        """
        self.stages.enter_analysis()
        self._review_active = False
        display_frames = dict(frame_runs[-1]) if frame_runs else {}
        spider_parts = [
            part for part in self.parts
            if part.step_created + self.OFFSET_SPIDER == self.current_step
        ]
        active_positions = []
        if accept_input_for_this_step:
            active_positions.append(self.OFFSET_INPUT)
        if spider_parts:
            active_positions.append(self.OFFSET_SPIDER)
        self._set_process(
            "ANALYSIS",
            "Параллельная проверка INPUT и CONTROL по одному snapshot",
            positions=active_positions,
        )

        input_future = None
        spider_future = None
        self._analysis_batch_active = True
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="inspection") as pool:
            if accept_input_for_this_step:
                input_future = pool.submit(self._process_input_stage, frame_runs)
            if spider_parts:
                spider_future = pool.submit(self._run_spider_inspection, frame_runs)

            input_result = None
            spider_result = None
            input_error = spider_error = None
            if input_future is not None:
                try:
                    input_result = input_future.result()
                except Exception as exc:
                    input_error = exc
            if spider_future is not None:
                try:
                    spider_result = spider_future.result()
                except Exception as exc:
                    spider_error = exc
            if input_error is not None:
                raise input_error
            if spider_error is not None:
                raise spider_error

        current_generation = getattr(getattr(self, "cameras", None), "live_generation", None)
        if (
            getattr(self, "_snapshot_generation", None) is not None
            and current_generation is not None
            and current_generation != getattr(self, "_snapshot_generation", None)
        ):
            raise RuntimeError(
                "camera stream recovered during inspection; snapshot is no longer current"
            )
        self._analysis_batch_active = False
        run_frames = []
        run_rule_results = []
        input_is_empty = bool(input_result is not None and input_result.is_empty_tray)
        if input_result is not None and not input_is_empty:
            display_frames.update(input_result.raw_frames)
            run_frames = self._merge_run_frames(
                run_frames, getattr(input_result, "run_frames", None) or []
            )
            run_rules = getattr(input_result, "run_rule_results", None) or []
            if run_rules:
                run_rule_results = self._merge_run_rule_rows(run_rule_results, run_rules)
        elif input_is_empty:
            release_roles = getattr(self.stages, "release_roles", None)
            if callable(release_roles):
                release_roles(self.inspector.INPUT_ROLES)
            for role in self.inspector.INPUT_ROLES:
                display_frames.pop(role, None)
            self._inspection_display_roles = tuple(
                role for role in self._inspection_display_roles
                if role not in self.inspector.INPUT_ROLES
            )

        if spider_result is not None:
            display_frames.update(spider_result.raw_frames)
            run_frames = self._merge_run_frames(
                run_frames, getattr(spider_result, "run_frames", None) or []
            )
            run_rules = getattr(spider_result, "run_rule_results", None) or []
            if run_rules:
                run_rule_results = self._merge_run_rule_rows(run_rule_results, run_rules)

        # Atomic publication of all stage results and lightweight line state.
        if run_frames:
            self._review_active = True
            # This is the single publication boundary at which the UI may
            # replace live pixels with immutable inspection evidence.
            self._review_published = True
            self._refresh_monitor(
                display_frames,
                run_frames=run_frames,
                run_rule_results=run_rule_results,
            )
        return display_frames

    @staticmethod
    def _merge_run_frames(acc: list, incoming: list) -> list:
        """Слить наборы кадров по номерам прогонов (INPUT + SPIDER)."""
        if not incoming:
            return acc
        merged = []
        for index in range(INSPECTION_RUNS):
            base = dict(acc[index]) if index < len(acc) else {}
            if index < len(incoming) and isinstance(incoming[index], dict):
                base.update(incoming[index])
            merged.append(base)
        return merged

    @staticmethod
    def _merge_run_rule_rows(acc: list, incoming: list) -> list:
        """Слить правила по прогонам (INPUT + SPIDER) для разметки кадров."""
        if not incoming:
            return acc
        merged = []
        for index in range(INSPECTION_RUNS):
            base = list(acc[index]) if index < len(acc) else []
            if index < len(incoming) and isinstance(incoming[index], list):
                base.extend(incoming[index])
            merged.append(base)
        return merged

    def _stage_review(self, display_frames):
        """REVIEW: пауза на просмотр работы нейросетей после анализа.

        Кадры со статичной разметкой уже опубликованы и остаются на
        экране, а лента стоит: оператор успевает отсмотреть результат
        до начала следующего шага. Паузу можно прервать остановкой или
        выходом из программы.
        """
        if not getattr(self, "_review_active", False):
            return
        current_generation = getattr(getattr(self, "cameras", None), "live_generation", None)
        if (
            getattr(self, "_snapshot_generation", None) is not None
            and current_generation is not None
            and current_generation != getattr(self, "_snapshot_generation", None)
        ):
            raise RuntimeError("camera stream recovered before REVIEW; evidence must be recaptured")
        enter_review = getattr(self.stages, "enter_review", None)
        if callable(enter_review):
            enter_review()
        if self.review_seconds <= 0:
            return
        self._refresh_monitor(display_frames)
        deadline = time.monotonic() + self.review_seconds
        shown_seconds = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if (
                self._cancel_motion.is_set()
                or self.sm.force_exit
                or self.sm.exit_requested
                or self.sm.state != State.RUNNING
            ):
                break
            current_generation = getattr(getattr(self, "cameras", None), "live_generation", None)
            if (
                getattr(self, "_snapshot_generation", None) is not None
                and current_generation is not None
                and current_generation != getattr(self, "_snapshot_generation", None)
            ):
                raise RuntimeError("camera stream recovered during REVIEW")
            whole = int(remaining + 0.999)
            if whole != shown_seconds:
                shown_seconds = whole
                self._set_process(
                    "ANALYSIS_REVIEW",
                    "Просмотр результатов анализа: "
                    f"{whole} с до следующего шага",
                    positions=[self.OFFSET_INPUT, self.OFFSET_SPIDER],
                )
            time.sleep(min(0.1, max(remaining, 0.01)))
        # FORCE EXIT во время паузы сбрасывает цепочку фаз: выходить нужно
        # штатной ошибкой отмены до входа в PUBLISH, а не сбросом шага.
        self._check_motion_cancelled()

    def _stage_publish(self, display_frames):
        """PUBLISH: clear REVIEW and return inspected roles to clean live."""
        self.stages.enter_publish()
        release_capture = getattr(self.stages, "release_capture_roles", None)
        if callable(release_capture):
            release_capture()
        self._review_published = False
        self._review_active = False
        self._inspection_display_roles = ()
        # Snapshot evidence remains in Part/archive; the UI overlay does not.
        self.live.clear_overlays()
        self._reset_frame_analysis()
        self._set_process("STEP_COMPLETE", "Шаг полностью завершён")
        self._refresh_monitor()

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
            camera_error = self.live.error or getattr(getattr(self, "cameras", None), "live_error", None)
            if camera_error:
                self._handle_fault(
                    "Ошибка камеры во время паузы: "
                    f"{camera_error}"
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
        """Обработать INPUT по свежему кадру."""

        candidate_id = self.part_counter + 1

        self._set_process(
            "INPUT_ANALYSIS",
            f"Вход: анализ кандидата #{candidate_id}",
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
                "Inspector не поддерживает обязательную INPUT инспекцию"
            )
        part_holder = {}

        def on_presence(presence_result):
            self.part_counter += 1
            part = Part(
                self.part_counter,
                self.current_step,
                batch_id=self.batch_id,
            )
            self._journal_append(
                "part_created",
                part_id=part.id,
                birth_step=part.birth_step,
                presence=dict(presence_result.details),
            )
            part.threshold_revision = self.config_revision
            part.part_manifest = dict(self.manifest)
            part.inspection_consensus["input_presence"] = {
                "presence_by_role": dict(
                    presence_result.details.get("presence_by_role", {})
                ),
                "count_by_role": dict(
                    presence_result.details.get("count_by_role", {})
                ),
            }
            self.parts.append(part)
            part_holder["part"] = part

        try:
            result = inspect_consensus(
                part_id=candidate_id,
                step=self.current_step,
                frame_runs=frame_runs,
                force_bad=self.force_all_bad,
                on_presence=on_presence,
            )
        except TypeError as exc:
            if "on_presence" not in str(exc):
                raise
            # Legacy/offline Inspector adapters have no identity callback;
            # retain the old post-result creation path for them only.
            result = inspect_consensus(
                part_id=candidate_id,
                step=self.current_step,
                frame_runs=frame_runs,
                force_bad=self.force_all_bad,
            )
        if result.is_empty_tray:
            self.empty_count += 1
            # Empty cells have no Part/evidence/history/archive entry.  The
            # transient "empty" label is published by the current process
            # status only.
            self._frame_analysis_groups["INPUT"] = self._empty_frame_analysis_entry()
            # Очищаем детекции для входных камер, чтобы не рисовать прямоугольники
            # на пустом лотке.
            for role in self.inspector.INPUT_ROLES:
                self._last_vision_results[role] = []
            self._last_rule_results.extend(result.rule_results)
            print(
                f"[EMPTY] Пустой лоток на step {self.current_step} "
                f"(total empty: {self.empty_count})"
            )
            # Пустой лоток остаётся нейтральным: Part и архив не создаются.
            return result

        part = part_holder.get("part")
        if part is None:
            # Fallback for old Inspector doubles; production Inspector calls
            # on_presence before ordinary INPUT rules.
            on_presence(
                type("Presence", (), {"details": {}})()
            )
            part = part_holder["part"]
        part.threshold_revision = self.config_revision
        part.part_manifest = dict(self.manifest)
        part.inspection_consensus["input"] = dict(result.consensus)
        self._record_frame_analysis("INPUT", part.id, result)
        print(f"[INPUT] Деталь #{part.id}")

        for defect in result.defects:
            part.add_input_defect(defect)
        part.mark_input_done()
        self._journal_append("input_completed", part_id=part.id, defects=list(result.defects))

        self._last_vision_results.update(result.vision_results)
        self._last_rule_results.extend(result.rule_results)

        if self.archive:
            self.archive.store_frames(
                part_id=part.id,
                stage="input",
                raw_frames=result.raw_frames,
                annotated_frames=result.annotated,
                raw_overlay_frames=result.raw_overlay_frames,
                run_frames=getattr(result, "run_frames", None),
                run_rule_results=getattr(result, "run_rule_results", None),
                run_vision_results=getattr(result, "run_vision_results", None),
            )

        print(
            f"[INPUT] Деталь #{part.id} "
            f"дефекты: {result.defects or ['none']}"
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
                f"Контроль корпуса #{part.id}",
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
                    "Inspector не поддерживает обязательную SPIDER инспекцию"
                )
            result = inspect_consensus(
                part_id=part.id,
                step=self.current_step,
                frame_runs=frame_runs,
                force_bad=self.force_all_bad,
            )
            self._record_frame_analysis("SPIDER", part.id, result)
            part.inspection_consensus["spider"] = dict(result.consensus)

            for defect in result.defects:
                part.add_spider_defect(defect)

            part.mark_spider_done()
            self._journal_append("control_completed", part_id=part.id, defects=list(result.defects))

            self._last_vision_results.update(result.vision_results)
            self._last_rule_results.extend(result.rule_results)

            if self.archive:
                self.archive.store_frames(
                    part_id=part.id,
                    stage="spider",
                    raw_frames=result.raw_frames,
                    annotated_frames=result.annotated,
                    raw_overlay_frames=result.raw_overlay_frames,
                    run_frames=getattr(result, "run_frames", None),
                    run_rule_results=getattr(result, "run_rule_results", None),
                    run_vision_results=getattr(result, "run_vision_results", None),
                )

            print(
                f"[SPIDER] Деталь #{part.id} "
                f"дефекты: {result.defects or ['none']} "
                f"категория={part.route_category}"
            )
            return result
        # На позиции +4 в этом шаге детали нет: старый результат другой
        # детали показывать нельзя.
        self._frame_analysis_groups["SPIDER"] = self._empty_frame_analysis_entry()
        return None

    # Distributor flow

    def _find_pending_drop(self):
        """Вернуть корпус на +7, который на следующем шаге пройдёт заслонки."""
        for part in self.parts:
            if part.step_created + self.OFFSET_REJECT == self.current_step:
                return part
        return None

    def _prepare_drop(self):
        part = self._pending_drop
        if part is None:
            self.distributor.reset_target()
            return
        category = part.route_category
        if category in (CATEGORY_UNKNOWN, "IN_PROGRESS") or (
            not part.fully_inspected and not category
        ):
            missing = []
            if not part.input_inspected:
                missing.append("INPUT")
            if not part.spider_inspected:
                missing.append("CONTROL")
            part.mark_incomplete_inspection("/".join(missing) or "evidence")
            category = CATEGORY_BAD
            print(
                f"[FAIL-SAFE] Деталь #{part.id} достигла +7 без полного "
                f"контроля: {missing or ['data']} -> BAD"
            )
        # GOOD: DIST1=0. BAD/CLEANUP: сначала DIST2, затем DIST1=340.
        self._journal_append("route_selected", part_id=part.id, category=category)
        self.distributor.prepare_route(category, part.id)

    def _execute_drop(self):
        part = self._pending_drop
        if part is None:
            return
        category = part.route_category
        self.distributor.confirm_transfer(part.id, category)
        # The physical transfer has completed.  Update physical counters and
        # tracking before any diagnostic journal/archive write can fail; a
        # failure below is a traceability FAULT, never a transfer retry.
        if category == CATEGORY_GOOD:
            self.good_count += 1
            print(f"[PASS] #{part.id} -> GOOD ({self.good_count})")
        elif category == CATEGORY_BAD:
            self.bad_count += 1
            print(f"[REJECT] #{part.id} -> BAD ({self.bad_count})")
        elif category == CATEGORY_CLEANUP:
            self.cleanup_count += 1
            print(f"[CLEANUP] #{part.id} -> CLEANUP ({self.cleanup_count})")
        self._remove_part(part)
        self._pending_drop = None
        try:
            self._journal_append("transfer_confirmed", part_id=part.id, category=category)
            # Physical truth is committed before optional persistence.  If
            # archive finalization fails, this part is not put back on the
            # line and the caller enters FAULT without retrying a route/motion.
            self._archive_part(part)
            self._journal_append("archive_finalized", part_id=part.id, category=category)
        finally:
            # Even a traceability failure must leave a lightweight recent-part
            # record for the physically confirmed item.
            if not any(item.get("id") == part.id for item in self.recent_parts):
                self._register_finished(part)

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
        archive_extra = {
            "run_id": self.run_id,
            "birth_step": getattr(part, "birth_step", part.step_created),
            "confirmed_current_step": self.current_step,
            "identity": {
                "batch_id": self.batch_id,
                "run_id": self.run_id,
                "manifest": dict(self.manifest),
                "threshold_revision": self.config_revision,
            },
        }
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
        record = {
            "id":       part.id,
            "part_id":  part.id,
            "batch_id": getattr(self, "batch_id", getattr(self.archive, "batch_id", None)),
            "birth_step": getattr(part, "birth_step", part.step_created),
            "decision": part.final_decision,
            "category": part.route_category,
            "time":      time.time(),
        }
        # UI получает только лёгкую ссылку на архивную запись. Само
        # изображение не копируется в recent-кэш и не исчезает из архива,
        # когда деталь покидает последние десять.
        if self.archive:
            archive_info = self.archive.get_part_info(part.id)
            if archive_info:
                record["batch_id"] = self.archive.batch_id
                record["archive_folder"] = archive_info.get("relative_folder")
                record["manifest"] = archive_info.get("manifest")
                record["annotation_files"] = list(
                    archive_info.get("annotation_files") or []
                )
                record["sample_count"] = int(
                    archive_info.get("sample_count") or 0
                )
        self.recent_parts.append(record)

    # Анализ кадра по группам камер (ВХОД / КОНТРОЛЬ +4)

    def _empty_frame_analysis_entry(self) -> dict:
        return {
            "part_id": None,
            "rule_results": [],
            "models": [],
            "picture_run": None,
            "picture_reason": None,
            "updated_at": None,
        }

    def _empty_frame_analysis_groups(self) -> dict:
        return {
            group: self._empty_frame_analysis_entry()
            for group in self.FRAME_ANALYSIS_GROUPS
        }

    def _reset_frame_analysis(self):
        self._frame_analysis_groups = self._empty_frame_analysis_groups()

    def _record_frame_analysis(self, group: str, part_id, result):
        """Сохранить итог стадии в клетку её группы камер.

        Правая панель UI показывает анализ выбранной оператором камеры,
        поэтому результаты хранятся раздельно для ВХОДА и КОНТРОЛЯ +4.
        """
        rows = getattr(result, "model_health", None)
        if not isinstance(rows, list) or not rows:
            vision = getattr(self.inspector, "vision", None)
            rows = getattr(vision, "last_health", None) or []
        consensus = getattr(result, "consensus", None) or {}
        
        # Подготовить модели с детальной информацией о прогоне
        model_details = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            model_details.append({
                "role": item.get("role"),
                "model": item.get("model"),
                "ok": item.get("ok"),
                "runs": item.get("runs"),
                "elapsed_ms": item.get("elapsed_ms"),
                "elapsed_total_ms": item.get("elapsed_total_ms"),
                "detections": item.get("detections"),
                "detections_by_run": item.get("detections_by_run", []),
                "error": item.get("error"),
            })
        
        self._frame_analysis_groups[group] = {
            "part_id": part_id,
            "rule_results": list(result.rule_results),
            "models": model_details,
            "picture_run": (
                int(consensus.get("picture_run"))
                if consensus.get("picture_run") else None
            ),
            "picture_reason": (
                str(consensus.get("picture_reason"))
                if consensus.get("picture_reason") else None
            ),
            "updated_at": time.time(),
        }

    def _active_frame_analysis_group(self) -> str:
        """Группа камер, чей анализ показывать: за выбранной камерой UI."""
        input_roles = set(
            getattr(self.inspector, "INPUT_ROLES", None)
            or ("INPUT_LEFT", "INPUT_RIGHT")
        )
        try:
            role = self._get_active_camera_role()
        except Exception:
            role = None
        if role in input_roles:
            return "INPUT"
        if role is not None:
            return "SPIDER"
        # Камера ещё не выбрана: последняя обновлённая группа.
        updated = {
            name: entry.get("updated_at") or 0
            for name, entry in self._frame_analysis_groups.items()
        }
        if any(updated.values()):
            return max(updated, key=updated.get)
        return "INPUT"

    def _on_stage_change(self, previous, current, elapsed: float):
        """Печать границы фаз шага: видно, где именно проводится время."""
        print(
            f"[STAGE] {previous.value} -> {current.value} "
            f"(предыдущая фаза {elapsed:.2f} с)"
        )

    def _on_state_change(self, old, new, action: str):
        try:
            self._journal_append(
                "state_transition",
                old_state=old.value,
                new_state=new.value,
                action=action,
            )
        except Exception as exc:
            # A journal failure is itself a fault; avoid recursively calling
            # the transition callback while the state lock is in use.
            self._fault_reason = f"operational journal failure: {exc}"
            print(f"[FAULT] {self._fault_reason}")
        if new == State.STOPPING:
            self._set_process("DRAINING", "Завершение корпусов на линии")
        elif new == State.STOPPED:
            # After a normal drain both distributor axes are returned home
            # and confirmed before STOPPED is published.
            return_home = getattr(self.distributor, "return_home", None)
            if callable(return_home):
                try:
                    return_home()
                except Exception as exc:
                    self._handle_fault(f"distributor home after drain failed: {exc}")
                    return
            # Линия пуста: последние кадры с разметкой остаются на экране,
            # пока оператор не войдёт в JOG или не запустит цикл заново.
            self.stages.reset()
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
            and not (self.live.error or getattr(getattr(self, "cameras", None), "live_error", None))
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
                # CameraManager/LivePreview remain live in IDLE, PAUSED and
                # STOPPED; only application shutdown stops the readers.
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
                # Conservative boundary: even a short accepted hold may have
                # moved the belt, so old snapshots are never reused.
                self._jog_moved = True
                self._snapshot_after = time.monotonic()
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
            # Панель следует за камерой, выбранной оператором: анализ
            # меняется при каждом переключении камеры в рабочем цикле.
            # Показываются только правила и замеры этой камеры, а не
            # всей группы (INPUT / SPIDER / TOP).
            group = self._active_frame_analysis_group()
            entry = self._frame_analysis_groups[group]
            stage_label = "ВХОД" if group == "INPUT" else "КОНТРОЛЬ +4"
            try:
                active_role = self._get_active_camera_role()
            except Exception:
                active_role = None
            models = [
                dict(item) for item in entry["models"]
                if not active_role or item.get("role") == active_role
            ]
            rules = self._rule_report_rows(
                entry["rule_results"], role=active_role,
            )
            has_data = (
                entry["updated_at"] is not None
                and bool(
                    rules
                    or models
                    or entry["rule_results"]
                    or entry["models"]
                )
            )
            role_suffix = f" · {active_role}" if active_role else ""
            if has_data:
                message = (
                    f"{stage_label}{role_suffix}: итог по свежему кадру; "
                    "правила считаются по единственному замеру"
                )
            else:
                message = (
                    f"{stage_label}{role_suffix}: "
                    "результатов анализа пока нет"
                )
            return {
                "available": True,
                "kind": "CYCLE",
                "active": True,
                "title": "АНАЛИЗ ТЕКУЩЕГО КАДРА",
                "role": active_role,
                "group": group,
                "stage": stage_label,
                "part_id": entry["part_id"],
                "message": message,
                "models": models,
                "rules": rules,
                "picture_run": entry.get("picture_run"),
                "picture_reason": entry.get("picture_reason"),
                "updated_at": entry["updated_at"],
            }

        if selected_report:
            # Ручной анализ уже снимает и считает только выбранную камеру
            # (rules_for_role + capture_single), поэтому extra-filter не нужен.
            return {
                "available": True,
                "kind": "SELECTED",
                "active": self._selected_analysis_active,
                "title": "АНАЛИЗ КАДРА",
                "role": (
                    report.get("selected_role")
                    or self._selected_analysis_role
                ),
                "part_id": None,
                "message": report.get("message") or "Анализ кадра",
                "status": report.get("status"),
                "cameras": [dict(item) for item in report.get("cameras", [])],
                "models": [dict(item) for item in report.get("models", [])],
                "rules": [dict(item) for item in report.get("rules", [])],
                "picture_run": report.get("picture_run"),
                "picture_reason": report.get("picture_reason"),
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
            "picture_run": None,
            "picture_reason": None,
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
            # На шаге передачи маршрут уже выставлен: GOOD проходит через
            # DIST1=0, BAD/CLEANUP — через DIST1=340 и DIST2.
            dropping = self._pending_drop is not None and self._pending_drop is part
            line_parts.append({
                "id": part.id,
                "position": position,
                "category": part.route_category,
                # Флаг оставлен для обратной совместимости UI; механического
                # удержания корпуса в этой линии нет.
                "held": False,
                "dropping": dropping,
            })

        state_name = sm_snap["state"]
        operation_busy = self._operation_lock.locked()
        jog_snapshot = self.jog.status if self.jog is not None else {}
        jog_busy = bool(jog_snapshot.get("busy", False))
        camera_error = self.live.error or getattr(getattr(self, "cameras", None), "live_error", None)
        jog_error = jog_snapshot.get("error") or camera_error
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
            "fault": getattr(self, "_fault_latch", FaultLatch()).report(),
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
            # Inspection блокирует live только у захватываемых ролей.
            # Остальные камеры продолжают поток даже на статической фазе.
            "live": {
                "running": bool(getattr(getattr(self, "cameras", None), "live_running", False) or self.live.running),
                "streaming": bool(getattr(getattr(self, "cameras", None), "live_running", False) or self.live.running),
                # Capture/analysis remain live; only the atomic result
                # publication creates UI-frozen roles for REVIEW.
                "static": bool(getattr(self, "_review_published", False)),
                "static_roles": list(
                    self._inspection_display_roles
                    if getattr(self, "_review_published", False) else ()
                ),
                "all_roles_static": False,
                "stage": self.stages.stage.value,
                "fps": self._current_live_fps(),
                "error": camera_error,
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

    def _refresh_monitor(
        self,
        frames: dict | None = None,
        run_frames: list | None = None,
        run_rule_results: list | None = None,
    ):
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
                run_frames=run_frames,
                run_rule_results=run_rule_results,
            )
        else:
            self.monitor.update(
                line_status=status,
                recent_parts=list(self.recent_parts),
                run_frames=run_frames,
                run_rule_results=run_rule_results,
            )
