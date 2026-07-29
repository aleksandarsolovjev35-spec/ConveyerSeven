import asyncio
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from vision.ui.live_monitor import LiveMonitor
from vision.ui.server.server import UIServer


class ApiTests(unittest.TestCase):
    def test_windows_ui_server_uses_selector_policy_for_clean_stream_disconnects(self):
        import vision.ui.server.server as module

        class FakeSelectorPolicy:
            pass

        with (
            patch.object(module.sys, "platform", "win32"),
            patch.object(
                module.asyncio,
                "WindowsSelectorEventLoopPolicy",
                FakeSelectorPolicy,
                create=True,
            ),
            patch.object(
                module.asyncio,
                "get_event_loop_policy",
                return_value=object(),
            ),
            patch.object(module.asyncio, "set_event_loop_policy") as setter,
        ):
            self.assertTrue(UIServer._configure_windows_event_loop_policy())
            setter.assert_called_once()
            self.assertIsInstance(setter.call_args.args[0], FakeSelectorPolicy)

    def test_ui_server_suppresses_only_expected_connection_resets(self):
        loop = type("Loop", (), {"default_exception_handler": lambda self, context: setattr(self, "context", context)})()
        UIServer._quiet_connection_reset_handler(
            loop, {"exception": ConnectionResetError(10054, "reset")},
        )
        self.assertFalse(hasattr(loop, "context"))
        context = {"exception": RuntimeError("unexpected")}
        UIServer._quiet_connection_reset_handler(loop, context)
        self.assertIs(loop.context, context)

    def test_mjpeg_stream_has_separate_raw_and_rules_views(self):
        server = UIServer()
        frame = np.full((72, 128, 3), 80, dtype=np.uint8)
        server.update(
            frames={"TOP": frame},
            vision_results={"TOP": []},
            rule_results=[],
        )
        raw, raw_version = server.get_stream_jpeg("TOP", "RAW")
        rules, rules_version = server.get_stream_jpeg("TOP", "RULES")
        self.assertTrue(raw)
        self.assertTrue(rules)
        self.assertEqual(raw_version, rules_version)
        self.assertIn(("TOP", "RAW"), server._latest_stream_jpeg)
        self.assertIn(("TOP", "RULES"), server._latest_stream_jpeg)

    def test_live_monitor_propagates_callback_results_and_errors(self):
        monitor = LiveMonitor(start_callback=lambda: False, fullscreen=False)
        self.assertFalse(monitor.server.on_start())

        def fail():
            raise RuntimeError("callback failed")

        monitor.start_callback = fail
        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            monitor.server.on_start()
        commands = []
        monitor.distributor_diagnostic_callback = (
            lambda command: commands.append(command) or True
        )
        self.assertTrue(monitor.server.on_distributor_diagnostic("DIST2_BAD"))
        self.assertEqual(commands, ["DIST2_BAD"])
        checks = []
        monitor.camera_diagnostic_callback = (
            lambda: checks.append("cameras") or True
        )
        monitor.vision_rule_diagnostic_callback = (
            lambda: checks.append("vision_rules") or True
        )
        self.assertTrue(monitor.server.on_camera_diagnostic())
        self.assertTrue(monitor.server.on_vision_rule_diagnostic())
        monitor.selected_model_analysis_callback = (
            lambda role: checks.append(role) or True
        )
        monitor.selected_model_release_callback = (
            lambda: checks.append("release") or True
        )
        self.assertTrue(monitor.server.on_selected_model_analysis("TOP"))
        self.assertTrue(monitor.server.on_selected_model_release())
        self.assertEqual(checks, ["cameras", "vision_rules", "TOP", "release"])
        exits = []
        monitor.exit_callback = lambda: exits.append("exit") or True
        self.assertTrue(monitor.server.on_exit())
        self.assertEqual(exits, ["exit"])

    def test_commands_report_not_ready_rejected_and_success(self):
        asyncio.run(self._run())

    async def _run(self):
        server = UIServer()
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.post("/api/start")
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.json()["ok"])

            server.on_start = lambda: False
            response = await client.post("/api/start")
            self.assertEqual(response.status_code, 409)
            self.assertFalse(response.json()["ok"])

            server.on_start = lambda: True
            response = await client.post("/api/start")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])

            server.on_pause = lambda: True
            response = await client.post("/api/pause")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])

            server.on_resume = lambda: True
            response = await client.post("/api/resume")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])

            response = await client.post("/api/mode/RAW")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["mode"], "RAW")
            self.assertIsInstance(response.json()["frame_version"], int)

            response = await client.post("/api/active_camera/TOP")
            self.assertEqual(response.status_code, 400)
            server.update(
                frames={"TOP": np.full((72, 128, 3), 80, dtype=np.uint8)}
            )
            response = await client.post("/api/active_camera/TOP")
            self.assertEqual(response.status_code, 200)
            response = await client.get("/api/status")
            self.assertEqual(response.json()["frame_versions"]["TOP"], 1)

            prestart = []
            server.on_camera_diagnostic = (
                lambda: prestart.append("cameras") or True
            )
            server.on_vision_rule_diagnostic = (
                lambda: prestart.append("vision_rules") or True
            )
            response = await client.post("/api/diagnostics/cameras")
            self.assertEqual(response.status_code, 200)
            response = await client.post("/api/diagnostics/vision-rules")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(prestart, ["cameras", "vision_rules"])

            selected = []
            server.on_selected_model_analysis = (
                lambda role: selected.append(role) or True
            )
            server.on_selected_model_release = (
                lambda: selected.append("release") or True
            )
            response = await client.post("/api/diagnostics/selected/TOP")
            self.assertEqual(response.status_code, 200)
            response = await client.post("/api/diagnostics/selected/release")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(selected, ["TOP", "release"])

            diagnostics = []
            server.on_distributor_diagnostic = (
                lambda command: diagnostics.append(command) or True
            )
            response = await client.post(
                "/api/distributor/diagnostic/DIST1_OPEN"
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(diagnostics, ["DIST1_OPEN"])
            response = await client.post(
                "/api/distributor/diagnostic/INVALID"
            )
            self.assertEqual(response.status_code, 400)

            server.on_jog_hold_start = lambda direction: False
            response = await client.post(
                "/api/jog/hold/start",
                json={"direction": "+"},
            )
            self.assertEqual(response.status_code, 409)

            response = await client.post(
                "/api/jog/hold/start",
                json={"direction": "invalid"},
            )
            self.assertEqual(response.status_code, 400)

            server.on_jog_hold_heartbeat = lambda direction: True
            response = await client.post(
                "/api/jog/hold/heartbeat",
                json={"direction": "-"},
            )
            self.assertEqual(response.status_code, 200)

            server.on_jog_hold_release = lambda reason: True
            response = await client.post(
                "/api/jog/hold/release",
                json={"reason": "test release"},
            )
            self.assertEqual(response.status_code, 200)

    def test_boot_messages_are_formatted_on_a_single_line(self):
        """Любое сообщение загрузки обязано выводиться в одну строку."""
        server = UIServer()
        server.boot_step_start("serial", "Поиск контроллера\r\nна COM-портах")
        self.assertEqual(server.boot_message, "Поиск контроллера на COM-портах")
        self.assertIn("Поиск контроллера на COM-портах", server.splash_log)

        server.boot_step_error("serial", "Ошибка порта:\n\tCOM1 занят")
        self.assertEqual(server.boot_message, "ОШИБКА: Ошибка порта: COM1 занят")
        self.assertEqual(server.boot_error, "Ошибка порта: COM1 занят")
        self.assertIn("[ОШИБКА] Ошибка порта: COM1 занят", server.splash_log)


if __name__ == "__main__":
    unittest.main()
