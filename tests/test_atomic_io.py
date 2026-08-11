"""Атомарная запись конфигурации: файл либо старый, либо новый целиком.

Пороги и конфиг архива правятся оператором прямо во время смены. Раньше
запись шла обычным ``open(path, "w")``: усечение файла происходит сразу, а
содержимое пишется потом — отключение питания между этими моментами
оставляло на диске пустой или обрезанный JSON, и линия не поднималась.
"""

import json
from pathlib import Path

import pytest

from domain.atomic_io import atomic_write_bytes, atomic_write_json, atomic_write_text
from domain.threshold_loader import ThresholdLoader

THRESHOLDS_FILE = Path(__file__).resolve().parents[1] / "thresholds.json"


def test_запись_создаёт_файл_с_ожидаемым_содержимым(tmp_path):
    target = tmp_path / "config.json"
    atomic_write_json(str(target), {"role": {"param": 1}})

    assert json.loads(target.read_text(encoding="utf-8")) == {"role": {"param": 1}}
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_кириллица_не_экранируется(tmp_path):
    """Пороги содержат русские подписи — они должны читаться глазами."""
    target = tmp_path / "labels.json"
    atomic_write_json(str(target), {"_label.depth": "Глубина, px"})

    assert "Глубина, px" in target.read_text(encoding="utf-8")


def test_ошибка_записи_сохраняет_прежнюю_версию_файла(tmp_path, monkeypatch):
    """Сбой на середине записи не должен портить рабочий конфиг."""
    target = tmp_path / "config.json"
    atomic_write_text(str(target), '{"старое": "значение"}')

    import domain.atomic_io as atomic_io

    def boom(*args, **kwargs):
        raise OSError("диск отвалился")

    # Падаем уже после того, как временный файл создан и заполнен.
    monkeypatch.setattr(atomic_io.os, "replace", boom)

    with pytest.raises(OSError):
        atomic_write_text(str(target), '{"новое": "значение"}')

    assert target.read_text(encoding="utf-8") == '{"старое": "значение"}'


def test_временный_файл_не_остаётся_после_сбоя(tmp_path, monkeypatch):
    target = tmp_path / "config.json"

    import domain.atomic_io as atomic_io

    monkeypatch.setattr(
        atomic_io.os, "replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("сбой")),
    )

    with pytest.raises(OSError):
        atomic_write_bytes(str(target), b"data")

    assert list(tmp_path.iterdir()) == [], "мусорный .tmp не должен переживать сбой"


def test_несериализуемый_объект_не_создаёт_файл(tmp_path):
    """JSON собирается в память до открытия файла."""
    target = tmp_path / "config.json"

    with pytest.raises(TypeError):
        atomic_write_json(str(target), {"bad": object()})

    assert not target.exists()


def test_перезапись_не_оставляет_хвоста_от_длинного_файла(tmp_path):
    """``os.replace`` подменяет файл целиком, а не пишет поверх."""
    target = tmp_path / "config.json"
    atomic_write_text(str(target), "x" * 5000)
    atomic_write_text(str(target), "короткая версия")

    assert target.read_text(encoding="utf-8") == "короткая версия"


def test_сохранение_порогов_идёт_через_атомарную_запись(tmp_path, monkeypatch):
    """Регрессия: ThresholdLoader.save_file больше не усекает целевой файл.

    Проверяем именно способ записи: целевой путь не должен открываться в
    режиме "w" — иначе сбой питания посреди сохранения обнулит пороги.
    """
    loader = ThresholdLoader(str(THRESHOLDS_FILE))
    target = tmp_path / "thresholds.json"

    truncating_open_used = []
    real_open = open

    def watched_open(file, mode="r", *args, **kwargs):
        if "w" in str(mode) and str(file) == str(target):
            truncating_open_used.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", watched_open)
    ThresholdLoader.save_file(str(target), loader.thresholds, loader.labels)
    monkeypatch.undo()

    assert truncating_open_used == [], (
        "целевой файл порогов нельзя открывать на запись напрямую"
    )
    # Сохранённое читается обратно тем же загрузчиком без потерь.
    assert ThresholdLoader(str(target)).thresholds == loader.thresholds
