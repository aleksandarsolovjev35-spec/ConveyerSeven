"""Журналирование работы линии.

Раньше вся диагностика жила в ``print()``: сообщения писались только в
консоль и исчезали вместе с окном терминала — расследовать инцидент на
объекте было нечем. Этот модуль даёт две вещи:

1. Настоящий ``logging`` с ротацией: ``logs/session_YYYYMMDD_HHMMSS.log``
   (10 МБ × 5 файлов), таймстампы с миллисекундами, уровни.
2. Tee-перехват ``sys.stdout``/``sys.stderr``: все существующие ``print``
   продолжают работать как раньше, но дублируются в файл. Конвертировать
   150+ мест печати не обязательно — они уже в журнале; новые места должны
   использовать :func:`get_logger` напрямую.

Важно: консольный обработчик создаётся ДО подмены ``sys.stderr``, поэтому
цикла «лог пишет в stdout, tee пишет в лог» не возникает.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR_ENV = "LOG_DIR"
LOG_LEVEL_ENV = "LOG_LEVEL"

_DEFAULT_LOG_DIR = "logs"
_FILE_MAX_BYTES = 10 * 1024 * 1024
_FILE_BACKUP_COUNT = 5

_FILE_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s"
)
_CONSOLE_FORMAT = "%(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_session_path: Path | None = None
_tee_stdout: "_PrintTee | None" = None
_tee_stderr: "_PrintTee | None" = None


def get_logger(name: str) -> logging.Logger:
    """Логгер модуля. Имя канала видно в файле журнала."""
    return logging.getLogger(name)


def setup_logging(
    log_dir: str | None = None,
    level: str | None = None,
) -> Path:
    """Настроить корневой логгер: файл с ротацией + консоль.

    Идемпотентна: повторный вызов возвращает путь уже открытого журнала.
    Файл получает DEBUG и ниже, консоль — INFO и ниже (уровень консоли
    можно поднять/опустить через ``LOG_LEVEL``).
    """
    global _session_path
    if _session_path is not None:
        return _session_path

    root = logging.getLogger()
    if root.handlers:
        # Кто-то настроил раньше (например, тесты) — не плодим обработчики.
        for handler in root.handlers:
            if isinstance(handler, RotatingFileHandler):
                _session_path = Path(handler.baseFilename)
                return _session_path

    console_level = _coerce_level(level or os.environ.get(LOG_LEVEL_ENV))
    root_dir = Path(
        log_dir or os.environ.get(LOG_DIR_ENV) or _DEFAULT_LOG_DIR
    )
    root_dir.mkdir(parents=True, exist_ok=True)
    session = (
        root_dir
        / f"session_{datetime.now():%Y%m%d_%H%M%S}.log"
    )

    file_handler = RotatingFileHandler(
        session,
        maxBytes=_FILE_MAX_BYTES,
        backupCount=_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(_FILE_FORMAT, _DATE_FORMAT)
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))

    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.captureWarnings(True)
    _session_path = session
    return session


def _coerce_level(raw: str | None) -> int:
    if not raw:
        return logging.INFO
    value = logging.getLevelName(str(raw).upper())
    return value if isinstance(value, int) else logging.INFO


class _PrintTee:
    """Поток-tee: печатает в исходный stream и построчно — в логгер."""

    def __init__(self, stream, logger: logging.Logger, level: int):
        self._stream = stream
        self._logger = logger
        self._level = level
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        written = self._stream.write(text)
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if line.strip():
                    self._logger.log(self._level, line.rstrip())
        return written if written is not None else len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        self._stream.flush()

    def flush_pending(self):
        """Дописать в лог «хвост» без перевода строки (при выходе)."""
        with self._lock:
            tail = self._buffer
            self._buffer = ""
        if tail.strip():
            self._logger.log(self._level, tail.rstrip())

    def isatty(self):
        return self._stream.isatty()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def capture_prints() -> None:
    """Начать дублировать stdout/stderr в журнал.

    Вызывать ПОСЛЕ :func:`setup_logging`: консольный обработчик к этому
    моменту уже держит ссылку на исходный stderr, поэтому рекурсии нет.
    """
    global _tee_stdout, _tee_stderr
    if _tee_stdout is not None:
        return
    _tee_stdout = _PrintTee(sys.stdout, get_logger("stdout"), logging.INFO)
    _tee_stderr = _PrintTee(sys.stderr, get_logger("stderr"), logging.ERROR)
    sys.stdout = _tee_stdout
    sys.stderr = _tee_stderr
    atexit.register(_flush_tees)


def _flush_tees():
    for tee in (_tee_stdout, _tee_stderr):
        if tee is not None:
            try:
                tee.flush_pending()
            except Exception:
                pass


def _restore_print_streams() -> None:
    """Снять tee-перехват и вернуть исходные stdout/stderr."""
    global _tee_stdout, _tee_stderr
    for tee, name in ((_tee_stdout, "stdout"), (_tee_stderr, "stderr")):
        if tee is None:
            continue
        try:
            tee.flush_pending()
        except Exception:
            pass
        if getattr(sys, name) is tee:
            setattr(sys, name, tee._stream)
    _tee_stdout = None
    _tee_stderr = None


def _reset_for_tests() -> None:
    """Полный сброс глобального состояния модуля. Только для тестов."""
    global _session_path
    _restore_print_streams()
    _flush_tees()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    _session_path = None


def install_excepthooks() -> None:
    """Необработанные исключения потоков и процесса — в журнал как CRITICAL."""

    def _sys_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        get_logger("crash").critical(
            "Необработанное исключение",
            exc_info=(exc_type, exc, tb),
        )

    def _thread_hook(args: threading.ExceptHookArgs):
        thread_name = args.thread.name if args.thread else "?"
        get_logger("crash").critical(
            "Необработанное исключение в потоке %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
