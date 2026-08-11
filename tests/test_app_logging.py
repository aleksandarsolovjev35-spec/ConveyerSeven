"""Системное журналирование: файловый sink, tee print, идемпотентность.

Журнал — единственный источник истины при разборе смены, поэтому базовые
гарантии фиксируются тестами: файл создаётся, print() попадает в него,
повторная инициализация не плодит обработчики.
"""

import logging

import pytest

from core import app_logging
from core.app_logging import capture_prints, get_logger, setup_logging


@pytest.fixture(autouse=True)
def _clean_logging_state():
    app_logging._reset_for_tests()
    yield
    app_logging._reset_for_tests()


def test_setup_logging_создаёт_файл_и_идемпотентна(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    first = setup_logging()
    assert first.exists()
    assert first.name.startswith("session_")
    # Повторный вызов возвращает тот же файл и не дублирует обработчики.
    root_handlers = len(logging.getLogger().handlers)
    assert setup_logging() == first
    assert len(logging.getLogger().handlers) == root_handlers


def test_get_logger_возвращает_именованный_логгер():
    assert get_logger("probe").name == "probe"
    assert get_logger("probe") is get_logger("probe")


def test_записи_попадают_в_файл(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    log_file = setup_logging()
    get_logger("probe.channel").info("маркер-запись-123")
    logging.shutdown()
    text = log_file.read_text(encoding="utf-8")
    assert "маркер-запись-123" in text
    assert "probe.channel" in text


def test_capture_prints_направляет_print_в_журнал(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    log_file = setup_logging()
    capture_prints()
    print("строка-от-print-456")  # tee сам пишет в лог
    app_logging._restore_print_streams()
    logging.shutdown()
    text = log_file.read_text(encoding="utf-8")
    assert "строка-от-print-456" in text
    assert "stdout" in text


def test_restore_возвращает_исходные_потоки(tmp_path, monkeypatch):
    import sys
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    setup_logging()
    original = sys.stdout
    capture_prints()
    assert sys.stdout is not original
    app_logging._restore_print_streams()
    assert sys.stdout is original
    # Повторный захват после восстановления работает снова.
    capture_prints()
    assert sys.stdout is not original
    app_logging._restore_print_streams()
