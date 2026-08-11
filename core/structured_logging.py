"""Structlog configuration: JSON-lines ``app.log`` + human console mirror.

Разбор инцидента на линии — это вопросы вида «что было с деталью №417»
и «сколько раз за смену срабатывал watchdog». Отвечать на них грепом по
русскому тексту неудобно, поэтому события пишутся структурно:

``{"event": "part_sorted", "part_id": 417, "category": "BAD", ...}``

Один вызов ``slog.info("part_sorted", part_id=..., category=...)`` даёт:

* строку JSON в ``logs/app.log`` (машинный разбор, выгрузка в аналитику);
* человекочитаемую строку в консоли и ``session_*.log`` через штатный
  ``logging`` — оператор продолжает видеть привычный поток событий.

Зеркалирование во второй канал делает процессор :func:`_mirror_to_stdlib`,
поэтому в коде цикла остаётся ровно один вызов на событие.
"""

from __future__ import annotations

import atexit
import logging
from pathlib import Path
from typing import Any, TextIO

import structlog

JSON_LOG_NAME = "app.log"

# Ключи, которые уже отражены в самой stdlib-записи (уровень, время,
# канал) и не нужны в человекочитаемом зеркале.
_MIRROR_SKIP_KEYS = frozenset({"event", "level", "timestamp", "logger"})

_configured: bool = False
_json_path: Path | None = None
_json_stream: TextIO | None = None
_mirror_enabled: bool = True


def configure_structlog(
    log_dir: Path | str,
    *,
    level: int = logging.INFO,
    mirror_to_stdlib: bool = True,
) -> Path:
    """Настроить structlog и вернуть путь JSON-журнала ``app.log``.

    Идемпотентна: повторный вызов не открывает файл второй раз и не
    плодит обработчики (важно для тестов и для рестарта UI-потока).
    """
    global _configured, _json_path, _json_stream, _mirror_enabled

    if _configured and _json_path is not None:
        return _json_path

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / JSON_LOG_NAME

    # buffering=1 — построчный сброс: при аварийном обесточивании станка
    # в файле остаются все события вплоть до последнего.
    stream = json_path.open("a", encoding="utf-8", buffering=1)

    _mirror_enabled = bool(mirror_to_stdlib)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _channel_to_logger,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _mirror_to_stdlib,
    ]
    structlog.configure(
        processors=[*shared_processors, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.WriteLoggerFactory(file=stream),
        # Кэш выключен намеренно: модули держат ``slog`` в глобальной
        # переменной, а конфигурация применяется позже (и заново — в
        # тестах). С кэшем такой логгер навсегда запомнил бы поток,
        # существовавший до вызова configure_structlog.
        cache_logger_on_first_use=False,
    )

    _configured = True
    _json_path = json_path
    _json_stream = stream
    atexit.register(_close_stream)
    return json_path


def get_struct_logger(name: str) -> structlog.typing.BindableLogger:
    """Структурированный логгер канала ``name`` (попадает в поле ``logger``).

    Возвращается именно ленивый прокси structlog, а не результат
    ``.bind()``: модули объявляют ``slog = get_struct_logger("cycle")`` на
    уровне импорта, то есть ДО :func:`configure_structlog`. ``.bind()``
    материализовал бы логгер с конфигурацией по умолчанию, и события
    ушли бы в stdout мимо ``app.log``. Прокси же берёт конфигурацию в
    момент первой записи.
    """
    return structlog.get_logger(channel=name)


def json_log_path() -> Path | None:
    """Путь текущего JSON-журнала или ``None``, если структлог не настроен."""
    return _json_path


def _channel_to_logger(_logger, _method_name: str, event_dict: dict) -> dict:
    """Перенести связанный ``channel`` в стандартное поле ``logger``.

    Имя канала связывается под ключом ``channel``, потому что
    ``structlog.get_logger(logger=...)`` конфликтует с сигнатурой самой
    библиотеки. В журнале же поле должно называться привычно.
    """
    channel = event_dict.pop("channel", None)
    if channel is not None:
        event_dict.setdefault("logger", channel)
    return event_dict


def _mirror_to_stdlib(_logger, method_name: str, event_dict: dict) -> dict:
    """Продублировать событие в обычный ``logging`` человекочитаемой строкой.

    Процессор ничего не меняет в ``event_dict``: JSONRenderer получает тот
    же словарь и пишет полную структуру в ``app.log``.
    """
    if not _mirror_enabled:
        return event_dict
    channel = str(event_dict.get("logger") or "app")
    level = _LEVELS.get(method_name, logging.INFO)
    target = logging.getLogger(channel)
    if not target.isEnabledFor(level):
        return event_dict
    pairs = " ".join(
        f"{key}={value!r}"
        for key, value in event_dict.items()
        if key not in _MIRROR_SKIP_KEYS
    )
    message = str(event_dict.get("event", ""))
    target.log(level, f"{message} {pairs}".rstrip())
    return event_dict


_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "exception": logging.ERROR,
    "msg": logging.INFO,
}


def _close_stream() -> None:
    """Закрыть файловый поток JSON-журнала при выходе из процесса."""
    global _json_stream
    stream, _json_stream = _json_stream, None
    if stream is not None:
        try:
            stream.flush()
            stream.close()
        except Exception:  # noqa: BLE001 - выход не должен падать из-за лога
            pass


def _reset_for_tests() -> None:
    """Полный сброс состояния модуля. Только для тестов."""
    global _configured, _json_path
    _close_stream()
    structlog.reset_defaults()
    _configured = False
    _json_path = None
