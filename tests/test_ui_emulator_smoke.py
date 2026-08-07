"""Smoke-test the ui_emulator using a cv2 stub (sandbox has no libGL).

Injects a fake `cv2` module so the whole UI stack can boot headlessly, then
exercises the emulator: boot, auto-start, parts appearing/dropping, distributor
routes, all control endpoints, jog, diagnostics, thresholds and frame serving.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Реальный cv2 может быть недоступен в песочнице без libGL: подставляем
# лёгкую заглушку только для headless-запусков (см. тест-README).
try:
    import cv2  # noqa: F401
    HAVE_CV2 = True
except ImportError:
    HAVE_CV2 = False

# ── cv2 stub ──────────────────────────────────────────────────────────────
class _Cv2Stub:
    FONT_HERSHEY_SIMPLEX = 0
    INTER_AREA = 3
    IMWRITE_JPEG_QUALITY = 1
    IMWRITE_JPEG_PROGRESSIVE = 2
    __version__ = "stub"

    @staticmethod
    def imencode(ext, frame, params=None):
        import numpy as np
        return True, np.frombuffer(b"FAKEJPEG", dtype=np.uint8)

    @staticmethod
    def resize(frame, size, interpolation=None):
        return frame

    @staticmethod
    def line(frame, *a, **k):
        return frame

    @staticmethod
    def rectangle(frame, *a, **k):
        return frame

    @staticmethod
    def putText(frame, *a, **k):
        return frame


FAILURES = []


def _ensure_cv2():
    """Вернуть cv2, при необходимости подставив заглушку (headless)."""
    if HAVE_CV2:
        return cv2
    sys.modules["cv2"] = _Cv2Stub
    return _Cv2Stub


def check(name, cond, detail=""):
    tag = "OK " if cond else "FAIL"
    if not cond:
        FAILURES.append(name)
    print(f"[{tag}] {name} {detail}")


def main():
    global ui_emulator
    _ensure_cv2()
    import ui_emulator

    emu = ui_emulator.Emulator(seed=7, review_seconds=0.0, auto_start=True,
                               time_scale=0.3)
    emu._boot()
    emu.server.start_server(host="127.0.0.1", port=8765)
    emu.begin_simulation(start=False)  # manually drive start below
    base = "http://127.0.0.1:8765"
    import httpx

    try:
        # Boot complete
        boot = httpx.get(base + "/api/boot").json()
        check("boot complete", boot["active"] is False)

        # Start the cycle
        r = httpx.post(base + "/api/start").json()
        check("start accepted", r.get("ok") is True, str(r))

        # Let a couple steps run
        time.sleep(6.0)

        st = httpx.get(base + "/api/status").json()
        ls = st.get("line_status", {})
        check("state RUNNING", ls.get("state") == "RUNNING", ls.get("state"))
        check("step advanced", ls.get("step", 0) >= 1, str(ls.get("step")))
        check("line_parts list", isinstance(ls.get("line_parts"), list))
        check("dist1 key", "dist1_position" in ls)
        check("controls start off in RUNNING", ls.get("controls", {}).get("start") is not True)

        # Frame serving
        r = httpx.get(base + "/frame/INPUT_LEFT")
        check("frame 200", r.status_code == 200, str(r.status_code))

        # Cameras
        cams = httpx.get(base + "/api/cameras").json()
        check("cameras", len(cams.get("cameras", [])) == 7, str(cams))

        # Thresholds
        th = httpx.get(base + "/api/thresholds", params={"role": "INPUT_LEFT"}).json()
        check("thresholds available", th.get("available") is True)

        # Pause / resume / stop
        check("pause", httpx.post(base + "/api/pause").json().get("ok") is True)
        time.sleep(0.3)
        check("resume", httpx.post(base + "/api/resume").json().get("ok") is True)
        check("stop", httpx.post(base + "/api/stop").json().get("ok") is True)
        # Ждём, пока все корпуса на линии доедут до сброса (штатная остановка).
        for _ in range(40):
            st2 = httpx.get(base + "/api/status").json()
            ls2 = st2.get("line_status", {})
            if ls2.get("state") == "STOPPED" and not ls2.get("line_parts"):
                break
            time.sleep(0.5)
        check("stopped empty", ls2.get("state") == "STOPPED" and not ls2.get("line_parts"),
              f"{ls2.get('state')} parts={len(ls2.get('line_parts', []))}")
        # После остановки распределитель возвращается на концевик (0/0).
        check("distributor homed on stop",
              ls2.get("dist1_position") == 0 and ls2.get("dist2_position") == 0,
              f"d1={ls2.get('dist1_position')} d2={ls2.get('dist2_position')}")

        # Counters accumulated
        ls_total = ls2.get("total", 0)
        check("total>0", ls_total > 0, f"total={ls_total} good={ls2.get('good')} rej={ls2.get('rejected')} clean={ls2.get('cleanup')} empty={ls2.get('empty')}")

        # JOG enter/hold/release
        check("jog enter", httpx.post(base + "/api/jog/enter").json().get("ok") is True)
        check("jog hold start", httpx.post(base + "/api/jog/hold/start",
                                           json={"direction": "+"}).json().get("ok") is True)
        check("jog heartbeat", httpx.post(base + "/api/jog/hold/heartbeat",
                                          json={"direction": "+"}).json().get("ok") is True)
        check("jog release", httpx.post(base + "/api/jog/hold/release",
                                        json={"reason": "test"}).json().get("ok") is True)
        check("jog exit", httpx.post(base + "/api/jog/exit").json().get("ok") is True)

        # Diagnostics
        check("cam diag", httpx.post(base + "/api/diagnostics/cameras").json().get("ok") is True)
        check("vision diag", httpx.post(base + "/api/diagnostics/vision-rules").json().get("ok") is True)
        check("dist diag", httpx.post(base + "/api/distributor/diagnostic/DIST1_OPEN").json().get("ok") is True)

        # Selected frame analysis + release
        check("sel analysis", httpx.post(base + "/api/diagnostics/selected/INPUT_LEFT").json().get("ok") is True)
        check("sel release", httpx.post(base + "/api/diagnostics/selected/release").json().get("ok") is True)

        # Restart and verify parts drop with distributor routes
        check("start again", httpx.post(base + "/api/start").json().get("ok") is True)
        time.sleep(8.0)
        st3 = httpx.get(base + "/api/status").json()
        ls3 = st3.get("line_status", {})
        check("running again", ls3.get("state") == "RUNNING")
        # Verify a recent part exists
        rp = st3.get("recent_parts", [])
        check("recent parts present", len(rp) > 0, f"recent={len(rp)}")

        # Archive gallery: each part stores all seven camera roles, so the
        # "Последние корпуса" block shows a full set of images for every part.
        if rp:
            part_id = rp[-1]["id"]
            ap = httpx.get(base + f"/api/archive/part/{part_id}").json()
            roles = ap.get("roles", [])
            check("archive part has 7 roles", len(roles) == 7, f"roles={len(roles)}")

        print("\n=== SUMMARY ===")
        if FAILURES:
            print("FAILURES:", FAILURES)
            sys.exit(1)
        print("ALL CHECKS PASSED")
        sys.exit(0)
    finally:
        emu.request_force_exit()
        emu.server.stop_server()


if __name__ == "__main__":
    main()
