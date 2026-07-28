import time
from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP


class Distributor:
    """
    Распределитель деталей на линии.

    DIST1 (axis 0) — заслонка сброса: IDLE->OPEN->CLOSED
    DIST2 (axis 1) — направляющая: BAD / CLEANUP позиции
    """

    def __init__(
        self,
        dist1_axis,
        dist2_axis,
        dist1_open_position: int,
        dist2_bad_position: int,
        dist2_cleanup_position: int,
        drop_time: float = 0.8,
    ):
        if type(dist1_open_position) is not int or dist1_open_position <= 0:
            raise ValueError("dist1_open_position должен быть положительным int")
        if type(dist2_bad_position) is not int or dist2_bad_position < 0:
            raise ValueError("dist2_bad_position должен быть неотрицательным int")
        if type(dist2_cleanup_position) is not int or dist2_cleanup_position <= 0:
            raise ValueError("dist2_cleanup_position должен быть положительным int")
        if dist2_bad_position == dist2_cleanup_position:
            raise ValueError("DIST2 BAD и CLEANUP должны различаться")

        self.dist1 = dist1_axis
        self.dist2 = dist2_axis

        self.dist1_open_position = dist1_open_position
        self.dist2_bad_position = dist2_bad_position
        self.dist2_cleanup_position = dist2_cleanup_position
        self.drop_time = drop_time

        # Состояния для UI
        self.dist1_state = "IDLE"
        self.dist2_state = "IDLE"
        self.dist2_target = CATEGORY_BAD
        self.last_action  = "-"
        self._dist1_position = 0
        self._dist2_position = 0

        # Callbacks для UI и отмены force-exit.
        self.on_state_changed = None
        self.cancel_check = None

    # Properties for UI

    @property
    def status(self) -> dict:
        """Снимок состояния для UI."""
        return {
            "dist1_position": max(0, self._dist1_position),
            "dist1_max":      self.dist1_open_position,
            "dist1_state":    self.dist1_state,
            "dist2_position": max(0, self._dist2_position),
            "dist2_max":      max(
                self.dist2_bad_position,
                self.dist2_cleanup_position,
                1,
            ),
            "dist2_state":    self.dist2_state,
            "dist2_target":   self.dist2_target,
            "last_distributor_action": self.last_action,
        }

    # Main operations

    def initialize(self):
        """Последовательно установить физический ноль обеих осей."""
        self._check_cancelled()
        self.dist1_state = "HOMING"
        self.dist2_state = "WAITING"
        self._notify()
        self.dist1.home()
        self._wait_dist1(timeout=30.0)
        self._check_cancelled()
        self.dist1.verify_homed()
        self._dist1_position = 0

        self.dist1_state = "IDLE"
        self.dist2_state = "HOMING"
        self._notify()
        self.dist2.home()
        self._wait_dist2(timeout=30.0)
        self._check_cancelled()
        self.dist2.verify_homed()
        self._dist2_position = 0

        self.dist2_state = "IDLE"
        self.dist2_target = CATEGORY_BAD
        self.last_action = "HOMED"
        self._notify()

    def park_production(self):
        """Перед START вернуть заслонку HOME и селектор в BAD/home."""
        self.last_action = "PARK FOR PRODUCTION"
        self._close_dist1()
        self.dist2_target = CATEGORY_BAD
        self._move_dist2(CATEGORY_BAD)
        self.last_action = "PRODUCTION READY"
        self._notify()

    def diagnostic_gate(self, position: str):
        """Диагностически установить лопасть DIST1 в HOME или OPEN."""
        if position == "HOME":
            self.last_action = "DIAGNOSTIC DIST1 -> HOME"
            self._close_dist1()
        elif position == "OPEN":
            self.last_action = "DIAGNOSTIC DIST1 -> OPEN"
            self._open_dist1()
        else:
            raise ValueError(f"Unsupported DIST1 diagnostic position: {position}")
        self._notify()

    def diagnostic_route(self, category: str):
        """Диагностически установить DIST2 в BAD или CLEANUP."""
        if category not in (CATEGORY_BAD, CATEGORY_CLEANUP):
            raise ValueError(f"Unsupported DIST2 diagnostic route: {category}")
        self.dist2_target = category
        self.last_action = f"DIAGNOSTIC DIST2 -> {category}"
        self._move_dist2(category)
        self._notify()

    def prepare(self, category: str, part_id: int | None = None):
        """Подготовить распределитель к сбросу."""
        if category not in (CATEGORY_BAD, CATEGORY_CLEANUP):
            raise ValueError(f"Unsupported distributor category: {category}")
        self.dist2_target = category
        label = f"PART #{part_id}" if part_id else "PART"
        self.last_action = f"{label} -> {category}"

        self._move_dist2(category)
        self._open_dist1()

    def mark_pass(self, part_id: int):
        """Деталь проходит без сброса; фактический канал DIST2 не меняется."""
        self.last_action = f"PART #{part_id} -> PASS"
        self._notify()

    def drop_and_close(self, part_id: int, category: str):
        """Ждать падения детали и закрыть заслонку."""
        if category not in (CATEGORY_BAD, CATEGORY_CLEANUP):
            raise ValueError(f"Unsupported drop category: {category}")
        self._check_cancelled()
        self.last_action = f"PART #{part_id} DROP..."
        self._notify()

        time.sleep(self.drop_time)
        self._check_cancelled()

        self._close_dist1()
        self.last_action = f"PART #{part_id} -> {category} DONE"
        self._notify()

    def reset_target(self):
        """Сохранить в UI фактический текущий канал DIST2."""
        self._notify()

    def emergency_stop(self):
        """Остановить обе NEMA-оси через firmware command G25."""
        try:
            self.dist1.transport.send("G25")
        finally:
            self.dist1_state = "FAULT"
            self.dist2_state = "FAULT"
            self.last_action = "EMERGENCY STOP"
            self._notify()

    # Internal

    def _move_dist2(self, category: str):
        if category == CATEGORY_BAD:
            target = self.dist2_bad_position
        else:
            target = self.dist2_cleanup_position

        self._check_cancelled()
        self.dist2_state = "MOVING"
        self._notify()

        self.dist2.move_absolute(target)
        self._wait_dist2()
        self._check_cancelled()

        pos = self.dist2.position
        if pos != target:
            raise RuntimeError(
                f"DIST2 target mismatch: expected {target}, got {pos}"
            )
        self._dist2_position = pos
        print(f"[DIST2] {category} POS={pos}")

        self.dist2_state = "READY"
        self._notify()

    def _open_dist1(self):
        """Перевести DIST1 прямо в OPEN без повторного homing."""
        self._check_cancelled()
        self.dist1_state = "OPENING"
        self._notify()

        # Обе оси физически homed один раз в initialize(). После этого DIST1,
        # как и DIST2, работает по абсолютным координатам: одно нажатие — одна
        # команда на требуемое положение, без промежуточного ухода в HOME.
        self.dist1.move_absolute(self.dist1_open_position)
        self._wait_dist1()
        self._check_cancelled()

        pos = self.dist1.position
        if pos != self.dist1_open_position:
            raise RuntimeError(
                "DIST1 open mismatch: "
                f"expected {self.dist1_open_position}, got {pos}"
            )
        self._dist1_position = pos
        print(f"[DIST1] OPEN POS={pos}")

        self.dist1_state = "OPEN"
        self._notify()

    def _close_dist1(self):
        """Перевести DIST1 прямо в HOME=0 без повторного homing."""
        self._check_cancelled()
        self.dist1_state = "CLOSING"
        self._notify()

        self.dist1.move_absolute(0)
        self._wait_dist1()
        self._check_cancelled()

        pos = self.dist1.position
        if pos != 0:
            raise RuntimeError(f"DIST1 home mismatch: expected 0, got {pos}")
        self._dist1_position = pos
        print(f"[DIST1] HOME POS={pos}")

        self.dist1_state = "IDLE"
        self._notify()

    def _check_cancelled(self):
        if self.cancel_check is not None and self.cancel_check():
            raise RuntimeError("Distributor operation cancelled")

    def _wait_dist1(self, timeout: float = 12.0):
        self.dist1.wait_stop(
            timeout=timeout,
            progress_callback=self._update_dist1_position,
        )

    def _wait_dist2(self, timeout: float = 12.0):
        self.dist2.wait_stop(
            timeout=timeout,
            progress_callback=self._update_dist2_position,
        )

    def _update_dist1_position(self, position, moving):
        self._check_cancelled()
        if position is not None:
            # Firmware may expose a transient negative homing coordinate.
            # Operator coordinates start at the physical HOME=0.
            self._dist1_position = max(0, int(position))
        self._notify()

    def _update_dist2_position(self, position, moving):
        self._check_cancelled()
        if position is not None:
            self._dist2_position = max(0, int(position))
        self._notify()

    def _notify(self):
        if self.on_state_changed:
            self.on_state_changed()