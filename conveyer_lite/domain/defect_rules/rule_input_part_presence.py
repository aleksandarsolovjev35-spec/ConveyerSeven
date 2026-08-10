from domain.defect_rules.base import BaseRule, RuleResult


class InputPartPresenceRule(BaseRule):
    """Детектор наличия детали по INPUT_LEFT и INPUT_RIGHT.

    Для каждой камеры 0..false_positive_max_count найденных flatness считаются
    ложными срабатываниями. Деталь присутствует только при превышении порога
    одновременно на INPUT_LEFT и INPUT_RIGHT.

    Это служебное правило: triggered всегда False, а Inspector использует
    details["empty_tray"] для решения, создавать Part или считать лоток пустым.
    """

    name = "part_presence"
    ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    TARGET_CLASS = "flatness"

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        if not self.enabled:
            return self._make_skip(self.name)

        min_confidence_by_role = {}
        false_positive_max_count_by_role = {}
        raw_counts = {}
        effective_counts = {}
        ignored_counts = {}
        presence_by_role = {}

        # Если в vision_results только одна из двух камер (например, при
        # ручном анализе кадра), проверяем только её.
        roles_to_check = [r for r in self.ROLES if r in vision_results]
        if not roles_to_check:
            roles_to_check = list(self.ROLES)

        for role in roles_to_check:
            confidence_key = f"{role}.input_window_geometry_min_confidence"
            min_confidence = self.thresholds.get(confidence_key)
            if (
                type(min_confidence) not in (int, float)
                or not 0.0 <= float(min_confidence) <= 1.0
            ):
                raise ValueError(f"{confidence_key} должен быть числом 0..1")

            false_positive_max_count = self._get(
                "input_part_presence_false_positive_max_count",
                2,
                role=role,
            )
            if (
                type(false_positive_max_count) is not int
                or false_positive_max_count < 0
            ):
                raise ValueError(
                    f"{role}.input_part_presence_false_positive_max_count "
                    "должен быть целым числом >= 0"
                )

            min_confidence_by_role[role] = float(min_confidence)
            false_positive_max_count_by_role[role] = false_positive_max_count
            detections = [
                detection
                for detection in vision_results.get(role, [])
                if detection.get("class") == self.TARGET_CLASS
                and float(detection.get("confidence", 0.0))
                >= float(min_confidence)
            ]
            raw_count = len(detections)
            present_on_role = raw_count > false_positive_max_count
            raw_counts[role] = raw_count
            presence_by_role[role] = present_on_role
            effective_counts[role] = raw_count if present_on_role else 0
            ignored_counts[role] = 0 if present_on_role else raw_count

        is_empty = not all(presence_by_role.values())

        details = {
            "min_confidence_by_role": min_confidence_by_role,
            "false_positive_max_count_by_role": (
                false_positive_max_count_by_role
            ),
            "flatness_left": raw_counts.get("INPUT_LEFT", 0),
            "flatness_right": raw_counts.get("INPUT_RIGHT", 0),
            "effective_flatness_left": effective_counts.get("INPUT_LEFT", 0),
            "effective_flatness_right": effective_counts.get("INPUT_RIGHT", 0),
            "false_positive_ignored_left": ignored_counts.get("INPUT_LEFT", 0),
            "false_positive_ignored_right": ignored_counts.get("INPUT_RIGHT", 0),
            "presence_by_role": presence_by_role,
            "empty_tray": is_empty,
        }

        return RuleResult(
            self.name,
            triggered=False,
            details=details,
            drawings=[],
        )
