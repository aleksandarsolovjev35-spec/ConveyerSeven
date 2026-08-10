"""Bounded execution helper for defect-rule calls."""

from __future__ import annotations

from inspection.model_worker import run_in_terminating_worker


class RuleWorkerTimeout(TimeoutError):
    pass


def run_rule_with_timeout(rule, vision_results, *, frames=None, timeout: float = 5.0):
    """Execute one rule in a terminating process when a caller needs a bound.

    The rule and evidence are plain Python/numpy structures in production and
    are deliberately returned as-is; any worker crash/timeout is propagated as
    a technical fault.
    """
    kwargs = {} if frames is None else {"frames": frames}
    return run_in_terminating_worker(
        rule.check,
        vision_results,
        timeout=timeout,
        **kwargs,
    )
