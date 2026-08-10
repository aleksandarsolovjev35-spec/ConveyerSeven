"""Control domain package.

Imports are lazy so pure automaton tests do not load OpenCV/model dependencies.
"""

from core.control_model import (
    DisplayState,
    HealthState,
    LineSnapshot,
    LineState,
    OperatorCommand,
    PauseContinuation,
    PendingIntent,
    PersistenceState,
    StepPhase,
)
from core.state_machine import State, StateMachine

__all__ = [
    "LineState", "StepPhase", "PendingIntent", "PauseContinuation",
    "HealthState", "DisplayState", "PersistenceState", "OperatorCommand",
    "LineSnapshot", "StateMachine", "State", "DecisionEngine",
    "ProductionCycle", "LineReducer", "ControlCore", "CoreStateMachine", "AtomicPublisher",
]


def __getattr__(name):
    if name == "DecisionEngine":
        from core.decision_engine import DecisionEngine
        return DecisionEngine
    if name == "ProductionCycle":
        from core.production_cycle import ProductionCycle
        return ProductionCycle
    if name == "LineReducer":
        from core.line_reducer import LineReducer
        return LineReducer
    if name == "ControlCore":
        from core.control_core import ControlCore
        return ControlCore
    if name == "CoreStateMachine":
        from core.core_state_machine import CoreStateMachine
        return CoreStateMachine
    if name == "AtomicPublisher":
        from core.atomic_publisher import AtomicPublisher
        return AtomicPublisher
    raise AttributeError(name)
