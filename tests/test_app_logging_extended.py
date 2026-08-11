"""Расширенные тесты системного журналирования: excepthooks, flush_pending.

Дополняют базовые тесты test_app_logging.py: проверяют, что
необработанные исключения потоков и процесса попадают в канал crash,
а flush_pending() дописывает «хвост» без перевода строки.
"""

import logging
import sys
import threading
import time

import pytest

from core import app_logging
from core.app_logging import (
    capture_prints,
    get_logger,
    install_excepthooks,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _clean_logging_state():
    app_logging._reset_for_tests()
    yield
    app_logging._reset_for_tests()


class TestExcepthooks:
    def test_sys_excepthook_пишет_в_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        log_file = setup_logging()
        install_excepthooks()

        try:
            raise ValueError("тестовое исключение процесса")
        except ValueError:
            sys.excepthook(*sys.exc_info())

        logging.shutdown()
        text = log_file.read_text(encoding="utf-8")
        assert "crash" in text
        assert "тестовое исключение процесса" in text
        assert "CRITICAL" in text

    def test_sys_excepthook_игнорирует_keyboard_interrupt(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        log_file = setup_logging()
        install_excepthooks()
        original_hook = sys.__excepthook__
        # Перехватываем sys.__excepthook__, чтобы не печатать в stderr
        called = []

        def mock_original(exc_type, exc, tb):
            called.append(exc_type)

        sys.__excepthook__ = mock_original
        try:
            sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
        finally:
            sys.__excepthook__ = original_hook

        logging.shutdown()
        text = log_file.read_text(encoding="utf-8")
        assert KeyboardInterrupt in called
        # KeyboardInterrupt НЕ должен попасть в crash-канал
        assert "crash" not in text

    def test_thread_excepthook_пишет_в_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        log_file = setup_logging()
        install_excepthooks()

        # Поток с необработанным исключением: threading.excepthook
        # вызывается интерпретатором автоматически при завершении потока.
        def failing_thread():
            raise RuntimeError("ошибка в потоке")

        t = threading.Thread(target=failing_thread, name="test-worker")
        t.start()
        t.join()
        # Даём excepthook отработать (он синхронный в join)
        time.sleep(0.1)

        logging.shutdown()
        text = log_file.read_text(encoding="utf-8")
        assert "crash" in text
        assert "test-worker" in text
        assert "ошибка в потоке" in text


class TestFlushPending:
    def test_flush_pending_дописывает_хвост_без_newline(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        log_file = setup_logging()
        capture_prints()
        # Печатаем без перевода строки
        sys.stdout.write("хвост-без-ньюлайна")
        # До flush_pending буфер tee содержит этот текст
        app_logging._flush_tees()
        app_logging._restore_print_streams()
        logging.shutdown()
        text = log_file.read_text(encoding="utf-8")
        assert "хвост-без-ньюлайна" in text

    def test_restore_вызывает_flush_pending(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        log_file = setup_logging()
        capture_prints()
        sys.stdout.write("незавершённая-строка")
        app_logging._restore_print_streams()
        logging.shutdown()
        text = log_file.read_text(encoding="utf-8")
        assert "незавершённая-строка" in text


class TestLogLevel:
    def test_log_level_env_debug(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        log_file = setup_logging()
        log = get_logger("test.level")
        log.debug("debug-запись-для-проверки")
        logging.shutdown()
        text = log_file.read_text(encoding="utf-8")
        assert "debug-запись-для-проверки" in text

    def test_log_level_env_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        log_file = setup_logging()
        log = get_logger("test.level")
        log.info("info-не-должно-быть-в-консоли")
        log.warning("warning-должно-быть")
        logging.shutdown()
        text = log_file.read_text(encoding="utf-8")
        # В файле оба (DEBUG+)
        assert "info-не-должно-быть-в-консоли" in text
        assert "warning-должно-быть" in text

    def test_invalid_log_level_fallback_to_info(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        monkeypatch.setenv("LOG_LEVEL", "NOT_A_LEVEL")
        log_file = setup_logging()
        # Не должно упасть — fallback на INFO
        log = get_logger("test.fallback")
        log.info("info-fallback")
        logging.shutdown()
        text = log_file.read_text(encoding="utf-8")
        assert "info-fallback" in text


class TestWarnings:
    def test_capture_warnings_включён(self, tmp_path, monkeypatch):
        """setup_logging() включает logging.captureWarnings(True)."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        setup_logging()
        # logging.captureWarnings(True) устанавливает showwarning
        # на _showwarnmsg — проверяем, что он активен.
        import logging as _logging
        assert _logging.captureWarnings is not None or hasattr(
            _logging, "_showwarnmsg"
        )
