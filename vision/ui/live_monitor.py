"""UI facade that emits intents rather than invoking production hardware."""

from __future__ import annotations

from typing import Any

from core.event_bus import EventBus
from vision.ui.server.server import UIServer


class LiveMonitorApi:
    """Native bridge for presentation-only operations unavailable to browsers."""

    def __init__(self, monitor: "LiveMonitor") -> None:
        self.monitor = monitor

    def choose_archive_folder(self) -> dict[str, Any]:
        """Open the operating system's directory selector through pywebview."""
        window = self.monitor._webview_window
        if window is None:
            return {"ok": False, "error": "Окно интерфейса ещё не готово"}
        try:
            import webview

            selected = window.create_file_dialog(webview.FOLDER_DIALOG)
        except (ImportError, RuntimeError, OSError) as error:
            return {"ok": False, "error": str(error)}
        if not selected:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": str(selected[0])}


class LiveMonitor:
    """Presentation facade; it emits UI intents and never owns equipment."""

    def __init__(
        self,
        event_bus: EventBus,
        window_name: str = "РОБОТЕХНИЧЕСКИЙ КОМПЛЕКС КОНВЕЙЕРНОГО ТИПА 7",
        host: str = "127.0.0.1",
        port: int = 8000,
        fullscreen: bool = True,
    ) -> None:
        """Create the HMI adapter attached only to an event bus."""
        self.window_name = window_name
        self.host = host
        self.port = port
        self.fullscreen = fullscreen
        self._event_bus = event_bus
        self.server = UIServer()
        self._bind_server_events()
        self._webview_window: Any | None = None
        self.webview_api = LiveMonitorApi(self)
        self._close_requested = False

    def set_splash_status(self, text: str) -> None:
        """Publish boot status text to the UI server."""
        self.server.set_splash_status(text)

    def boot_step_start(self, key: str, message: str | None = None) -> None:
        """Mark one boot step as active."""
        self.server.boot_step_start(key, message)

    def boot_step_done(self, key: str, message: str | None = None) -> None:
        """Mark one boot step as complete."""
        self.server.boot_step_done(key, message)

    def boot_step_error(self, key: str, message: str) -> None:
        """Expose a boot failure to the operator."""
        self.server.boot_step_error(key, message)

    def boot_complete(self) -> None:
        """Dismiss the boot splash screen."""
        self.server.boot_complete()

    def update(self, **kwargs: Any) -> None:
        """Update presentation state from a validated core snapshot."""
        self.server.update(**kwargs)

    def close_window(self) -> None:
        """Close the webview window once."""
        if self._webview_window is None or self._close_requested:
            return
        self._close_requested = True
        self._webview_window.destroy()

    def stop_server(self, timeout: float = 3.0) -> None:
        """Stop the UI server and wait a bounded time for its thread."""
        self.server.stop_server()
        thread = self.server.get_server_thread()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _emit(self, event_name: str, *args: Any) -> Any:
        """Emit an intent and return the last handler's result to the API."""
        results = self._event_bus.emit(event_name, *args)
        return results[-1] if results else False

    def _bind_server_events(self) -> None:
        """Map HTTP actions to named intents, without core callback references."""
        mappings: dict[str, str] = {
            "on_start": "ui:start_requested", "on_stop": "ui:stop_requested",
            "on_pause": "ui:pause_requested", "on_resume": "ui:resume_requested",
            "on_exit": "ui:exit_requested", "on_camera_diagnostic": "ui:camera_diagnostic_requested",
            "on_vision_rule_diagnostic": "ui:vision_rule_diagnostic_requested",
            "on_selected_model_release": "ui:selected_model_release_requested",
            "on_jog_enter": "ui:jog_enter_requested", "on_jog_exit": "ui:jog_exit_requested",
        }
        for attribute, event_name in mappings.items():
            setattr(self.server, attribute, lambda event=event_name: self._emit(event))
        self.server.on_distributor_diagnostic = lambda command: self._emit("ui:distributor_diagnostic_requested", command)
        self.server.on_selected_model_analysis = lambda role: self._emit("ui:selected_model_analysis_requested", role)
        self.server.on_active_camera_changed = lambda role: self._emit("ui:active_camera_changed", role)
        self.server.on_jog_hold_start = lambda direction: self._emit("ui:jog_hold_start_requested", direction)
        self.server.on_jog_hold_heartbeat = lambda direction: self._emit("ui:jog_hold_heartbeat", direction)
        self.server.on_jog_hold_release = lambda reason="button released": self._emit("ui:jog_hold_release_requested", reason)
        self.server.on_thresholds_apply = lambda role, values, labels: self._emit("ui:thresholds_apply_requested", role, values, labels)
        self.server.on_thresholds_reload = lambda fresh: self._emit("ui:thresholds_reload_requested", fresh)
