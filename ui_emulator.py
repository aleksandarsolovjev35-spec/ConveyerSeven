#!/usr/bin/env python3
"""Полноценный эмулятор HMI-интерфейса конвейерного комплекса без железа.

Поднимает настоящий UIServer (весь фронтенд из ``vision/ui``) и имитирует
буквально всю производственную линию без камер, моделей, контроллера и
распределителя:

- появление корпуса решает случайно каждая из двух первых камер
  (INPUT_LEFT / INPUT_RIGHT), как в реальном правиле part_presence: деталь
  засчитывается, только если её видят ОБЕ входные камеры;
- при наличии корпуса входные defect-правила (window_geometry, window_sinks)
  дают дефекты входа, на +4 spider-правила — дефекты контроля, а маршрут
  GOOD / BAD / CLEANUP вычисляется строго как в Part._recompute() (нет
  дефектов → GOOD; только glass/glass_glare → CLEANUP; иначе BAD);
- лента движется по шагам, позиции корпусов анимируются в «Пути корпусов»;
- распределитель DIST1/DIST2 выставляет маршрут строго по
  ``DISTRIBUTOR_LOGIC.md`` до каждого шага ленты;
- работают ВСЕ кнопки: ПУСК / СТОП / ПАУЗА / ПРОДОЛЖИТЬ / ВЫХОД,
  ручное управление лентой (JOG), диагностика камер / моделей / распределителя,
  анализ выбранного кадра, редактирование порогов правил и архив партий;
- панель «Анализ кадра» наполняется реалистичными замерами и правилами;
- главная камера показывает синтетическое изображение линии с деталью.

Использование:

    python ui_emulator.py                  # http://localhost:8000
    python ui_emulator.py --host 0.0.0.0 --port 8000 --seed 42
    python ui_emulator.py --review 2 --auto-start --no-archive

Горячие клавиши недоступны (это браузерный режим); все команды — через
кнопки интерфейса.
"""

import argparse
import random
import threading
import time
from collections import deque

import cv2
import numpy as np

from vision.ui.live_monitor import LiveMonitor
from vision.ui.server.server import CAMERA_ORDER
from core.state_machine import StateMachine, State
from core.rule_report import HUMAN_CAUSE_MAP
from domain.threshold_loader import ThresholdLoader
from config import load_archive_config

from domain.part import (
    CATEGORY_GOOD,
    CATEGORY_BAD,
    CATEGORY_CLEANUP,
    CATEGORY_UNKNOWN,
)

# ─────────────────────────────────────────────────────────────────────
# Конфигурация имитации
# ─────────────────────────────────────────────────────────────────────

# Позиция сортировки (совпадает с ProductionCycle.OFFSET_REJECT).
OFFSET_REJECT = 7
# Позиция входа.
OFFSET_INPUT = 0
# Позиция контроля (SPIDER/TOP).
OFFSET_SPIDER = 4

# Распределитель: положения, как в calibration.json.
DIST1_OPEN = 340
DIST2_BAD = 0
DIST2_CLEANUP = 340

# Какие правила показывать для каждой роли камеры в «Анализе кадра».
ROLE_RULES = {
    "INPUT_LEFT": ["window_geometry", "window_sinks"],
    "INPUT_RIGHT": ["window_geometry", "window_sinks"],
    "SPIDER_LEFT": ["contacts_long", "long_omission"],
    "SPIDER_RIGHT": ["contacts_long", "long_omission"],
    "SPIDER_IN": ["contacts_short", "short_omission"],
    "SPIDER_OUT": ["contacts_short", "short_omission"],
    "TOP": [
        "top_contacts",
        "top_platform",
        "platform_contacts_overlap",
        "sinks",
        "glass",
        "glass_on_contacts",
    ],
}

# Набор метрик для каждого правила (label, limit, key). Значение генерируется
# случайно около порога; при срабатывании правила — за порогом.
RULE_METRICS = {
    "window_geometry": [
        ("Найдено окон, шт", "7", "found"),
        ("T до перекладины: мин., px", "20", "top_px_min"),
        ("T до перекладины: макс., px", "40", "top_px_max"),
        ("B после перекладины: мин., px", "20", "bottom_px_min"),
        ("B после перекладины: макс., px", "40", "bottom_px_max"),
        ("Окон вне допуска, шт", "0", "windows_out_of_tolerance"),
    ],
    "window_sinks": [
        ("Найдено раковин, шт", "2", "sinks_found"),
        ("Пересечений с окнами, шт", "0", "sinks_hits"),
    ],
    "contacts_long": [
        ("Найдено контактов, шт", "5", "found"),
        ("Заслонка: перепад, px", "5.0", "damper_open_max_px"),
        ("Стены: разброс, px", "4.0", "gap_dev_max_px"),
    ],
    "long_omission": [
        ("Толщина полосы, px", "12.0", "allowed_thickness_px"),
        ("Избыток: крупнейший фрагмент, px", "400", "largest_component_px"),
    ],
    "contacts_short": [
        ("Найдено контактов, шт", "2", "found"),
        ("Заслонка: открытие, px", "4.0", "damper_open_max_px"),
    ],
    "short_omission": [
        ("Толщина полосы, px", "10.0", "allowed_thickness_px"),
        ("Избыток: крупнейший фрагмент, px", "300", "largest_component_px"),
    ],
    "top_contacts": [
        ("Найдено контактов, шт", "14", "found"),
        ("Допуск до края, доля", "0.5", "edge_distance_deviation_ratio"),
    ],
    "top_platform": [
        ("Ширина платформы, px", "420", "rect_width_px"),
        ("Смещение центра, px", "12.0", "shift_distance_px"),
    ],
    "platform_contacts_overlap": [
        ("Заплыв: крупнейший фрагмент, px", "200", "largest_component_px"),
        ("Контактов в области, шт", "14", "used_contacts"),
    ],
    "sinks": [
        ("Раковин внутри корпуса, шт", "2", "sinks_hits"),
        ("Пересечений с платформой, шт", "0", "forbidden_px"),
    ],
    "glass": [
        ("Стекол, шт", "2", "glass_hits"),
        ("Совпадений платформы, шт", "0", "platform_overlap_px"),
    ],
    "glass_on_contacts": [
        ("Пар стекло/контакт, шт", "0", "glass_contact_pairs"),
        ("Стекол на контактах, шт", "0", "overlap_pixels"),
    ],
}

# ── Присутствие детали (как реальное правило part_presence) ─────
# Деталь под входными камерами засчитывается, только если её видят ОБЕ
# INPUT-камеры (flatness >= порога на каждой). Эмулятор моделирует это так:
# каждая камера «видит» деталь с вероятностью INPUT_CAMERA_PRESENT_PROB,
# а наличие корпуса = присутствие на обеих. Это и есть «случайное появление
# ячеек под первыми двумя камерами».
INPUT_CAMERA_PRESENT_PROB = 0.93
# Порог присутствия детали на каждой камере (совпадает со смыслом
# input_part_presence_false_positive_max_count + 1 = 3).
INPUT_PRESENCE_THRESHOLD = 3

# Дефекты, которые приводят на CLEANUP (как в domain/part.py).
CLEANUP_DEFECTS = {"glass", "glass_glare"}

# INPUT defect-правила (вход +0): их срабатывание даёт дефекты входа.
INPUT_RULES = ("window_geometry", "window_sinks")

# SPIDER/TOP defect-правила (контроль +4): дефекты контроля.
SPIDER_RULES = (
    "contacts_long",
    "long_omission",
    "contacts_short",
    "short_omission",
    "top_contacts",
    "top_platform",
    "platform_contacts_overlap",
    "sinks",
    "glass",
    "glass_on_contacts",
)

# Вероятность срабатывания каждого defect-правила (эмуляция «по алгоритму»:
# правило сработало -> дефект в список, как в инспекции).
RULE_TRIGGER_PROB = {
    "window_geometry": 0.10,
    "window_sinks": 0.05,
    "contacts_long": 0.08,
    "long_omission": 0.05,
    "contacts_short": 0.06,
    "short_omission": 0.04,
    "top_contacts": 0.07,
    "top_platform": 0.05,
    "platform_contacts_overlap": 0.05,
    "sinks": 0.05,
    "glass": 0.05,
    "glass_on_contacts": 0.05,
}


def _metric(label, value, limit, ok, key):
    """Метрика панели «Анализ кадра» (тот же формат, что у UIServer)."""
    def _to_float(text):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None
    return {
        "label": label,
        "value": value,
        "limit": limit,
        "ok": ok,
        "value_raw": _to_float(value),
        "limit_raw": _to_float(limit),
        "key": key,
    }


def _generate_metrics(rule_name, triggered):
    """Случайные замеры для правила; при срабатывании — вне допуска."""
    pool = RULE_METRICS.get(rule_name)
    if not pool:
        return []
    out = []
    for idx, (label, limit, key) in enumerate(pool):
        limit_f = float(limit) if limit not in ("", None) else 0.0
        # При срабатывании первый и каждый третий замер за порогом.
        bad = triggered and (idx == 0 or idx % 3 == 0)
        if bad:
            value = round(limit_f * random.uniform(1.15, 2.2), 1)
            ok = False
        else:
            value = round(limit_f * random.uniform(0.25, 0.95), 1)
            ok = True
        text = str(int(value)) if isinstance(limit, str) and limit.isdigit() else f"{value:g}"
        out.append(_metric(label, text, limit, ok, key))
    return out


def _rule_row(rule_name, role, part_id, present=True, defects=None):
    """Строка правила для панели «Анализ кадра».

    ``present`` — подтверждено ли присутствие корпуса обеими входными камерами;
    ``defects`` — сработавшие defect-правила этого корпуса (на текущей стадии).
    ``triggered`` для правила берётся из ``defects``, как в реальной инспекции.
    """
    defects = defects or []
    if rule_name == "part_presence":
        if not present:
            return {
                "name": "part_presence",
                "triggered": False,
                "skipped": False,
                "part_absent": True,
                "status_label": "КОРПУС НЕ ОБНАРУЖЕН",
                "human_cause": None,
                "vote_details": {
                    "decision": "empty", "empty_votes": 1,
                    "present_votes": 0, "triggered_votes": 1,
                    "total_runs": 1, "required_votes": 1,
                },
                "run_cards": [[{
                    "role": role, "ok": None,
                    "verdict": "лоток пуст", "metrics": [],
                }]],
            }
        return {
            "name": "part_presence",
            "triggered": False,
            "skipped": False,
            "part_absent": False,
            "status_label": "КОРПУС ОБНАРУЖЕН",
            "human_cause": None,
            "vote_details": {
                "decision": "present", "present_votes": 1,
                "empty_votes": 0, "total_runs": 1, "required_votes": 1,
            },
            "run_cards": [[{
                "role": role, "ok": True, "verdict": "корпус виден",
                "metrics": [
                    _metric("flatness", "12", "30", True, "false_positive_max_count"),
                    _metric("Зачтено, шт", "12", None, None, "effective_flatness"),
                ],
            }]],
        }

    triggered = rule_name in defects
    human = HUMAN_CAUSE_MAP.get((rule_name, True))
    metrics = _generate_metrics(rule_name, triggered)
    return {
        "name": rule_name,
        "triggered": triggered,
        "skipped": False,
        "part_absent": False,
        "status_label": ("СРАБОТАЛО" if triggered else "НОРМА") + " · 1/1",
        "human_cause": human,
        "vote_details": {
            "decision": ("triggered" if triggered else "normal"),
            "triggered_votes": (1 if triggered else 0),
            "normal_votes": (0 if triggered else 1),
            "total_runs": 1, "required_votes": 1,
        },
        "run_cards": [[{
            "role": role, "ok": (not triggered),
            "verdict": ("отклонение" if triggered else "в норме"),
            "metrics": metrics,
        }]],
    }


class Emulator:
    """Имитация производственной линии поверх настоящего UIServer."""

    def __init__(self, seed=None, review_seconds=4.0, auto_start=True,
                 archive_enabled=True):
        self.rng = random.Random(seed)
        if seed is not None:
            random.seed(seed)

        self.monitor = LiveMonitor(
            start_callback=None, stop_callback=None, exit_callback=None,
            fullscreen=False,
        )
        self.server = self.monitor.server

        self.sm = StateMachine(on_transition=self._on_state_change)

        # Состояние линии
        self.parts = []                 # list[dict {id, step_created, category}]
        self.part_counter = 0
        self.current_step = 0
        self.good = 0
        self.rejected = 0
        self.cleanup = 0
        self.empty = 0
        self.recent_parts = deque(maxlen=10)
        self._process = self._empty_process()

        # Имитация распределителя
        self.dist1_position = 0
        self.dist1_state = "GOOD"
        self.dist2_position = 0
        self.dist2_state = "IDLE"
        self.dist2_target = CATEGORY_BAD
        self.last_distributor_action = "PRODUCTION READY"

        # JOG
        self.jog_active = False
        self.jog_busy = False
        self.jog_hold_steps = 0
        self.jog_direction = None
        self.jog_last_action = "-"
        self.jog_error = None
        self._jog_lock = threading.Lock()

        # Анализ выбранного кадра
        self.selected_analysis_active = False
        self.selected_analysis_role = None

        self._diagnostics = {
            "status": "NOT_RUN", "kind": None,
            "message": "Проверки ещё не запускались",
            "cameras": [], "models": [], "rules": [],
            "updated_at": None,
        }

        self.review_seconds = max(0.0, float(review_seconds))
        self.auto_start = auto_start
        self._cancel = threading.Event()
        self._cycle_thread = None
        self._live_thread = None

        self._current_camera_role = CAMERA_ORDER[0]

        self._setup_thresholds()
        self._setup_archive(archive_enabled)
        self._bind_server_callbacks()

    # ── Настройка порогов и архива ─────────────────────────────

    def _setup_thresholds(self):
        try:
            loader = ThresholdLoader()
            thresholds = loader.get_all()
            self.server.thresholds = dict(thresholds)
            self.server.threshold_labels = dict(loader.labels or {})
            self.server.thresholds_path = "thresholds.json"
            self.server.on_thresholds_apply = self._thresholds_apply
            self.server.on_thresholds_reload = self._thresholds_reload
            print(f"[EMU] Пороги загружены: {len(thresholds)} значений")
        except Exception as exc:
            print(f"[EMU] Пороги недоступны: {exc}")

    def _thresholds_apply(self, role, values, labels):
        if self.sm.state not in (State.IDLE, State.STOPPED):
            raise RuntimeError(
                "Изменение порогов доступно только до пуска "
                "и после полной остановки"
            )
        updated = dict(self.server.thresholds or {})
        changed = []
        for key, value in values.items():
            full_key = f"{role}.{key}" if not str(key).startswith(f"{role}.") else str(key)
            if full_key not in updated:
                raise ValueError(f"Неизвестный порог: {full_key}")
            updated[full_key] = value
            changed.append(full_key)
        ThresholdLoader.validate(updated)
        full_labels = dict(self.server.threshold_labels or {})
        for key, name in (labels or {}).items():
            full_key = f"{role}.{key}" if not str(key).startswith(f"{role}.") else str(key)
            if name is None or not str(name).strip():
                full_labels.pop(full_key, None)
            else:
                full_labels[full_key] = str(name).strip()
        ThresholdLoader.save_file("thresholds.json", updated, labels=full_labels)
        self.server.thresholds = dict(updated)
        self.server.threshold_labels = dict(full_labels)
        print(f"[EMU] Применено порогов {role}: {len(changed)}")
        return updated

    def _thresholds_reload(self, fresh):
        self.server.thresholds = dict(fresh)
        return fresh

    def _setup_archive(self, enabled):
        try:
            cfg = load_archive_config()
            from inspection.part_archive import PartArchive
            archive = PartArchive(
                root_folder=cfg["root_path"] if enabled else "archive",
                enabled=enabled,
                jpeg_quality=cfg["jpeg_quality"],
                zip_compression=cfg["zip_compression"],
                zip_level=cfg["zip_level"],
                compress_on_shutdown=cfg["compress_on_shutdown"],
                delete_original_after_zip=cfg["delete_original_after_zip"],
            )
            self.archive = archive
            self.server.archive = archive
            self.server.archive_config_path = "archive_config.json"
            print(f"[EMU] Архив партий готов: {archive.batch_id}")
        except Exception as exc:
            self.archive = None
            self.server.archive = None
            print(f"[EMU] Архив недоступен: {exc}")

    # ── Привязка серверных колбэков ────────────────────────────

    def _bind_server_callbacks(self):
        s = self.server
        s.on_start = self.request_start
        s.on_stop = self.request_stop
        s.on_pause = self.request_pause
        s.on_resume = self.request_resume
        s.on_exit = self.request_exit
        s.on_distributor_diagnostic = self.distributor_diagnostic
        s.on_camera_diagnostic = self.diagnostic_check_cameras
        s.on_vision_rule_diagnostic = self.diagnostic_check_vision_rules
        s.on_selected_model_analysis = self.diagnostic_analyze_selected_camera
        s.on_selected_model_release = self.diagnostic_release_selected_camera
        s.on_active_camera_changed = self._on_active_camera_changed
        s.on_jog_enter = self.enter_jog
        s.on_jog_exit = self.exit_jog
        s.on_jog_hold_start = self.jog_hold_start
        s.on_jog_hold_heartbeat = self.jog_hold_heartbeat
        s.on_jog_hold_release = self.jog_hold_release

    def _on_active_camera_changed(self, role):
        if role in self.server.frames:
            self._current_camera_role = role
        self._refresh()

    # ── Команды оператора ──────────────────────────────────────

    def request_start(self):
        if self.sm.state not in (State.IDLE, State.STOPPED):
            return False
        if self.selected_analysis_active:
            return False
        if self.jog_busy:
            return False
        self._set_route_good()
        self.last_distributor_action = "PRODUCTION READY"
        self._set_process("START_POSITIONING", "Возврат распределителя в рабочее положение")
        if not self.sm.request_start():
            return False
        self._set_process("READY", "Цикл запущен")
        return True

    def request_stop(self):
        return self.sm.request_stop()

    def request_pause(self):
        if self.sm.state != State.RUNNING:
            return False
        self._set_process("PAUSE_REQUESTED", "Пауза будет применена после остановки шага")
        # Пауза применяется на границе шага в цикле.
        return self.sm.request_pause()

    def request_resume(self):
        if self.sm.state != State.PAUSED:
            return False
        if self.jog_busy:
            return False
        ok = self.sm.request_resume()
        if ok:
            self._set_process("RESUMED", "Работа возобновлена после паузы")
        return ok

    def request_exit(self):
        self.sm.request_exit()
        self._set_process("STOPPING", "Штатная остановка -> завершение деталей на линии")
        return True

    def request_force_exit(self):
        self._cancel.set()
        self.sm.request_force_exit()
        return True

    def _on_state_change(self, old, new, action):
        if new == State.STOPPING:
            self._set_process("DRAINING", "Завершение корпусов на линии")
        elif new == State.STOPPED:
            self._park_distributor_home()
            self._set_process("STOPPED", "Линия остановлена и пуста")
        elif new == State.FAULT:
            self._set_process("FAULT", "Цикл остановлен из-за ошибки")
        else:
            self._refresh()

    # ── Диагностика ────────────────────────────────────────────

    def _prestart_diagnostic_allowed(self):
        return (
            self.sm.state in (State.IDLE, State.STOPPED)
            and not self.parts
            and not self.jog_busy
            and not self.selected_analysis_active
            and not self.sm.exit_requested
        )

    def distributor_diagnostic(self, command):
        if not self._prestart_diagnostic_allowed():
            return False
        self._set_process("DISTRIBUTOR_DIAGNOSTIC", f"Проверка распределителя: {command}")
        if command == "DIST1_HOME":
            self._set_route_good()
            self.last_distributor_action = "DIAGNOSTIC DIST1 -> GOOD"
        elif command == "DIST1_OPEN":
            self._set_route_bad(animate=True)
            self.last_distributor_action = "DIAGNOSTIC DIST1 -> DIST2"
        elif command == "DIST2_BAD":
            self._set_dist1_good()
            self.dist2_position = DIST2_BAD
            self.dist2_state = "READY"
            self.dist2_target = CATEGORY_BAD
            self.last_distributor_action = "DIAGNOSTIC DIST2 -> BAD"
        elif command == "DIST2_CLEANUP":
            self._set_dist1_good()
            self.dist2_position = DIST2_CLEANUP
            self.dist2_state = "READY"
            self.dist2_target = CATEGORY_CLEANUP
            self.last_distributor_action = "DIAGNOSTIC DIST2 -> CLEANUP"
        else:
            raise ValueError(f"Unknown distributor diagnostic: {command}")
        self._set_process("DIAGNOSTIC_DONE", f"Положение распределителя подтверждено: {command}")
        self._set_diagnostics_passed("DISTRIBUTOR", f"Распределитель: {command}")
        self._refresh()
        return True

    def diagnostic_check_cameras(self):
        if not self._prestart_diagnostic_allowed():
            return False
        self._set_process("CAMERA_DIAGNOSTIC", "Проверка семи камер")
        rows = []
        for role in CAMERA_ORDER:
            rows.append({
                "role": role, "ok": True,
                "width": 1280, "height": 720,
            })
        self._diagnostics = {
            "status": "PASSED", "kind": "CAMERAS",
            "message": f"Камеры: {len(rows)}/{len(rows)} OK",
            "cameras": rows, "models": [], "rules": [],
            "updated_at": time.time(),
        }
        self._set_process("DIAGNOSTIC_DONE", "Семь камер проверены")
        self._generate_frames()
        self._refresh()
        return True

    def diagnostic_check_vision_rules(self):
        if not self._prestart_diagnostic_allowed():
            return False
        self._set_process("VISION_RULE_DIAGNOSTIC",
                          "Запуск всех моделей и правил дефектов без движения линии")
        models = []
        for role in CAMERA_ORDER:
            models.append({
                "role": role, "ok": True, "model": "yolo",
                "runs": 1, "elapsed_ms": 18, "detections": 6,
                "detections_by_run": [6],
            })
        camera_rows = []
        for role in CAMERA_ORDER:
            camera_rows.append({
                "role": role, "ok": True, "width": 1280, "height": 720,
                "detections": 6,
            })
        rules = [
            _rule_row("part_presence", "INPUT_LEFT", None,
                      present=True, defects=[]),
            _rule_row("window_geometry", "INPUT_LEFT", None,
                      present=True, defects=["window_geometry"]),
        ]
        self._diagnostics = {
            "status": "PASSED", "kind": "VISION_RULES",
            "message": f"Модели: {len(models)} исправны; правил: {len(rules)}",
            "cameras": camera_rows, "models": models, "rules": rules,
            "updated_at": time.time(),
        }
        self._set_process("DIAGNOSTIC_DONE", "Модели и правила дефектов выполнены")
        self._generate_frames()
        self._refresh()
        return True

    def _set_diagnostics_passed(self, kind, message):
        self._diagnostics = {
            "status": "PASSED", "kind": kind, "message": message,
            "cameras": [], "models": [], "rules": [],
            "updated_at": time.time(),
        }

    def diagnostic_analyze_selected_camera(self, role):
        if not self._prestart_diagnostic_allowed():
            return False
        if role not in self.server.frames:
            raise ValueError(f"Неизвестная роль камеры: {role}")
        self.selected_analysis_active = True
        self.selected_analysis_role = role
        self._set_process("SELECTED_MODEL_ANALYSIS", f"Анализ кадра {role}")
        # Стоящий анализ: деталь присутствует, часть правил роли срабатывает
        # случайно (как в реальной инспекции по свежему кадру).
        role_defects = [
            r for r in ROLE_RULES.get(role, [])
            if random.random() < 0.3
        ]
        rules = [_rule_row("part_presence", role, None,
                           present=True, defects=[])]
        rules.extend(
            _rule_row(r, role, None, present=True, defects=role_defects)
            for r in ROLE_RULES.get(role, [])
        )
        self._diagnostics = {
            "status": "PASSED", "kind": "SELECTED_MODEL",
            "message": f"{role}: свежий кадр; правил {len(rules)}; объекты 5",
            "selected_role": role,
            "cameras": [{
                "role": role, "selected": True, "ok": True,
                "width": 1280, "height": 720, "runs": 1, "detections": 5,
                "detections_by_run": [5],
            }],
            "models": [{
                "role": role, "ok": True, "model": "yolo",
                "runs": 1, "elapsed_ms": 18, "detections": 5,
                "detections_by_run": [5],
            }],
            "rules": rules,
            "updated_at": time.time(),
        }
        self._set_process("SELECTED_MODEL_READY",
                          f"Анализ кадра {role} завершён; поток приостановлен")
        self._refresh()
        return True

    def diagnostic_release_selected_camera(self):
        if not self.selected_analysis_active:
            return False
        role = self.selected_analysis_role
        self.selected_analysis_active = False
        self.selected_analysis_role = None
        self._diagnostics = {
            "status": "NOT_RUN", "kind": None,
            "message": "Анализ кадра не выполнялся",
            "cameras": [], "models": [], "rules": [], "updated_at": None,
        }
        self._set_process("LIVE_SELECTED_CAMERA", f"Поток восстановлен: {role}")
        self._refresh()
        return True

    # ── JOG ────────────────────────────────────────────────────

    def can_enter_jog(self):
        return (
            self.sm.state in (State.IDLE, State.STOPPED, State.PAUSED)
            and not self.sm.exit_requested
            and not self.jog_error
        )

    def enter_jog(self):
        with self._jog_lock:
            if self.jog_active:
                return True
            if not self.can_enter_jog():
                return False
            self.jog_active = True
        self._refresh()
        return True

    def exit_jog(self):
        with self._jog_lock:
            if not self.jog_active:
                return True
            self.jog_active = False
            self.jog_busy = False
            self.jog_hold_steps = 0
            self.jog_direction = None
            self.jog_last_action = "-"
        self._refresh()
        return True

    def jog_hold_start(self, direction):
        if not self.jog_active or self.sm.state not in (
            State.IDLE, State.STOPPED, State.PAUSED,
        ):
            return False
        with self._jog_lock:
            self.jog_busy = True
            self.jog_direction = direction
            self.jog_hold_steps = 0
            self.jog_last_action = "ДВИЖЕНИЕ " + ("ВПЕРЕД" if direction == "+" else "НАЗАД")
        self._set_process("JOG_HOLD",
                          "Ручное движение ленты вправо" if direction == "+"
                          else "Ручное движение ленты влево")
        return True

    def jog_hold_heartbeat(self, direction):
        with self._jog_lock:
            self.jog_direction = direction
            self.jog_hold_steps += 1
        return True

    def jog_hold_release(self, reason="button released"):
        with self._jog_lock:
            if not self.jog_active:
                return False
            self.jog_busy = False
            self.jog_hold_steps = 0
            self.jog_direction = None
            self.jog_last_action = f"ОСТАНОВЛЕНО: {reason}"
        self._set_process("JOG_STOPPED", f"Ручное движение остановлено: {reason}")
        return True

    # ── Распределитель (маршруты по DISTRIBUTOR_LOGIC.md) ──────

    def _set_dist1_good(self):
        self.dist1_position = 0
        self.dist1_state = "GOOD"

    def _set_route_good(self):
        self._set_dist1_good()
        self.last_distributor_action = "-> GOOD"

    def _set_route_bad(self, animate=False):
        # DIST2=0 сначала, затем DIST1=340.
        self.dist2_position = DIST2_BAD
        self.dist2_state = "READY"
        self.dist2_target = CATEGORY_BAD
        if animate:
            self.dist1_state = "MOVING_TO_DIST2"
        self.dist1_position = DIST1_OPEN
        self.dist1_state = "TO_DIST2"
        self.last_distributor_action = "-> BAD"

    def _set_route_cleanup(self, animate=False):
        # DIST2=340 сначала, затем DIST1=340.
        self.dist2_position = DIST2_CLEANUP
        self.dist2_state = "READY"
        self.dist2_target = CATEGORY_CLEANUP
        if animate:
            self.dist1_state = "MOVING_TO_DIST2"
        self.dist1_position = DIST1_OPEN
        self.dist1_state = "TO_DIST2"
        self.last_distributor_action = "-> CLEANUP"

    def _prepare_route(self, category):
        if category == CATEGORY_GOOD:
            self._set_route_good()
        elif category == CATEGORY_BAD:
            self._set_route_bad(animate=True)
        elif category == CATEGORY_CLEANUP:
            self._set_route_cleanup(animate=True)

    def _park_distributor_home(self):
        """Вернуть обе заслонки на концевик (позиция 0) после остановки.

        После полной остановки линии распределитель не остаётся в последнем
        положении маршрута, а уходит к домашнему концевику: DIST1 -> GOOD (0),
        DIST2 -> канал BAD (0).
        """
        self.dist1_position = 0
        self.dist1_state = "GOOD"
        self.dist2_position = DIST2_BAD
        self.dist2_state = "IDLE"
        self.dist2_target = CATEGORY_BAD
        self.last_distributor_action = "HOMED"

    # ── Процесс / статус ───────────────────────────────────────

    def _empty_process(self):
        return {
            "phase": "IDLE", "label": "Система готова к пуску",
            "step": 0, "part_id": None, "positions": [],
            "conveyor": {}, "revision": 0, "updated_at": time.time(),
        }

    def _set_process(self, phase, label, part_id=None, positions=None):
        self._process = {
            "phase": phase, "label": label,
            "step": self.current_step, "part_id": part_id,
            "positions": list(positions or []),
            "conveyor": {"speed": 20000, "normal_steps": 19048},
            "revision": self._process.get("revision", 0) + 1,
            "updated_at": time.time(),
        }

    def _positions(self):
        """Словарь position -> список корпусов на этой позиции."""
        positions = {}
        for part in self.parts:
            pos = min(max(self.current_step - part["step_created"], 0), OFFSET_REJECT)
            positions.setdefault(pos, []).append(part)
        return positions

    def _part_at(self, position):
        parts = self._positions().get(position)
        return parts[0] if parts else None

    def _pending_drop(self):
        for part in self.parts:
            if self.current_step - part["step_created"] == OFFSET_REJECT:
                return part
        return None

    def _build_frame_analysis(self, state_name):
        role = self._current_camera_role
        if state_name not in ("RUNNING", "STOPPING"):
            if self.selected_analysis_active:
                return self._selected_frame_analysis()
            return {
                "available": False, "kind": None, "active": False,
                "title": None, "role": None, "part_id": None,
                "message": None, "models": [], "rules": [],
                "picture_run": None, "picture_reason": None, "updated_at": None,
            }

        # Активный корпус, чей анализ показать (чаще всего на +0 или +4).
        # Для INPUT-ролей — корпус под входом, для SPIDER/TOP — под контролем.
        is_input_role = role in ("INPUT_LEFT", "INPUT_RIGHT")
        part = self._part_at(OFFSET_INPUT if is_input_role else OFFSET_SPIDER)
        if part is None:
            stage = "ВХОД" if is_input_role else "КОНТРОЛЬ +4"
            return {
                "available": True, "kind": "CYCLE", "active": True,
                "title": "АНАЛИЗ ТЕКУЩЕГО КАДРА", "role": role,
                "group": "INPUT" if is_input_role else "SPIDER",
                "stage": stage, "part_id": None,
                "message": f"{stage} · {role}: результатов анализа пока нет",
                "models": [], "rules": [],
                "picture_run": None, "picture_reason": None, "updated_at": time.time(),
            }

        # Правила, сработавшие на текущей стадии корпуса (вход/контроль).
        if is_input_role:
            stage_defects = part.get("input_defects") or []
        else:
            stage_defects = part.get("spider_defects") or []
        # part_presence подтверждён (деталь на линии есть) — пустой лоток
        # показываем только когда под входом ничего нет (part is None выше).
        rules = [_rule_row("part_presence", role, part["id"],
                           present=True, defects=[])]
        rules.extend(
            _rule_row(r, role, part["id"], present=True, defects=stage_defects)
            for r in ROLE_RULES.get(role, [])
        )
        stage = "ВХОД" if is_input_role else "КОНТРОЛЬ +4"
        group = "INPUT" if is_input_role else "SPIDER"
        return {
            "available": True, "kind": "CYCLE", "active": True,
            "title": "АНАЛИЗ ТЕКУЩЕГО КАДРА", "role": role, "group": group,
            "stage": stage, "part_id": part["id"],
            "message": f"{stage} · {role}: итог по свежему кадру",
            "models": [], "rules": rules,
            "picture_run": 1, "picture_reason": "замер ближе всего к порогу",
            "updated_at": time.time(),
        }

    def _selected_frame_analysis(self):
        report = self._diagnostics
        role = report.get("selected_role")
        return {
            "available": True, "kind": "SELECTED",
            "active": self.selected_analysis_active,
            "title": "АНАЛИЗ КАДРА", "role": role,
            "part_id": None, "message": report.get("message"),
            "status": report.get("status"),
            "cameras": report.get("cameras", []),
            "models": report.get("models", []),
            "rules": report.get("rules", []),
            "picture_run": None, "picture_reason": None,
            "updated_at": report.get("updated_at"),
        }

    def _build_status(self):
        state_name = self.sm.state.value
        parts_snapshot = list(self.parts)
        in_line = len(parts_snapshot)

        line_parts = []
        for part in parts_snapshot:
            position = min(max(self.current_step - part["step_created"], 0), OFFSET_REJECT)
            pending = self._pending_drop()
            dropping = pending is not None and pending["id"] == part["id"]
            line_parts.append({
                "id": part["id"],
                "position": position,
                "category": part["category"],
                "held": False,
                "dropping": dropping,
            })

        diagnostic_allowed = (
            state_name in ("IDLE", "STOPPED")
            and not parts_snapshot
            and not self.jog_busy
            and not self.jog_error
            and not self.selected_analysis_active
            and not self.sm.exit_requested
        )
        controls = {
            "start": (
                state_name in ("IDLE", "STOPPED")
                and not parts_snapshot and not self.jog_busy
                and not self.selected_analysis_active
                and not self.sm.exit_requested
            ),
            "stop": state_name in ("RUNNING", "PAUSED"),
            "pause": state_name == "RUNNING" and not self.sm.exit_requested,
            "resume": (
                state_name == "PAUSED"
                and not self.jog_busy and not self.sm.exit_requested
            ),
            "exit": not self.sm.exit_requested and not self.jog_busy,
            "jog_hold": (
                state_name in ("IDLE", "STOPPED", "PAUSED")
                and self.jog_active and not self.jog_busy
                and not self.selected_analysis_active
            ),
            "selected_model_analysis": diagnostic_allowed,
            "selected_model_release": (
                self.selected_analysis_active
                and state_name in ("IDLE", "STOPPED")
            ),
            "distributor_diagnostic": diagnostic_allowed,
            "camera_diagnostic": diagnostic_allowed,
            "vision_rule_diagnostic": diagnostic_allowed,
        }

        return {
            "state": state_name,
            "exit_requested": self.sm.exit_requested,
            "fault_reason": None,
            "step": self.current_step,
            "in_line": in_line,
            "line_parts": line_parts,
            "total": self.part_counter,
            "good": self.good,
            "rejected": self.rejected,
            "cleanup": self.cleanup,
            "empty": self.empty,
            "dist1_position": self.dist1_position,
            "dist1_max": DIST1_OPEN,
            "dist1_state": self.dist1_state,
            "dist2_position": self.dist2_position,
            "dist2_max": max(DIST2_BAD, DIST2_CLEANUP, 1),
            "dist2_state": self.dist2_state,
            "dist2_target": self.dist2_target,
            "last_distributor_action": self.last_distributor_action,
            "axis_position": self.dist1_position,
            "axis_max": DIST1_OPEN,
            "distributor_state": self.dist1_state,
            "process": dict(self._process),
            "diagnostic_allowed": diagnostic_allowed,
            "diagnostic_busy": False,
            "controls": controls,
            "selected_analysis": {
                "active": self.selected_analysis_active,
                "role": self.selected_analysis_role,
            },
            "live": {
                "running": state_name in ("RUNNING", "STOPPING") or self.jog_active,
                "streaming": False,
                "static": False,
                "fps": 0.0,
                "error": None,
            },
            "frame_analysis": self._build_frame_analysis(state_name),
            "diagnostics": {
                **self._diagnostics,
                "cameras": [dict(r) for r in self._diagnostics["cameras"]],
                "models": [dict(r) for r in self._diagnostics["models"]],
                "rules": [dict(r) for r in self._diagnostics["rules"]],
            },
            "jog": {
                "active": bool(self.jog_active and state_name in
                               ("IDLE", "STOPPED", "PAUSED")),
                "can_enter": self.can_enter_jog(),
                "hold_steps": self.jog_hold_steps,
                "last_action": self.jog_last_action,
                "busy": self.jog_busy,
                "direction": self.jog_direction,
                "error": self.jog_error,
                "live_fps": 0.0,
            },
        }

    def _refresh(self, frames=None, run_frames=None, run_rule_results=None):
        self.monitor.update(
            frames=frames,
            vision_results={},
            rule_results=[],
            line_status=self._build_status(),
            recent_parts=list(self.recent_parts),
            run_frames=run_frames,
            run_rule_results=run_rule_results,
        )

    # ── Синтетические кадры ────────────────────────────────────

    def _render_role_frame(self, role, part_under):
        """Синтетическое изображение линии для одной камеры."""
        frame = np.full((720, 1280, 3), 26, dtype=np.uint8)
        # фон-транспортёр
        frame[40:680, 40:1240] = (38, 38, 38)
        # направляющие линии ленты
        cv2.line(frame, (80, 640), (1200, 640), (52, 52, 52), 2)
        cv2.line(frame, (80, 80), (1200, 80), (52, 52, 52), 2)

        if part_under:
            # корпус: тёмная платформа
            cx, cy = 640, 360
            cv2.rectangle(frame, (cx - 300, cy - 180), (cx + 300, cy + 180),
                          (70, 70, 74), -1)
            cv2.rectangle(frame, (cx - 300, cy - 180), (cx + 300, cy + 180),
                          (110, 110, 116), 2)
            # окна
            for i, wx in enumerate(range(cx - 220, cx + 221, 110)):
                top = cy - 120 + random.randint(-6, 6)
                bottom = cy + 100 + random.randint(-4, 4)
                cv2.rectangle(frame, (wx - 28, top), (wx + 28, bottom),
                              (150, 150, 155), 1)
            # контакты
            for i, kx in enumerate(range(cx - 220, cx + 221, 88)):
                cv2.rectangle(frame, (kx - 8, cy - 30), (kx + 8, cy + 40),
                              (200, 200, 205), 1)
            # номер корпуса
            cv2.putText(frame, f"#{part_under['id']}", (cx - 40, cy - 210),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.4, (150, 150, 155), 2)
        else:
            cv2.putText(frame, "ЛОТОК ПУСТ", (560, 360),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (90, 90, 95), 2)

        # надписи роли и шага
        cv2.putText(frame, role, (60, 690),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (120, 120, 125), 1)
        cv2.putText(frame, f"ШАГ {self.current_step}", (1060, 690),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (120, 120, 125), 1)
        # лёгкий шум, чтобы кадр «жил»
        noise = np.random.randint(-3, 4, frame.shape, dtype=np.int16)
        frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        return frame

    def _generate_frames(self):
        positions = self._positions()
        frames = {}
        for role in CAMERA_ORDER:
            pos = OFFSET_INPUT if role in ("INPUT_LEFT", "INPUT_RIGHT") else OFFSET_SPIDER
            part_under = (positions.get(pos) or [None])[0]
            frames[role] = self._render_role_frame(role, part_under)
        return frames

    # ── Производственный цикл (имитация) ───────────────────────

    def _run_once(self):
        """Один шаг линии: маршрут -> движение -> анализ -> передача."""
        self._check_cancelled()

        # 1. Маршрут корпуса на +7 до движения.
        pending = self._pending_drop()
        if pending:
            self._prepare_route(pending["category"])
            self._set_process(
                "ROUTE_PREPARE", "Подготовка маршрута распределителя",
                part_id=pending["id"], positions=[OFFSET_REJECT],
            )
            self._refresh()
            time.sleep(0.5)

        # 2. Движение ленты.
        self._set_process(
            "CONVEYOR_MOVING", "Лента перемещает корпуса на следующую позицию",
            part_id=(pending["id"] if pending else None),
            positions=range(OFFSET_REJECT + 1),
        )
        frames = self._generate_frames()
        self._refresh(frames=frames)
        time.sleep(1.0)
        self._check_cancelled()

        # 3. Подтверждение позиций: логический шаг выполнен.
        self.current_step += 1

        # 4. Передача корпуса, достигшего +8.
        if pending:
            self._set_process(
                "PART_TRANSFER", "Корпус прошёл распределитель",
                part_id=pending["id"], positions=[OFFSET_REJECT],
            )
            self._execute_drop(pending)

        # 5. Анализ входа (+0) и контроля (+4).
        new_part = self._spawn_input_part()
        self._run_spider_inspection()
        frames = self._generate_frames()
        self._set_process(
            "SETTLE", "Ожидание затухания вибрации перед съёмкой",
            positions=[OFFSET_INPUT, OFFSET_SPIDER],
        )
        self._refresh(frames=frames)
        time.sleep(0.4)

        if new_part is not None:
            self._set_process(
                "INPUT_ANALYSIS", f"Вход: анализ кандидата #{new_part['id']}",
                part_id=new_part["id"], positions=[OFFSET_INPUT],
            )
        else:
            self._set_process("INPUT_ANALYSIS", "Вход: лоток пуст",
                              positions=[OFFSET_INPUT])
        self._refresh(frames=frames)
        time.sleep(0.3)

        # 6. Просмотр результатов (REVIEW).
        self._set_process(
            "ANALYSIS_REVIEW", "Просмотр результатов анализа",
            positions=[OFFSET_INPUT, OFFSET_SPIDER],
        )
        self._refresh(frames=frames)
        deadline = time.monotonic() + self.review_seconds
        while time.monotonic() < deadline:
            if self._cancel.is_set() or self.sm.exit_requested \
                    or self.sm.state != State.RUNNING:
                break
            time.sleep(0.1)

        if self.sm.state == State.RUNNING and not self.sm.exit_requested:
            self._set_process("STEP_COMPLETE", "Шаг полностью завершён")
            self._refresh(frames=frames)

    def _input_presence(self):
        """Присутствие детали под входными камерами (как part_presence).

        Деталь засчитывается только если её видят ОБЕ камеры (INPUT_LEFT и
        INPUT_RIGHT). Каждая камера «видит» деталь случайно с вероятностью
        INPUT_CAMERA_PRESENT_PROB — это случайное появление ячеек под первыми
        двумя камерами. Возвращает (present: bool, left: bool, right: bool).
        """
        left = self.rng.random() < INPUT_CAMERA_PRESENT_PROB
        right = self.rng.random() < INPUT_CAMERA_PRESENT_PROB
        return (left and right), left, right

    def _run_defect_rules(self, rule_names):
        """Сработавшие defect-правила: правило сработало -> его имя в списке."""
        return [
            rule for rule in rule_names
            if self.rng.random() < RULE_TRIGGER_PROB.get(rule, 0.0)
        ]

    def _recompute_route(self, part):
        """Категория корпуса строго как в Part._recompute().

        - нет дефектов: GOOD только после полной инспекции, иначе UNKNOWN;
        - только glass/glass_glare -> CLEANUP;
        - любой другой дефект -> BAD.
        """
        defects = part["input_defects"] + part["spider_defects"]
        fully = part["input_inspected"] and part["spider_inspected"]
        if not defects:
            part["category"] = CATEGORY_GOOD if fully else CATEGORY_UNKNOWN
            return
        if all(d in CLEANUP_DEFECTS for d in defects):
            part["category"] = CATEGORY_CLEANUP
        else:
            part["category"] = CATEGORY_BAD

    def _spawn_input_part(self):
        """Обработать вход +0: присутствие -> входные правила -> корпус.

        Если деталь не подтверждена обеими камерами — пустой лоток (Part и
        архив не создаются). Иначе создаётся корпус, проходят INPUT
        defect-правила (window_geometry, window_sinks) и предварительно
        пересчитывается маршрут (полная инспекция завершится на +4).
        """
        if not self.sm.accepts_new_parts:
            return None
        present, left, right = self._input_presence()
        if not present:
            self.empty += 1
            print(f"[EMU] Step {self.current_step}: пустой лоток "
                  f"(LEFT={left}, RIGHT={right}; empty={self.empty})")
            return None
        self.part_counter += 1
        input_defects = self._run_defect_rules(INPUT_RULES)
        part = {
            "id": self.part_counter,
            "step_created": self.current_step,
            "category": CATEGORY_UNKNOWN,
            "input_defects": input_defects,
            "spider_defects": [],
            "input_inspected": True,
            "spider_inspected": False,
        }
        self._recompute_route(part)
        self.parts.append(part)
        print(f"[EMU] Step {self.current_step}: корпус #{part['id']} "
              f"вход-дефекты={input_defects or ['нет']} "
              f"маршрут(предв.)={part['category']}")
        return part

    def _run_spider_inspection(self):
        """Контроль +4: SPIDER/TOP defect-правила корпусов на этой позиции.

        После этой стадии корпус полностью инспектирован, маршрут
        финализируется по Part._recompute().
        """
        for part in self.parts:
            if self.current_step - part["step_created"] != OFFSET_SPIDER:
                continue
            if part["spider_inspected"]:
                continue
            part["spider_inspected"] = True
            part["spider_defects"] = self._run_defect_rules(SPIDER_RULES)
            self._recompute_route(part)
            print(f"[EMU] Контроль +4: корпус #{part['id']} "
                  f"дефекты={part['spider_defects'] or ['нет']} "
                  f"маршрут={part['category']}")

    def _execute_drop(self, part):
        category = part["category"]
        self.last_distributor_action = f"PART #{part['id']} -> {category} DONE"
        if category == CATEGORY_GOOD:
            self.good += 1
        elif category == CATEGORY_BAD:
            self.rejected += 1
        elif category == CATEGORY_CLEANUP:
            self.cleanup += 1
        self._archive_part(part)
        self._register_finished(part)
        self.parts.remove(part)
        print(f"[EMU] Корпус #{part['id']} передан: {category}")

    def _archive_part(self, part):
        if not self.archive:
            return
        try:
            frames = self._generate_frames()
            # В реальной системе каждый корпус проходит обе стадии инспекции
            # (вход +0: 2 камеры, контроль +4: 5 камер) — итого все 7 ролей.
            # Эмулятор сохраняет все семь камер, чтобы галерея «Последних
            # корпусов» у любой детали показывала полный набор изображений.
            roles = CAMERA_ORDER
            stage_frames = {r: frames[r] for r in roles if r in frames}
            self.archive.store_frames(
                part_id=part["id"], stage="input",
                raw_frames=stage_frames, annotated_frames=stage_frames,
                raw_overlay_frames=stage_frames,
            )
            self.archive.finalize(
                part_id=part["id"], category=part["category"],
                decision=part["category"], defects=[], step=part["step_created"],
            )
        except Exception as exc:
            print(f"[EMU] Архив корпуса #{part['id']} не записан: {exc}")

    def _register_finished(self, part):
        record = {
            "id": part["id"], "decision": part["category"],
            "category": part["category"], "time": time.time(),
        }
        if self.archive:
            try:
                info = self.archive.get_part_info(part["id"])
                if info:
                    record["batch_id"] = self.archive.batch_id
                    record["archive_folder"] = info.get("relative_folder")
                    record["annotation_files"] = list(info.get("annotation_files") or [])
                    record["sample_count"] = int(info.get("sample_count") or 0)
            except Exception:
                pass
        self.recent_parts.append(record)

    def _check_cancelled(self):
        if self._cancel.is_set() or self.sm.force_exit:
            raise RuntimeError("physical operation cancelled")

    def _cycle_loop(self):
        try:
            while True:
                if self.sm.force_exit:
                    break
                if self.sm.is_active:
                    if self.sm.state == State.STOPPING and not self.parts:
                        self.sm.notify_line_empty()
                        self._refresh()
                        if self.sm.exit_requested:
                            break
                        continue
                    try:
                        self._run_once()
                    except RuntimeError:
                        pass
                    if self.sm.state == State.STOPPING and not self.parts:
                        self.sm.notify_line_empty()
                        self._refresh()
                        if self.sm.exit_requested:
                            break
                else:
                    if self.sm.exit_requested:
                        break
                    self._refresh()
                    time.sleep(0.15)
        finally:
            print("[EMU] Цикл имитации завершён")

    # ── Live-поток камер (обновление картинки при простое) ─────

    def _live_loop(self):
        last_role = None
        last_step = None
        while not self._cancel.is_set():
            frames = self._generate_frames()
            self.server.update(frames=frames)
            time.sleep(1.2)

    # ── Запуск / остановка ─────────────────────────────────────

    def _boot(self):
        for key, _ in self.server.BOOT_STEPS:
            self.server.boot_step_start(key)
        import time as _t
        for key, _ in self.server.BOOT_STEPS:
            _t.sleep(0.12)
            self.server.boot_step_done(key)
        self.server.boot_complete()

    def begin_simulation(self, start=True):
        """Опубликовать начальные кадры и запустить фоновые потоки.

        Вызывается после ``server.start_server``. ``start=True`` включает
        автозапуск производственного цикла.
        """
        self._refresh(frames=self._generate_frames())
        # Активная камера по умолчанию — входная, чтобы главное окно и
        # панель «Анализ кадра» сразу показывали картинку.
        if self.server.active_camera_role != self._current_camera_role:
            self.server.set_active_camera_role(self._current_camera_role)
        if self.auto_start and start:
            print("[EMU] Автозапуск цикла...")
            self.request_start()

        self._cycle_thread = threading.Thread(
            target=self._cycle_loop, daemon=True, name="emu-cycle",
        )
        self._cycle_thread.start()

        self._live_thread = threading.Thread(
            target=self._live_loop, daemon=True, name="emu-live",
        )
        self._live_thread.start()

    def run(self, host="0.0.0.0", port=8000, no_auto_start=False):
        self._boot()
        self.server.start_server(host=host, port=port)
        print(f"EMULATOR UI: http://localhost:{port}  (Ctrl+C для выхода)")
        self.begin_simulation(start=not no_auto_start)

        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n[EMU] Выход по Ctrl+C")
        finally:
            self.request_force_exit()
            self.server.stop_server()


def _parse_args():
    p = argparse.ArgumentParser(description="Полноценный эмулятор UI")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--review", type=float, default=4.0,
                   help="пауза просмотра анализа, сек (0 = минимум)")
    p.add_argument("--no-auto-start", action="store_true",
                   help="не запускать цикл автоматически (нажать ПУСК вручную)")
    p.add_argument("--no-archive", action="store_true",
                   help="отключить запись архива партий")
    return p.parse_args()


def main():
    args = _parse_args()
    emu = Emulator(
        seed=args.seed,
        review_seconds=args.review,
        auto_start=not args.no_auto_start,
        archive_enabled=not args.no_archive,
    )
    emu.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
