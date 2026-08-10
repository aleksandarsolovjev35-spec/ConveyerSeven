"""Pure physical part tracking by immutable birth step."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


class TrackingInvariantError(RuntimeError):
    pass


@dataclass(frozen=True)
class InputPresenceDecision:
    parts: dict[Any, Any]
    next_part_id: int
    created_part: Any | None
    empty_increment: int
    mismatch: bool


class PartTracker:
    """Part identity and position rules without hardware or persistence I/O."""

    CONTROL_OFFSET = 4
    TRANSFER_OFFSET = 7

    @staticmethod
    def position(part: Any, confirmed_current_step: int) -> int:
        birth_step = getattr(part, "birth_step", getattr(part, "step_created", None))
        if type(birth_step) is not int or birth_step < 0:
            raise TrackingInvariantError("part has no valid immutable birth_step")
        if type(confirmed_current_step) is not int or confirmed_current_step < birth_step:
            raise TrackingInvariantError("confirmed step precedes part birth")
        return confirmed_current_step - birth_step

    @classmethod
    def at_position(
        cls,
        parts: Mapping[Any, Any],
        confirmed_current_step: int,
        offset: int,
    ) -> list[Any]:
        matches = [
            part for part in parts.values()
            if cls.position(part, confirmed_current_step) == offset
        ]
        if len(matches) > 1:
            raise TrackingInvariantError(
                f"{len(matches)} parts occupy logical position +{offset}"
            )
        return matches

    @classmethod
    def expected_control(
        cls, parts: Mapping[Any, Any], confirmed_current_step: int,
    ) -> Any | None:
        matches = cls.at_position(parts, confirmed_current_step, cls.CONTROL_OFFSET)
        return matches[0] if matches else None

    @classmethod
    def pending_transfer(
        cls, parts: Mapping[Any, Any], confirmed_current_step: int,
    ) -> Any | None:
        matches = cls.at_position(parts, confirmed_current_step, cls.TRANSFER_OFFSET)
        return matches[0] if matches else None

    @staticmethod
    def commit_input_presence(
        parts: Mapping[Any, Any],
        *,
        presence_by_role: Mapping[str, bool],
        next_part_id: int,
        birth_step: int,
        batch_id: str,
        part_factory: Callable[..., Any] | None = None,
    ) -> InputPresenceDecision:
        """Reserve identity only after both INPUT presence results exist.

        Both absent means one physical empty cell and no identity.  One-sided
        presence creates a real Part and records a mismatch on that Part.
        """
        required = {"INPUT_LEFT", "INPUT_RIGHT"}
        if set(presence_by_role) != required:
            raise TrackingInvariantError(
                "presence result must contain exactly INPUT_LEFT and INPUT_RIGHT"
            )
        values = {role: bool(presence_by_role[role]) for role in required}
        if not any(values.values()):
            return InputPresenceDecision(
                parts=dict(parts),
                next_part_id=next_part_id,
                created_part=None,
                empty_increment=1,
                mismatch=False,
            )
        if type(next_part_id) is not int or next_part_id < 1:
            raise TrackingInvariantError("next_part_id must be positive")
        if next_part_id in parts:
            raise TrackingInvariantError(f"part_id {next_part_id} already exists")
        if part_factory is None:
            # Lazy import keeps this pure module usable by reducer tests that
            # intentionally do not install the OpenCV inspection stack.
            from domain.part import Part
            part_factory = Part
        part = part_factory(
            next_part_id,
            birth_step=birth_step,
            batch_id=batch_id,
        )
        mismatch = len(set(values.values())) > 1
        if mismatch:
            add = getattr(part, "add_input_defect", None)
            if callable(add):
                add("input_presence_mismatch")
        updated = dict(parts)
        updated[next_part_id] = part
        return InputPresenceDecision(
            parts=updated,
            next_part_id=next_part_id + 1,
            created_part=part,
            empty_increment=0,
            mismatch=mismatch,
        )

    @staticmethod
    def remove_after_transfer(parts: Mapping[Any, Any], part_id: Any) -> dict[Any, Any]:
        if part_id not in parts:
            raise TrackingInvariantError("transferred part is not tracked")
        updated = dict(parts)
        del updated[part_id]
        return updated
