import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np

from domain.threshold_loader import ThresholdLoader
from vision.ui.live_monitor import LiveMonitor
from vision.ui.server.server import UIServer

REPO_ROOT = Path(__file__).resolve().parents[1]


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


class ThresholdsApiTests(unittest.TestCase):
    """Редактор порогов правил: GET /api/thresholds и POST /api/thresholds."""

    @classmethod
    def setUpClass(cls):
        cls.thresholds = ThresholdLoader(
            REPO_ROOT / "thresholds.json"
        ).get_all()

    def test_get_thresholds_groups_role_parameters_by_rule(self):
        server = UIServer()
        server.thresholds = dict(self.thresholds)
        payload = server.build_thresholds_payload("TOP")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["role"], "TOP")
        self.assertFalse(payload["editable"])  # splash_active by default
        rules = {group["rule"]: group for group in payload["rules"]}
        self.assertIn("top_contacts", rules)
        self.assertIn("top_platform", rules)
        self.assertIn("top_platform_overlap", rules)
        self.assertIn("top_sinks", rules)
        self.assertIn("top_glass", rules)
        keys = {
            param["key"]
            for group in payload["rules"]
            for param in group["params"]
        }
        role_keys = {
            key.split(".", 1)[1]
            for key in self.thresholds
            if key.startswith("TOP.")
        }
        self.assertEqual(keys, role_keys)
        # У каждого параметра есть метаданные для редактора
        for group in payload["rules"]:
            for param in group["params"]:
                self.assertIn("label", param)
                self.assertIn("step", param)
                self.assertIn("min", param)
                self.assertIn("max", param)
        self.assertEqual(
            payload["values"]["top_contacts_min_confidence"],
            self.thresholds["TOP.top_contacts_min_confidence"],
        )

    def test_get_thresholds_unknown_role_is_not_available(self):
        server = UIServer()
        server.thresholds = dict(self.thresholds)
        payload = server.build_thresholds_payload("NOPE")
        self.assertFalse(payload["available"])

    def test_thresholds_editable_only_when_line_is_idle_or_stopped(self):
        server = UIServer()
        server.splash_active = False
        self.assertFalse(server.thresholds_editable())
        server.line_status = {"state": "IDLE"}
        self.assertTrue(server.thresholds_editable())
        server.line_status = {"state": "STOPPED"}
        self.assertTrue(server.thresholds_editable())
        for state in ("RUNNING", "PAUSED", "STOPPING", "FAULT"):
            server.line_status = {"state": state}
            self.assertFalse(server.thresholds_editable(), state)

    def test_post_thresholds_requires_stopped_line_and_applies_via_callback(self):
        asyncio.run(self._run_post())

    async def _run_post(self):
        server = UIServer()
        server.splash_active = False
        server.line_status = {"state": "RUNNING"}
        server.thresholds = dict(self.thresholds)
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.post("/api/thresholds", json={
                "role": "TOP",
                "values": {"top_contacts_min_confidence": 0.5},
            })
            self.assertEqual(response.status_code, 409)

            server.line_status = {"state": "IDLE"}
            response = await client.post("/api/thresholds", json={
                "role": "TOP",
                "values": {"top_contacts_min_confidence": 0.5},
            })
            # Callback не подключён — сервер честно сообщает об этом
            self.assertEqual(response.status_code, 503)
            self.assertIn("не готова", response.json()["error"])

            applied = {}

            def apply_cb(role, values):
                applied.update({role: values})
                updated = dict(server.thresholds)
                for key, value in values.items():
                    full_key = (
                        f"{role}.{key}"
                        if not key.startswith(f"{role}.")
                        else key
                    )
                    if full_key not in updated:
                        raise ValueError(f"Неизвестный порог: {full_key}")
                    updated[full_key] = value
                return updated

            server.on_thresholds_apply = apply_cb
            response = await client.post("/api/thresholds", json={
                "role": "TOP",
                "values": {"top_contacts_min_confidence": 0.5},
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(applied["TOP"]["top_contacts_min_confidence"], 0.5)
            updated = response.json()["thresholds"]
            self.assertTrue(updated["editable"])
            self.assertEqual(
                updated["values"]["top_contacts_min_confidence"], 0.5,
            )

            # Неизвестный параметр отклоняется
            response = await client.post("/api/thresholds", json={
                "role": "TOP",
                "values": {"nope_parameter": 1},
            })
            self.assertEqual(response.status_code, 400)

    def test_threshold_loader_save_round_trip_and_rejects_bad_value(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "thresholds.json"
            data = dict(self.thresholds)
            data["TOP.top_contacts_min_confidence"] = 0.35
            ThresholdLoader.save_file(str(path), data)
            reloaded = ThresholdLoader(str(path)).get_all()
            self.assertEqual(
                reloaded["TOP.top_contacts_min_confidence"], 0.35,
            )
            self.assertEqual(len(reloaded), len(data))
            bad = dict(data)
            bad["TOP.top_contacts_min_confidence"] = 1.5
            with self.assertRaisesRegex(ValueError, "0..1"):
                ThresholdLoader.validate(bad)

    def test_thresholds_auto_reload_from_file_and_status_revision(self):
        """Пороги сами подтягиваются из thresholds.json при ручной правке."""
        asyncio.run(self._run_auto_reload())

    async def _run_auto_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "thresholds.json"
            ThresholdLoader.save_file(str(path), self.thresholds)

            server = UIServer()
            server.thresholds = dict(self.thresholds)
            server.thresholds_path = str(path)
            server.splash_active = False
            server.line_status = {"state": "IDLE"}

            # Первый вызов: известного mtime нет — файл фиксируется,
            # содержимое не менялось, ревизия не растёт.
            self.assertTrue(server.thresholds_file_mtime_changed())
            self.assertFalse(server.reload_thresholds_from_file())
            self.assertEqual(server.thresholds_revision, 0)

            # Вручную правим файл «снаружи» — как будто оператор отредактировал
            # thresholds.json текстовым редактором.
            time.sleep(0.01)  # гарантировать смену mtime
            modified = dict(self.thresholds)
            modified["TOP.top_contacts_min_confidence"] = 0.77
            ThresholdLoader.save_file(str(path), modified)

            self.assertTrue(server.thresholds_file_mtime_changed())
            reloaded = []
            server.on_thresholds_reload = (
                lambda fresh: reloaded.append(fresh) or fresh
            )
            self.assertTrue(server.reload_thresholds_from_file())
            self.assertEqual(server.thresholds_revision, 1)
            self.assertEqual(
                server.thresholds["TOP.top_contacts_min_confidence"], 0.77,
            )
            self.assertEqual(len(reloaded), 1)
            # После применения mtime запомнен — повторной перезагрузки нет.
            self.assertFalse(server.thresholds_file_mtime_changed())

            # Тот же контент, новое время записи (случай сохранения через UI):
            # ничего не меняем, ревизия не растёт.
            time.sleep(0.01)
            ThresholdLoader.save_file(str(path), modified)
            self.assertTrue(server.thresholds_file_mtime_changed())
            self.assertFalse(server.reload_thresholds_from_file())
            self.assertEqual(server.thresholds_revision, 1)

            # Во время работы линии файл не применяется до остановки.
            time.sleep(0.01)
            modified["TOP.top_contacts_min_confidence"] = 0.55
            ThresholdLoader.save_file(str(path), modified)
            server.line_status = {"state": "RUNNING"}
            self.assertTrue(server.thresholds_file_mtime_changed())
            self.assertFalse(server.reload_thresholds_from_file())
            self.assertEqual(server.thresholds_revision, 1)
            server.line_status = {"state": "STOPPED"}
            self.assertTrue(server.reload_thresholds_from_file())
            self.assertEqual(server.thresholds_revision, 2)
            self.assertEqual(
                server.thresholds["TOP.top_contacts_min_confidence"], 0.55,
            )

            # /api/status сообщает актуальную ревизию порогов.
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8000",
            ) as client:
                response = await client.get("/api/status")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json()["thresholds_revision"], 2,
                )

    def test_operator_workflow_select_camera_edit_save_run_stop_switch(self):
        """Полный цикл оператора: выбор камеры → правка → сохранение →
        запуск анализа → остановка → повторная правка → переключение
        на другую камеру и тот же цикл для неё."""
        asyncio.run(self._run_operator_workflow())

    async def _run_operator_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "thresholds.json"
            ThresholdLoader.save_file(str(path), self.thresholds)

            server = UIServer()
            server.thresholds = dict(self.thresholds)
            server.thresholds_path = str(path)
            server.splash_active = False
            server.line_status = {"state": "IDLE"}

            def apply_cb(role, values):
                updated = dict(server.thresholds)
                for key, value in values.items():
                    full_key = (
                        f"{role}.{key}"
                        if not key.startswith(f"{role}.")
                        else key
                    )
                    if full_key not in updated:
                        raise ValueError(f"Неизвестный порог: {full_key}")
                    updated[full_key] = value
                ThresholdLoader.validate(updated)
                ThresholdLoader.save_file(str(path), updated)
                return updated

            server.on_thresholds_apply = apply_cb
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8000",
            ) as client:
                # 1. Выбрали камеру TOP — приходят только пороги её правил.
                response = await client.get(
                    "/api/thresholds", params={"role": "TOP"},
                )
                self.assertEqual(response.status_code, 200)
                top = response.json()
                top_keys = {
                    param["key"]
                    for group in top["rules"]
                    for param in group["params"]
                }
                self.assertIn("top_contacts_min_confidence", top_keys)
                self.assertIn("top_platform_overlap_margin_px", top_keys)
                # Чужие камеры не подмешиваются.
                self.assertNotIn("spider_contacts_long_min_confidence", top_keys)
                self.assertNotIn("input_window_geometry_min_confidence", top_keys)

                # 2. Изменили и сохранили.
                response = await client.post("/api/thresholds", json={
                    "role": "TOP",
                    "values": {"top_contacts_min_confidence": 0.5},
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json()["thresholds"]["values"][
                        "top_contacts_min_confidence"
                    ], 0.5,
                )

                # 3. Запустили анализ — линия работает, правка недоступна.
                server.line_status = {"state": "RUNNING"}
                self.assertFalse(server.thresholds_editable())
                response = await client.post("/api/thresholds", json={
                    "role": "TOP",
                    "values": {"top_contacts_min_confidence": 0.6},
                })
                self.assertEqual(response.status_code, 409)

                # 4. Остановились — значения на месте, можно снова менять.
                server.line_status = {"state": "STOPPED"}
                self.assertTrue(server.thresholds_editable())
                response = await client.get(
                    "/api/thresholds", params={"role": "TOP"},
                )
                self.assertEqual(
                    response.json()["values"][
                        "top_contacts_min_confidence"
                    ], 0.5,
                )
                response = await client.post("/api/thresholds", json={
                    "role": "TOP",
                    "values": {"top_contacts_min_confidence": 0.55},
                })
                self.assertEqual(response.status_code, 200)

                # 5. Переключились на SPIDER_LEFT — свои пороги, TOP не виден.
                response = await client.get(
                    "/api/thresholds", params={"role": "SPIDER_LEFT"},
                )
                self.assertEqual(response.status_code, 200)
                spider = response.json()
                spider_keys = {
                    param["key"]
                    for group in spider["rules"]
                    for param in group["params"]
                }
                self.assertIn("spider_contacts_long_min_confidence", spider_keys)
                self.assertIn("spider_long_omission_allowed_thickness_px", spider_keys)
                self.assertNotIn("top_contacts_min_confidence", spider_keys)
                response = await client.post("/api/thresholds", json={
                    "role": "SPIDER_LEFT",
                    "values": {"spider_contacts_long_min_confidence": 0.4},
                })
                self.assertEqual(response.status_code, 200)

                # 6. Файл хранит все изменения по обеим камерам.
                saved = ThresholdLoader(str(path)).get_all()
                self.assertEqual(
                    saved["TOP.top_contacts_min_confidence"], 0.55,
                )
                self.assertEqual(
                    saved["SPIDER_LEFT.spider_contacts_long_min_confidence"], 0.4,
                )


if __name__ == "__main__":
    unittest.main()
