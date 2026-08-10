"""Тесты упрощённого UIServer: атомарная публикация + HTTP-эндпоинты."""

import unittest

import numpy as np

from domain.defect_rules.base import RuleResult
from vision.ui.server.server import UIServer


def _frame(value=0, h=8, w=8):
    return np.full((h, w, 3), value, dtype=np.uint8)


class UIServerPublishTest(unittest.TestCase):
    def setUp(self):
        self.server = UIServer()

    def test_publish_bumps_version_once(self):
        frame = _frame()
        version = self.server._cache_version
        self.server.update(
            frames={"A": frame},
            run_frames=[{"A": frame}],
            run_rule_results=[[{"name": "x"}]],
            line_status={"state": "RUNNING"},
        )
        self.assertEqual(self.server._cache_version, version + 1)

    def test_same_frames_do_not_bump(self):
        frame = _frame()
        self.server.update(frames={"A": frame})
        version = self.server._cache_version
        self.server.update(frames={"A": frame})
        self.assertEqual(self.server._cache_version, version)

    def test_review_publish_does_not_bump_with_numpy_rules(self):
        rule = RuleResult(
            rule_name="window_geometry",
            triggered=False,
            details={"per_role": {
                "INPUT_LEFT": {"valid": True, "threshold": np.float64(12.5)},
            }},
            drawings=[{"type": "window_geometry_item", "role": "INPUT_LEFT",
                       "pts": np.array([[1, 2], [3, 4]])}],
        )
        frame = _frame()
        vision = {"A": [{"class": "flatness"}]}
        self.server.update(
            frames={"A": frame}, vision_results=vision,
            rule_results=[rule],
            run_frames=[{"A": frame}], run_rule_results=[[rule]],
        )
        version = self.server._cache_version
        # REVIEW/PUBLISH: same objects — no bump
        self.server.update(frames={"A": frame}, vision_results=vision,
                           rule_results=[rule])
        self.assertEqual(self.server._cache_version, version)


class UIServerHttpTest(unittest.TestCase):
    def setUp(self):
        self.server = UIServer()
        try:
            from fastapi.testclient import TestClient
            self.client = TestClient(self.server.app)
        except Exception:
            self.skipTest("fastapi.testclient unavailable")

    def _publish(self):
        frame = np.full((240, 320, 3), 100, dtype=np.uint8)
        rule = RuleResult("window_geometry", False,
                          details={"per_role": {}}, drawings=[])
        self.server.update(
            frames={"INPUT_LEFT": frame},
            rule_results=[rule],
            run_frames=[{"INPUT_LEFT": frame}],
            run_rule_results=[[rule]],
            line_status={"state": "RUNNING", "step": 1, "line_parts": []},
        )
        self.server.boot_complete()

    def test_status_flow(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self._publish()
        r = self.client.get("/api/status")
        self.assertEqual(r.json()["line_status"]["state"], "RUNNING")

    def test_frames_endpoint(self):
        self._publish()
        r = self.client.get("/frame/INPUT_LEFT?mode=RULES")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("image/jpeg"))


if __name__ == "__main__":
    unittest.main()
