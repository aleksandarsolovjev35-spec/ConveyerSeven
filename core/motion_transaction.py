"""Exactly-once conveyor motion command and software-cycle evidence."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping


class MotionTransactionError(RuntimeError):
    pass


class CycleEvidenceMissing(MotionTransactionError, TimeoutError):
    pass


@dataclass(frozen=True)
class MotionEvidence:
    expected_target: int = 38_096
    start_last_ready_ms: int | None = None
    armed_target_seen: bool = False
    armed_position_seen: bool = False
    ready_epoch_changed: bool = False
    final_reset_seen: bool = False
    last_status: Mapping[str, Any] | None = None

    def observe(self, status: Mapping[str, Any], *, stopped_reply: bool | None = None) -> "MotionEvidence":
        """Fold one telemetry sample without issuing any physical command."""
        row = dict(status)
        armed = self.armed_target_seen or row.get("tgt") == self.expected_target
        position_seen = self.armed_position_seen
        if armed and row.get("pos") is not None:
            position = row["pos"]
            if type(position) is not int or not 0 <= position <= self.expected_target:
                raise MotionTransactionError(
                    f"POS={position!r} outside armed range 0..{self.expected_target}"
                )
            position_seen = True
        ready = self.ready_epoch_changed
        current_ready = row.get("lastreadyms")
        if armed and self.start_last_ready_ms is not None and current_ready is not None:
            ready = ready or current_ready != self.start_last_ready_ms
        final = bool(
            armed
            and position_seen
            and ready
            and stopped_reply is True
            and row.get("lasterr") == 0
            and row.get("mov") == 0
            and row.get("wait") == 0
            and row.get("pos") == 0
            and row.get("tgt") == 0
            and row.get("paused") == 1
            and row.get("auto") == 1
        )
        return replace(
            self,
            armed_target_seen=armed,
            armed_position_seen=position_seen,
            ready_epoch_changed=ready,
            final_reset_seen=self.final_reset_seen or final,
            last_status=row,
        )

    @property
    def complete(self) -> bool:
        return bool(
            self.armed_target_seen
            and self.armed_position_seen
            and self.ready_epoch_changed
            and self.final_reset_seen
        )


class MotionTransaction:
    """Journal intent, issue once, then use telemetry polling only."""

    def __init__(
        self,
        transaction_id: str,
        run_id: int,
        *,
        start_last_ready_ms: int,
        expected_target: int = 38_096,
    ):
        if not isinstance(transaction_id, str) or not transaction_id:
            raise ValueError("transaction_id is required")
        if type(run_id) is not int or run_id < 1:
            raise ValueError("run_id must be positive")
        if type(start_last_ready_ms) is not int:
            raise ValueError("start_last_ready_ms is required")
        self.transaction_id = transaction_id
        self.run_id = run_id
        self.evidence = MotionEvidence(
            expected_target=expected_target,
            start_last_ready_ms=start_last_ready_ms,
        )
        self.intent_committed = False
        self.command_issued = False
        self.committed = False

    def commit_intent(self, append: Callable[..., Any], **fields) -> None:
        if self.command_issued:
            raise MotionTransactionError("cannot journal intent after motion command")
        if self.intent_committed:
            return
        append(
            "motion_intent",
            transaction_id=self.transaction_id,
            run_id=self.run_id,
            target=self.evidence.expected_target,
            **fields,
        )
        self.intent_committed = True

    def issue_once(self, command: Callable[[], Any]) -> Any:
        if not self.intent_committed:
            raise MotionTransactionError("durable motion intent is required")
        if self.command_issued:
            raise MotionTransactionError("motion command already issued")
        # Latch before crossing into adapter code.  An exception may mean its
        # acknowledgement was lost, so retrying would be unsafe.
        self.command_issued = True
        return command()

    def observe(self, status: Mapping[str, Any], *, stopped_reply: bool | None = None) -> bool:
        if not self.command_issued:
            raise MotionTransactionError("telemetry cannot confirm an unissued command")
        self.evidence = self.evidence.observe(status, stopped_reply=stopped_reply)
        return self.evidence.complete

    def poll_until_confirmed(
        self,
        poll: Callable[[], tuple[Mapping[str, Any], bool | None] | Mapping[str, Any]],
        *,
        timeout: float,
        interval: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> MotionEvidence:
        if not self.command_issued:
            raise MotionTransactionError("motion command has not been issued")
        if timeout <= 0 or interval < 0:
            raise ValueError("invalid telemetry polling timing")
        deadline = clock() + timeout
        while clock() <= deadline:
            sample = poll()
            if isinstance(sample, tuple):
                status, stopped = sample
            else:
                status, stopped = sample, None
            if self.observe(status, stopped_reply=stopped):
                return self.evidence
            if interval:
                sleep(interval)
        raise CycleEvidenceMissing(
            "CYCLE_EVIDENCE_MISSING: telemetry did not prove the armed target, "
            "ready epoch and exact final reset; motion command was not repeated"
        )

    def commit(self) -> MotionEvidence:
        if self.committed:
            return self.evidence
        if not self.evidence.complete:
            raise CycleEvidenceMissing("CYCLE_EVIDENCE_MISSING")
        self.committed = True
        return self.evidence
