# core/production_cycle.py — тонкий оркестратор (после разделения на core/cycle/)
"""Производственный цикл теперь делегирует в 4 независимых файла:
  - core/cycle/step_engine.py
  - core/cycle/pause_logic.py
  - core/cycle/inspection_stage.py
  - core/cycle/monitor_builders.py
"""

from core.cycle.step_engine import ProductionCycle as _ProductionCycleBase
from core.cycle.pause_logic import PauseLogic
from core.cycle.inspection_stage import InspectionStage
from core.cycle.monitor_builders import MonitorBuilders

# Экспортируем основной класс для обратной совместимости
ProductionCycle = _ProductionCycleBase

# Экспортируем вспомогательные классы
__all__ = [
    "ProductionCycle",
    "PauseLogic",
    "InspectionStage",
    "MonitorBuilders",
]
