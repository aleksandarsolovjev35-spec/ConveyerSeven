"""JSON-журнал событий линии: формат, зеркало в stdlib, идемпотентность.

Разбор инцидента строится на `logs/app.log`, поэтому тесты проверяют не
факт вызова, а сам файл: каждая строка обязана быть валидным JSON с
полями `event`/`level`/`timestamp`, иначе выгрузка в аналитику встанет.
"""

import json
import logging

import pytest

from core.structured_logging import (
    JSON_LOG_NAME,
    configure_structlog,
    get_struct_logger,
    json_log_path,
)
from core.structured_logging import _reset_for_tests as reset_structlog


@pytest.fixture(autouse=True)
def _clean_structlog():
    reset_structlog()
    yield
    reset_structlog()


def read_events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_создаёт_app_log_в_каталоге_логов(tmp_path):
    path = configure_structlog(tmp_path / "logs")
    assert path.name == JSON_LOG_NAME
    assert path.parent.is_dir()
    assert json_log_path() == path


def test_событие_пишется_одной_строкой_json(tmp_path):
    path = configure_structlog(tmp_path / "logs")
    get_struct_logger("cycle").info(
        "part_sorted", part_id=417, category="BAD", defects=["glass"]
    )

    [event] = read_events(path)
    assert event["event"] == "part_sorted"
    assert event["part_id"] == 417
    assert event["category"] == "BAD"
    assert event["defects"] == ["glass"]
    assert event["logger"] == "cycle"
    assert event["level"] == "info"
    assert event["timestamp"].endswith("Z"), "время обязано быть в UTC ISO"


def test_кириллица_читаема_а_не_экранирована(tmp_path):
    path = configure_structlog(tmp_path / "logs")
    get_struct_logger("cycle").warning("line_fault", reason="Обрыв связи")
    [event] = read_events(path)
    assert event["reason"] == "Обрыв связи"


def test_уровень_ниже_порога_не_пишется(tmp_path):
    path = configure_structlog(tmp_path / "logs", level=logging.INFO)
    slog = get_struct_logger("cycle")
    slog.debug("part_persisted", record_id=1)
    slog.info("step_started", step=1)

    events = read_events(path)
    assert [e["event"] for e in events] == ["step_started"]


def test_событие_дублируется_в_обычный_logging(tmp_path, caplog):
    configure_structlog(tmp_path / "logs")
    with caplog.at_level(logging.INFO, logger="cycle"):
        get_struct_logger("cycle").info("part_sorted", part_id=7, category="GOOD")

    [record] = [r for r in caplog.records if r.name == "cycle"]
    # Оператор в консоли видит человекочитаемую строку с теми же данными.
    assert "part_sorted" in record.message
    assert "part_id=7" in record.message
    assert record.levelno == logging.INFO


def test_зеркало_можно_отключить(tmp_path, caplog):
    path = configure_structlog(tmp_path / "logs", mirror_to_stdlib=False)
    with caplog.at_level(logging.INFO, logger="cycle"):
        get_struct_logger("cycle").info("part_sorted", part_id=7)

    assert [r for r in caplog.records if r.name == "cycle"] == []
    assert len(read_events(path)) == 1, "в JSON событие обязано остаться"


def test_повторный_вызов_не_плодит_файлы_и_обработчики(tmp_path):
    first = configure_structlog(tmp_path / "logs")
    second = configure_structlog(tmp_path / "other")

    assert first == second, "повторная настройка не должна менять журнал"
    assert not (tmp_path / "other").exists()

    get_struct_logger("cycle").info("step_started", step=1)
    assert len(read_events(first)) == 1


def test_записи_дописываются_после_перезапуска(tmp_path):
    path = configure_structlog(tmp_path / "logs")
    get_struct_logger("cycle").info("session_started", session_id=1)
    reset_structlog()

    configure_structlog(tmp_path / "logs")
    get_struct_logger("cycle").info("session_ended", session_id=1)

    events = read_events(path)
    assert [e["event"] for e in events] == ["session_started", "session_ended"]


def test_исключение_попадает_в_json(tmp_path):
    path = configure_structlog(tmp_path / "logs")
    try:
        raise RuntimeError("привод не ответил")
    except RuntimeError:
        get_struct_logger("cycle").exception("line_fault", stage="drop")

    [event] = read_events(path)
    assert event["event"] == "line_fault"
    assert "RuntimeError" in event["exception"]
    assert "привод не ответил" in event["exception"]
