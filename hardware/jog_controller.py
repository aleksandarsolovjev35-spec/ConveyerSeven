# hardware/jog_controller.py

"""Dead-man continuous JOG control for the Conveyor.

A long movement segment runs while a UI heartbeat is alive. Releasing
left/right sends G1 immediately. If a segment ends during a very long hold,
the worker starts another segment without changing direction.
"""

import threading
import time

from hardware.conveyor import Conveyor

DEFAULT_HOLD_HEARTBEAT_TIMEOUT = 0.40
DEFAULT_HOLD_JOIN_TIMEOUT = 3.0

# Firmware отмечает начало хода только на следующем проходе loop() после G3.
# Conveyor.move_step использует ту же фиксированную задержку.
NUDGE_MOTION_START_DELAY = 0.4

# Чанк доводится до конца, поэтому join ждёт дольше, чем мгновенный G1.
NUDGE_HOLD_JOIN_TIMEOUT = 20.0

MODE_JOG_HOLD = "jog"
MODE_NUDGE_HOLD = "nudge"


class JogController:

    def __init__(
        self,
        transport,
        calibration: dict,
        heartbeat_timeout: float = DEFAULT_HOLD_HEARTBEAT_TIMEOUT,
    ):
        self.transport = transport
        self.calib = calibration
        self.hold_steps = int(calibration["jog_hold_steps"])
        if not 10_000 <= self.hold_steps <= 10_000_000:
            raise ValueError("jog_hold_steps должен быть 10000..10000000")
        self._normal_steps_restore = int(calibration["normal_steps"])
        if self._normal_steps_restore <= 0:
            raise ValueError("normal_steps должен быть > 0")

        # ── Ограниченная коррекция ленты внутри паузы ───────────
        self.micro_steps = int(calibration.get("micro_steps", 500))
        if not 1 <= self.micro_steps <= 5000:
            raise ValueError("micro_steps должен быть 1..5000")
        self.nudge_limit_steps = int(calibration.get("nudge_limit_steps", 1000))
        if not 1 <= self.nudge_limit_steps <= 5000:
            raise ValueError("nudge_limit_steps должен быть 1..5000")
        # Скорость удержания в паузе. На производственных 20000 шаг/с весь
        # бюджет ±nudge_limit_steps выбирается за сотые доли секунды, и
        # удержание кнопки физически неощутимо.
        self._production_speed = int(calibration.get("conveyor_speed", 20000))
        if self._production_speed <= 0:
            raise ValueError("conveyor_speed должен быть > 0")
        self.pause_hold_speed = int(calibration.get("pause_hold_speed", 2000))
        if not 100 <= self.pause_hold_speed <= self._production_speed:
            raise ValueError(
                "pause_hold_speed должен быть 100..conveyor_speed"
            )
        # Квант удержания. Отпускание кнопки применяется на границе чанка,
        # поэтому он задаёт и точность остановки, и задержку реакции.
        self.nudge_hold_chunk_steps = int(
            calibration.get("nudge_hold_chunk_steps", 100)
        )
        if not 1 <= self.nudge_hold_chunk_steps <= self.nudge_limit_steps:
            raise ValueError(
                "nudge_hold_chunk_steps должен быть 1..nudge_limit_steps"
            )
        # Прошивка ждёт pause_between_movements между ходами; на время
        # удержания пауза обнуляется и обязана восстановиться после.
        self._pause_between_restore = int(
            calibration.get("pause_between_movements", 2000)
        )
        if self._pause_between_restore < 0:
            raise ValueError("pause_between_movements должен быть >= 0")
        if self.micro_steps > self.nudge_limit_steps:
            raise ValueError("micro_steps не может превышать nudge_limit_steps")
        cell_steps = self._normal_steps_restore * 2
        if self.nudge_limit_steps * 2 >= cell_steps:
            raise ValueError(
                "±nudge_limit_steps должен быть строго меньше шага ячейки "
                f"{cell_steps}"
            )
        # Знаковая сумма всех применённых коррекций текущей паузы.
        self._nudge_offset = 0
        if not 0.15 <= float(heartbeat_timeout) <= 2.0:
            raise ValueError("heartbeat_timeout должен быть 0.15..2.0s")
        self.heartbeat_timeout = float(heartbeat_timeout)

        self.last_action = "-"
        self._mode = None
        self._state_lock = threading.RLock()
        self._command_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._busy = False
        self._direction = None
        self._last_heartbeat = 0.0
        self._armed_at = 0.0
        self._worker_error = None

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return self._busy

    @property
    def status(self) -> dict:
        with self._state_lock:
            return {
                "hold_steps": self.hold_steps,
                "last_action": self.last_action,
                "busy": self._busy,
                "mode": self._mode,
                "nudge_hold_busy": self._mode == MODE_NUDGE_HOLD and self._busy,
                "pause_hold_speed": self.pause_hold_speed,
                "nudge_hold_chunk_steps": self.nudge_hold_chunk_steps,
                "direction": self._direction,
                "heartbeat_timeout_ms": int(self.heartbeat_timeout * 1000),
                "error": self._worker_error,
                "micro_steps": self.micro_steps,
                "nudge_limit_steps": self.nudge_limit_steps,
                "nudge_offset": self._nudge_offset,
                "nudge_remaining_forward": (
                    self.nudge_limit_steps - self._nudge_offset
                ),
                "nudge_remaining_backward": (
                    self.nudge_limit_steps + self._nudge_offset
                ),
            }

    @property
    def nudge_offset(self) -> int:
        """Знаковое накопленное смещение ленты за текущую паузу."""
        with self._state_lock:
            return self._nudge_offset

    def reset_nudge_offset(self):
        """Сбросить накопитель коррекции при входе в новую паузу."""
        with self._state_lock:
            self._nudge_offset = 0

    def nudge(self, direction: str, steps: int = None) -> int:
        """Сдвинуть ленту на ограниченное число микрошагов.

        Возвращает фактически применённое знаковое смещение. Движение
        синхронное: метод возвращает управление только после подтверждённой
        остановки ленты, поэтому вызывающий код всегда знает точную позицию.
        """
        if direction not in ("+", "-"):
            raise ValueError("direction должен быть '+' или '-'")

        requested = self.micro_steps if steps is None else int(steps)
        if requested <= 0:
            raise ValueError("steps должен быть > 0")
        # Одно нажатие никогда не превышает разовый предел.
        if requested > self.nudge_limit_steps:
            raise ValueError(
                f"Запрошено {requested} микрошагов при пределе "
                f"{self.nudge_limit_steps}"
            )

        with self._state_lock:
            if self._busy or (self._thread is not None and self._thread.is_alive()):
                raise RuntimeError("Коррекция запрещена во время удержания JOG")
            signed_request = requested if direction == "+" else -requested
            target = self._nudge_offset + signed_request
            # Клампинг по накопленной сумме, а не только по одному нажатию:
            # иначе N нажатий увели бы ленту в соседнюю ячейку.
            clamped = max(
                -self.nudge_limit_steps,
                min(self.nudge_limit_steps, target),
            )
            applied = clamped - self._nudge_offset
            if applied == 0:
                self.last_action = (
                    f"ЛИМИТ КОРРЕКЦИИ ±{self.nudge_limit_steps}"
                )
                return 0
            self._busy = True

        error = None
        try:
            with self._command_lock:
                # Firmware после каждого хода ждёт pause_between_movements и,
                # если autoPauseMode выключен, САМА запускает следующий ход с
                # текущими G7/G6. Во время коррекции это дало бы повторный
                # сдвиг ±steps каждые pause_between_movements миллисекунд,
                # пока оператор держит руки на ленте. Подтверждаем G12 S1
                # явно: дефолт прошивки нигде больше не проверяется.
                self.transport.send("G12 S1")
                self.transport.send(f"G7 S{applied}")
                self.transport.send("G6 S1")
                self.transport.send("G3")
            self._wait_nudge_stop()
        except Exception as exc:
            error = exc
            try:
                self.transport.send("G1")
            except Exception as stop_exc:
                error = RuntimeError(
                    f"{exc}; аварийная команда G1 не отправлена: {stop_exc}"
                )
        finally:
            # Геометрия обязана восстановиться даже после ошибки: следующий
            # производственный шаг использует normal_steps и G6 S2.
            try:
                self.transport.send(f"G7 S{self._normal_steps_restore}")
                self.transport.send("G6 S2")
            except Exception as restore_exc:
                if error is None:
                    error = restore_exc
            with self._state_lock:
                self._busy = False
                if error is None:
                    self._nudge_offset += applied
                    self.last_action = (
                        f"КОРРЕКЦИЯ {applied:+d} "
                        f"(сумма {self._nudge_offset:+d})"
                    )
                else:
                    # Позиция после сбоя неизвестна: не засчитываем смещение
                    # и выставляем ошибку, чтобы цикл ушёл в FAULT.
                    self._worker_error = str(error)
                    self.last_action = f"ERR: {error}"

        if error is not None:
            raise RuntimeError(f"Коррекция ленты не выполнена: {error}") from error
        return applied

    # ── Удержание внутри паузы ──────────────────────────────────

    def start_nudge_hold(self, direction: str) -> bool:
        """Начать удержание в паузе в пределах остатка бюджета коррекции."""
        if direction not in ("+", "-"):
            raise ValueError("direction должен быть '+' или '-'")

        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                if self._mode != MODE_NUDGE_HOLD or self._direction != direction:
                    return False
                self._last_heartbeat = time.monotonic()
                return True
            if self._busy:
                return False
            remaining = (
                self.nudge_limit_steps - self._nudge_offset
                if direction == "+"
                else self.nudge_limit_steps + self._nudge_offset
            )
            if remaining <= 0:
                self.last_action = f"ЛИМИТ КОРРЕКЦИИ ±{self.nudge_limit_steps}"
                return False
            self._stop_event.clear()
            self._busy = True
            self._mode = MODE_NUDGE_HOLD
            self._direction = direction
            self._last_heartbeat = 0.0
            self._armed_at = time.monotonic()
            self._worker_error = None
            self.last_action = (
                "КОРРЕКЦИЯ ВПРАВО" if direction == "+" else "КОРРЕКЦИЯ ВЛЕВО"
            )
            signed_remaining = remaining if direction == "+" else -remaining
            self._thread = threading.Thread(
                target=self._nudge_hold_worker,
                args=(signed_remaining,),
                name="conveyor-nudge-hold",
                daemon=True,
            )
            self._thread.start()
        return True

    def _nudge_hold_worker(self, signed_remaining: int):
        """Удержание короткими завершёнными ходами внутри бюджета.

        Прошивка convey15 обнуляет POS и в `G1` (`brake(); reset()`), и при
        штатном завершении хода в `loop()`. Измерить пройденный путь после
        остановки невозможно, поэтому засчитывается только полностью
        завершённый чанк, длина которого известна заранее.
        """
        error = None
        applied = 0
        sign = 1 if signed_remaining > 0 else -1
        budget = abs(signed_remaining)
        geometry_dirty = False
        try:
            with self._command_lock:
                # Пониженная скорость: иначе бюджет выбирается быстрее, чем
                # оператор успевает отпустить кнопку.
                self.transport.send(f"G5 S{self.pause_hold_speed}")
                # Между ходами прошивка выжидает pause_between_movements;
                # без обнуления удержание рвалось бы на 2 секунды.
                self.transport.send("G9 S0")
                # autoPauseMode=1: после хода прошивка обязана ждать G3, а не
                # повторять ход сама, пока руки оператора у ленты.
                self.transport.send("G12 S1")
                self.transport.send("G6 S1")
                geometry_dirty = True

            while applied < budget:
                if self._stop_event.is_set():
                    break
                with self._state_lock:
                    last_heartbeat = self._last_heartbeat
                    armed_at = self._armed_at
                reference = last_heartbeat if last_heartbeat > 0 else armed_at
                if time.monotonic() - reference > self.heartbeat_timeout:
                    self.last_action = "STOP: heartbeat timeout"
                    break

                chunk = min(self.nudge_hold_chunk_steps, budget - applied)
                with self._command_lock:
                    if self._stop_event.is_set():
                        break
                    self.transport.send(f"G7 S{sign * chunk}")
                    self.transport.send("G3")
                # Чанк никогда не прерывается: только завершённый ход даёт
                # точно известное смещение.
                self._wait_chunk_done(chunk)
                applied += chunk
                with self._state_lock:
                    self._nudge_offset += sign * chunk
        except Exception as exc:
            error = exc
            try:
                with self._command_lock:
                    self.transport.send("G1")
                self._confirm_stopped_after_g1()
            except Exception as stop_exc:
                error = RuntimeError(
                    f"{exc}; аварийная команда G1 не отправлена: {stop_exc}"
                )
        finally:
            if geometry_dirty:
                try:
                    with self._command_lock:
                        self.transport.send(f"G5 S{self._production_speed}")
                        self.transport.send(f"G9 S{self._pause_between_restore}")
                        self.transport.send(f"G7 S{self._normal_steps_restore}")
                        self.transport.send("G6 S2")
                except Exception as restore_exc:
                    if error is None:
                        error = restore_exc

            with self._state_lock:
                self._busy = False
                self._mode = None
                self._direction = None
                self._last_heartbeat = 0.0
                self._armed_at = 0.0
                self._thread = None
                if error is None:
                    self.last_action = (
                        f"КОРРЕКЦИЯ {sign * applied:+d} "
                        f"(сумма {self._nudge_offset:+d})"
                    )
                else:
                    # Прерванный чанк оставляет позицию неизвестной.
                    self._worker_error = str(error)
                    self.last_action = f"ERR: {error}"

    def _wait_chunk_done(self, chunk_steps: int, timeout: float = 15.0):
        """Дождаться подтверждённого конца чанка, не прерывая ход.

        Фиксированная задержка `NUDGE_MOTION_START_DELAY` здесь не годится:
        при чанке в сотню шагов она заняла бы больше времени, чем сам ход,
        и удержание стало бы рваным. Вместо неё остановка засчитывается
        только после подтверждённого начала движения (`MOV=1`) либо после
        времени, за которое чанк заведомо не мог не завершиться.
        """
        expected = chunk_steps / max(1.0, float(self.pause_hold_speed))
        # Запас на разгон, торможение и задержку последовательного порта.
        settle_after = expected * 3.0 + 0.25
        started = time.monotonic()
        deadline = started + timeout
        motion_seen = False
        last_i1 = ""
        last_i2 = ""
        while time.monotonic() < deadline:
            last_i1 = self.transport.query("I1", delay=0.02)
            # _parse_motion_reply: True = остановлен, False = движется.
            stopped = Conveyor._parse_motion_reply(last_i1)
            if stopped is False:
                motion_seen = True
            elif stopped is True:
                # «Остановлен» засчитывается только если движение уже было
                # видно или прошло время, за которое чанк обязан завершиться.
                if motion_seen or time.monotonic() - started >= settle_after:
                    last_i2 = self.transport.query("I2", delay=0.02)
                    if Conveyor._strict_stop_confirmed(last_i2):
                        return
            time.sleep(0.005)
        raise TimeoutError(
            f"Чанк коррекции не завершился за {timeout}s; "
            f"I1={last_i1!r}; I2={last_i2!r}"
        )

    def _wait_nudge_stop(self, timeout: float = 15.0):
        # Прошивка выставляет MOV=1 только на следующем проходе loop() после
        # G3. Немедленный опрос вернул бы MOV=0/WAIT=0 от ещё не начатого
        # хода и был бы принят за подтверждённую остановку. Conveyor.move_step
        # решает это тем же способом — фиксированной задержкой после G3.
        time.sleep(NUDGE_MOTION_START_DELAY)
        deadline = time.monotonic() + timeout
        last_i1 = ""
        last_i2 = ""
        while time.monotonic() < deadline:
            last_i1 = self.transport.query("I1", delay=0.05)
            if Conveyor._parse_motion_reply(last_i1) is True:
                last_i2 = self.transport.query("I2", delay=0.05)
                if Conveyor._strict_stop_confirmed(last_i2):
                    return
            time.sleep(0.03)
        raise TimeoutError(
            f"Коррекция не завершилась за {timeout}s; "
            f"I1={last_i1!r}; I2={last_i2!r}"
        )

    def start_hold(self, direction: str) -> bool:
        if direction not in ("+", "-"):
            raise ValueError("direction должен быть '+' или '-'")
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                if self._mode != MODE_JOG_HOLD or self._direction != direction:
                    return False
                self._last_heartbeat = time.monotonic()
                return True
            if self._busy:
                return False
            self._stop_event.clear()
            self._busy = True
            self._mode = MODE_JOG_HOLD
            self._direction = direction
            self._last_heartbeat = 0.0
            self._armed_at = time.monotonic()
            self._worker_error = None
            self.last_action = "HOLD RIGHT" if direction == "+" else "HOLD LEFT"
            self._thread = threading.Thread(
                target=self._hold_worker,
                name="conveyor-jog-hold",
                daemon=True,
            )
            self._thread.start()
        return True

    def heartbeat(self, direction: str, mode: str = MODE_JOG_HOLD) -> bool:
        with self._state_lock:
            if (
                not self._busy
                or self._thread is None
                or not self._thread.is_alive()
                or self._direction != direction
                or self._mode != mode
            ):
                return False
            self._last_heartbeat = time.monotonic()
            return True

    def release(self, reason: str = "button released") -> bool:
        """Immediately request Conveyor stop and wait briefly for worker cleanup."""
        self._stop_event.set()
        stop_error = None
        try:
            with self._command_lock:
                self.transport.send("G1")
        except Exception as exc:
            stop_error = exc
        with self._state_lock:
            thread = self._thread
            self.last_action = f"STOP: {reason}"
        if thread is not None and thread is not threading.current_thread():
            thread.join(DEFAULT_HOLD_JOIN_TIMEOUT)
            if thread.is_alive():
                raise RuntimeError("JOG worker did not stop after G1")
        if stop_error is not None:
            raise RuntimeError(f"JOG G1 stop failed: {stop_error}") from stop_error
        return True

    def release_nudge_hold(self, reason: str = "button released") -> int:
        """Остановить удержание на границе чанка и вернуть смещение.

        `G1` здесь намеренно не отправляется: он оборвал бы текущий чанк на
        середине, а прошивка обнуляет POS, и фактическая позиция стала бы
        неизвестной. Вместо этого воркер доводит начатый чанк до конца и не
        запускает следующий, поэтому смещение остаётся точным.
        """
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
            self.last_action = f"STOP: {reason}"
        if thread is not None and thread is not threading.current_thread():
            thread.join(NUDGE_HOLD_JOIN_TIMEOUT)
            if thread.is_alive():
                raise RuntimeError(
                    "Коррекция не остановилась на границе чанка"
                )
        with self._state_lock:
            error = self._worker_error
            offset = self._nudge_offset
        if error is not None:
            raise RuntimeError(f"Коррекция ленты не выполнена: {error}")
        return offset

    def _hold_worker(self):
        error = None
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                with self._state_lock:
                    direction = self._direction
                    last_heartbeat = self._last_heartbeat
                    armed_at = self._armed_at
                if last_heartbeat <= 0:
                    if now - armed_at > self.heartbeat_timeout:
                        self.last_action = "STOP: heartbeat timeout"
                        self._stop_event.set()
                        self.transport.send("G1")
                        self._confirm_stopped_after_g1()
                        break
                    self._stop_event.wait(0.01)
                    continue
                if now - last_heartbeat > self.heartbeat_timeout:
                    self.last_action = "STOP: heartbeat timeout"
                    self._stop_event.set()
                    self.transport.send("G1")
                    self._confirm_stopped_after_g1()
                    break

                signed_steps = self.hold_steps if direction == "+" else -self.hold_steps
                with self._command_lock:
                    if self._stop_event.is_set():
                        break
                    self.transport.send(f"G7 S{signed_steps}")
                    self.transport.send("G6 S1")
                    self.transport.send("G3")
                self._wait_segment_or_release()
        except Exception as exc:
            error = exc
            try:
                self.transport.send("G1")
            except Exception as stop_exc:
                error = RuntimeError(
                    f"{exc}; аварийная команда G1 не отправлена: {stop_exc}"
                )
        finally:
            try:
                self.transport.send(f"G7 S{self._normal_steps_restore}")
                self.transport.send("G6 S2")
            except Exception as restore_exc:
                if error is None:
                    error = restore_exc
            with self._state_lock:
                self._worker_error = None if error is None else str(error)
                self._busy = False
                self._mode = None
                self._direction = None
                self._last_heartbeat = 0.0
                self._armed_at = 0.0
                self._thread = None
                if error is not None:
                    self.last_action = f"ERR: {error}"

    def _confirm_stopped_after_g1(self, timeout: float = 2.5):
        deadline = time.monotonic() + timeout
        last_i1 = ""
        last_i2 = ""
        while time.monotonic() < deadline:
            last_i1 = self.transport.query("I1", delay=0.05)
            if Conveyor._parse_motion_reply(last_i1) is True:
                last_i2 = self.transport.query("I2", delay=0.05)
                if Conveyor._strict_stop_confirmed(last_i2):
                    return
            time.sleep(0.03)
        raise TimeoutError(
            f"JOG G1 stop was not confirmed; I1={last_i1!r}; I2={last_i2!r}"
        )

    def _wait_segment_or_release(self, timeout: float = 120.0):
        deadline = time.monotonic() + timeout
        last_i1 = ""
        last_i2 = ""
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                self._confirm_stopped_after_g1()
                return
            with self._state_lock:
                heartbeat_age = time.monotonic() - self._last_heartbeat
            if heartbeat_age > self.heartbeat_timeout:
                self.last_action = "STOP: heartbeat timeout"
                self._stop_event.set()
                self.transport.send("G1")
                self._confirm_stopped_after_g1()
                return
            last_i1 = self.transport.query("I1", delay=0.05)
            if Conveyor._parse_motion_reply(last_i1) is True:
                last_i2 = self.transport.query("I2", delay=0.05)
                if Conveyor._strict_stop_confirmed(last_i2):
                    return
            self._stop_event.wait(0.03)
        raise TimeoutError(
            f"JOG segment did not stop in {timeout}s; I1={last_i1!r}; I2={last_i2!r}"
        )
