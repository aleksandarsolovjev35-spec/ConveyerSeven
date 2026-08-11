import os
import signal
import threading
import time
import traceback

import webview

from config import load_archive_config, load_calibration
from core.app_logging import (
    capture_prints,
    get_logger,
    install_excepthooks,
    setup_logging,
)
from core.config_app import get_settings
from core.database import Database
from core.decision_engine import DecisionEngine
from core.event_bus import EventBus
from core.production_cycle import ProductionCycle
from core.structured_logging import configure_structlog
from domain.threshold_loader import ThresholdLoader
from hardware.axis import Axis
from hardware.conveyor import Conveyor
from hardware.distributor import Distributor
from hardware.jog_controller import JogController
from hardware.mock_hardware import (
    MockCamera,
    MockConveyor,
    MockDistributor,
    MockSerialTransport,
)
from hardware.port_discovery import find_controller
from hardware.serial_transport import SerialTransport
from inspection.debug_recorder import DebugRecorder
from inspection.inspector import Inspector
from inspection.part_archive import PartArchive
from vision.camera_calibration_console import launch_camera_calibrator
from vision.camera_manager import CameraManager
from vision.mock_vision import MockVisionCluster
from vision.ui import LiveMonitor
from vision.vision_cluster import VisionCluster

log = get_logger("main")

CYCLE_JOIN_TIMEOUT = 15.0
INIT_JOIN_TIMEOUT = 60.0
GRACEFUL_EXIT_TIMEOUT = 135.0
COMPRESS_TIMEOUT = 60.0


# Helpers

def _probe_controller_fw(transport) -> tuple[str, bool]:
    """Запросить версию прошивки (I6) и распознать ожидаемую.

    Возвращает (первая строка ответа, True если прошивка опознана).
    Отсутствие ответа или ошибка запроса — не фатальны: контроллер уже
    идентифицирован форматом I2 при поиске порта.
    """
    try:
        reply = transport.query("I6", delay=0.2) or ""
    except Exception as exc:
        print(f"[SERIAL] Запрос версии прошивки (I6) не удался: {exc}")
        return "", False

    fw_line = next(
        (line.strip() for line in reply.splitlines() if line.strip()),
        "",
    )
    known = any(token in reply.lower() for token in ("convey", "fw"))
    print(f"[SERIAL] Прошивка контроллера: {fw_line or reply!r}")
    return fw_line, known


def _env_clamped_float(
    name: str, default: float, minimum: float, maximum: float,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"[CONFIG] {name}={raw!r} не число, используется {default}")
        value = default
    return max(minimum, min(maximum, value))


def _weak_camera_warmup_reasons(stats: dict) -> dict:
    """Вернуть роли, не отдавшие ни одного кадра во время прогрева."""
    reasons = {}
    for role, row in (stats or {}).items():
        try:
            reads = int(row.get("reads", 0) or 0)
        except Exception:
            reads = 0
        if reads <= 0:
            reasons[role] = "нет кадров"
    return reasons


def _format_warmup_reasons(reasons: dict) -> str:
    return "; ".join(
        f"{role}: {reason}" for role, reason in sorted(reasons.items())
    )


def _recover_weak_cameras_after_warmup(cameras, stats: dict, phase: str) -> dict:
    """Повторно прогреть роли без кадров и проверить их готовность.

    Если менеджер поддерживает ``reopen_roles``, после неудачного прогрева
    выполняется попытка переоткрытия. Текущий CameraManager возвращает
    неуспех и запуск завершается ошибкой.
    """
    reasons = _weak_camera_warmup_reasons(stats)
    if not reasons:
        return stats

    roles = tuple(reasons)
    retry_seconds = _env_clamped_float(
        "CAMERA_RECOVERY_WARMUP_SECONDS", 2.5, 0.2, 10.0,
    )
    print(
        f"[CAMERA] {phase}: слабый прогрев ({_format_warmup_reasons(reasons)}); "
        f"повторно прогреваем {', '.join(roles)} {retry_seconds:.1f}с"
    )
    retry_stats = cameras.warmup_roles(roles, duration=retry_seconds)
    retry_reasons = _weak_camera_warmup_reasons(retry_stats)
    merged = dict(stats or {})
    merged.update(retry_stats)
    if not retry_reasons:
        return merged

    # Менеджер может реализовать переоткрытие ролей; отсутствие такой
    # возможности или повторный отказ блокируют запуск.
    reopen = getattr(cameras, "reopen_roles", None)
    if reopen is None:
        raise RuntimeError(
            f"Камеры не стабилизировались после прогрева ({phase}): "
            f"{_format_warmup_reasons(retry_reasons)}"
        )
    stuck = tuple(retry_reasons)
    print(
        f"[CAMERA] {phase}: повторный прогрев не помог "
        f"({_format_warmup_reasons(retry_reasons)}); "
        f"пересоздаём потоки {', '.join(stuck)}"
    )
    reopened = reopen(stuck)
    final_stats = cameras.warmup_roles(stuck, duration=retry_seconds)
    merged.update(final_stats)
    final_reasons = _weak_camera_warmup_reasons(final_stats)
    if final_reasons:
        not_reopened = ", ".join(
            role for role in stuck if not reopened.get(role)
        )
        hint = (
            f" (поток не пересоздался: {not_reopened})"
            if not_reopened
            else ""
        )
        raise RuntimeError(
            f"Камеры не стабилизировались после прогрева ({phase}): "
            f"{_format_warmup_reasons(final_reasons)}{hint}"
        )
    recovered = ", ".join(role for role in stuck if reopened.get(role))
    print(
        f"[CAMERA] {phase}: камеры восстановлены пересозданием "
        f"потока: {recovered or '—'}"
    )
    return merged


def _shutdown_compress(archive):
    if not archive or not archive.enabled or not archive.compress_on_shutdown:
        return

    t = threading.Thread(
        target=_safe_compress, args=(archive,), daemon=True,
    )
    t.start()
    t.join(timeout=COMPRESS_TIMEOUT)
    if t.is_alive():
        print(
            "[SHUTDOWN] Сжатие архива не завершилось за "
            f"{COMPRESS_TIMEOUT}с, пропускаем"
        )


def _safe_compress(archive):
    try:
        print("[SHUTDOWN] Сжатие архива...")
        archive.compress(delete_original=archive.delete_original_after_zip)
    except Exception as e:
        print(f"[SHUTDOWN] Ошибка сжатия архива: {e}")


def _make_idle_status(distributor) -> dict:
    # Должен повторять ключи, которые ожидает frontend из _build_status(),
    # иначе до первого тика production-цикла UI получает KeyError/undefined.
    return {
        "state": "IDLE",
        "exit_requested": False,
        "fault_reason": None,
        "step": 0,
        "in_line": 0,
        "line_parts": [],
        "total": 0,
        "good": 0,
        "rejected": 0,
        "cleanup": 0,
        "empty": 0,
        "dist1_position": 0,
        "dist1_max": distributor.dist1_open_position,
        "dist1_state": "IDLE",
        "dist2_position": 0,
        "dist2_max": max(
            distributor.dist2_bad_position,
            distributor.dist2_cleanup_position,
            1,
        ),
        "dist2_state": "IDLE",
        "dist2_target": "BAD",
        "last_distributor_action": "-",
        "process": {
            "phase": "IDLE",
            "label": "Система готова к пуску",
            "step": 0,
            "part_id": None,
            "positions": [],
            "conveyor": {},
            "capture_roles": [],
            "inspection_roles": [],
            "revision": 0,
            "updated_at": time.time(),
        },
        "diagnostic_allowed": False,
        "diagnostic_busy": False,
        "controls": {
            "start": False,
            "stop": False,
            "pause": False,
            "resume": False,
            "exit": True,
            "jog_hold": False,
            "selected_model_analysis": False,
            "selected_model_release": False,
            "distributor_diagnostic": False,
            "camera_diagnostic": False,
            "vision_rule_diagnostic": False,
        },
        "selected_analysis": {
            "active": False,
            "role": None,
        },
        "live": {
            "running": False,
            "streaming": False,
            "static": False,
            "static_roles": [],
            "all_roles_static": False,
            "stage": "IDLE",
            "fps": 0.0,
            "error": None,
        },
        "frame_analysis": {
            "available": False,
            "kind": None,
            "active": False,
            "title": None,
            "role": None,
            "group": None,
            "stage": None,
            "part_id": None,
            "message": None,
            "models": [],
            "rules": [],
            "picture_run": None,
            "picture_reason": None,
            "updated_at": None,
        },
        "diagnostics": {
            "status": "NOT_RUN",
            "kind": None,
            "message": "Проверки ещё не запускались",
            "cameras": [],
            "models": [],
            "rules": [],
            "updated_at": None,
        },
        "jog": {
            "active": False,
            "can_enter": False,
            "hold_steps": 0,
            "last_action": "-",
            "busy": False,
            "direction": None,
            "error": None,
            "live_fps": 0.0,
        },
    }


class ConveyerApp:
    """Object-oriented composition root and lifecycle owner."""

    def __init__(self):
        self.settings = get_settings()
        self.event_bus = EventBus()
        self.database = Database(self.settings.database_file)
        self.monitor = LiveMonitor(event_bus=self.event_bus, fullscreen=True)

        # Слоты состояния
        self.cameras = None
        self.transport = None
        self.cycle = None
        self.archive = None

        self.shutdown_requested = threading.Event()
        self.exit_press_count = 0
        self.exit_lock = threading.Lock()

        self.vision = None
        self.inspector = None
        self.conveyor = None
        self.distributor = None
        self.jog = None
        self.calibration = None
        self.cycle_thread = None
        self.init_thread = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

    def _ensure_initialization_active(self):
        if self.shutdown_requested.is_set():
            raise RuntimeError("initialization cancelled by operator")

    def _report_startup_failure(self):
        """Оставить startup-ошибку на splash до решения оператора."""
        print("[INIT] Startup failed; waiting for operator to close the UI")

    def _init_vision(self):
        self._ensure_initialization_active()
        self.monitor.boot_step_start("cameras", "Открытие камер")
        self.cameras = (
            MockCamera(self.settings.simulation_video_file)
            if self.settings.simulation_mode
            else CameraManager()
        )
        self.monitor.boot_step_done(
            "cameras", f"Открыто камер: {len(self.cameras.cameras)}",
        )
        self._ensure_initialization_active()

        self.monitor.boot_step_start("camera_warmup", "Прогрев камер")
        warmup_seconds = _env_clamped_float(
            "CAMERA_WARMUP_SECONDS", 2.5, 0.5, 10.0,
        )
        stats = self.cameras.warmup_all(duration=warmup_seconds)
        stats = _recover_weak_cameras_after_warmup(
            self.cameras, stats, "стартовый прогрев",
        )
        total_reads = sum(s.get("reads", 0) for s in stats.values())
        self.monitor.boot_step_done(
            "camera_warmup", f"Прогрев камер: {total_reads} кадров",
        )
        self._ensure_initialization_active()

        self.monitor.boot_step_start("models_load", "Загрузка моделей")
        self.vision = (
            MockVisionCluster()
            if self.settings.simulation_mode
            else VisionCluster(device="auto")
        )
        self.monitor.boot_step_done(
            "models_load", f"Загружено моделей: {len(self.vision.models)}",
        )
        self._ensure_initialization_active()

        self.monitor.boot_step_start("models_warm", "Прогрев моделей")
        self.vision.warmup()
        self.monitor.boot_step_done("models_warm", "Прогрев завершён")
        self._ensure_initialization_active()

        self.monitor.boot_step_start("inspection", "Настройка системы контроля")
        threshold_loader = ThresholdLoader()
        thresholds = threshold_loader.get_all()
        decision = DecisionEngine(thresholds=thresholds)
        recorder = DebugRecorder(
            folder="debug_frames",
            enabled=False,
            save_interval=1,
        )
        self.inspector = Inspector(
            vision=self.vision,
            decision=decision,
            recorder=recorder,
        )

        archive_config = load_archive_config(str(self.settings.archive_config_file))
        self.archive = PartArchive(
            root_folder=archive_config["root_path"],
            enabled=archive_config["enabled"],
            jpeg_quality=archive_config["jpeg_quality"],
            compress_on_shutdown=archive_config["compress_on_shutdown"],
            delete_original_after_zip=archive_config["delete_original_after_zip"],
        )
        self.monitor.server.archive = self.archive
        self.monitor.server.archive_config_path = str(self.settings.archive_config_file)

        # Редактор порогов правил: сервер отдаёт текущие значения
        # (GET /api/thresholds), а применение изменений пересоздаёт
        # DecisionEngine внутри Inspector'а и сохраняет файл.
        # Пороги автоматически подтягиваются из thresholds.json:
        # ручные правки файла перечитываются без перезапуска.
        self.monitor.server.thresholds = dict(thresholds)
        self.monitor.server.threshold_labels = dict(threshold_loader.labels or {})
        self.monitor.server.thresholds_path = str(self.settings.thresholds_file)
        self.event_bus.subscribe(
            "ui:thresholds_reload_requested",
            self._thresholds_reload_from_file,
        )
        self.event_bus.subscribe(
            "ui:thresholds_apply_requested",
            self._thresholds_apply,
        )
        self.monitor.boot_step_done(
            "inspection", f"Настроено правил: {len(decision.rules)}",
        )
        self._ensure_initialization_active()

    def _init_hardware(self):
        self._ensure_initialization_active()
        self.calibration = load_calibration(str(self.settings.calibration_file))
        self._ensure_initialization_active()

        self.monitor.boot_step_start("serial", "Поиск контроллера")
        serial_baud = self.settings.serial_baud
        preferred_port = self.settings.serial_port
        found_port, port_message = (
            ("SIMULATION", "Симулятор контроллера")
            if self.settings.simulation_mode
            else find_controller(
                baudrate=serial_baud,
                preferred_port=preferred_port,
            )
        )
        if found_port is None:
            raise RuntimeError(port_message)

        self.transport = (
            MockSerialTransport()
            if self.settings.simulation_mode
            else SerialTransport(port=found_port, baudrate=serial_baud)
        )
        # Start from a stopped controller before any configuration.
        self.transport.send("G1")
        self.transport.send("G25")
        fw_line, fw_known = _probe_controller_fw(self.transport)
        if not fw_known:
            print(
                "[SERIAL] ВНИМАНИЕ: неизвестная прошивка контроллера: "
                f"{fw_line or 'нет ответа на I6'}"
            )
            self.monitor.set_splash_status(
                "ВНИМАНИЕ: прошивка контроллера не опознана"
            )
            serial_detail = (
                f"Контроллер: {found_port} @ {serial_baud} · "
                "прошивка не опознана"
            )
        else:
            serial_detail = (
                f"Контроллер: {found_port} @ {serial_baud}"
                + (f" · {fw_line}" if fw_line else "")
            )
        self.monitor.boot_step_done("serial", serial_detail)
        self._ensure_initialization_active()

        self.monitor.boot_step_start("hardware", "Инициализация оборудования")
        if self.settings.simulation_mode:
            self.conveyor = MockConveyor(self.transport)
            self.distributor = MockDistributor()
            self.jog = None
        else:
            self.conveyor = Conveyor(
                self.transport,
                speed=self.calibration["conveyor_speed"],
                accel=self.calibration["conveyor_accel"],
                steps_per_division=self.calibration["normal_steps"],
                divisions_per_movement=2,
            )
            dist1_axis = Axis(
                self.transport,
                axis_id=0,
                minimum=0,
                maximum=self.calibration["dist1_open_position"],
                speed=self.calibration["axis_speed"],
                accel=self.calibration["axis_accel"],
            )
            dist2_axis = Axis(
                self.transport,
                axis_id=1,
                minimum=0,
                maximum=max(
                    self.calibration["dist2_bad_position"],
                    self.calibration["dist2_cleanup_position"],
                ),
                speed=self.calibration["axis_speed"],
                accel=self.calibration["axis_accel"],
            )
            self.distributor = Distributor(
                dist1_axis=dist1_axis,
                dist2_axis=dist2_axis,
                dist1_open_position=self.calibration["dist1_open_position"],
                dist2_bad_position=self.calibration["dist2_bad_position"],
                dist2_cleanup_position=self.calibration["dist2_cleanup_position"],
                drop_time=self.calibration["drop_time"],
            )
            if (
                self.distributor.dist1_open_position
                != self.calibration["dist1_open_position"]
                or self.distributor.dist2_bad_position
                != self.calibration["dist2_bad_position"]
                or self.distributor.dist2_cleanup_position
                != self.calibration["dist2_cleanup_position"]
            ):
                raise RuntimeError(
                    "Distributor endpoints do not match calibration.json"
                )
            self.distributor.cancel_check = self.shutdown_requested.is_set
            self.jog = JogController(
                transport=self.transport,
                calibration=self.calibration,
            )
        self.monitor.boot_step_done("hardware", "Лента и две оси инициализированы")
        self._ensure_initialization_active()

    def _init_cycle(self):
        self._ensure_initialization_active()
        self.monitor.boot_step_start("cycle", "Создание производственного цикла")
        print("[HARDWARE] Homing distributor axes...")
        self.distributor.initialize()
        self._ensure_initialization_active()

        self.cycle = ProductionCycle(
            conveyor=self.conveyor,
            cameras=self.cameras,
            inspector=self.inspector,
            distributor=self.distributor,
            monitor=self.monitor,
            archive=self.archive,
            jog=self.jog,
            settle_seconds=self.calibration["settle_time"],
            stage_trace_seconds=self.calibration["stage_trace_time"],
            review_seconds=self.calibration["review_time"],
        )
        self.event_bus.subscribe("ui:start_requested", self.cycle.request_start)
        self.event_bus.subscribe("ui:stop_requested", self.cycle.request_stop)
        self.event_bus.subscribe("ui:pause_requested", self.cycle.request_pause)
        self.event_bus.subscribe("ui:resume_requested", self.cycle.request_resume)
        self.event_bus.subscribe(
            "ui:distributor_diagnostic_requested",
            self.cycle.distributor_diagnostic,
        )
        self.event_bus.subscribe(
            "ui:camera_diagnostic_requested",
            self.cycle.diagnostic_check_cameras,
        )
        self.event_bus.subscribe(
            "ui:vision_rule_diagnostic_requested",
            self.cycle.diagnostic_check_vision_rules,
        )
        self.event_bus.subscribe(
            "ui:selected_model_analysis_requested",
            self.cycle.diagnostic_analyze_selected_camera,
        )
        self.event_bus.subscribe(
            "ui:selected_model_release_requested",
            self.cycle.diagnostic_release_selected_camera,
        )
        self.event_bus.subscribe(
            "ui:active_camera_changed",
            lambda _role: self.cycle._refresh_monitor(),
        )
        self.event_bus.subscribe("ui:jog_enter_requested", self.cycle.enter_jog)
        self.event_bus.subscribe("ui:jog_exit_requested", self.cycle.exit_jog)
        self.event_bus.subscribe("ui:jog_hold_start_requested", self.cycle.jog_hold_start)
        self.event_bus.subscribe("ui:jog_hold_heartbeat", self.cycle.jog_hold_heartbeat)
        self.event_bus.subscribe(
            "ui:jog_hold_release_requested",
            self.cycle.jog_hold_release,
        )
        self.monitor.boot_step_done("cycle")
        self._ensure_initialization_active()

    def _prepare_initial_preview(self):
        # Re-warmup before preview (models loading took time). Некоторые
        # UVC-камеры после простоя снова отдают пустые/тёмные кадры;
        # короткой 1с паузы INPUT_LEFT не всегда хватало.
        quick = _env_clamped_float(
            "CAMERA_PRE_PREVIEW_WARMUP_SECONDS", 2.5, 0.0, 5.0,
        )
        if quick > 0.0:
            stats = self.cameras.warmup_all(duration=quick)
            _recover_weak_cameras_after_warmup(
                self.cameras, stats, "прогрев перед preview",
            )

        self.monitor.boot_step_start("preview", "Получение начальных кадров")
        preview_frames = self.cameras.capture_all()
        self.monitor.update(
            frames=preview_frames,
            vision_results={},
            rule_results=[],
            line_status=_make_idle_status(self.distributor),
            recent_parts=[],
        )
        self.monitor.boot_step_done("preview", "Начальные кадры получены")
        self._ensure_initialization_active()

    def _start_cycle_thread(self):
        self.monitor.boot_step_start("ready", "Запуск системы")
        self.cycle_thread = threading.Thread(target=self.cycle.start, daemon=True)
        self.cycle_thread.start()
        self._ensure_initialization_active()
        self.monitor.boot_step_done("ready", "Система готова к работе")

    def _boot_sequence(self):
        try:
            self._init_vision()
            self._init_hardware()
            self._init_cycle()
            # Прогрев и первый кадр
            self._prepare_initial_preview()
            self._start_cycle_thread()
            time.sleep(0.6)
            self.monitor.boot_complete()
        except Exception as e:
            traceback.print_exc()
            current = self.monitor.server.boot_current or "init"
            self.monitor.boot_step_error(current, str(e))
            self._report_startup_failure()

    def _thresholds_reload_from_file(self, fresh):
        if self.inspector is None:
            raise RuntimeError("Система контроля ещё не инициализирована")
        self.inspector.decision = DecisionEngine(thresholds=fresh)
        print(
            "[THRESHOLDS] Пороги перечитаны из thresholds.json; "
            "правила пересозданы"
        )
        return fresh

    def _thresholds_apply(self, role, values, labels):
        if self.cycle is None or self.inspector is None:
            raise RuntimeError("Система контроля ещё не инициализирована")
        if self.cycle.state not in ("IDLE", "STOPPED"):
            raise RuntimeError(
                "Изменение порогов доступно только до пуска "
                "и после полной остановки"
            )
        if self.cycle.jog is not None and self.cycle.jog.status.get("busy"):
            raise RuntimeError("Нельзя менять пороги во время движения ленты")
        if not isinstance(values, dict) or not values:
            raise ValueError("Нет изменённых порогов")

        updated = dict(self.inspector.decision.thresholds)
        changed = []
        for key, value in values.items():
            full_key = (
                f"{role}.{key}"
                if not str(key).startswith(f"{role}.")
                else str(key)
            )
            if full_key not in updated:
                raise ValueError(f"Неизвестный порог: {full_key}")
            updated[full_key] = value
            changed.append(full_key)

        # Полная валидация, как при загрузке файла.
        ThresholdLoader.validate(updated)
        # Понятные названия порогов для оператора: сохраняются вместе со
        # значениями, на логику правил не влияют.
        full_labels = dict(self.monitor.server.threshold_labels or {})
        for key, name in (labels or {}).items():
            full_key = (
                f"{role}.{key}"
                if not str(key).startswith(f"{role}.")
                else str(key)
            )
            if name is None or not str(name).strip():
                full_labels.pop(full_key, None)
            else:
                full_labels[full_key] = str(name).strip()

        ThresholdLoader.save_file(
            str(self.settings.thresholds_file),
            updated,
            labels=full_labels,
        )
        self.database.audit(
            action="thresholds.updated",
            payload_json=str({"role": role, "keys": sorted(changed)}),
        )
        # Правила пересоздаются: Inspector берёт decision каждый раз заново,
        # поэтому замена объекта применяется сразу.
        self.inspector.decision = DecisionEngine(thresholds=updated)
        self.monitor.server.thresholds = dict(updated)
        self.monitor.server.threshold_labels = dict(full_labels)
        print(
            "[THRESHOLDS] Применено "
            f"{len(changed)} изменение(й) для {role}: "
            f"{', '.join(sorted(changed))}"
        )
        return updated

    def _schedule_close(self, force: bool = False):
        def _wait_and_close():
            started = time.monotonic()
            if self.cycle_thread and self.cycle_thread.is_alive():
                timeout = CYCLE_JOIN_TIMEOUT if force else GRACEFUL_EXIT_TIMEOUT
                self.cycle_thread.join(timeout=timeout)
                waited = time.monotonic() - started
                print(f"[EXIT] Ожидание цикла: {waited:.2f} с")
                if self.cycle_thread.is_alive() and not force:
                    print(
                        "[EXIT] Линия ещё выполняет штатную остановку; "
                        "окно остаётся открытым. Нажмите ВЫХОД второй раз "
                        "для принудительного завершения."
                    )
                    return
            self.monitor.close_window()

        threading.Thread(target=_wait_and_close, daemon=True).start()

    def _request_startup_stop(self, prefix: str):
        if self.cycle is None and self.transport is not None:
            try:
                self.transport.send("G1")
                self.transport.send("G25")
            except Exception as exc:
                print(f"[{prefix}] Startup stop failed: {exc}")

    def handle_exit_request(self):
        self.shutdown_requested.set()
        self._request_startup_stop("EXIT")

        with self.exit_lock:
            self.exit_press_count += 1
            count = self.exit_press_count

        force = count > 1 or bool(self.cycle and self.cycle.state == "FAULT")
        if force:
            print("[EXIT] Force exit")
            if self.cycle:
                self.cycle.request_force_exit()
        else:
            print("[EXIT] Штатная остановка -> завершение деталей на линии")
            if self.cycle:
                self.cycle.request_exit()

        self._schedule_close(force=force)

    def _handle_signal(self, _signum, _frame):
        print("\n[SIGINT] Ctrl+C -> запрос выхода")
        self.handle_exit_request()

    def _print_console_banner(self):
        print("=" * 60)
        print("Система запускается.")
        print("  F5 ПУСК | F6 СТОП | TAB вид")
        print("  ESC ВЫХОД (1× штатная остановка, 2× принудительный выход)")
        print("=" * 60)

    def run(self):
        log_file = setup_logging()
        json_log_file = configure_structlog(self.settings.log_dir)
        install_excepthooks()
        capture_prints()
        log.info(
            "=== Запуск ConveyerSeven; журнал: %s; JSON: %s ===",
            log_file,
            json_log_file,
        )

        try:
            if (
                not os.path.exists(str(self.settings.camera_mapping_file))
                and not launch_camera_calibrator(
                    str(self.settings.camera_mapping_file)
                )
            ):
                print(
                    "[STARTUP] camera_mapping.json не создан; "
                    "основное приложение не запускается"
                )
                return

            self.monitor.server.start_server(
                host=self.monitor.host,
                port=self.monitor.port,
            )
            # EXIT должен работать даже при ошибке до создания ProductionCycle.
            self.event_bus.subscribe("ui:exit_requested", self.handle_exit_request)
            signal.signal(signal.SIGINT, self._handle_signal)

            self.init_thread = threading.Thread(
                target=self._boot_sequence,
                daemon=True,
            )
            self.init_thread.start()
            self._print_console_banner()

            window = webview.create_window(
                title=self.monitor.window_name,
                url=f"http://{self.monitor.host}:{self.monitor.port}/",
                fullscreen=self.monitor.fullscreen,
                background_color="#0b0f13",
                js_api=self.monitor.webview_api,
            )
            self.monitor._webview_window = window
            webview.start()

            print("[UI] Окно закрыто, завершение...")
            if self.cycle and not self.cycle.force_exit_requested:
                self.cycle.request_force_exit()
            if self.cycle_thread and self.cycle_thread.is_alive():
                self.cycle_thread.join(timeout=CYCLE_JOIN_TIMEOUT)
                if self.cycle_thread.is_alive():
                    print(
                        "[WARN] cycle thread не завершился за "
                        f"{CYCLE_JOIN_TIMEOUT}с"
                    )
        finally:
            self.shutdown()

    def shutdown(self):
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        shutdown_started = time.monotonic()
        print("[SHUTDOWN] Завершение...")
        self.shutdown_requested.set()
        self._request_startup_stop("SHUTDOWN")

        if self.init_thread and self.init_thread.is_alive():
            self.init_thread.join(timeout=INIT_JOIN_TIMEOUT)
            if self.init_thread.is_alive():
                print(
                    "[SHUTDOWN] Initialization thread did not stop in "
                    f"{INIT_JOIN_TIMEOUT}s"
                )

        if self.cycle and not self.cycle.force_exit_requested:
            self.cycle.request_force_exit()
        if self.cycle_thread and self.cycle_thread.is_alive():
            self.cycle_thread.join(timeout=CYCLE_JOIN_TIMEOUT)

        phase_started = time.monotonic()
        try:
            self.monitor.stop_server()
        except Exception as exc:
            print(f"[SHUTDOWN] UI server stop failed: {exc}")
        print(
            f"[SHUTDOWN] Остановка UI-сервера: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

        phase_started = time.monotonic()
        if self.cycle_thread and self.cycle_thread.is_alive():
            print("[SHUTDOWN] Cycle still active; archive compression skipped")
        else:
            _shutdown_compress(self.archive)
        print(
            f"[SHUTDOWN] Архив: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

        phase_started = time.monotonic()
        # Live-просмотр останавливается до освобождения камер: иначе фоновые
        # чтения продолжались бы на уже закрытых VideoCapture.
        if self.cycle:
            try:
                self.cycle.live.stop()
            except Exception as exc:
                print(f"[SHUTDOWN] Live preview stop failed: {exc}")
        try:
            if self.cameras:
                self.cameras.release()
        except Exception as exc:
            print(f"[SHUTDOWN] Camera release failed: {exc}")
        print(
            f"[SHUTDOWN] Освобождение камер: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

        phase_started = time.monotonic()
        try:
            if self.transport:
                self.transport.close()
        except Exception as exc:
            print(f"[SHUTDOWN] Serial close failed: {exc}")
        print(
            f"[SHUTDOWN] Закрытие COM: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

        print(
            f"[SHUTDOWN] Готово за "
            f"{time.monotonic() - shutdown_started:.2f} с."
        )

