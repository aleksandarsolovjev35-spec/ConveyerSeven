import serial
import time
import threading


class SerialTransport:
    """
    Потокобезопасная обёртка над Serial-портом.
    Операции выполняются один раз: после ошибки состояние движения неизвестно,
    поэтому приложение должно перейти в FAULT, а не переподключаться и повторять.
    """

    def __init__(self, port: str = "COM4", baudrate: int = 115200):
        self.port     = port
        self.baudrate = baudrate
        self.lock     = threading.Lock()
        self.ser      = self._open(port, baudrate)

    def send(self, command: str):
        """Отправить команду один раз, добавив перевод строки.

        Запись могла дойти до контроллера даже если host получил ошибку.
        Поэтому motion/config commands никогда не повторяются автоматически.
        """
        with self.lock:
            self._do_send(command)

    def query(self, command: str, delay: float = 0.15) -> str:
        """Один атомарный request/response без reconnect во время цикла."""
        with self.lock:
            return self._do_query(command, delay)

    def close(self):
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception as exc:
                    print(f"[SERIAL] Ошибка закрытия порта: {exc}")

    # Internal

    @staticmethod
    def _open(port: str, baudrate: int) -> serial.Serial:
        ser = serial.Serial(
            port,
            baudrate,
            timeout=0.5,
            write_timeout=2.0,
        )
        time.sleep(2)
        return ser

    def _do_send(self, command: str):
        self.ser.write(f"{command}\n".encode())
        self.ser.flush()

    def _do_query(self, command: str, delay: float) -> str:
        self.ser.reset_input_buffer()
        self.ser.write(f"{command}\n".encode())
        self.ser.flush()

        time.sleep(delay)

        data = self.ser.read_all().decode(errors="ignore").strip()
        return data