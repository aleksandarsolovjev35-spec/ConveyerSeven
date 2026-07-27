"""Пауза внутри производственного цикла — извлечённый модуль."""
import time

class PauseLogic:
    """Логика паузы, коррекции ленты и восстановления потока."""

    def __init__(self, monitor, jog, operation_lock):
        self.monitor = monitor
        self.jog = jog
        self._operation_lock = operation_lock
        self._pause_requested = None  # устанавливается извне
        self._pause_frame_active = False
        self._live_capture_pause = None
        self._jog_stop_event = None
        self._jog_frame_times = None

    # Методы будут обёрнуты при интеграции с ProductionCycle

    # Интеграция с ProductionCycle (через делегирование)
    def integrate(self, cycle):
        """Привязка логики паузы к экземпляру цикла."""
        self.cycle = cycle
