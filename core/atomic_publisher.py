"""Atomic publication boundary for complete logical line snapshots."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from core.control_model import LineSnapshot, StepPhase


class PublicationError(RuntimeError):
    pass


@dataclass(frozen=True)
class InspectionExecutionResult:
    """A complete inspection aggregate tied to exactly one publication."""

    snapshot: LineSnapshot
    publication_version: int
    aggregate: Any = None

    def __post_init__(self):
        if self.publication_version != self.snapshot.state_version:
            raise ValueError(
                "publication_version must equal result.snapshot.state_version"
            )
        if self.snapshot.step_phase is not StepPhase.NONE:
            raise ValueError(
                "final InspectionExecutionResult requires COMMAND_GATE/NONE "
                "to complete before publication"
            )


class AtomicPublisher:
    """Publish a complete snapshot under one monotonic logical version.

    Heavy image caches have their own frame versions and are intentionally not
    owned by this class.  A duplicate call with the same snapshot object is
    idempotent; any other non-monotonic publication is rejected.
    """

    def __init__(self, sink: Callable[[LineSnapshot], Any] | None = None):
        self._sink = sink
        self._lock = threading.RLock()
        self._snapshot: LineSnapshot | None = None

    @property
    def snapshot(self) -> LineSnapshot | None:
        with self._lock:
            return self._snapshot

    @property
    def state_version(self) -> int:
        with self._lock:
            return self._snapshot.state_version if self._snapshot else 0

    def publish(self, snapshot: LineSnapshot) -> int:
        if not isinstance(snapshot, LineSnapshot):
            raise TypeError("AtomicPublisher accepts only a complete LineSnapshot")
        with self._lock:
            previous = self._snapshot
            if previous is snapshot:
                return snapshot.state_version
            if previous is not None and snapshot.state_version <= previous.state_version:
                raise PublicationError(
                    f"non-monotonic state_version {snapshot.state_version}; "
                    f"current={previous.state_version}"
                )
            # The sink receives the same complete immutable object.  Commit the
            # local pointer only if the sink accepted it, avoiding a partially
            # published logical state on callback failure.
            if self._sink is not None:
                returned = self._sink(snapshot)
                if returned is not None and returned != snapshot.state_version:
                    raise PublicationError(
                        "publisher sink returned a different state_version"
                    )
            self._snapshot = snapshot
            return snapshot.state_version

    def inspection_result(self, snapshot: LineSnapshot, aggregate: Any = None) -> InspectionExecutionResult:
        version = self.publish(snapshot)
        return InspectionExecutionResult(
            snapshot=snapshot,
            publication_version=version,
            aggregate=aggregate,
        )
