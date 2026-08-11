"""Разбор строковых ответов контроллера convey15.

Прошивка отвечает неструктурированным текстом: на I1 — чистые "0"/"1",
на I2 — строка ключ=значение, причём сервисные сообщения могут
перемешиваться с ответами. Разбор обязан быть устойчив к этому.
"""

import pytest

from hardware.conveyor import Conveyor
from hardware.port_discovery import is_controller_response


class TestParseMotionReply:
    """I1: True = остановлен; False = движется; None = ответ не разобран."""

    @pytest.mark.parametrize("data,expected", [
        ("0", True),
        ("1", False),
        (" 0 \r\n", True),          # пробелы/CR вокруг ответа
        ("", None),
        (None, None),
        ("garbage", None),
        ("OK", None),
        # Сервисные строки прошивки не должны ломать разбор:
        # значение берётся из последней строки с чистым ответом.
        ("1\r\nMovement on pause...", False),
        ("Movement on pause...\r\n0", True),
        ("1\r\n0", True),           # последнее достоверное значение — в конце
    ])
    def test_разбор(self, data, expected):
        assert Conveyor._parse_motion_reply(data) is expected


class TestParseStatus:
    def test_полная_строка(self):
        parsed = Conveyor._parse_status(
            "MOV=1 WAIT=0 POS=1234 TGT=38096 lastErr=0"
        )
        assert parsed["mov"] == 1
        assert parsed["wait"] == 0
        assert parsed["pos"] == 1234
        assert parsed["tgt"] == 38096
        assert parsed["lasterr"] == 0

    def test_недостающие_ключи_дают_none(self):
        parsed = Conveyor._parse_status("POS=5")
        assert parsed["mov"] is None
        assert parsed["wait"] is None
        assert parsed["tgt"] is None
        assert parsed["lasterr"] is None
        assert parsed["pos"] == 5

    def test_пробелы_вокруг_равно_и_минус(self):
        parsed = Conveyor._parse_status("MOV = 0  WAIT= 1 lastErr= -1")
        assert parsed["mov"] == 0
        assert parsed["wait"] == 1
        assert parsed["lasterr"] == -1

    def test_пустая_строка(self):
        parsed = Conveyor._parse_status("")
        assert parsed["mov"] is None


class TestStrictStopConfirmed:
    """Остановка считается доказанной только при MOV=0, WAIT=0 и lastErr=0."""

    def test_чистая_остановка(self):
        assert Conveyor._strict_stop_confirmed(
            "MOV=0 WAIT=0 POS=0 TGT=0 lastErr=0"
        ) is True

    @pytest.mark.parametrize("data", [
        "MOV=1 WAIT=0 POS=0 TGT=0 lastErr=0",   # ещё едет
        "MOV=0 WAIT=1 POS=0 TGT=0 lastErr=0",   # межходовая пауза прошивки
        "MOV=0 WAIT=0 POS=0 TGT=0 lastErr=9",   # липкая ошибка прошивки
        "MOV=0 WAIT=0 POS=0 TGT=0",             # lastErr отсутствует вовсе
        "MOV=0 POS=0 TGT=0 lastErr=0",          # WAIT отсутствует
        "",                                      # пусто
        None,
    ])
    def test_неподтверждённые_варианты(self, data):
        assert Conveyor._strict_stop_confirmed(data) is False

    def test_регистр_ключей_не_важен(self):
        assert Conveyor._strict_stop_confirmed(
            "mov=0 wait=0 lasterr=0"
        ) is True


class TestControllerFingerprint:
    """find_controller опознаёт контроллер по формату I2-ответа."""

    def test_настоящий_ответ(self):
        assert is_controller_response(
            "MOV=0 WAIT=0 POS=0 TGT=0 lastErr=0"
        ) is True

    @pytest.mark.parametrize("response", [
        "",
        None,
        "MOV=0 WAIT=0 POS=0",            # нет lastErr
        "convey15 v2.4.0",               # ответ I6 — не I2
        "OK",
    ])
    def test_чужие_ответы(self, response):
        assert is_controller_response(response) is False
