import re
import time

# Время, за которое ход должен проявиться в ответах контроллера. Если за это
# окно все опросы показывают чистую остановку без единого признака движения,
# команда G3 не была выполнена (потеряна на линии). Повторять физический шаг
# нельзя — соответствие деталь/позиция уже неизвестно, поэтому это FAULT.
MOTION_EVIDENCE_TIMEOUT = 2.5


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
        self._motion_started_at: float = 0.0
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
        self._motion_started_at = time.monotonic()
        self.transport.send("G3")
        time.sleep(0.4)

    def wait_stop(self, timeout: float = 15.0, progress_callback=None):
        """Ждать остановки и публиковать фактический I2 status.

        Остановка принимается только по свидетельствам контроллера: хотя бы
        один опрос обязан показать реальный ход (MOV=1 / POS≠0 / TGT≠0).
        Иначе «ход завершён» неотличим от «команда G3 не дошла».
        """
        start = time.monotonic()
        data = ""
        status = ""
        motion_seen = False
        motion_started_at = self._motion_started_at or start

        while True:
            data = self.transport.query("I1", delay=0.1)
            stopped = self._parse_motion_reply(data)
            status = self.transport.query("I2", delay=0.1)
            parsed_status = self._parse_status(status)
            if progress_callback is not None:
                progress_callback(parsed_status)

            if (
                stopped is False
                or parsed_status.get("mov") == 1
                or (parsed_status.get("pos") or 0) != 0
                or (parsed_status.get("tgt") or 0) != 0
            ):
                motion_seen = True

            # lastErr в прошивке липкий (команды сброса нет): ненулевое
            # значение — аппаратное событие, а не повод молча ждать таймаут.
            err_code = parsed_status.get("lasterr")
            if err_code not in (None, 0):
                raise RuntimeError(
                    f"Контроллер зафиксировал ошибку lastErr={err_code} "
                    f"(I2={status!r}); прошивка не сбрасывает её программно — "
                    "требуется перезапуск контроллера"
                )

            if stopped is True and self._strict_stop_confirmed(status):
                if motion_seen:
                    time.sleep(0.05)
                    self._motion_started_at = 0.0
                    return
                if time.monotonic() - motion_started_at > MOTION_EVIDENCE_TIMEOUT:
                    raise RuntimeError(
                        "Контроллер сообщает остановку без признаков хода: "
                        "команда G3 не выполнена — логические позиции деталей "
                        "не соответствуют механике "
                        f"(I1={data!r}, I2={status!r})"
                    )

            if time.monotonic() - start > timeout:
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
        # Фиксируем протокол остановки явно, а не дефолтами прошивки:
        # автопауза-стоп после хода нужна (G12 S1), а дефолтная межходовая
        # пауза 2000 мс — нет (G9 S0). Иначе WAIT=1 удерживался бы ~2 с после
        # каждого хода, и _strict_stop_confirmed ждал бы это окно на каждом
        # шаге линии.
        self.transport.send("G12 S1")
        self.transport.send("G9 S0")
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
