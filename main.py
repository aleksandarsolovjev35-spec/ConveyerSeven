import os
import signal
import threading
import time

import webview

from config import load_calibration, load_archive_config
from config.machine_config import load_machine_config

from hardware.serial_transport import SerialTransport
from hardware.port_discovery   import find_controller
from hardware.axis             import Axis
from hardware.conveyor         import Conveyor
from hardware.distributor      import Distributor
from hardware.jog_controller   import JogController

from vision.camera_manager             import CameraManager
from vision.camera_calibration_console import launch_camera_calibrator
from vision.vision_cluster             import VisionCluster
from vision.ui                         import LiveMonitor

from domain.threshold_loader  import ThresholdLoader
from core.decision_engine     import DecisionEngine
from core.production_cycle    import ProductionCycle
from core.operational_log      import OperationalLog
from core.boot                 import BootCoordinator, sha256_file

from inspection.debug_recorder import DebugRecorder
from inspection.inspector      import Inspector
from inspection.part_archive   import PartArchive
from inspection.model_worker   import run_in_terminating_worker


CYCLE_JOIN_TIMEOUT   = 15.0
INIT_JOIN_TIMEOUT    = 60.0
GRACEFUL_EXIT_TIMEOUT = 135.0
COMPRESS_TIMEOUT     = 60.0


def main():

    boot_coordinator = BootCoordinator(marker_path=".production_process.json", app_version=os.environ.get("APP_VERSION", "dev"))

    if not os.path.exists("camera_mapping.json") and not launch_camera_calibrator(
        "camera_mapping.json"
    ):
        print(
            "[STARTUP] camera_mapping.json не создан; "
            "основное приложение не запускается"
        )
        return

    monitor = LiveMonitor(
        start_callback=None,
        stop_callback=None,
        exit_callback=None,
        fullscreen=True,
    )

    try:
        previous = boot_coordinator.detect_previous_unclean()
    except Exception as exc:
        monitor.boot_step_error("init", f"Маркер предыдущего процесса повреждён: {exc}")
        previous = {"error": str(exc)}
    if previous is not None:
        # The UI must expose the incident and wait for an explicit manual
        # cleanup acknowledgement; no automatic restart/resume is allowed.
        monitor.boot_step_error(
            "init",
            "Предыдущий batch "
            f"{previous.get('batch_id', 'unknown') if isinstance(previous, dict) else 'unknown'} "
            "завершён нештатно. Требуется ручная очистка линии.",
        )
        print(f"[BOOT] Previous unclean process: {previous}")

    monitor.server.start_server(
        host=monitor.host,
        port=monitor.port,
    )

    cameras      = None
    transport    = None
    cycle        = None
    cycle_thread = None
    init_thread  = None
    archive      = None
    shutdown_requested = threading.Event()

    exit_press_count = 0
    exit_lock = threading.Lock()

    # Exit logic

    def handle_exit_request():
        nonlocal exit_press_count
        shutdown_requested.set()
        if cycle is None and transport is not None:
            try:
                transport.send("G1")
                transport.send("G25")
            except Exception as exc:
                print(f"[EXIT] Startup stop failed: {exc}")

        with exit_lock:
            exit_press_count += 1
            count = exit_press_count

        force = count > 1 or bool(cycle and cycle.state == "FAULT")
        if force:
            print("[EXIT] Force exit")
            if cycle:
                cycle.request_force_exit()
        else:
            print("[EXIT] Штатная остановка -> завершение деталей на линии")
            if cycle:
                cycle.request_exit()

        _schedule_close(force=force)

    def _schedule_close(force: bool = False):
        def _wait_and_close():
            started = time.monotonic()
            if cycle_thread and cycle_thread.is_alive():
                timeout = (
                    CYCLE_JOIN_TIMEOUT if force else GRACEFUL_EXIT_TIMEOUT
                )
                cycle_thread.join(timeout=timeout)
                waited = time.monotonic() - started
                print(f"[EXIT] Ожидание цикла: {waited:.2f} с")
                if cycle_thread.is_alive() and not force:
                    print(
                        "[EXIT] Линия ещё выполняет штатную остановку; окно остаётся открытым. "
                        "Нажмите ВЫХОД второй раз для принудительного завершения."
                    )
                    return
            monitor.close_window()

        threading.Thread(
            target=_wait_and_close, daemon=True,
        ).start()

    def _report_startup_failure():
        """Оставить startup-ошибку на splash до решения оператора."""
        print("[INIT] Startup failed; waiting for operator to close the UI")

    # EXIT должен работать даже при ошибке до создания ProductionCycle.
    monitor.exit_callback = handle_exit_request

    def _ensure_initialization_active():
        if shutdown_requested.is_set():
            raise RuntimeError("initialization cancelled by operator")

    # System init

    def initialize_system():
        nonlocal cameras, transport, cycle, cycle_thread, archive

        try:
            _ensure_initialization_active()
            calib = load_calibration()
            machine_config, machine_config_sha = load_machine_config("machine_config.json")
            if machine_config["conveyor"]["production_target"] != calib["normal_steps"] * 2:
                raise RuntimeError("machine_config conveyor geometry disagrees with calibration")
            _ensure_initialization_active()

            # Cameras
            monitor.boot_step_start(
                "cameras", "Открытие камер",
            )
            try:
                cameras = CameraManager()
            except Exception as e:
                print(
                    f"[CAMERA] Ошибка инициализации: "
                    f"{type(e).__name__}: {e}"
                )
                monitor.boot_step_error(
                    "cameras",
                    f"Ошибка камеры: {e}",
                )
                _report_startup_failure()
                return
            expected_serials = {
                role: spec["serial"]
                for role, spec in machine_config["cameras"].items()
            }
            if getattr(cameras, "serials", {}) != expected_serials:
                raise RuntimeError("camera serial mapping differs from machine_config")
            monitor.boot_step_done(
                "cameras",
                f"Открыто камер: {len(cameras.cameras)}",
            )
            _ensure_initialization_active()

            # Camera warmup after idle (fixes near-black after long pause)
            monitor.boot_step_start(
                "camera_warmup", "Прогрев камер",
            )
            try:
                warmup_seconds = _env_clamped_float(
                    "CAMERA_WARMUP_SECONDS", 2.5, 0.5, 10.0,
                )
                stats = cameras.warmup_all(duration=warmup_seconds)
                stats = _recover_weak_cameras_after_warmup(
                    cameras, stats, "стартовый прогрев",
                )
                total_reads = sum(
                    s.get("reads", 0) for s in stats.values()
                )
                monitor.boot_step_done(
                    "camera_warmup",
                    f"Прогрев камер: {total_reads} кадров",
                )
                # From this BOOT boundary until process close every one of
                # the seven physical streams remains live.  REVIEW/snapshot
                # later copies from these buffers and never takes ownership
                # of VideoCapture.
                cameras.start_live()
            except Exception as e:
                monitor.boot_step_error(
                    "camera_warmup",
                    f"Ошибка прогрева камер: {e}",
                )
                _report_startup_failure()
                return
            _ensure_initialization_active()

            # Models load
            monitor.boot_step_start(
                "models_load", "Загрузка моделей",
            )
            try:
                isolated_workers = (
                    os.environ.get("INFERENCE_ISOLATED_WORKERS", "1").strip().lower()
                    not in ("0", "false", "off")
                )
                vision = VisionCluster(
                    device="cpu",
                    worker_runner=(run_in_terminating_worker if isolated_workers else None),
                    worker_timeout=float(os.environ.get("MODEL_TIMEOUT", "30.0")),
                )
            except Exception as e:
                monitor.boot_step_error(
                    "models_load",
                    f"Ошибка загрузки моделей: {e}",
                )
                _report_startup_failure()
                return
            monitor.boot_step_done(
                "models_load",
                f"Загружено моделей: {len(vision.models)}",
            )
            _ensure_initialization_active()

            # Models warmup
            monitor.boot_step_start(
                "models_warm", "Прогрев моделей",
            )
            try:
                vision.warmup()
            except Exception as e:
                monitor.boot_step_error(
                    "models_warm",
                    f"Ошибка прогрева моделей: {e}",
                )
                _report_startup_failure()
                return
            monitor.boot_step_done(
                "models_warm", "Прогрев завершён",
            )
            _ensure_initialization_active()

            # Inspection pipeline
            monitor.boot_step_start(
                "inspection", "Настройка системы контроля",
            )
            try:
                threshold_loader = ThresholdLoader()
                thresholds = threshold_loader.get_all()
                decision   = DecisionEngine(thresholds=thresholds)
                recorder = DebugRecorder(
                    folder="debug_frames",
                    enabled=False,
                    save_interval=1,
                )
                inspector = Inspector(
                    vision=vision,
                    decision=decision,
                    recorder=recorder,
                )
                archive_config = load_archive_config()
                archive = PartArchive(
                    root_folder=archive_config["root_path"],
                    enabled=archive_config["enabled"],
                    jpeg_quality=archive_config["jpeg_quality"],
                    zip_compression=archive_config["zip_compression"],
                    zip_level=archive_config["zip_level"],
                    compress_on_shutdown=archive_config["compress_on_shutdown"],
                    delete_original_after_zip=archive_config[
                        "delete_original_after_zip"
                    ],
                )
                monitor.server.archive = archive
                monitor.server.archive_config_path = "archive_config.json"
                if archive.enabled:
                    if archive.startup_error:
                        raise RuntimeError(f"archive startup failed: {archive.startup_error}")
                    archive.validate_root(archive.root_folder)
                    archive.require_space()

                # Редактор порогов правил: сервер отдаёт текущие значения
                # (GET /api/thresholds), а применение изменений пересоздаёт
                # DecisionEngine внутри Inspector'а и сохраняет файл.
                # Пороги автоматически подтягиваются из thresholds.json:
                # ручные правки файла перечитываются без перезапуска.
                monitor.server.thresholds = dict(thresholds)
                monitor.server.threshold_labels = dict(
                    threshold_loader.labels or {}
                )
                monitor.server.thresholds_path = "thresholds.json"

                def _thresholds_reload_from_file(fresh):
                    if inspector is None:
                        raise RuntimeError(
                            "Система контроля ещё не инициализирована"
                        )
                    inspector.decision = DecisionEngine(thresholds=fresh)
                    print(
                        "[THRESHOLDS] Пороги перечитаны из thresholds.json; "
                        "правила пересозданы"
                    )
                    return fresh

                monitor.thresholds_reload_callback = _thresholds_reload_from_file

                def _thresholds_apply(role, values, labels=None):
                    if cycle is None or inspector is None:
                        raise RuntimeError(
                            "Система контроля ещё не инициализирована"
                        )
                    if cycle.state not in ("IDLE", "STOPPED"):
                        raise RuntimeError(
                            "Изменение порогов доступно только до пуска "
                            "и после полной остановки"
                        )
                    if cycle.jog is not None and cycle.jog.status.get("busy"):
                        raise RuntimeError(
                            "Нельзя менять пороги во время движения ленты"
                        )
                    if labels:
                        raise ValueError("labels не входят в operator write API")
                    if not isinstance(values, dict) or not values:
                        raise ValueError("Нет изменённых порогов")
                    updated = ThresholdLoader.operator_update(
                        dict(inspector.decision.thresholds), role, values
                    )
                    changed = [
                        key if str(key).startswith(f"{role}.") else f"{role}.{key}"
                        for key in values
                    ]
                    ThresholdLoader.save_file(
                        "thresholds.json", updated,
                        labels=dict(monitor.server.threshold_labels or {}),
                    )
                    # Правила пересоздаются: Inspector берёт decision каждый
                    # раз заново, поэтому замена объекта применяется сразу.
                    inspector.decision = DecisionEngine(thresholds=updated)
                    print(
                        "[THRESHOLDS] Применено "
                        f"{len(changed)} изменение(й) для {role}: "
                        f"{', '.join(sorted(changed))}"
                    )
                    return updated

                monitor.thresholds_apply_callback = _thresholds_apply
            except Exception as e:
                monitor.boot_step_error(
                    "inspection",
                    f"Ошибка настройки контроля: {e}",
                )
                _report_startup_failure()
                return
            monitor.boot_step_done(
                "inspection",
                f"Настроено правил: {len(decision.rules)}",
            )
            _ensure_initialization_active()

            # Serial (автопоиск)
            monitor.boot_step_start(
                "serial", "Поиск контроллера",
            )
            serial_baud = int(os.environ.get(
                "SERIAL_BAUD", "115200",
            ))
            preferred_port = os.environ.get("SERIAL_PORT")

            try:
                found_port, port_message = find_controller(
                    baudrate=serial_baud,
                    preferred_port=preferred_port,
                )

                if found_port is None:
                    monitor.boot_step_error(
                        "serial", port_message,
                    )
                    _report_startup_failure()
                    return

                transport = SerialTransport(
                    port=found_port, baudrate=serial_baud,
                )
                # BOOT does not guess or repeat a zeroing command.  The
                # controller's reset telemetry is verified by START, while
                # distributor axes are homed once in the hardware gate below.
            except Exception as e:
                monitor.boot_step_error(
                    "serial",
                    f"Ошибка последовательного порта: {e}",
                )
                _report_startup_failure()
                return

            monitor.boot_step_done(
                "serial",
                f"Контроллер: {found_port} @ {serial_baud}",
            )
            _ensure_initialization_active()

            # Hardware
            monitor.boot_step_start(
                "hardware", "Инициализация оборудования",
            )
            try:
                conveyor = Conveyor(
                    transport,
                    speed=calib["conveyor_speed"],
                    accel=calib["conveyor_accel"],
                    steps_per_division=calib["normal_steps"],
                    divisions_per_movement=2,
                )
                dist1_axis = Axis(
                    transport,
                    axis_id=0,
                    minimum=0,
                    maximum=calib["dist1_open_position"],
                    speed=calib["axis_speed"],
                    accel=calib["axis_accel"],
                )
                dist2_axis = Axis(
                    transport,
                    axis_id=1,
                    minimum=0,
                    maximum=max(
                        calib["dist2_bad_position"],
                        calib["dist2_cleanup_position"],
                    ),
                    speed=calib["axis_speed"],
                    accel=calib["axis_accel"],
                )
                distributor = Distributor(
                    dist1_axis=dist1_axis,
                    dist2_axis=dist2_axis,
                    dist1_open_position=calib[
                        "dist1_open_position"
                    ],
                    dist2_bad_position=calib[
                        "dist2_bad_position"
                    ],
                    dist2_cleanup_position=calib[
                        "dist2_cleanup_position"
                    ],
                    drop_time=calib["drop_time"],
                )
                if (
                    distributor.dist1_open_position != calib["dist1_open_position"]
                    or distributor.dist2_bad_position != calib["dist2_bad_position"]
                    or distributor.dist2_cleanup_position
                    != calib["dist2_cleanup_position"]
                ):
                    raise RuntimeError(
                        "Distributor endpoints do not match calibration.json"
                    )
                distributor.cancel_check = shutdown_requested.is_set
                jog = JogController(
                    transport=transport,
                    calibration=calib,
                )
            except Exception as e:
                monitor.boot_step_error(
                    "hardware",
                    f"Ошибка оборудования: {e}",
                )
                _report_startup_failure()
                return
            monitor.boot_step_done(
                "hardware", "Лента и две оси инициализированы",
            )
            _ensure_initialization_active()


            # Production cycle
            monitor.boot_step_start(
                "cycle", "Создание производственного цикла",
            )
            try:
                _ensure_initialization_active()
                print("[HARDWARE] Homing distributor axes...")
                distributor.initialize()
                print("[HARDWARE] Verifying GOOD/CLEANUP/BAD routes...")
                distributor.verify_routes()
                # No conveyor movement is used to establish the logical zero;
                # Conveyor START preflight proves POS=TGT=0 directly.
                conveyor.verify_reset_state()
                _ensure_initialization_active()
                model_identity = {}
                for model_path in getattr(vision, "models", {}):
                    try:
                        model_identity[str(model_path)] = sha256_file(model_path)
                    except OSError as exc:
                        raise RuntimeError(f"model identity unavailable: {model_path}: {exc}") from exc
                manifest = {
                    "app_version": os.environ.get("APP_VERSION", "dev"),
                    "rules_version": os.environ.get("RULES_VERSION", "production"),
                    "models_sha256": model_identity,
                    "machine_config_sha256": machine_config_sha,
                    "threshold_sha256": sha256_file("thresholds.json"),
                }
                if archive is not None:
                    archive.identity_manifest = dict(manifest)
                journal = OperationalLog(
                    path=os.path.join(
                        archive.root_folder if archive and archive.enabled else ".",
                        "production_operational.jsonl",
                    ),
                    enabled=True,
                )
                cycle = ProductionCycle(
                    conveyor=conveyor,
                    cameras=cameras,
                    inspector=inspector,
                    distributor=distributor,
                    monitor=monitor,
                    archive=archive,
                    jog=jog,
                    settle_seconds=calib["settle_time"],
                    stage_trace_seconds=calib["stage_trace_time"],
                    review_seconds=calib["review_time"],
                    journal=journal,
                    manifest=manifest,
                    threshold_revision=manifest["threshold_sha256"],
                    initial_frame_max_age=float(os.environ.get("INITIAL_FRAME_MAX_AGE", "5.0")),
                )
                monitor.server.command_dispatcher = cycle.dispatch_command
                monitor.server.on_hmi_lost = cycle.request_pause
                monitor.start_callback  = cycle.request_start
                monitor.stop_callback   = cycle.request_stop
                monitor.pause_callback  = cycle.request_pause
                monitor.resume_callback = cycle.request_resume
                monitor.exit_callback   = handle_exit_request
                monitor.distributor_diagnostic_callback = (
                    cycle.distributor_diagnostic
                )
                monitor.camera_diagnostic_callback = (
                    cycle.diagnostic_check_cameras
                )
                monitor.vision_rule_diagnostic_callback = (
                    cycle.diagnostic_check_vision_rules
                )
                monitor.selected_model_analysis_callback = (
                    cycle.diagnostic_analyze_selected_camera
                )
                monitor.selected_model_release_callback = (
                    cycle.diagnostic_release_selected_camera
                )
                monitor.active_camera_callback = (
                    lambda _role: cycle._refresh_monitor()
                )

                monitor.jog_enter_callback = cycle.enter_jog
                monitor.jog_exit_callback = cycle.exit_jog
                monitor.jog_hold_start_callback = cycle.jog_hold_start
                monitor.jog_hold_heartbeat_callback = cycle.jog_hold_heartbeat
                monitor.jog_hold_release_callback = cycle.jog_hold_release
            except Exception as e:
                monitor.boot_step_error(
                    "cycle",
                    f"Ошибка создания цикла: {e}",
                )
                _report_startup_failure()
                return
            monitor.boot_step_done("cycle")
            _ensure_initialization_active()

            # Re-warmup before preview (models loading took time). Некоторые
            # UVC-камеры после простоя снова отдают пустые/тёмные кадры;
            # короткой 1с паузы INPUT_LEFT не всегда хватало.
            try:
                quick = _env_clamped_float(
                    "CAMERA_PRE_PREVIEW_WARMUP_SECONDS", 2.5, 0.0, 5.0,
                )
                if quick > 0.0:
                    stats = cameras.warmup_all(duration=quick)
                    _recover_weak_cameras_after_warmup(
                        cameras, stats, "прогрев перед preview",
                    )
            except Exception as exc:
                monitor.boot_step_error(
                    "preview", f"Ошибка прогрева перед preview: {exc}",
                )
                _report_startup_failure()
                return

            # Preview
            monitor.boot_step_start(
                "preview", "Получение начальных кадров",
            )
            try:
                preview_frames = cameras.capture_all()
                monitor.update(
                    frames=preview_frames,
                    vision_results={},
                    rule_results=[],
                    line_status=_make_idle_status(distributor),
                    recent_parts=[],
                )
                monitor.boot_step_done(
                    "preview", "Начальные кадры получены",
                )
                # UI live publication starts before READY and remains alive
                # across IDLE/STOPPED; physical CameraManager readers already
                # started immediately after camera BOOT.
                cycle.live.start()
            except Exception as e:
                monitor.boot_step_error(
                    "preview", f"Ошибка получения начальных кадров: {e}",
                )
                _report_startup_failure()
                return

            _ensure_initialization_active()

            # A process marker is written only after every BOOT gate passes;
            # an unclean process therefore remains visible on next launch.
            boot_coordinator.mark_process_open(archive.batch_id if archive else "unknown")

            # Start cycle thread
            monitor.boot_step_start(
                "ready", "Запуск системы",
            )
            cycle_thread = threading.Thread(
                target=cycle.start, daemon=True,
            )
            cycle_thread.start()
            _ensure_initialization_active()
            monitor.boot_step_done(
                "ready", "Система готова к работе",
            )

            time.sleep(0.6)
            monitor.boot_complete()

        except Exception as e:
            import traceback
            traceback.print_exc()
            current = monitor.server.boot_current or "init"
            monitor.boot_step_error(current, str(e))
            _report_startup_failure()

    init_thread = None
    if previous is None:
        init_thread = threading.Thread(
            target=initialize_system, daemon=True,
        )
        init_thread.start()
    else:
        def acknowledge_cleanup_and_restart(confirmed=True):
            nonlocal init_thread
            boot_coordinator.acknowledge_manual_cleanup(bool(confirmed))
            with monitor.server.lock:
                monitor.server.boot_error = None
                monitor.server.boot_current = None
                monitor.server.boot_message = "Ручная очистка подтверждена; запуск нового batch"
                monitor.server.boot_steps = {
                    key: "pending" for key, _ in monitor.server.BOOT_STEPS
                }
            init_thread = threading.Thread(
                target=initialize_system, daemon=True,
            )
            init_thread.start()
            return True
        monitor.server.on_manual_cleanup = acknowledge_cleanup_and_restart
        monitor.server.manual_cleanup_required = True

    # Signal handler

    def signal_handler(_signum, _frame):
        print("\n[SIGINT] Ctrl+C -> запрос выхода")
        handle_exit_request()

    signal.signal(signal.SIGINT, signal_handler)

    # Console info

    print("=" * 60)
    print("Система запускается.")
    print("  F5 ПУСК | F6 СТОП | TAB вид")
    print("  ESC ВЫХОД (1× штатная остановка, 2× принудительный выход)")
    print("=" * 60)

    # Main: webview blocks here

    try:
        window = webview.create_window(
            title=monitor.window_name,
            url=f"http://{monitor.host}:{monitor.port}/",
            fullscreen=monitor.fullscreen,
            background_color="#0b0f13",
            js_api=monitor.webview_api,
        )
        monitor._webview_window = window
        webview.start()

        print("[UI] Окно закрыто, завершение...")

        if cycle and not cycle.force_exit_requested and not (
            cycle.exit_requested and cycle.state == "STOPPED"
        ):
            cycle.request_force_exit()

        if cycle_thread and cycle_thread.is_alive():
            cycle_thread.join(timeout=CYCLE_JOIN_TIMEOUT)
            if cycle_thread.is_alive():
                print(
                    "[WARN] cycle thread не завершился за "
                    f"{CYCLE_JOIN_TIMEOUT}с"
                )

    finally:
        shutdown_started = time.monotonic()
        print("[SHUTDOWN] Завершение...")
        shutdown_requested.set()
        if cycle is None and transport is not None:
            try:
                transport.send("G1")
                transport.send("G25")
            except Exception as exc:
                print(f"[SHUTDOWN] Startup stop failed: {exc}")
        if init_thread and init_thread.is_alive():
            init_thread.join(timeout=INIT_JOIN_TIMEOUT)
            if init_thread.is_alive():
                print(
                    "[SHUTDOWN] Initialization thread did not stop in "
                    f"{INIT_JOIN_TIMEOUT}s"
                )

        if cycle and not cycle.force_exit_requested and not (
            cycle.exit_requested and cycle.state == "STOPPED"
        ):
            cycle.request_force_exit()
        if cycle_thread and cycle_thread.is_alive():
            cycle_thread.join(timeout=CYCLE_JOIN_TIMEOUT)

        phase_started = time.monotonic()
        try:
            monitor.stop_server()
        except Exception as exc:
            print(f"[SHUTDOWN] UI server stop failed: {exc}")
        print(
            f"[SHUTDOWN] Остановка UI-сервера: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

        phase_started = time.monotonic()
        if archive is not None:
            try:
                archive.close_batch(
                    "CLOSED" if cycle is not None and cycle.state == "STOPPED"
                    and not cycle.force_exit_requested else "ABORTED"
                )
            except Exception as exc:
                print(f"[SHUTDOWN] Batch close failed: {exc}")
        if cycle_thread and cycle_thread.is_alive():
            print("[SHUTDOWN] Cycle still active; archive compression skipped")
        elif cycle is not None and cycle.state == "STOPPED" and not cycle.force_exit_requested:
            _shutdown_compress(archive)
        else:
            print("[SHUTDOWN] Aborted shutdown: secondary ZIP skipped")
        print(
            f"[SHUTDOWN] Архив: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

        phase_started = time.monotonic()
        # Live-просмотр останавливается до освобождения камер: иначе фоновые
        # чтения продолжались бы на уже закрытых VideoCapture.
        if cycle:
            try:
                cycle.live.stop()
            except Exception as exc:
                print(f"[SHUTDOWN] Live preview stop failed: {exc}")
        try:
            if cameras:
                cameras.release()
        except Exception as exc:
            print(f"[SHUTDOWN] Camera release failed: {exc}")
        print(
            f"[SHUTDOWN] Освобождение камер: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

        phase_started = time.monotonic()
        try:
            if transport:
                transport.close()
        except Exception as exc:
            print(f"[SHUTDOWN] Serial close failed: {exc}")
        print(
            f"[SHUTDOWN] Закрытие COM: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

        try:
            if archive is not None:
                status = "closed" if (
                    cycle is not None
                    and cycle.state == "STOPPED"
                    and not cycle.force_exit_requested
                ) else "aborted"
                boot_coordinator.close_process(batch_id=archive.batch_id, status=status)
        except Exception as exc:
            print(f"[SHUTDOWN] Process marker close failed: {exc}")
        print(
            f"[SHUTDOWN] Готово за "
            f"{time.monotonic() - shutdown_started:.2f} с."
        )


# Helpers

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
    """Вернуть роли, которые после прогрева не отдали ни одного кадра.

    Проверка яркости убрана: тёмные кадры после проявления AGC — это
    нормальный переходный процесс. Production-захват ``_grab()`` повторяет
    чтение до 30 раз (2.4 с), пока кадр не станет валидным. Прогрев
    только убеждается, что камера жива и отдаёт поток.
    """
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
    """Догреть камеры, которые после прогрева всё ещё пустые/тёмные.

    Эскалация в два шага: сначала точечный повторный прогрев (лечит
    медленный AGC), затем пересоздание потока через ``reopen_roles``
    (лечит мёртвый UVC-поток, из которого кадры не появятся никогда).
    RuntimeError выбрасывается, только когда не помогло ни то, ни другое.
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

    # Повторное чтение не помогло: дело не в медленном AGC. Скорее
    # всего, UVC-поток мёртв (нет изохронной полосы на хабе или backend
    # собрал битый граф) — такой VideoCapture нужно не читать дальше,
    # а пересоздать, как это и делает production-захват.
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
    return {
        "state":          "IDLE",
        "exit_requested": False,
        "fault_reason":   None,
        "step":           0,
        "in_line":        0,
        "line_parts":     [],
        "total":          0,
        "good":           0,
        "rejected":       0,
        "cleanup":        0,
        "empty":          0,
        "dist1_position": 0,
        "dist1_max":      distributor.dist1_open_position,
        "dist1_state":    "IDLE",
        "dist2_position": 0,
        "dist2_max":      distributor.dist2_cleanup_position,
        "dist2_state":    "IDLE",
        "dist2_target":   "BAD",
        "last_distributor_action": "-",
        "axis_position":     0,
        "axis_max":          distributor.dist1_open_position,
        "distributor_state": "IDLE",
        "process": {
            "phase": "IDLE",
            "label": "Система готова к пуску",
            "step": 0,
            "part_id": None,
            "positions": [],
            "conveyor": {},
            "revision": 0,
            "updated_at": time.time(),
        },
        "diagnostic_allowed": False,
        "diagnostic_busy": False,
        "controls": {
            "start": False,
            "stop": False,
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
            "fps": 0.0,
            "error": None,
        },
        "frame_analysis": {
            "available": False,
            "kind": None,
            "active": False,
            "models": [],
            "rules": [],
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
            "active":      False,
            "can_enter":   False,
            "hold_steps":  0,
            "last_action": "-",
            "busy":        False,
            "direction":   None,
            "error":       None,
            "live_fps":    0.0,
        },
    }


if __name__ == "__main__":
    main()
