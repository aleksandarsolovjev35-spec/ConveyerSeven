"""Dev mock server: serves the HMI in UI-test mode with a realistic
frame-analysis payload so the panel layout can be inspected in a browser
without cameras / hardware / models.
"""
import http.server
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).parent          # vision/ui
STATIC = ROOT / "static"
TEMPLATE = ROOT / "templates" / "index.html"


def metric(key, label, value, ok, limit="", value_raw=None, limit_raw=None):
    if value_raw is None:
        value_raw = value
    return {
        "key": key, "label": label, "value": value, "ok": ok,
        "limit": limit, "value_raw": value_raw, "limit_raw": limit_raw,
    }


def run_card(role, metrics):
    return {"role": role, "metrics": metrics}


# ---- Triggered rule: window_sinks (2 из 3) ----
window_sinks_metrics = [
    metric("sinks_hits", "Пересечений, шт", 5, False, "≤ 3", 5, 3),
    metric("shell_1_forbidden_px", "Раковина #1 запрещ., px", 212.4, False, "≤ 200", 212.4, 200),
    metric("shell_2_forbidden_px", "Раковина #2 запрещ., px", 34.1, True, "≤ 200", 34.1, 200),
]
window_sinks = {
    "name": "window_sinks",
    "status_label": "СРАБОТАЛО",
    "triggered": True,
    "human_cause": "Раковина #1 пересекает запретную зону",
    "threshold_conclusion": "2 из 3 прогонов: пересечение 212px > 200px → брак",
    "run_cards": [
        [run_card("INPUT_LEFT", window_sinks_metrics),
         run_card("INPUT_RIGHT", [metric("sinks_hits", "Пересечений, шт", 4, False, "≤ 3", 4, 3)])],
        [run_card("INPUT_LEFT", window_sinks_metrics),
         run_card("INPUT_RIGHT", [metric("sinks_hits", "Пересечений, шт", 5, False, "≤ 3", 5, 3)])],
        [run_card("INPUT_LEFT", [metric("sinks_hits", "Пересечений, шт", 1, True, "≤ 3", 1, 3)]),
         run_card("INPUT_RIGHT", [metric("sinks_hits", "Пересечений, шт", 2, True, "≤ 3", 2, 3)])],
    ],
    "threshold_breaches": [
        {"label": "Пересечений, шт", "key": "sinks_hits", "role": "INPUT_LEFT"},
        {"label": "Раковина #1 запрещ., px", "key": "shell_1_forbidden_px", "role": "INPUT_LEFT"},
    ],
    "vote_details": {"decision": "triggered", "triggered_votes": 2, "normal_votes": 1,
                     "total_runs": 3, "picture_run": 2, "evidence_run": 2},
}

# ---- Normal rule: part_presence ----
part_presence = {
    "name": "part_presence",
    "status_label": "НОРМА",
    "triggered": False,
    "run_cards": [
        [run_card("INPUT_LEFT", [metric("found", "Корпус, найдено", 1, True, "≥ 1", 1, 1)])],
        [run_card("INPUT_LEFT", [metric("found", "Корпус, найдено", 1, True, "≥ 1", 1, 1)])],
        [run_card("INPUT_LEFT", [metric("found", "Корпус, найдено", 1, True, "≥ 1", 1, 1)])],
    ],
    "vote_details": {"decision": "present", "present_votes": 3, "total_runs": 3},
}

# ---- Skipped rule ----
skipped_rule = {
    "name": "top_contacts",
    "status_label": "НЕ ВЫПОЛНЕНО",
    "triggered": False,
    "skipped": True,
    "vote_details": {"decision": "present", "present_votes": 3, "total_runs": 3},
}

# ---- Normal rule: glass ----
glass = {
    "name": "glass",
    "status_label": "НОРМА",
    "triggered": False,
    "run_cards": [
        [run_card("TOP", [metric("glass_count", "Стекол, шт", 2, True, "≤ 2", 2, 2),
                          metric("glass_1_platform_px", "Стекло #1 платформа, px", 0, True, "= 0", 0, 0)])],
        [run_card("TOP", [metric("glass_count", "Стекол, шт", 2, True, "≤ 2", 2, 2)])],
        [run_card("TOP", [metric("glass_count", "Стекол, шт", 2, True, "≤ 2", 2, 2)])],
    ],
    "vote_details": {"decision": "normal", "normal_votes": 3, "total_runs": 3},
}

FRAME_ANALYSIS = {
    "available": True,
    "kind": "CYCLE",
    "stage": "КОНТРОЛЬ",
    "role": "SPIDER_IN",
    "part_id": 12,
    "title": "АНАЛИЗ КАДРА",
    "status": "DONE",
    "message": "Анализ завершён",
    "picture_run": 2,
    "picture_reason": "Прогон с максимальным отклонением",
    "updated_at": 1,
    "models": [
        {"role": "INPUT_LEFT", "model": "window_sinks.pt"},
        {"role": "INPUT_RIGHT", "model": "window_sinks.pt"},
        {"role": "TOP", "model": "glass_v.1.pt"},
    ],
    "rules": [window_sinks, part_presence, skipped_rule, glass],
}

LINE_STATUS = {
    "state": "RUNNING",
    "step": 4,
    "total": 12, "good": 8, "rejected": 3, "cleanup": 1, "empty": 2,
    "in_line": 3,
    "controls": {"start": False, "stop": True},
    "line_parts": [
        {"id": 12, "position": 4, "category": "BAD"},
    ],
    "process": {"phase": "REVIEW", "conveyor": {"speed": 20000}},
    "jog": {"active": False},
    "frame_analysis": FRAME_ANALYSIS,
}

HARNESS = """
<script>window.__TRANSPORTER_UI_TEST__ = true;</script>
<script src="/static/js/bootstrap.js?v=34"></script>
<script>
(function(){
  var A = window.__TRANSPORTER_UI_TEST_API__;
  A.setupForTest();
  A.updateLineStatus(%s);
})();
</script>
""" % (json.dumps(LINE_STATUS, ensure_ascii=False),)


ALL_NORMAL_FA = dict(FRAME_ANALYSIS)
ALL_NORMAL_FA["rules"] = [part_presence, glass]
ALL_NORMAL_FA["message"] = "Анализ завершён, дефектов нет"
ALL_NORMAL_FA["updated_at"] = 2
ALL_NORMAL_FA["part_id"] = 13

ALL_NORMAL_LINE = dict(LINE_STATUS)
ALL_NORMAL_LINE["state"] = "IDLE"
ALL_NORMAL_LINE["frame_analysis"] = ALL_NORMAL_FA
ALL_NORMAL_LINE["line_parts"] = [
    {"id": 13, "position": 0, "category": "GOOD"},
]


def build_harness(line_status):
    return """
<script>window.__TRANSPORTER_UI_TEST__ = true;</script>
<script src="/static/js/bootstrap.js?v=34"></script>
<script>
(function(){
  var A = window.__TRANSPORTER_UI_TEST_API__;
  A.setupForTest();
  A.updateLineStatus(%s);
})();
</script>
""" % (json.dumps(line_status, ensure_ascii=False),)


def build_test_page(all_normal):
    line_status = ALL_NORMAL_LINE if all_normal else LINE_STATUS
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace(
        '<script src="/static/js/bootstrap.js?v=34"></script>',
        build_harness(line_status),
        1,
    )
    return html


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            all_normal = "all=1" in self.path
            body = build_test_page(all_normal).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            f = STATIC / rel
            if f.is_file():
                body = f.read_bytes()
                ctype = {
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".svg": "image/svg+xml",
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".html": "text/html; charset=utf-8",
                }.get(f.suffix.lower(), "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"not found")

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_PORT", "8050"))
    http.server.HTTPServer(("0.0.0.0", port), Handler).serve_forever()
