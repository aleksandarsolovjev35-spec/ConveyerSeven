"""Интеграционный HTTP-тест UIServer через FastAPI TestClient.

Проверяет реальный HTTP-путь: /api/status, /frame (RAW/RULES/run/preview),
/api/mode, /api/active_camera, /api/cameras и обработку edge cases.
"""

import unittest

import numpy as np

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - зависит от окружения
    TestClient = None

from domain.defect_rules.base import RuleResult
from vision.ui.server.server import UIServer


@unittest.skipIf(TestClient is None, "fastapi.testclient не доступен "
                                      "(нужен httpx2 или совместимый starlette)")
class UIServerHttpTest(unittest.TestCase):
    def setUp(self):
        self.server = UIServer()
        self.client = TestClient(self.server.app)

    def _publish_analysis(self):
        frame = np.full((240, 320, 3), 100, dtype=np.uint8)
        rule = RuleResult(
            "window_geometry", False, details={"per_role": {}}, drawings=[],
        )
        self.server.update(
            frames={"INPUT_LEFT": frame, "INPUT_RIGHT": frame},
            vision_results={"INPUT_LEFT": [{"class": "flatness"}]},
            rule_results=[rule],
            run_frames=[{"INPUT_LEFT": frame}],
            run_rule_results=[[rule]],
            line_status={
                "state": "RUNNING",
                "step": 1,
                "line_parts": [{"id": 1, "position": 0, "category": "GOOD"}],
            },
            recent_parts=[{"id": 1, "decision": "none",
                           "category": "GOOD", "time": 1}],
        )
        self.server.boot_complete()

    def test_status_flow(self):
        # До публикации
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["frame_runs"], 0)
        self.assertTrue(r.json()["splash_active"])

        self._publish_analysis()

        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["splash_active"])
        self.assertEqual(body["frame_runs"], 1)
        self.assertEqual(body["line_status"]["state"], "RUNNING")
        self.assertEqual(body["line_status"]["line_parts"][0]["category"],
                         "GOOD")
        self.assertEqual(body["mode"], "RULES")
        self.assertIsInstance(body["frame_version"], int)
        self.assertIsInstance(body["frame_versions"], dict)

    def test_frames(self):
        self._publish_analysis()

        for query in ("?mode=RULES", "?mode=RULES&run=1", "?mode=RAW",
                      "?mode=RULES&preview=1"):
            r = self.client.get(f"/frame/INPUT_LEFT{query}")
            self.assertEqual(r.status_code, 200, query)
            self.assertTrue(
                r.headers["content-type"].startswith("image/jpeg"), query,
            )
            self.assertGreater(len(r.content), 0, query)

        r = self.client.get("/frame/UNKNOWN")
        self.assertEqual(r.status_code, 404)

    def test_mode_switch(self):
        self._publish_analysis()

        r = self.client.post("/api/mode/RAW")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "RAW")
        r = self.client.get("/api/mode")
        self.assertEqual(r.json()["mode"], "RAW")

        r = self.client.post("/api/mode/XXX")
        self.assertEqual(r.status_code, 400)

    def test_active_camera(self):
        self._publish_analysis()

        r = self.client.post("/api/active_camera/INPUT_RIGHT")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["active_camera"], "INPUT_RIGHT")

        r = self.client.post("/api/active_camera/BAD_ROLE")
        self.assertEqual(r.status_code, 400)

    def test_commands_without_callbacks(self):
        self._publish_analysis()

        for path in ("/api/start", "/api/stop", "/api/pause", "/api/resume",
                     "/api/exit"):
            r = self.client.post(path)
            self.assertEqual(r.status_code, 503, path)

    def test_cameras_list(self):
        self._publish_analysis()

        r = self.client.get("/api/cameras")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            set(r.json()["cameras"]),
            {"INPUT_LEFT", "INPUT_RIGHT"},
        )


if __name__ == "__main__":
    unittest.main()
