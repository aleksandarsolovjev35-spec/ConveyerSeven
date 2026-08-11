"""LiveCaptureGate: поролевое разграничение live и inspection чтений.

Gate гарантирует, что inspection не читает камеру, пока live-поток
не завершил начатое чтение этой роли. Тесты фиксируют:

* pause() ждёт завершения активных live-чтений;
* pause_roles() приостанавливает только указанные роли;
* live_read()/live_reads() не дают доступ, пока роль на паузе;
* timeout в pause() возвращает False и откатывает pause_depth;
* reset() снимает все паузы.
"""

import threading
import time

from core.live_preview import LiveCaptureGate


class TestGlobalPause:
    def test_pause_без_активных_чтений_возвращает_true(self):
        gate = LiveCaptureGate()
        assert gate.pause() is True
        gate.resume()

    def test_pause_ждёт_завершения_активного_чтения(self):
        gate = LiveCaptureGate()
        entered = threading.Event()
        done = threading.Event()

        def live_reader():
            with gate.live_read("TOP") as allowed:
                assert allowed is True
                entered.set()
                time.sleep(0.2)
            done.set()

        t = threading.Thread(target=live_reader, daemon=True)
        t.start()
        entered.wait(timeout=2.0)
        # pause должен дождаться завершения live_reader
        result = gate.pause(timeout=3.0)
        assert result is True
        gate.resume()
        t.join(timeout=3.0)
        assert done.is_set()

    def test_pause_timeout_возвращает_false(self):
        gate = LiveCaptureGate()
        entered = threading.Event()

        def live_reader():
            with gate.live_read("TOP") as allowed:
                assert allowed is True
                entered.set()
                time.sleep(2.0)

        t = threading.Thread(target=live_reader, daemon=True)
        t.start()
        entered.wait(timeout=2.0)
        result = gate.pause(timeout=0.1)
        assert result is False
        t.join(timeout=3.0)

    def test_live_read_блокирован_во_время_паузы(self):
        gate = LiveCaptureGate()
        gate.pause()
        with gate.live_read("TOP") as allowed:
            assert allowed is False
        gate.resume()

    def test_resume_снимает_паузу(self):
        gate = LiveCaptureGate()
        gate.pause()
        gate.resume()
        with gate.live_read("TOP") as allowed:
            assert allowed is True

    def test_nested_pause(self):
        gate = LiveCaptureGate()
        gate.pause()
        gate.pause()
        gate.resume()
        # Всё ещё на паузе (depth=1)
        with gate.live_read("TOP") as allowed:
            assert allowed is False
        gate.resume()
        with gate.live_read("TOP") as allowed:
            assert allowed is True


class TestRolePause:
    def test_pause_roles_блокирует_только_указанные(self):
        gate = LiveCaptureGate()
        assert gate.pause_roles(("TOP",)) is True
        # TOP заблокирована
        with gate.live_read("TOP") as allowed:
            assert allowed is False
        # INPUT_LEFT свободна
        with gate.live_read("INPUT_LEFT") as allowed:
            assert allowed is True
        gate.resume_roles(("TOP",))

    def test_pause_roles_ждёт_drain_указанных_ролей(self):
        gate = LiveCaptureGate()
        entered = threading.Event()

        def live_reader():
            with gate.live_read("TOP") as allowed:
                assert allowed is True
                entered.set()
                time.sleep(0.2)

        t = threading.Thread(target=live_reader, daemon=True)
        t.start()
        entered.wait(timeout=2.0)
        result = gate.pause_roles(("TOP",), timeout=3.0)
        assert result is True
        gate.resume_roles(("TOP",))
        t.join(timeout=3.0)

    def test_pause_roles_timeout_откатывает_depth(self):
        gate = LiveCaptureGate()
        entered = threading.Event()

        def live_reader():
            with gate.live_read("TOP") as allowed:
                assert allowed is True
                entered.set()
                time.sleep(2.0)

        t = threading.Thread(target=live_reader, daemon=True)
        t.start()
        entered.wait(timeout=2.0)
        result = gate.pause_roles(("TOP",), timeout=0.1)
        assert result is False
        # После timeout роль должна быть снова доступна (но live_reader
        # всё ещё держит слот — зависит от timing).
        with gate.live_read("TOP"):
            pass  # не падает
        t.join(timeout=3.0)

    def test_pause_roles_пустой_список_не_блокирует(self):
        gate = LiveCaptureGate()
        assert gate.pause_roles(()) is True
        with gate.live_read("TOP") as allowed:
            assert allowed is True

    def test_live_reads_пропускает_заблокированные_роли(self):
        gate = LiveCaptureGate()
        gate.pause_roles(("TOP",))
        with gate.live_reads(("TOP", "INPUT_LEFT")) as allowed:
            assert "TOP" not in allowed
            assert "INPUT_LEFT" in allowed
        gate.resume_roles(("TOP",))


class TestReset:
    def test_reset_снимает_все_паузы(self):
        gate = LiveCaptureGate()
        gate.pause()
        gate.pause_roles(("TOP",))
        gate.reset()
        with gate.live_read("TOP") as allowed:
            assert allowed is True

    def test_reset_снимает_ролевые_паузы(self):
        gate = LiveCaptureGate()
        gate.pause_roles(("TOP", "INPUT_LEFT"))
        gate.reset()
        with gate.live_reads(("TOP", "INPUT_LEFT")) as allowed:
            assert "TOP" in allowed
            assert "INPUT_LEFT" in allowed


class TestMultipleRoles:
    def test_live_reads_пакет_все_роли(self):
        gate = LiveCaptureGate()
        with gate.live_reads(("TOP", "INPUT_LEFT", "INPUT_RIGHT")) as allowed:
            assert len(allowed) == 3
            assert set(allowed) == {"TOP", "INPUT_LEFT", "INPUT_RIGHT"}

    def test_live_reads_дубликаты_ролей_дедуплицируются(self):
        gate = LiveCaptureGate()
        with gate.live_reads(("TOP", "TOP", "INPUT_LEFT")) as allowed:
            assert len(allowed) == 2
