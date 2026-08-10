"""Immutable snapshot analysis and all-or-nothing inspection aggregation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from core.atomic_publisher import AtomicPublisher, InspectionExecutionResult
from core.control_model import LineSnapshot


INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
CONTROL_ROLES = (
    "SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP",
)


class InspectionTransactionError(RuntimeError):
    pass


@dataclass(frozen=True)
class InspectionAggregate:
    transaction_id: str
    run_id: int
    snapshots: Mapping[str, Any]
    input_result: Any | None
    control_result: Any | None


class InspectionTransaction:
    """Run independent groups, but expose only their complete aggregate."""

    def __init__(
        self,
        transaction_id: str,
        run_id: int,
        required_roles,
        snapshots: Mapping[str, Any],
    ):
        if not isinstance(transaction_id, str) or not transaction_id:
            raise ValueError("transaction_id is required")
        if type(run_id) is not int or run_id < 1:
            raise ValueError("run_id must be positive")
        required = tuple(dict.fromkeys(required_roles))
        unknown = set(required) - set(INPUT_ROLES + CONTROL_ROLES)
        if unknown:
            raise ValueError(f"unknown inspection roles: {sorted(unknown)}")
        if set(snapshots) != set(required):
            raise InspectionTransactionError(
                f"snapshot roles differ from required roles: "
                f"required={sorted(required)}, actual={sorted(snapshots)}"
            )
        self.transaction_id = transaction_id
        self.run_id = run_id
        self.required_roles = required
        # The mapping cannot be changed.  Camera adapters must already have
        # copied each frame before constructing this transaction.
        self.snapshots = MappingProxyType(dict(snapshots))
        self._aggregate: InspectionAggregate | None = None

    def execute(
        self,
        *,
        input_worker: Callable[[Mapping[str, Any]], Any] | None = None,
        control_worker: Callable[[Mapping[str, Any]], Any] | None = None,
        timeout: float | None = None,
    ) -> InspectionAggregate:
        input_needed = any(role in self.snapshots for role in INPUT_ROLES)
        control_needed = any(role in self.snapshots for role in CONTROL_ROLES)
        if input_needed and not all(role in self.snapshots for role in INPUT_ROLES):
            raise InspectionTransactionError("both INPUT snapshots are mandatory")
        if control_needed and not all(role in self.snapshots for role in CONTROL_ROLES):
            raise InspectionTransactionError("all five CONTROL snapshots are mandatory")
        if input_needed and input_worker is None:
            raise InspectionTransactionError("INPUT worker is required")
        if control_needed and control_worker is None:
            raise InspectionTransactionError("CONTROL worker is required")

        input_result = control_result = None
        futures = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="inspection-pure") as pool:
            if input_needed:
                frames = MappingProxyType({role: self.snapshots[role] for role in INPUT_ROLES})
                futures["input"] = pool.submit(input_worker, frames)
            if control_needed:
                frames = MappingProxyType({role: self.snapshots[role] for role in CONTROL_ROLES})
                futures["control"] = pool.submit(control_worker, frames)
            try:
                if "input" in futures:
                    input_result = futures["input"].result(timeout=timeout)
                if "control" in futures:
                    control_result = futures["control"].result(timeout=timeout)
            except FutureTimeout as exc:
                for future in futures.values():
                    future.cancel()
                raise InspectionTransactionError("model/rule timeout") from exc
            except Exception:
                for future in futures.values():
                    future.cancel()
                raise

        return self.complete(input_result=input_result, control_result=control_result)

    def complete(self, *, input_result=None, control_result=None) -> InspectionAggregate:
        """Seal externally scheduled result-only workers into one aggregate."""
        input_needed = any(role in self.snapshots for role in INPUT_ROLES)
        control_needed = any(role in self.snapshots for role in CONTROL_ROLES)
        if input_needed and input_result is None:
            raise InspectionTransactionError("INPUT aggregate result is missing")
        if control_needed and control_result is None:
            raise InspectionTransactionError("CONTROL aggregate result is missing")
        aggregate = InspectionAggregate(
            transaction_id=self.transaction_id,
            run_id=self.run_id,
            snapshots=self.snapshots,
            input_result=input_result,
            control_result=control_result,
        )
        self._aggregate = aggregate
        return aggregate

    def persist_and_publish(
        self,
        snapshot: LineSnapshot,
        *,
        persist: Callable[[InspectionAggregate], Any],
        publisher: AtomicPublisher,
    ) -> InspectionExecutionResult:
        if self._aggregate is None:
            raise InspectionTransactionError("inspection aggregate is not complete")
        if snapshot.run_id != self.run_id:
            raise InspectionTransactionError("snapshot belongs to another run")
        # The transaction identity remains in the aggregate/evidence after the
        # active step is cleared at COMMAND_GATE. The final logical snapshot is
        # intentionally StepPhase.NONE.
        persist(self._aggregate)
        return publisher.inspection_result(snapshot, self._aggregate)
