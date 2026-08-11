"""Геометрия оверлея обязана переживать JPEG-кодирование и не смешиваться
с чужими кадрами.

Регрессионные сценарии (жалобы оператора):
1. «Геометрия иногда вообще не рисуется» — линии 1px исчезали при
   JPEG-кодировании (chroma subsampling), разметка становилась невидимой.
2. «Показывает не ту / висит на экране» — разметка стадии (run) смешивалась
   с live-кадрами, а после остановки/FAULT общий набор правил оставался
   висеть на движущемся изображении.
"""

import cv2
import numpy as np

from vision.overlay.debug_overlay import DebugOverlay
from vision.ui.server.server import UIServer


class FakeRuleResult:
    """Совместимый с DebugOverlay результат правила."""

    def __init__(self, drawings):
        self.drawings = drawings


def _rule_bbox(role, x, triggered=False):
    return {
        "role": role,
        "type": "rule_bbox",
        "bbox": [x, 230, x + 175, 350],
        "triggered": triggered,
    }


def _frame(marker=120):
    """Тёмный кадр с яркой «деталью» — как реальная камера на конвейере."""
    frame = np.zeros((480, 800, 3), dtype=np.uint8)
    frame[:] = (30, 34, 40)
    frame[245:340, marker:marker + 165] = (80, 205, 105)
    return frame


def _green_in_overlay_zone(img, y_limit=245):
    """Число «зелёных» пикселей разметки над деталью (y < y_limit).

    Считаем только верхнюю полосу кадра: там проходит верхняя сторона
    bbox'а разметки, а сама деталь начинается ниже (y >= 245), поэтому
    её собственные зелёные пиксели в подсчёт не попадают.
    """
    zone = img[:y_limit]
    mask = (
        (zone[:, :, 1] > 150)
        & (zone[:, :, 0] < 100)
        & (zone[:, :, 2] < 100)
    )
    return int(mask.sum())


def test_линии_геометрии_переживают_jpeg_кодирование():
    """1px-линии разметки исчезали при JPEG: после кодирования в кадре
    не оставалось ни одного пикселя разметки (chroma subsampling).

    Толщина 2px обязана сохранять верхнюю сторону bbox'а видимой.
    """
    server = UIServer()
    frame = _frame()
    drawing = _rule_bbox("INPUT_LEFT", 100)
    rules = [FakeRuleResult(drawings=[drawing])]

    server.update(
        frames={"INPUT_LEFT": frame},
        vision_results={},
        rule_results=rules,
        run_frames=[{"INPUT_LEFT": frame}],
        run_rule_results=[rules],
    )

    # Прямой рендер: разметка нарисована.
    rendered = DebugOverlay.render_frame(frame.copy(), "INPUT_LEFT", rules)
    assert _green_in_overlay_zone(rendered) >= 150, (
        "разметка не нарисована на кадре до кодирования"
    )

    # Через /frame (как браузер): разметка обязана пережить encode/decode.
    jpeg = server._get_or_render("INPUT_LEFT", "RULES", "main", None)
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert _green_in_overlay_zone(decoded) >= 150, (
        "геометрия исчезла после JPEG-кодирования: "
        f"осталось {_green_in_overlay_zone(decoded)} пикселей"
    )

    # Миниатюра (preview) — тоже.
    preview_jpeg = server._get_or_render("INPUT_LEFT", "RULES", "preview", None)
    decoded = cv2.imdecode(
        np.frombuffer(preview_jpeg, np.uint8), cv2.IMREAD_COLOR,
    )
    assert _green_in_overlay_zone(decoded) >= 150, (
        "геометрия исчезла в миниатюре после JPEG-кодирования"
    )


def test_run_кадр_рисуется_с_разметкой_этой_же_стадии():
    server = UIServer()
    run_frame = _frame(marker=200)
    live_frame = _frame(marker=400)
    drawing = _rule_bbox("TOP", 200)
    run_rules = [FakeRuleResult(drawings=[drawing])]

    server.update(
        frames={"TOP": live_frame},
        vision_results={},
        rule_results=[],
        run_frames=[{"TOP": run_frame}],
        run_rule_results=[run_rules],
    )

    # run=1: замороженный кадр стадии + разметка этой стадии.
    jpeg = server._get_or_render("TOP", "RULES", "main", 1)
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert _green_in_overlay_zone(decoded) >= 150, (
        "run=1 не нарисовал разметку стадии"
    )

    # Без run: live-кадр + общий набор правил (пустой) — без чужой разметки.
    jpeg = server._get_or_render("TOP", "RULES", "main", None)
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert _green_in_overlay_zone(decoded) < 150, (
        "live-кадр не должен нести разметку стадии"
    )


def test_run_запрос_роли_вне_стадии_не_применяет_разметку_стадии():
    """Роль отсутствует в кадрах стадии: fallback на live-кадр обязан
    использовать общий набор правил, а не разметку стадии, которая
    посчитана по другим кадрам («показывает не ту»)."""
    server = UIServer()
    live_frame = _frame(marker=400)
    stage_drawing = _rule_bbox("TOP", 200)
    stage_rules = [FakeRuleResult(drawings=[stage_drawing])]

    server.update(
        frames={"TOP": live_frame},
        vision_results={},
        rule_results=[],
        # В стадии только INPUT-роли; TOP в наборе нет.
        run_frames=[{"INPUT_LEFT": _frame(marker=100)}],
        run_rule_results=[stage_rules],
    )

    jpeg = server._get_or_render("TOP", "RULES", "main", 1)
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert _green_in_overlay_zone(decoded) < 150, (
        "разметка стадии нарисована поверх live-кадра чужой роли"
    )


def test_clear_evidence_убирает_правила_но_сохраняет_кадры_стадии():
    server = UIServer()
    frame = _frame()
    rules = [FakeRuleResult(drawings=[_rule_bbox("TOP", 100)])]

    server.update(
        frames={"TOP": frame},
        vision_results={"TOP": [{"class": "case", "bbox": [100, 230, 275, 350]}]},
        rule_results=rules,
        run_frames=[{"TOP": frame}],
        run_rule_results=[rules],
    )
    assert server.get_frame_count() == 1
    assert server.rule_results

    server.clear_evidence()

    assert server.rule_results == [], "clear_evidence обязана убрать правила"
    assert server.vision_results == {}, (
        "clear_evidence обязана убрать детекции"
    )
    assert server.get_frame_count() == 1, (
        "кадры стадии должны сохраниться для диагностики"
    )
    # run=1 по-прежнему отдаёт кадр стадии С его разметкой — оператор может
    # посмотреть последний анализ даже после очистки общего набора.
    jpeg = server._get_or_render("TOP", "RULES", "main", 1)
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert _green_in_overlay_zone(decoded) >= 150, (
        "run=1 потерял разметку стадии после clear_evidence"
    )
    # А live-путь (без run) больше не несёт правил.
    jpeg = server._get_or_render("TOP", "RULES", "main", None)
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert _green_in_overlay_zone(decoded) < 150, (
        "live-кадр несёт правила после clear_evidence"
    )


def test_update_нормализует_run_rule_results():
    """run_rule_results приходят списком правил на прогон; сервер должен
    хранить их списком и отдавать в _get_or_render без потерь."""
    server = UIServer()
    frame = _frame()
    rules = [FakeRuleResult(drawings=[_rule_bbox("TOP", 100)])]

    server.update(
        frames={"TOP": frame},
        vision_results={},
        rule_results=[],
        run_frames=[{"TOP": frame}],
        run_rule_results=[rules],
    )

    assert len(server.run_rule_results) == 1
    assert server.run_rule_results[0] == rules
