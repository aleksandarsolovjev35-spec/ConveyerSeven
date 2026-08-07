import time

from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP, CATEGORY_GOOD


class Distributor:
    """Двухступенчатый распределитель корпусов.

    DIST1 (axis 0) — первая заслонка. В ``0`` корпус падает в GOOD; в
    ``dist1_open_position`` корпус передаётся на вторую заслонку DIST2.
    DIST1 никого не удерживает и не является лепестком.

    DIST2 (axis 1) выбирает канал только для корпуса, переданного DIST1:
    ``dist2_bad_position`` — BAD, ``dist2_cleanup_position`` — CLEANUP.

    Маршрут всегда готовится *до* команды движения ленты. При смене DIST2
    DIST1 сначала возвращается в GOOD=0: направляющая никогда не движется,
    когда первая заслонка направляет корпус на неё.
    """

    def __init__(self, dist1_axis, dist2_axis, dist1_open_position: int,
                 dist2_bad_position: int, dist2_cleanup_position: int,
                 drop_time: float = 0.8):
        if type(dist1_open_position) is not int or dist1_open_position <= 0:
            raise ValueError("dist1_open_position должен быть положительным int")
        if type(dist2_bad_position) is not int or dist2_bad_position < 0:
            raise ValueError("dist2_bad_position должен быть неотрицательным int")
        if type(dist2_cleanup_position) is not int or dist2_cleanup_position <= 0:
            raise ValueError("dist2_cleanup_position должен быть положительным int")
        if dist2_bad_position == dist2_cleanup_position:
            raise ValueError("DIST2 BAD и CLEANUP должны различаться")
        self.dist1, self.dist2 = dist1_axis, dist2_axis
        # Имя поля сохранено для совместимости calibration.json.
        self.dist1_open_position = dist1_open_position
        self.dist2_bad_position = dist2_bad_position
        self.dist2_cleanup_position = dist2_cleanup_position
        self.drop_time = drop_time
        self.dist1_state = "GOOD"
        self.dist2_state = "IDLE"
        self.dist2_target = CATEGORY_BAD
        self.last_action = "-"
        self._dist1_position = 0
        self._dist2_position = 0
        self.on_state_changed = None
        self.cancel_check = None

    @property
    def status(self) -> dict:
        return {
            "dist1_position": max(0, self._dist1_position),
            "dist1_max": self.dist1_open_position,
            "dist1_state": self.dist1_state,
            "dist2_position": max(0, self._dist2_position),
            "dist2_max": max(self.dist2_bad_position, self.dist2_cleanup_position, 1),
            "dist2_state": self.dist2_state,
            "dist2_target": self.dist2_target,
            "last_distributor_action": self.last_action,
        }

    def initialize(self):
        """Установить физический ноль обеих осей."""
        self._check_cancelled()
        self.dist1_state, self.dist2_state = "HOMING", "WAITING"
        self._notify()
        self.dist1.home(); self._wait_dist1(timeout=30.0); self._check_cancelled()
        self.dist1.verify_homed(); self._dist1_position = 0
        self.dist1_state, self.dist2_state = "GOOD", "HOMING"
        self._notify()
        self.dist2.home(); self._wait_dist2(timeout=30.0); self._check_cancelled()
        self.dist2.verify_homed(); self._dist2_position = 0
        self.dist2_state, self.dist2_target, self.last_action = "IDLE", CATEGORY_BAD, "HOMED"
        self._notify()

    def park_production(self):
        """Перед START выставить безопасный GOOD=0 и канал BAD=0."""
        self.last_action = "PARK FOR PRODUCTION"
        self._set_dist1_good()
        self.dist2_target = CATEGORY_BAD
        self._move_dist2(CATEGORY_BAD)
        self.last_action = "PRODUCTION READY"
        self._notify()

    def diagnostic_gate(self, position: str):
        """Диагностика DIST1: HOME=GOOD, OPEN=передать корпус на DIST2.

        Имена HOME/OPEN оставлены только для совместимости HTTP-команд.
        """
        if position == "HOME":
            self.last_action = "DIAGNOSTIC DIST1 -> GOOD"
            self._set_dist1_good()
        elif position == "OPEN":
            self.last_action = "DIAGNOSTIC DIST1 -> DIST2"
            self._set_dist1_to_dist2()
        else:
            raise ValueError(f"Unsupported DIST1 diagnostic position: {position}")
        self._notify()

    def diagnostic_route(self, category: str):
        if category not in (CATEGORY_BAD, CATEGORY_CLEANUP):
            raise ValueError(f"Unsupported DIST2 diagnostic route: {category}")
        # Не двигать DIST2, пока DIST1 направляет корпус на неё.
        self._set_dist1_good()
        self.dist2_target, self.last_action = category, f"DIAGNOSTIC DIST2 -> {category}"
        self._move_dist2(category)
        self._notify()

    def prepare_route(self, category: str, part_id: int | None = None):
        """Выставить маршрут следующего корпуса до движения ленты."""
        if category not in (CATEGORY_GOOD, CATEGORY_BAD, CATEGORY_CLEANUP):
            raise ValueError(f"Unsupported distributor category: {category}")
        label = f"PART #{part_id}" if part_id is not None else "PART"
        if category == CATEGORY_GOOD:
            self.last_action = f"{label} -> GOOD"
            self._set_dist1_good()
            self._notify()
            return
        # Смена DIST2 допускается лишь при закрытом пути на вторую заслонку.
        target = self._dist2_target_position(category)
        if self._dist2_position != target:
            self._set_dist1_good()
        self.dist2_target = category
        self._move_dist2(category)
        self._set_dist1_to_dist2()
        self.last_action = f"{label} -> {category} READY"
        self._notify()

    def confirm_transfer(self, part_id: int, category: str):
        """Подтвердить уход корпуса после остановки ленты без смены маршрута."""
        if category not in (CATEGORY_GOOD, CATEGORY_BAD, CATEGORY_CLEANUP):
            raise ValueError(f"Unsupported completed category: {category}")
        self._check_cancelled()
        if self.drop_time:
            time.sleep(self.drop_time)
        self._check_cancelled()
        self.last_action = f"PART #{part_id} -> {category} DONE"
        self._notify()

    def reset_target(self):
        self._notify()

    def emergency_stop(self):
        try:
            self.dist1.transport.send("G25")
        finally:
            self.dist1_state = self.dist2_state = "FAULT"
            self.last_action = "EMERGENCY STOP"
            self._notify()

    def _dist2_target_position(self, category):
        return self.dist2_bad_position if category == CATEGORY_BAD else self.dist2_cleanup_position

    def _move_dist2(self, category: str):
        target = self._dist2_target_position(category)
        self._check_cancelled()
        if self._dist2_position == target:
            print(f"[DIST2] {category} already at POS={target}")
            return
        self.dist2_state = "MOVING"; self._notify()
        self.dist2.move_absolute(target); self._wait_dist2(); self._check_cancelled()
        if self.dist2.position != target:
            raise RuntimeError(f"DIST2 target mismatch: expected {target}, got {self.dist2.position}")
        self._dist2_position = target
        self.dist2_state = "READY"
        print(f"[DIST2] {category} POS={target}")
        self._notify()

    def _set_dist1_to_dist2(self):
        self._check_cancelled()
        if self._dist1_position == self.dist1_open_position:
            print("[DIST1] already TO_DIST2")
            return
        self.dist1_state = "MOVING_TO_DIST2"; self._notify()
        self.dist1.move_absolute(self.dist1_open_position); self._wait_dist1(); self._check_cancelled()
        if self.dist1.position != self.dist1_open_position:
            raise RuntimeError(f"DIST1 DIST2 mismatch: expected {self.dist1_open_position}, got {self.dist1.position}")
        self._dist1_position, self.dist1_state = self.dist1_open_position, "TO_DIST2"
        print(f"[DIST1] TO_DIST2 POS={self._dist1_position}")
        self._notify()

    def _set_dist1_good(self):
        self._check_cancelled()
        if self._dist1_position == 0:
            self.dist1_state = "GOOD"
            return
        self.dist1_state = "MOVING_TO_GOOD"; self._notify()
        self.dist1.move_absolute(0); self._wait_dist1(); self._check_cancelled()
        if self.dist1.position != 0:
            raise RuntimeError(f"DIST1 GOOD mismatch: expected 0, got {self.dist1.position}")
        self._dist1_position, self.dist1_state = 0, "GOOD"
        print("[DIST1] GOOD POS=0")
        self._notify()

    def _check_cancelled(self):
        if self.cancel_check is not None and self.cancel_check():
            raise RuntimeError("Distributor operation cancelled")

    def _wait_dist1(self, timeout=12.0):
        self.dist1.wait_stop(timeout=timeout, progress_callback=self._update_dist1_position)

    def _wait_dist2(self, timeout=12.0):
        self.dist2.wait_stop(timeout=timeout, progress_callback=self._update_dist2_position)

    def _update_dist1_position(self, position, moving):
        self._check_cancelled()
        if position is not None: self._dist1_position = max(0, int(position))
        self._notify()

    def _update_dist2_position(self, position, moving):
        self._check_cancelled()
        if position is not None: self._dist2_position = max(0, int(position))
        self._notify()

    def _notify(self):
        if self.on_state_changed: self.on_state_changed()
