"""Правило наличия корпуса на входе: строгий порог и консенсус двух камер.

Это служебное правило решает, есть ли деталь в лотке; ложное «есть» создаёт
Part без механики, ложное «нет» теряет деталь из учёта.
"""

import pytest

from domain.defect_rules.rule_input_part_presence import InputPartPresenceRule


THRESHOLDS = {
    "INPUT_LEFT.input_window_geometry_min_confidence": 0.40,
    "INPUT_RIGHT.input_window_geometry_min_confidence": 0.40,
    # До двух срабатываний на камеру считаются ложными.
    "input_part_presence_false_positive_max_count": 2,
}


def make_rule(thresholds=None):
    return InputPartPresenceRule(dict(THRESHOLDS if thresholds is None else thresholds))


def detections(count, confidence=0.9, cls="flatness"):
    return [{"class": cls, "confidence": confidence, "bbox": [0, 0, 10, 10]}
            for _ in range(count)]


def test_корпус_есть_когда_обе_камеры_выше_порога_ложных():
    rule = make_rule()
    result = rule.check({
        "INPUT_LEFT": detections(3),
        "INPUT_RIGHT": detections(4),
    })
    assert result.details["empty_tray"] is False
    assert result.details["presence_by_role"] == {
        "INPUT_LEFT": True, "INPUT_RIGHT": True,
    }
    # Служебное правило никогда не triggered.
    assert result.triggered is False


def test_порог_ложных_строгий_ровно_max_count_это_пусто():
    rule = make_rule()
    result = rule.check({
        "INPUT_LEFT": detections(2),   # ровно на границе — недостаточно
        "INPUT_RIGHT": detections(3),
    })
    assert result.details["empty_tray"] is True
    assert result.details["presence_by_role"]["INPUT_LEFT"] is False


def test_одна_камера_без_корпуса_значит_пусто_даже_при_полной_второй():
    rule = make_rule()
    result = rule.check({
        "INPUT_LEFT": detections(5),
        "INPUT_RIGHT": [],
    })
    assert result.details["empty_tray"] is True


def test_детекции_ниже_уверенности_не_считаются():
    rule = make_rule()
    result = rule.check({
        "INPUT_LEFT": detections(10, confidence=0.39),
        "INPUT_RIGHT": detections(10, confidence=0.41),
    })
    assert result.details["empty_tray"] is True
    assert result.details["presence_by_role"] == {
        "INPUT_LEFT": False, "INPUT_RIGHT": True,
    }


def test_считается_только_класс_flatness():
    rule = make_rule()
    result = rule.check({
        "INPUT_LEFT": detections(10, cls="case_central"),
        "INPUT_RIGHT": detections(3) + detections(5, cls="pin"),
    })
    assert result.details["empty_tray"] is True
    assert result.details["presence_by_role"] == {
        "INPUT_LEFT": False, "INPUT_RIGHT": True,
    }


def test_ручной_анализ_с_одной_камерой_проверяет_только_её():
    rule = make_rule()
    # INPUT_RIGHT в выдаче отсутствует вовсе — как при одиночном диагностике.
    result = rule.check({"INPUT_LEFT": detections(3)})
    assert result.details["empty_tray"] is False
    lonely = rule.check({"INPUT_LEFT": []})
    assert lonely.details["empty_tray"] is True


def test_без_камер_в_выдаче_требуются_обе():
    rule = make_rule()
    result = rule.check({})
    assert result.details["empty_tray"] is True


def test_отключённое_правило_возвращает_skip():
    thresholds = dict(THRESHOLDS)
    thresholds["disabled_rules"] = ["part_presence"]
    rule = make_rule(thresholds)
    assert rule.enabled is False
    result = rule.check({"INPUT_LEFT": detections(10)})
    assert result.triggered is False
    assert result.details.get("skipped")


def test_невалидная_уверенность_это_ошибка_конфигурации():
    thresholds = dict(THRESHOLDS)
    thresholds["INPUT_LEFT.input_window_geometry_min_confidence"] = 1.5
    with pytest.raises(ValueError):
        make_rule(thresholds).check({"INPUT_LEFT": [], "INPUT_RIGHT": []})


def test_невалидный_порог_ложных_это_ошибка_конфигурации():
    thresholds = dict(THRESHOLDS)
    thresholds["input_part_presence_false_positive_max_count"] = -1
    with pytest.raises(ValueError):
        make_rule(thresholds).check({"INPUT_LEFT": [], "INPUT_RIGHT": []})
