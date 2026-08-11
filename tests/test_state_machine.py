"""Таблица переходов конечного автомата линии.

Автомат — сердце fail-closed поведения: неправильный переход не должен
тихо менять состояние, а callback обязан получать (old, new, action).
"""

from core.state_machine import StateMachine, State


def make():
    events = []
    sm = StateMachine(on_transition=lambda *args: events.append(args))
    return sm, events


def test_начальное_состояние_idle():
    sm, _ = make()
    assert sm.state == State.IDLE
    snapshot = sm.get_snapshot()
    assert snapshot == {
        "state": "IDLE",
        "exit_requested": False,
        "force_exit": False,
    }


def test_легальные_переходы_полного_цикла():
    sm, events = make()
    assert sm.request_start() is True
    assert sm.state == State.RUNNING
    assert sm.request_pause() is True
    assert sm.state == State.PAUSED
    assert sm.request_resume() is True
    assert sm.request_stop() is True
    assert sm.state == State.STOPPING
    assert sm.notify_line_empty() is True
    assert sm.state == State.STOPPED
    # Повторный пуск из STOPPED разрешён.
    assert sm.request_start() is True
    assert sm.state == State.RUNNING

    # Callback получил каждый переход строго по порядку.
    actions = [(new.value, action) for _old, new, action in events]
    assert actions == [
        ("RUNNING", "START"),
        ("PAUSED", "PAUSE"),
        ("RUNNING", "RESUME"),
        ("STOPPING", "STOP"),
        ("STOPPED", "EMPTY"),
        ("RUNNING", "START"),
    ]


def test_нелегальные_переходы_игнорируются_и_ничего_не_ломают():
    sm, events = make()
    # Из IDLE нельзя ни на паузу, ни в останов, ни «линия пуста».
    assert sm.request_pause() is False
    assert sm.request_stop() is False
    assert sm.request_resume() is False
    assert sm.notify_line_empty() is False
    assert sm.state == State.IDLE
    assert events == []

    assert sm.request_start() is True
    # Повторный START и RESUME в RUNNING недопустимы.
    assert sm.request_start() is False
    assert sm.request_resume() is False
    assert sm.state == State.RUNNING


def test_fault_терминален_и_достижим_из_любого_рабочего_состояния():
    sm, _ = make()
    assert sm.request_start() is True
    assert sm.notify_fault() is True
    assert sm.state == State.FAULT
    # Из FAULT выхода таблицей не предусмотрено.
    assert sm.request_start() is False
    assert sm.request_stop() is False
    assert sm.request_pause() is False
    assert sm.state == State.FAULT


def test_request_exit_из_running_переходит_в_stopping_с_событием():
    sm, events = make()
    sm.request_start()
    events.clear()
    assert sm.request_exit() is True
    assert sm.exit_requested is True
    assert sm.state == State.STOPPING
    assert [action for _o, _n, action in events] == ["STOP"]


def test_request_exit_из_idle_не_меняет_состояние():
    sm, events = make()
    assert sm.request_exit() is True
    assert sm.exit_requested is True
    assert sm.state == State.IDLE
    assert events == []


def test_force_exit_ставит_оба_флага():
    sm, _ = make()
    assert sm.request_force_exit() is True
    assert sm.exit_requested is True
    assert sm.force_exit is True


def test_свойства_is_active_и_accepts_new_parts():
    sm, _ = make()
    assert not sm.is_active and not sm.accepts_new_parts
    sm.request_start()
    assert sm.is_active and sm.accepts_new_parts
    sm.request_stop()
    # STOPPING: линия ещё активна, но новые детали уже не принимает.
    assert sm.is_active and not sm.accepts_new_parts
    sm.notify_line_empty()
    assert not sm.is_active and not sm.accepts_new_parts
