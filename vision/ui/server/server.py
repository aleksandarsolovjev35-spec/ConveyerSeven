import asyncio
import sys
import threading
import time
from pathlib import Path

import cv2
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

from vision.overlay.raw_overlay import RawOverlay
from vision.overlay.debug_overlay import DebugOverlay

from vision.ui.server.routes_frames  import setup_frame_routes
from vision.ui.server.routes_api     import setup_api_routes
from vision.ui.server.routes_archive import setup_archive_routes


_UI_DIR        = Path(__file__).parent.parent
_TEMPLATES_DIR = _UI_DIR / "templates"
_STATIC_DIR    = _UI_DIR / "static"


BOOT_STEPS = [
    ("cameras",       "Камеры"),
    ("camera_warmup", "Прогрев камер"),
    ("models_load",   "Загрузка моделей"),
    ("models_warm",   "Прогрев моделей"),
    ("inspection",    "Система контроля"),
    ("serial",        "Контроллер"),
    ("hardware",      "Оборудование"),
    ("cycle",         "Производственный цикл"),
    ("preview",       "Начальные кадры"),
    ("ready",         "Готовность"),
]


CAMERA_ORDER = [
    "INPUT_LEFT",
    "INPUT_RIGHT",
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
]


class UIServer:

    JPEG_QUALITY        = 70
    STREAM_JPEG_QUALITY = 60
    PREVIEW_MAX_WIDTH   = 320
    BOOT_STEPS          = BOOT_STEPS

    def __init__(self):
        self.frames: dict         = {}
        self.vision_results: dict = {}
        self.rule_results: list   = []
        self.line_status: dict    = {}
        self.recent_parts: list   = []

        self.splash_active = True
        self.splash_log    = []
        self.boot_steps    = {
            key: "pending" for key, _ in BOOT_STEPS
        }
        self.boot_current  = None
        self.boot_message  = "Запуск..."
        self.boot_error    = None

        self.mode = "RULES"

        self.active_camera_role: str | None = None

        self.on_start:  callable | None = None
        self.on_stop:   callable | None = None
        self.on_pause:  callable | None = None
        self.on_resume: callable | None = None
        self.on_exit:   callable | None = None
        self.on_distributor_diagnostic: callable | None = None
        self.on_camera_diagnostic: callable | None = None
        self.on_vision_rule_diagnostic: callable | None = None
        self.on_selected_model_analysis: callable | None = None
        self.on_selected_model_release: callable | None = None

        self.on_jog_enter: callable | None = None
        self.on_jog_exit: callable | None = None
        self.on_jog_hold_start: callable | None = None
        self.on_jog_hold_heartbeat: callable | None = None
        self.on_jog_hold_release: callable | None = None

        self.archive = None

        # Кэш JPEG для pull-механики (/frame): ключ (role, mode, size)
        self._jpeg_cache: dict = {}
        self._cache_version = 0

        # Версия каждого кадра (растёт при update)
        self._latest_frames_ver: dict = {}

        # Ленивый кэш JPEG для stream: role -> (jpeg_bytes, version)
        self._latest_stream_jpeg: dict = {}

        self.lock = threading.Lock()

        self.app = FastAPI(title="Роботехнический комплекс конвейерного типа 7")

        self._setup_static()
        self._setup_routes()

        self._server_thread: threading.Thread | None = None
        self._uvicorn_server: uvicorn.Server | None = None
        self._server_loop = None

    # Public API

    def update(
        self,
        frames=None,
        vision_results=None,
        rule_results=None,
        line_status=None,
        recent_parts=None,
    ):
        with self.lock:
            should_invalidate = False
            stream_overlay_changed = False
            if frames is not None:
                for role, frame in frames.items():
                    self.frames[role] = frame
                    self._latest_frames_ver[role] = (
                        self._latest_frames_ver.get(role, 0) + 1
                    )
                should_invalidate = True
            if vision_results is not None:
                self.vision_results = vision_results
                should_invalidate = True
                stream_overlay_changed = True
            if rule_results is not None:
                self.rule_results = list(rule_results)
                should_invalidate = True
                stream_overlay_changed = True
            if line_status is not None:
                self.line_status = line_status
            if recent_parts is not None:
                self.recent_parts = list(recent_parts)
            if should_invalidate:
                self._jpeg_cache.clear()
                if stream_overlay_changed:
                    self._latest_stream_jpeg.clear()
                self._cache_version += 1

    def set_active_camera_role(self, role: str) -> bool:
        with self.lock:
            if not role or role not in self.frames:
                return False
            self.active_camera_role = role
        return True

    def boot_step_start(self, key, message=None):
        with self.lock:
            if key in self.boot_steps:
                self.boot_steps[key] = "running"
                self.boot_current = key
            if message:
                self.boot_message = message
                self._append_log(message)

    def boot_step_done(self, key, message=None):
        with self.lock:
            if key in self.boot_steps:
                self.boot_steps[key] = "done"
            if message:
                self._append_log(message)

    def boot_step_error(self, key, message):
        label = dict(BOOT_STEPS).get(key, key)
        print(f"[BOOT] Ошибка этапа '{label}': {message}")
        with self.lock:
            if key in self.boot_steps:
                self.boot_steps[key] = "error"
            self.boot_error = message
            self.boot_message = f"ОШИБКА: {message}"
            self._append_log(f"[ОШИБКА] {message}")

    def boot_complete(self):
        with self.lock:
            self.splash_active = False
            self.boot_message = "Готово"

    def set_splash_status(self, text):
        with self.lock:
            self.boot_message = text
            self._append_log(text)

    def _append_log(self, text):
        self.splash_log.append(text)
        if len(self.splash_log) > 30:
            self.splash_log = self.splash_log[-30:]

    @staticmethod
    def _configure_windows_event_loop_policy():
        """Избежать шумных WinError 10054 от Proactor при закрытии MJPEG."""
        if sys.platform != "win32":
            return False
        policy_class = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if policy_class is None:
            return False
        if not isinstance(asyncio.get_event_loop_policy(), policy_class):
            asyncio.set_event_loop_policy(policy_class())
        return True

    @staticmethod
    def _quiet_connection_reset_handler(loop, context):
        exception = context.get("exception")
        if isinstance(exception, ConnectionResetError):
            return
        loop.default_exception_handler(context)

    def _run_server_thread(self):
        if sys.platform != "win32":
            self._uvicorn_server.run()
            return

        # Явно создаём SelectorEventLoop: одной смены policy недостаточно для
        # некоторых сочетаний Python 3.11 + WebView2 + uvicorn.
        loop = asyncio.SelectorEventLoop()
        self._server_loop = loop
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(self._quiet_connection_reset_handler)
        try:
            loop.run_until_complete(self._uvicorn_server.serve())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                self._server_loop = None

    def start_server(self, host="127.0.0.1", port=8000):
        self._configure_windows_event_loop_policy()
        config = uvicorn.Config(
            self.app, host=host, port=port,
            log_level="warning", access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._server_thread = threading.Thread(
            target=self._run_server_thread, daemon=True,
        )
        self._server_thread.start()
        time.sleep(0.5)
        if not self._server_thread.is_alive():
            raise RuntimeError(f"UI server failed to start on {host}:{port}")
        print(f"[UI SERVER] http://{host}:{port}")

    def stop_server(self, timeout: float = 5.0):
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
        thread = self._server_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("UI server thread did not stop")
        self._server_thread = None
        self._uvicorn_server = None

    def get_server_thread(self) -> threading.Thread | None:
        return self._server_thread

    # Stream helpers

    def get_frame_version(self, role: str) -> int:
        with self.lock:
            return self._latest_frames_ver.get(role, 0)

    def get_stream_jpeg(self, role: str, mode: str = "RAW") -> tuple:
        """Вернуть JPEG и версию кадра для RAW/RULES MJPEG-стрима."""
        actual_mode = mode if mode in ("RAW", "RULES") else self.mode
        cache_key = (role, actual_mode)
        with self.lock:
            frame = self.frames.get(role)
            if frame is None:
                return None, 0

            current_ver = self._latest_frames_ver.get(role, 0)
            cached = self._latest_stream_jpeg.get(cache_key)

            if cached is not None:
                cached_jpeg, cached_ver = cached
                if cached_ver == current_ver:
                    return cached_jpeg, cached_ver

            frame_copy = frame.copy()
            vision_dets = (
                list(self.vision_results.get(role, []))
                if actual_mode == "RAW" else None
            )
            rule_results = (
                list(self.rule_results)
                if actual_mode == "RULES" else None
            )

        rendered = self._render(
            frame_copy,
            role,
            actual_mode,
            vision_dets,
            rule_results,
        )
        jpeg = self._encode_jpeg(rendered, self.STREAM_JPEG_QUALITY)

        with self.lock:
            actual_ver = self._latest_frames_ver.get(role, 0)
            if actual_ver == current_ver:
                self._latest_stream_jpeg[cache_key] = (jpeg, current_ver)

        return jpeg, current_ver

    # Internal setup

    def _setup_static(self):
        if _STATIC_DIR.exists():
            self.app.mount(
                "/static",
                StaticFiles(directory=str(_STATIC_DIR)),
                name="static",
            )

    def _setup_routes(self):
        @self.app.get("/")
        async def index():
            return FileResponse(
                str(_TEMPLATES_DIR / "index.html"),
            )

        setup_frame_routes(self.app, self)
        setup_api_routes(self.app, self)
        setup_archive_routes(self.app, self)

    # Rendering & caching (pull)

    def _get_or_render(self, role, mode, size_kind):
        cache_key = (role, mode, size_kind)
        with self.lock:
            cached = self._jpeg_cache.get(cache_key)
            if cached is not None:
                return cached
            frame = self.frames.get(role)
            if frame is None:
                return None
            version_before = self._cache_version
            frame_copy = frame.copy()
            vision_dets = (
                list(self.vision_results.get(role, []))
                if mode == "RAW" else None
            )
            rule_results = (
                list(self.rule_results)
                if mode == "RULES" else None
            )

        rendered = self._render(
            frame_copy, role, mode, vision_dets, rule_results,
        )
        if size_kind == "preview":
            rendered = self._resize_for_preview(rendered)
        jpeg = self._encode_jpeg(rendered, self.JPEG_QUALITY)

        with self.lock:
            if self._cache_version == version_before:
                self._jpeg_cache[cache_key] = jpeg
        return jpeg

    def _render(
        self, frame, role, mode, vision_dets, rule_results,
    ):
        if mode == "RAW":
            if vision_dets:
                return RawOverlay.render(frame, vision_dets)
            return frame.copy()

        if rule_results:
            return DebugOverlay.render_frame(
                frame, role, rule_results,
            )
        return frame.copy()

    @staticmethod
    def _resize_for_preview(frame):
        h, w = frame.shape[:2]
        if w <= UIServer.PREVIEW_MAX_WIDTH:
            return frame
        scale = UIServer.PREVIEW_MAX_WIDTH / w
        return cv2.resize(
            frame,
            (UIServer.PREVIEW_MAX_WIDTH, int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )

    @staticmethod
    def _encode_jpeg(frame, quality: int | None = None):
        q = quality if quality is not None else UIServer.JPEG_QUALITY
        ok, buf = cv2.imencode(
            ".jpg", frame,
            [cv2.IMWRITE_JPEG_QUALITY, q],
        )
        return buf.tobytes() if ok else b""

    # Helpers

    @staticmethod
    def _sort_by_order(roles: list) -> list:
        known = [r for r in CAMERA_ORDER if r in roles]
        unknown = sorted(
            r for r in roles if r not in CAMERA_ORDER
        )
        return known + unknown
