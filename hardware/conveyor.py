import re
import time


class Conveyor:
    """
    Управление конвейерной лентой.
    Использует I1 — ответ контроллера: одна строка "0" или "1".
    """

    def __init__(
        self,
        transport,
        speed: int = 20000,
        accel: int = 6000,
        steps_per_division: int = 19048,
        divisions_per_movement: int = 2,
    ):
        self.transport = transport
        if steps_per_division <= 0 or divisions_per_movement <= 0:
            raise ValueError("Conveyor geometry must be positive")
        self._set_params(
            speed,
            accel,
            steps_per_division,
            divisions_per_movement,
        )

    def move_step(self):
        """Один шаг конвейера."""
        self.transport.send("G3")
        time.sleep(0.4)

    def wait_stop(self, timeout: float = 15.0, progress_callback=None):
        """Ждать остановки и публиковать фактический I2 status."""
        start = time.time()
        data = ""
        status = ""

        while True:
            data = self.transport.query("I1", delay=0.1)
            stopped = self._parse_motion_reply(data)
            status = self.transport.query("I2", delay=0.1)
            parsed_status = self._parse_status(status)
            if progress_callback is not None:
                progress_callback(parsed_status)

            if stopped is True and self._strict_stop_confirmed(status):
                time.sleep(0.05)
                return

            if time.time() - start > timeout:
                raise TimeoutError(
                    f"Конвейер не остановился за {timeout}s. "
                    f"I1='{data}', I2='{status}'"
                )

            time.sleep(0.05)

    def emergency_stop(self):
        """Аварийная остановка."""
        self.transport.send("G1")

    def _set_params(
        self,
        speed: int,
        accel: int,
        steps_per_division: int,
        divisions_per_movement: int,
    ):
        self.transport.send(f"G5 S{speed}")
        self.transport.send(f"G4 S{accel}")
        self.transport.send(f"G7 S{steps_per_division}")
        self.transport.send(f"G6 S{divisions_per_movement}")
        # Сохраняем параметры: они читаются production-циклом
        # (_on_conveyor_progress) для расчёта длительности движения в UI.
        self.speed = int(speed)
        self.accel = int(accel)
        self.steps_per_division = int(steps_per_division)
        self.divisions_per_movement = int(divisions_per_movement)
        time.sleep(0.5)

    @staticmethod
    def _parse_status(data: str) -> dict:
        result = {"raw": data}
        for key in ("MOV", "WAIT", "POS", "TGT", "lastErr"):
            match = re.search(
                rf"\b{key}\s*=\s*(-?\d+)\b",
                data or "",
                re.IGNORECASE,
            )
            result[key.lower()] = int(match.group(1)) if match else None
        return result

    @staticmethod
    def _strict_stop_confirmed(data: str) -> bool:
        """I2 must confirm no movement, no inter-move wait and no error."""
        if not data:
            return False
        mov = re.search(r"\bMOV\s*=\s*(\d+)\b", data, re.IGNORECASE)
        wait = re.search(r"\bWAIT\s*=\s*(\d+)\b", data, re.IGNORECASE)
        error = re.search(r"\blastErr\s*=\s*(-?\d+)\b", data, re.IGNORECASE)
        return bool(
            mov and wait and error
            and int(mov.group(1)) == 0
            and int(wait.group(1)) == 0
            and int(error.group(1)) == 0
        )

    @staticmethod
    def _parse_motion_reply(data: str) -> bool | None:
        """
        Парсит ответ на I1.

        Прошивка отвечает ровно "0" (остановлен) или "1" (движется).
        Ищем последнюю строку содержащую только "0" или "1".
        Если ответ неразборчив — возвращаем None (= не уверены).
        """
        if not data:
            return None

        for line in reversed(data.splitlines()):
            stripped = line.strip()
            if stripped == "0":
                return True
            if stripped == "1":
                return False

        return None