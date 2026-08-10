"""Тесты SerialTransport: send/query/close через фейковый serial.Serial."""

import unittest
from unittest.mock import patch

from hardware.serial_transport import SerialTransport


class FakeSerial:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.is_open = True
        self.writes = []
        self._buffer = b""
        self.reply = b""          # отдельный «ответ контроллера»
        self.flush_calls = 0
        self.input_resets = 0

    def write(self, data):
        self.writes.append(data)
        self._buffer += data

    def flush(self):
        self.flush_calls += 1

    def reset_input_buffer(self):
        self.input_resets += 1

    def read_all(self):
        if self.reply:
            data, self.reply = self.reply, b""
            return data
        data = self._buffer
        self._buffer = b""
        return data

    def close(self):
        self.is_open = False


@patch("hardware.serial_transport.time.sleep", lambda _seconds: None)
class SerialTransportTest(unittest.TestCase):
    @patch("hardware.serial_transport.serial.Serial", FakeSerial)
    def test_open_uses_port_and_baudrate(self):
        transport = SerialTransport(port="COM9", baudrate=115200)
        fake = transport.ser
        self.assertEqual(fake.args[0], "COM9")
        self.assertEqual(fake.args[1], 115200)
        self.assertEqual(fake.kwargs["timeout"], 0.5)
        transport.close()
        self.assertFalse(fake.is_open)

    @patch("hardware.serial_transport.serial.Serial", FakeSerial)
    def test_send_adds_newline(self):
        transport = SerialTransport(port="COM9")
        transport.send("G5 S20000")
        self.assertEqual(transport.ser.writes, [b"G5 S20000\n"])
        transport.close()

    @patch("hardware.serial_transport.serial.Serial", FakeSerial)
    def test_query_returns_response(self):
        transport = SerialTransport(port="COM9")
        transport.ser.reply = b"0\r\n"   # ответ контроллера на I1
        reply = transport.query("I1", delay=0.0)
        self.assertEqual(reply, "0")
        self.assertEqual(transport.ser.input_resets, 1)
        self.assertEqual(transport.ser.writes[-1], b"I1\n")
        transport.close()

    @patch("hardware.serial_transport.serial.Serial", FakeSerial)
    def test_close_idempotent(self):
        transport = SerialTransport(port="COM9")
        transport.close()
        transport.close()  # не должно бросить
        self.assertFalse(transport.ser.is_open)


if __name__ == "__main__":
    unittest.main()
