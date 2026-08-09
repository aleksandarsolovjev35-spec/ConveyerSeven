"""The production INPUT presence rule.

Presence is intentionally a small, deterministic rule.  It consumes only
post-processed ``flatness`` detections and does not infer presence from a
missing model, a malformed result, or a different class.
"""

from __future__ import annotations

import math

from domain.defect_rules.base import BaseRule, RuleResult


class InputPartPresenceRule(BaseRule):
    name = "part_presence"
    ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    TARGET_CLASS = "flatness"
    DEFAULT_MIN_COUNT = 3

    def check(
        self,
        vision_results: dict,
        *,
        production: bool = True,
        **kwargs,
    ) -> RuleResult:
        if not self.enabled:
            return self._make_skip(self.name)
        if not isinstance(vision_results, dict):
            raise ValueError("part_presence requires a result object")

        missing = [role for role in self.ROLES if role not in vision_results]
        if missing and production:
            raise RuntimeError(
                "part_presence requires both INPUT roles; missing "
                + ", ".join(missing)
            )
        roles = self.ROLES if production else tuple(
            role for role in self.ROLES if role in vision_results
        )
        if not roles:
            raise RuntimeError("part_presence has no INPUT role")

        counts: dict[str, int] = {}
        thresholds: dict[str, float] = {}
        confidence_thresholds: dict[str, float] = {}
        presence: dict[str, bool] = {}
        for role in roles:
            key = f"{role}.presence_min_count"
            value = self.thresholds.get(key, self.DEFAULT_MIN_COUNT)
            # JSON numbers are int/float, but bool is a JSON subtype of int and
            # is not a meaningful calibration value.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} должен быть числом")
            if not math.isfinite(float(value)):
                raise ValueError(f"{key} должен быть конечным числом")
            threshold = float(value)
            confidence_key = f"{role}.input_window_geometry_min_confidence"
            confidence_min = self.thresholds.get(confidence_key, 0.0)
            if isinstance(confidence_min, bool) or not isinstance(confidence_min, (int, float)) \
                    or not math.isfinite(float(confidence_min)):
                raise ValueError(f"{confidence_key} должен быть конечным числом")
            confidence_min = float(confidence_min)
            confidence_thresholds[role] = confidence_min
            detections = vision_results.get(role)
            if not isinstance(detections, (list, tuple)):
                raise RuntimeError(f"{role}: malformed detection list")
            count = 0
            for detection in detections:
                if not isinstance(detection, dict):
                    raise RuntimeError(f"{role}: malformed detection")
                # VisionCluster has already post-processed detections.  We do
                # not silently repair/drop malformed entries here.
                confidence = detection.get("confidence")
                if isinstance(confidence, bool) or not isinstance(
                    confidence, (int, float)
                ) or not math.isfinite(float(confidence)):
                    raise RuntimeError(f"{role}: malformed detection confidence")
                if (
                    detection.get("class") == self.TARGET_CLASS
                    and float(confidence) >= confidence_min
                ):
                    count += 1
            counts[role] = count
            thresholds[role] = threshold
            presence[role] = count >= threshold

        # Production presence always requires both roles.  A one-sided
        # positive result is a mismatch, not an empty tray.
        left_present = bool(presence.get("INPUT_LEFT", False))
        right_present = bool(presence.get("INPUT_RIGHT", False))
        # A single positive INPUT role is still a physical part.  It is a
        # mismatch defect, never an empty cell.  Only two negatives are empty.
        production_present = any(presence.values())
        mismatch = production and left_present != right_present

        details = {
            "presence_min_count_by_role": thresholds,
            "confidence_min_by_role": confidence_thresholds,
            "flatness_left": counts.get("INPUT_LEFT", 0),
            "flatness_right": counts.get("INPUT_RIGHT", 0),
            "count_by_role": dict(counts),
            "presence_by_role": dict(presence),
            "presence_mismatch": mismatch,
            "part_present": production_present,
            "empty_tray": not production_present,
            # Stable compatibility names; they no longer represent a legacy
            # false-positive filter.
            "effective_flatness_left": counts.get("INPUT_LEFT", 0),
            "effective_flatness_right": counts.get("INPUT_RIGHT", 0),
        }
        return RuleResult(
            self.name,
            triggered=False,
            details=details,
            drawings=[],
        )
