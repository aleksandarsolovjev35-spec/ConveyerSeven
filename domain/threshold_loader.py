# domain/threshold_loader.py

import json
import math
import os


INPUT_WINDOW_GEOMETRY_PARAMETER_NAMES = (
    "input_window_geometry_min_confidence",
    "input_window_geometry_expected_count",
    "input_window_geometry_top_px_min",
    "input_window_geometry_top_px_max",
    "input_window_geometry_bottom_px_min",
    "input_window_geometry_bottom_px_max",
    "input_window_geometry_center_zone_ratio",
)
INPUT_WINDOW_SINK_PARAMETER_NAMES = (
    "input_window_sinks_min_confidence",
    "input_window_sinks_window_min_confidence",
    "input_window_sinks_overlap_min_px",
)
INPUT_PART_PRESENCE_PARAMETER_NAMES = (
    "input_part_presence_false_positive_max_count",
)
INPUT_ROLE_PARAMETER_KEYS = tuple(
    f"{role}.{name}"
    for role in ("INPUT_LEFT", "INPUT_RIGHT")
    for name in (
        *INPUT_PART_PRESENCE_PARAMETER_NAMES,
        *INPUT_WINDOW_GEOMETRY_PARAMETER_NAMES,
        *INPUT_WINDOW_SINK_PARAMETER_NAMES,
    )
)

LONG_CONTACT_PARAMETER_NAMES = (
    "spider_contacts_long_min_confidence",
    "spider_contacts_long_expected_count",
    "spider_contacts_long_line_deviation_ratio",
    "spider_contacts_long_omission_tilt_ratio_max",
    "spider_contacts_long_inscribed_rect_width_mm",
    "spider_contacts_long_inscribed_rect_height_mm",
    "spider_contacts_long_y_filter_ratio",
)
SHORT_CONTACT_PARAMETER_NAMES = (
    "spider_contacts_short_min_confidence",
    "spider_contacts_short_expected_count",
    "spider_contacts_short_level_deviation_ratio",
    "spider_contacts_short_omission_tilt_ratio_max",
    "spider_contacts_short_inscribed_rect_width_mm",
    "spider_contacts_short_inscribed_rect_height_mm",
    "spider_contacts_short_area_absolute_min",
    "spider_contacts_short_y_filter_ratio",
)
SPIDER_CONTACT_PARAMETER_KEYS = tuple(
    f"{role}.{name}"
    for role, names in (
        ("SPIDER_LEFT", LONG_CONTACT_PARAMETER_NAMES),
        ("SPIDER_RIGHT", LONG_CONTACT_PARAMETER_NAMES),
        ("SPIDER_IN", SHORT_CONTACT_PARAMETER_NAMES),
        ("SPIDER_OUT", SHORT_CONTACT_PARAMETER_NAMES),
    )
    for name in names
)
OMISSION_BOUNDARY_SUFFIXES = (
    "allowed_thickness_px",
    "excess_component_min_px",
    "top_line_max_residual_px",
)
OMISSION_BOUNDARY_ROLE_KEYS = tuple(
    f"{role}.spider_{family}_omission_{suffix}"
    for role, family in (
        ("SPIDER_LEFT", "long"),
        ("SPIDER_RIGHT", "long"),
        ("SPIDER_IN", "short"),
        ("SPIDER_OUT", "short"),
    )
    for suffix in OMISSION_BOUNDARY_SUFFIXES
)
TOP_PARAMETER_NAMES = (
    "top_contacts_min_confidence",
    "top_contacts_expected_count",
    "top_contacts_platform_min_confidence",
    "top_contacts_edge_distance_deviation_ratio",
    "top_contacts_side_rect_width_px",
    "top_contacts_side_rect_height_px",
    "top_contacts_edge_rect_width_px",
    "top_contacts_edge_rect_height_px",
    "top_platform_min_confidence",
    "top_platform_inscribed_rect_width_px",
    "top_platform_inscribed_rect_height_px",
    "top_platform_overlap_platform_min_confidence",
    "top_platform_overlap_boundary_width_px",
    "top_platform_overlap_boundary_height_px",
    "top_platform_overlap_excess_component_min_px",
    "top_sinks_min_confidence",
    "top_sinks_platform_min_confidence",
    "top_sinks_case_central_min_confidence",
    "top_glass_min_confidence",
    "top_glass_platform_min_confidence",
    "top_glass_case_min_confidence",
    "top_glass_case_central_min_confidence",
    "top_glass_pin_min_confidence",
)
TOP_ROLE_PARAMETER_KEYS = tuple(f"TOP.{name}" for name in TOP_PARAMETER_NAMES)
ROLE_SECTIONS = (
    "INPUT_LEFT",
    "INPUT_RIGHT",
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
)


class ThresholdLoader:

    OMISSION_CONFIDENCE_KEYS = (
        "SPIDER_LEFT.spider_long_omission_min_confidence",
        "SPIDER_RIGHT.spider_long_omission_min_confidence",
        "SPIDER_IN.spider_short_omission_min_confidence",
        "SPIDER_OUT.spider_short_omission_min_confidence",
    )
    INPUT_PARAMETER_KEYS = INPUT_ROLE_PARAMETER_KEYS
    CONTACT_PARAMETER_KEYS = SPIDER_CONTACT_PARAMETER_KEYS
    OMISSION_BOUNDARY_PARAMETER_KEYS = OMISSION_BOUNDARY_ROLE_KEYS
    TOP_PARAMETER_KEYS = TOP_ROLE_PARAMETER_KEYS
    REQUIRED_KEYS = (
        *INPUT_PARAMETER_KEYS,
        *CONTACT_PARAMETER_KEYS,
        *OMISSION_CONFIDENCE_KEYS,
        *OMISSION_BOUNDARY_PARAMETER_KEYS,
        *TOP_PARAMETER_KEYS,
    )
    ALLOWED_KEYS = {*REQUIRED_KEYS, "disabled_rules"}

    def __init__(self, path: str = "thresholds.json"):
        self.path = path
        self.thresholds = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            raise RuntimeError(f"Файл не найден: {self.path}")

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ошибка чтения {self.path}: {exc}") from exc
        if not isinstance(raw_data, dict):
            raise ValueError("thresholds.json должен содержать объект")
        data = self._flatten_sections(raw_data)

        for key in self.REQUIRED_KEYS:
            if key not in data:
                raise ValueError(
                    f"Отсутствует ключ в thresholds.json: {key}"
                )

        unknown = sorted(set(data) - self.ALLOWED_KEYS)
        if unknown:
            raise ValueError(
                "Лишние или неизвестные ключи в thresholds.json: "
                + ", ".join(unknown)
            )

        for key in self.INPUT_PARAMETER_KEYS:
            value = data[key]
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or float(value) < 0.0
            ):
                raise ValueError(f"{key} должен быть конечным числом >= 0")
            if key.endswith("_min_confidence") and float(value) > 1.0:
                raise ValueError(f"{key} должен быть числом 0..1")
            if key.endswith("_expected_count") and (
                type(value) is not int or value <= 0
            ):
                raise ValueError(f"{key} должен быть целым числом > 0")
            if key.endswith("_false_positive_max_count") and (
                type(value) is not int or value < 0
            ):
                raise ValueError(f"{key} должен быть целым числом >= 0")
            if key.endswith("_center_zone_ratio") and not 0.0 < float(value) <= 1.0:
                raise ValueError(f"{key} должен быть числом > 0 и <= 1")

        for role in ("INPUT_LEFT", "INPUT_RIGHT"):
            top_min = data[f"{role}.input_window_geometry_top_px_min"]
            top_max = data[f"{role}.input_window_geometry_top_px_max"]
            bottom_min = data[f"{role}.input_window_geometry_bottom_px_min"]
            bottom_max = data[f"{role}.input_window_geometry_bottom_px_max"]
            if float(top_min) > float(top_max):
                raise ValueError(f"{role}: top_px_min не может превышать top_px_max")
            if float(bottom_min) > float(bottom_max):
                raise ValueError(
                    f"{role}: bottom_px_min не может превышать bottom_px_max"
                )

        for key in self.CONTACT_PARAMETER_KEYS:
            value = data[key]
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or float(value) < 0.0
            ):
                raise ValueError(f"{key} должен быть конечным числом >= 0")
            if key.endswith("_min_confidence") and float(value) > 1.0:
                raise ValueError(f"{key} должен быть числом 0..1")
            if key.endswith("_expected_count") and (
                type(value) is not int or value <= 0
            ):
                raise ValueError(f"{key} должен быть целым числом > 0")

        for key in self.OMISSION_CONFIDENCE_KEYS:
            value = data[key]
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"{key} должен быть числом 0..1")

        for role, family in (
            ("SPIDER_LEFT", "long"),
            ("SPIDER_RIGHT", "long"),
            ("SPIDER_IN", "short"),
            ("SPIDER_OUT", "short"),
        ):
            prefix = f"{role}.spider_{family}_omission_"
            for suffix in (
                "allowed_thickness_px",
                "top_line_max_residual_px",
            ):
                value = data[prefix + suffix]
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(float(value))
                    or float(value) < 0.0
                ):
                    raise ValueError(
                        f"{prefix}{suffix} должен быть числом >= 0"
                    )
            component_min = data[prefix + "excess_component_min_px"]
            if type(component_min) is not int or component_min < 1:
                raise ValueError(
                    f"{prefix}excess_component_min_px должен быть целым >= 1"
                )

        for key in self.TOP_PARAMETER_KEYS:
            value = data[key]
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or float(value) < 0.0
            ):
                raise ValueError(f"{key} должен быть конечным числом >= 0")
            if key.endswith("_min_confidence") and float(value) > 1.0:
                raise ValueError(f"{key} должен быть числом 0..1")
            if key.endswith("_expected_count") and (
                type(value) is not int or value <= 0
            ):
                raise ValueError(f"{key} должен быть целым числом > 0")
            if (
                "inscribed_rect_" in key
                or "overlap_boundary_" in key
            ) and float(value) <= 0.0:
                raise ValueError(f"{key} должен быть числом > 0")
            if key.endswith("_excess_component_min_px") and (
                type(value) is not int or value < 1
            ):
                raise ValueError(f"{key} должен быть целым числом >= 1")

        inner_width = data["TOP.top_platform_inscribed_rect_width_px"]
        inner_height = data["TOP.top_platform_inscribed_rect_height_px"]
        boundary_width = data["TOP.top_platform_overlap_boundary_width_px"]
        boundary_height = data["TOP.top_platform_overlap_boundary_height_px"]
        if float(boundary_width) < float(inner_width):
            raise ValueError(
                "TOP.top_platform_overlap_boundary_width_px не может быть "
                "меньше top_platform_inscribed_rect_width_px"
            )
        if float(boundary_height) < float(inner_height):
            raise ValueError(
                "TOP.top_platform_overlap_boundary_height_px не может быть "
                "меньше top_platform_inscribed_rect_height_px"
            )

        disabled = data.get("disabled_rules", [])
        if not isinstance(disabled, list) or any(
            not isinstance(name, str) for name in disabled
        ):
            raise ValueError("disabled_rules должен быть списком строк")
        if "part_presence" in disabled:
            raise ValueError("part_presence нельзя отключать")
        return data

    @staticmethod
    def _flatten_sections(raw_data: dict) -> dict:
        """Преобразовать читаемые секции камер в ROLE.parameter.

        Ключи `_comment*` являются допустимыми комментариями JSON и полностью
        игнорируются загрузчиком. Старый плоский формат также читается, чтобы
        ошибка миграции была понятной, но неизвестные ключи затем отклоняются.
        """
        flattened = {}
        for key, value in raw_data.items():
            if str(key).startswith("_comment"):
                continue
            if key in ROLE_SECTIONS:
                if not isinstance(value, dict):
                    raise ValueError(f"Секция {key} должна быть объектом")
                for parameter, parameter_value in value.items():
                    if str(parameter).startswith("_comment"):
                        continue
                    if isinstance(parameter_value, (dict, list)):
                        raise ValueError(
                            f"{key}.{parameter} должен быть простым значением"
                        )
                    flattened[f"{key}.{parameter}"] = parameter_value
                continue
            flattened[key] = value
        return flattened

    def get_all(self) -> dict:
        return self.thresholds