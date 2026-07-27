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

    def test_pause_resume_and_bounded_nudge_endpoints(self):
        asyncio.run(self._run_pause())

    def test_nudge_hold_endpoints(self):
        asyncio.run(self._run_nudge_hold())

    async def _run_nudge_hold(self):
        server = UIServer()
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
        ) as client:
            # До готовности backend удержание обязано отклоняться.
            response = await client.post(
                "/api/nudge/hold/start", json={"direction": "+"},
            )
            self.assertEqual(response.status_code, 503)

            calls = []
            server.on_nudge_hold_start = (
                lambda direction: calls.append(("start", direction)) or True
            )
            server.on_nudge_hold_heartbeat = (
                lambda direction: calls.append(("beat", direction)) or True
            )
            server.on_nudge_hold_release = (
                lambda reason: calls.append(("release", reason)) or True
            )

            response = await client.post(
                "/api/nudge/hold/start", json={"direction": "+"},
            )
            self.assertEqual(response.status_code, 200)
            response = await client.post(
                "/api/nudge/hold/heartbeat", json={"direction": "+"},
            )
            self.assertEqual(response.status_code, 200)
            response = await client.post(
                "/api/nudge/hold/release", json={"reason": "pointerup"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                calls,
                [("start", "+"), ("beat", "+"), ("release", "pointerup")],
            )

            # Некорректное направление не должно доходить до железа.
            response = await client.post(
                "/api/nudge/hold/start", json={"direction": "up"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(len(calls), 3)

            # Запрет в текущем состоянии — 409, отказ железа — 500.
            server.on_nudge_hold_start = lambda direction: False
            response = await client.post(
                "/api/nudge/hold/start", json={"direction": "-"},
            )
            self.assertEqual(response.status_code, 409)

            def failing_release(reason):
                raise RuntimeError("stop failed")

            server.on_nudge_hold_release = failing_release
            response = await client.post("/api/nudge/hold/release", json={})
            self.assertEqual(response.status_code, 500)
            self.assertFalse(response.json()["ok"])

    async def _run_pause(self):
        server = UIServer()
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
        ) as client:
            # Пока backend не готов, команды паузы обязаны отклоняться.
            for path in ("/api/pause", "/api/resume", "/api/nudge/forward"):
                response = await client.post(path)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json()["ok"])

            # Недоступность в текущем состоянии — 409, а не тихий успех.
            server.on_pause = lambda: False
            response = await client.post("/api/pause")
            self.assertEqual(response.status_code, 409)

            calls = []
            server.on_pause = lambda: calls.append("pause") or True
            server.on_resume = lambda: calls.append("resume") or True
            server.on_nudge = lambda direction: calls.append(direction) or True

            response = await client.post("/api/pause")
            self.assertEqual(response.status_code, 200)
            response = await client.post("/api/nudge/forward")
            self.assertEqual(response.status_code, 200)
            response = await client.post("/api/nudge/backward")
            self.assertEqual(response.status_code, 200)
            response = await client.post("/api/resume")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(calls, ["pause", "+", "-", "resume"])

            # Произвольное направление не должно доходить до железа.
            response = await client.post("/api/nudge/sideways")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(len(calls), 4)

            # Отказ железа обязан возвращать 500, а не «ok».
            def failing_nudge(direction):
                raise RuntimeError("motion rejected")

            server.on_nudge = failing_nudge
            response = await client.post("/api/nudge/forward")
            self.assertEqual(response.status_code, 500)
            self.assertFalse(response.json()["ok"])

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


if __name__ == "__main__":
    unittest.main()
