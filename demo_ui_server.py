"""Демо-сервер UI «Анализ кадра» без железа.

Поднимает настоящий UIServer и публикует в /api/status демонстрационный
снимок панели «Анализ кадра»: правило window_geometry с блоками объектов
(Окно #1 … Окно #7), статистику корпусов (всего/годные/брак/очистка) и
версию кадра. Камеры/модели/конвейер не нужны — только просмотр панели.

Использование: python demo_ui_server.py  (затем открыть http://localhost:8000)
"""
import time

from vision.ui.server.server import UIServer, CAMERA_ORDER


def build_demo_frame_analysis():
    """Снимок, идентичный production-выводу _build_frame_analysis (CYCLE)."""
    def metric(label, value, limit, ok, key, obj=None):
        def _to_float(text):
            try:
                return float(text)
            except (TypeError, ValueError):
                return None
        m = {
            "label": label,
            "value": value,
            "limit": limit,
            "ok": ok,
            "value_raw": _to_float(value),
            "limit_raw": _to_float(limit),
            "key": key,
        }
        if obj:
            m["object"] = obj
        return m

    windows = []
    for idx in range(1, 8):
        top = 22 + idx * 2 if idx != 3 else 46          # окно #3 вне допуска
        bottom = 30 + (idx % 3)
        ok = 20 <= top <= 40
        windows.extend([
            metric(f"Окно #{idx}: верх, px", str(top), "20…40 px", ok,
                   f"window_{idx}_top_px", f"Окно #{idx}"),
            metric(f"Окно #{idx}: низ, px", str(bottom), "20…40 px", True,
                   f"window_{idx}_bottom_px", f"Окно #{idx}"),
            metric(f"Окно #{idx}: в допуске", "1" if ok else "0", "1", ok,
                   f"window_{idx}_ok", f"Окно #{idx}"),
        ])

    contacts = []
    for idx in range(1, 6):
        dev = 2.0 if idx != 4 else 8.0
        contacts.extend([
            metric(f"Контакт #{idx}: откл. верх, px", f"{dev:.1f}", "5.0",
                   dev <= 5.0, f"contact_{idx}_dev_top_px", f"Контакт #{idx}"),
            metric(f"Контакт #{idx}: откл. низ, px", "1.5", "5.0", True,
                   f"contact_{idx}_dev_bottom_px", f"Контакт #{idx}"),
        ])

    return {
        "available": True,
        "kind": "CYCLE",
        "active": True,
        "title": "АНАЛИЗ ТЕКУЩЕГО КАДРА",
        "role": "INPUT_LEFT",
        "group": "INPUT",
        "stage": "ВХОД",
        "part_id": 7,
        "message": "ВХОД · INPUT_LEFT: итог по свежему кадру",
        "picture_run": 1,
        "picture_reason": "window_geometry: замер ближе всего к порогу",
        "updated_at": time.time(),
        "models": [],
        "rules": [
            {
                "name": "part_presence",
                "triggered": False,
                "skipped": False,
                "part_absent": False,
                "status_label": "КОРПУС ОБНАРУЖЕН",
                "vote_details": {
                    "decision": "present", "present_votes": 1,
                    "empty_votes": 0, "total_runs": 1, "required_votes": 1,
                },
                "run_cards": [[{
                    "role": "INPUT_LEFT", "ok": True, "verdict": "корпус виден",
                    "metrics": [
                        metric("flatness", "12", "30", True, "false_positive_max_count"),
                        metric("Зачтено, шт", "12", None, None, "effective_flatness"),
                    ],
                }]],
            },
            {
                "name": "window_geometry",
                "triggered": True,
                "skipped": False,
                "part_absent": False,
                "status_label": "СРАБОТАЛО",
                "human_cause": "НЕПРАВИЛЬНАЯ ГЕОМЕТРИЯ ОКОН",
                "vote_details": {
                    "decision": "triggered", "triggered_votes": 1,
                    "normal_votes": 0, "total_runs": 1, "required_votes": 1,
                },
                "run_cards": [[{
                    "role": "INPUT_LEFT", "ok": False,
                    "verdict": "отклонение · окно вне допуска",
                    "metrics": [
                        metric("Найдено окон, шт", "7", "7", True, "found"),
                        metric("T до перекладины: мин., px", "22", "20", True, "top_px_min"),
                        metric("T до перекладины: макс., px", "46", "40", False, "top_px_max"),
                        metric("B после перекладины: мин., px", "30", "20", True, "bottom_px_min"),
                        metric("B после перекладины: макс., px", "33", "40", True, "bottom_px_max"),
                        metric("Окон вне допуска, шт", "1", "0", False, "windows_out_of_tolerance"),
                        *windows,
                    ],
                }]],
            },
            {
                "name": "contacts_long",
                "triggered": True,
                "skipped": False,
                "part_absent": False,
                "status_label": "СРАБОТАЛО",
                "human_cause": "НАКЛОН / СМЕЩЕНИЕ ДЛИННЫХ КОНТАКТОВ",
                "vote_details": {
                    "decision": "triggered", "triggered_votes": 1,
                    "normal_votes": 0, "total_runs": 1, "required_votes": 1,
                },
                "run_cards": [[{
                    "role": "SPIDER_LEFT", "ok": False,
                    "verdict": "отклонение · контакт вне допуска",
                    "metrics": [
                        metric("Найдено контактов, шт", "5", "5", True, "found"),
                        metric("Заслонка: перепад, px", "8.0", "5.0", False, "damper_open_max_px"),
                        metric("Стены: разброс, px", "2.1", "4.0", True, "gap_dev_max_px"),
                        *contacts,
                    ],
                }]],
            },
        ],
    }


def main():
    server = UIServer()
    for key, _ in server.BOOT_STEPS:
        server.boot_step_done(key)
    server.boot_complete()

    # Синтетические кадры: без железа главная камера показывает
    # имитацию изображения конвейера.
    try:
        import numpy as np
        frame = np.full((720, 1280, 3), 28, dtype=np.uint8)
        frame[60:660, 100:1180] = (36, 36, 36)
        frame[360:400, 200:1100] = (60, 60, 60)   # имитация детали
        frame[::4, 100] = (20, 20, 20)
        frame[::4, 1179] = (20, 20, 20)
        with server.lock:
            for role in CAMERA_ORDER:
                server.frames[role] = frame.copy()
                server._latest_frames_ver[role] = 1
            server._cache_version = 1
    except Exception as exc:
        print(f"[DEMO] Кадры недоступны: {exc}")

    with server.lock:
        server.line_status = {
            "state": "RUNNING",
            "exit_requested": False,
            "step": 12,
            "in_line": 5,
            "line_parts": [
                {"id": 4, "position": 0, "category": "GOOD"},
                {"id": 5, "position": 1, "category": "GOOD"},
                {"id": 6, "position": 2, "category": "GOOD"},
                {"id": 7, "position": 3, "category": "BAD"},
                {"id": 8, "position": 4, "category": "UNKNOWN"},
            ],
            "total": 12,
            "good": 8,
            "rejected": 3,
            "cleanup": 1,
            "empty": 2,
            "dist1_state": "IDLE",
            "dist1_position": 0,
            "dist1_max": 340,
            "dist1_target": "PRODUCTION READY",
            "dist2_state": "IDLE",
            "dist2_position": 0,
            "dist2_max": 340,
            "dist2_target": "BAD",
            "last_distributor_action": "PRODUCTION READY",
            "process": {
                "phase": "ANALYSIS_REVIEW",
                "label": "Просмотр результатов анализа",
                "step": 12,
                "part_id": 7,
                "positions": [0, 4],
            },
            "controls": {},
            "selected_analysis": {"active": False, "role": None},
            "live": {
                "running": False, "streaming": False, "static": True,
                "stage": "ANALYSIS", "fps": 0, "error": None,
            },
            "frame_analysis": build_demo_frame_analysis(),
            "diagnostics": {
                "status": "NOT_RUN", "kind": None, "message": "Демо",
                "cameras": [], "models": [], "rules": [],
                "updated_at": None,
            },
            "jog": {
                "active": False, "can_enter": False, "hold_steps": 0,
                "last_action": "-", "busy": False, "direction": None,
                "error": None, "live_fps": 0.0,
            },
        }
        server.active_camera_role = "INPUT_LEFT"

    server.start_server(host="0.0.0.0", port=8000)
    print("DEMO UI: http://localhost:8000 (Ctrl+C для выхода)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop_server()


if __name__ == "__main__":
    main()
