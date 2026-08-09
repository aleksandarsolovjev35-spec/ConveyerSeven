"""Domain model for a physically tracked part.

The production line is the source of truth for position.  A :class:`Part`
therefore stores the step at which it was born, but never stores a mutable
"current position" counter.  Callers pass the last *confirmed* conveyor step
when they need a position.
"""

from __future__ import annotations

from typing import Iterable


CATEGORY_GOOD = "GOOD"
CATEGORY_BAD = "BAD"
CATEGORY_CLEANUP = "CLEANUP"
CATEGORY_UNKNOWN = "UNKNOWN"
CATEGORY_IN_PROGRESS = "IN_PROGRESS"

# The production contract says that only the exact ``glass`` defect, and no
# other defect, is a cleanup route.
CLEANUP_DEFECTS = frozenset({"glass"})


class Part:
    """Immutable identity plus monotonic inspection evidence for one part.

    ``batch_id`` is optional for compatibility with old offline tests, but a
    production-created part always supplies it.  ``step_created`` is retained
    as a read-only compatibility alias for the old API; the canonical name is
    ``birth_step``.
    """

    def __init__(
        self,
        part_id: int,
        step_created: int | None = None,
        *,
        batch_id: str | None = None,
        birth_step: int | None = None,
    ):
        if type(part_id) is not int or part_id < 1:
            raise ValueError("part_id must be a positive int")
        if birth_step is None:
            birth_step = step_created
        if type(birth_step) is not int or birth_step < 0:
            raise ValueError("birth_step must be a non-negative int")
        if step_created is not None and step_created != birth_step:
            raise ValueError("step_created and birth_step disagree")

        self.id = part_id
        self.part_id = part_id
        self.batch_id = batch_id
        self._birth_step = birth_step

        self.input_defects: list[str] = []
        self.spider_defects: list[str] = []
        self.input_inspected = False
        self.spider_inspected = False
        self.final_decision = "none"
        self.route_category = CATEGORY_IN_PROGRESS
        self.inspection_consensus: dict = {}
        self.threshold_revision = None
        self.threshold_snapshot = None
        self.part_manifest = None
        self.created_at = None

    @property
    def birth_step(self) -> int:
        return self._birth_step

    @property
    def step_created(self) -> int:
        """Compatibility alias; assignment is intentionally forbidden."""
        return self._birth_step

    @step_created.setter
    def step_created(self, value):
        raise AttributeError("birth_step is immutable")

    @property
    def current_step(self) -> int:
        """The birth step is not a current step; use ``position_at`` instead.

        This property is provided only as a clear failure mode for code that
        used to treat a part's position as mutable state.
        """
        raise AttributeError("Part.current_step is undefined; use position_at(step)")

    def position_at(self, confirmed_current_step: int) -> int:
        """Return ``confirmed_current_step - birth_step``.

        The value is never allowed to go negative.  A caller must not pass a
        speculative step: production calls this only after telemetry has
        completely confirmed the physical movement.
        """
        if type(confirmed_current_step) is not int:
            raise ValueError("confirmed_current_step must be an int")
        return max(0, confirmed_current_step - self._birth_step)

    # A method alias makes the contract pleasant to use from adapters and
    # keeps hidden/third-party integrations from inventing their own counter.
    current_position = position_at
    position = position_at

    def add_input_defect(self, defect: str):
        if defect and defect not in self.input_defects:
            self.input_defects.append(str(defect))
            self._recompute()

    def add_spider_defect(self, defect: str):
        if defect and defect not in self.spider_defects:
            self.spider_defects.append(str(defect))
            self._recompute()

    def add_defects(self, defects: Iterable[str], stage: str):
        target = self.input_defects if stage == "input" else self.spider_defects
        for defect in defects:
            if defect and defect not in target:
                target.append(str(defect))
        self._recompute()

    def mark_input_done(self):
        """Atomically mark INPUT evidence complete and recompute status."""
        self.input_inspected = True
        self._recompute()

    def mark_spider_done(self):
        """Atomically mark CONTROL evidence complete and recompute status."""
        self.spider_inspected = True
        self._recompute()

    def get_all_defects(self) -> list[str]:
        return list(self.input_defects) + list(self.spider_defects)

    @property
    def fully_inspected(self) -> bool:
        return self.input_inspected and self.spider_inspected

    @property
    def category(self) -> str:
        return self.route_category

    def _recompute(self):
        """Apply the final category table without premature classification."""
        defects = self.get_all_defects()
        if not self.fully_inspected:
            self.final_decision = defects[0] if defects else "none"
            self.route_category = CATEGORY_IN_PROGRESS
            return

        if not defects:
            self.final_decision = "none"
            self.route_category = CATEGORY_GOOD
            return

        if all(defect in CLEANUP_DEFECTS for defect in defects):
            self.final_decision = defects[-1]
            self.route_category = CATEGORY_CLEANUP
            return

        # glass + anything else is BAD, as is every non-glass defect.
        self.final_decision = next(
            (defect for defect in defects if defect not in CLEANUP_DEFECTS),
            defects[-1],
        )
        self.route_category = CATEGORY_BAD

    def mark_incomplete_inspection(self, missing_stage: str, missing_data=None):
        """Fail-safe a part that reaches the output without full evidence."""
        detail = str(missing_stage or "unknown")
        if missing_data:
            detail = f"{detail}:{missing_data}"
        if "incomplete_inspection" not in self.spider_defects:
            self.spider_defects.append("incomplete_inspection")
        self.inspection_consensus.setdefault("incomplete_inspection", {})["missing"] = detail
        # A fail-safe part is BAD even if a caller has not yet marked both
        # stages; it is never eligible for GOOD/CLEANUP.
        # This is a terminal fail-safe path: the part must be routable even
        # when one normal stage is absent.
        self.input_inspected = True
        self.spider_inspected = True
        self._recompute()
        self.route_category = CATEGORY_BAD
        self.final_decision = "incomplete_inspection"

    def __repr__(self):
        return (
            f"<Part #{self.id} batch={self.batch_id!r} "
            f"birth_step={self.birth_step} category={self.route_category} "
            f"inspected={'full' if self.fully_inspected else 'partial'} "
            f"defects={self.get_all_defects()}>"
        )
