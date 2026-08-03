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
    "spider_contacts_long_max_level_slope",
    "spider_contacts_long_omission_tilt_ratio_max",
    "spider_contacts_long_inscribed_rect_width_px",
    "spider_contacts_long_inscribed_rect_height_px",
    "spider_contacts_long_y_filter_ratio",
)
SHORT_CONTACT_PARAMETER_NAMES = (
    "spider_contacts_short_min_confidence",
    "spider_contacts_short_expected_count",
    "spider_contacts_short_level_deviation_ratio",
    "spider_contacts_short_omission_tilt_ratio_max",
    "spider_contacts_short_inscribed_rect_width_px",
    "spider_contacts_short_inscribed_rect_height_px",
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
    "top_platform_overlap_excess_component_min_px",
    "top_platform_overlap_contact_min_confidence",
    "top_platform_overlap_contact_inner_ratio",
    "top_platform_overlap_margin_px",
    "top_platform_overlap_expand_x_ratio",
    "top_platform_overlap_expand_y_ratio",
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
    # Сохранено для обратной совместимости: жёсткого «списка разрешённых
    # ключей» больше нет, дополнительные пороги в файле разрешены.
    ALLOWED_KEYS = {*REQUIRED_KEYS, "disabled_rules"}

    def __init__(self, path: str = "thresholds.json"):
        self.path = path
        self.thresholds = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            raise RuntimeError(f"Файл не найден: {self.path}")

        try:
            with open(self.path, encoding="utf-8") as f:
                raw_data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ошибка чтения {self.path}: {exc}") from exc
        if not isinstance(raw_data, dict):
            raise ValueError("thresholds.json должен содержать объект")
        data = self._flatten_sections(raw_data)
        self.validate(data)
        return data

    @classmethod
    def validate(cls, data: dict) -> None:
        """Проверить плоский словарь порогов (ROLE.parameter -> value).

        Используется и при загрузке файла, и перед сохранением изменений,
        сделанных оператором через интерфейс, чтобы в файл не попал ни один
        некорректный порог.

        Обязательные ключи (REQUIRED_KEYS) должны присутствовать — без них
        правила не могут работать. Дополнительные ключи разрешены: новые
        пороги можно добавлять в thresholds.json вручную, они подхватываются
        при запуске, показываются в панели «Пороги правил» (группа «Прочие
        пороги») и свободно редактируются. Ограничение только одно — значение
        должно быть конечным числом, чтобы редактор мог его отображать.
        """
        for key in cls.REQUIRED_KEYS:
            if key not in data:
                raise ValueError(
                    f"Отсутствует ключ в thresholds.json: {key}"
                )

        extra_keys = sorted(
            set(data) - set(cls.REQUIRED_KEYS) - {"disabled_rules"}
        )
        for key in extra_keys:
            value = data[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{key} должен быть конечным числом")

        for key in cls.INPUT_PARAMETER_KEYS:
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

        for key in cls.CONTACT_PARAMETER_KEYS:
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
            if "inscribed_rect_" in key and float(value) <= 0.0:
                raise ValueError(f"{key} должен быть числом > 0")

        for key in cls.OMISSION_CONFIDENCE_KEYS:
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

        for key in cls.TOP_PARAMETER_KEYS:
            value = data[key]
            # margin может быть отрицательным (сжатие области)
            allow_negative = key.endswith("_margin_px")
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or (float(value) < 0.0 and not allow_negative)
            ):
                if allow_negative:
                    raise ValueError(f"{key} должен быть конечным числом")
                raise ValueError(f"{key} должен быть конечным числом >= 0")
            if key.endswith("_min_confidence") and float(value) > 1.0:
                raise ValueError(f"{key} должен быть числом 0..1")
            if key.endswith("_expected_count") and (
                type(value) is not int or value <= 0
            ):
                raise ValueError(f"{key} должен быть целым числом > 0")
            if "inscribed_rect_" in key and float(value) <= 0.0:
                raise ValueError(f"{key} должен быть числом > 0")
            if key.endswith("_excess_component_min_px") and (
                type(value) is not int or value < 1
            ):
                raise ValueError(f"{key} должен быть целым числом >= 1")

        # Пороги построения области заплыва через контакты
        contact_inner = data["TOP.top_platform_overlap_contact_inner_ratio"]
        if not 0.0 <= float(contact_inner) <= 1.0:
            raise ValueError(
                "TOP.top_platform_overlap_contact_inner_ratio должен быть 0..1"
            )
        expand_x = data["TOP.top_platform_overlap_expand_x_ratio"]
        expand_y = data["TOP.top_platform_overlap_expand_y_ratio"]
        if float(expand_x) <= 0.0 or float(expand_y) <= 0.0:
            raise ValueError(
                "TOP.top_platform_overlap_expand_*_ratio должны быть > 0"
            )

        disabled = data.get("disabled_rules", [])
        if not isinstance(disabled, list) or any(
            not isinstance(name, str) for name in disabled
        ):
            raise ValueError("disabled_rules должен быть списком строк")
        if "part_presence" in disabled:
            raise ValueError("part_presence нельзя отключать")
        # конец validate()

    @staticmethod
    def _flatten_sections(raw_data: dict) -> dict:
        """Преобразовать читаемые секции камер в ROLE.parameter.

        Ключи `_comment*` являются допустимыми комментариями JSON и полностью
        игнорируются загрузчиком. Дополнительные параметры в секциях камер
        сохраняются: новые пороги подхватываются при запуске и показываются
        в панели «Пороги правил» (группа «Прочие пороги»).
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

    @staticmethod
    def save_file(path: str, data: dict) -> None:
        """Сохранить плоский dict порогов в файл секциями по ролям.

        Формат повторяет читаемый вручную вид thresholds.json: секция камеры
        с параметрами и пустая строка между секциями. ``disabled_rules``
        записывается в конец. Перед сохранением вызывающий обязан выполнить
        :meth:`validate`, чтобы в файл не попали некорректные значения.
        """
        grouped: dict = {}
        for key, value in data.items():
            if key == "disabled_rules":
                continue
            role, dot, parameter = key.partition(".")
            if dot and role in ROLE_SECTIONS:
                grouped.setdefault(role, {})[parameter] = value
            else:
                grouped[key] = value

        ordered_keys = [
            role for role in ROLE_SECTIONS if role in grouped
        ]
        ordered_keys += [
            key for key in grouped if key not in ROLE_SECTIONS
        ]

        lines = ["{"]
        has_disabled = "disabled_rules" in data
        last_index = len(ordered_keys) - 1
        for index, role in enumerate(ordered_keys):
            if index:
                lines.append("")
            params = grouped[role]
            needs_comma = index < last_index or has_disabled
            if isinstance(params, dict):
                lines.append(f"    {json.dumps(role, ensure_ascii=False)}: {{")
                param_keys = list(params)
                for p_index, parameter in enumerate(param_keys):
                    comma = "," if p_index < len(param_keys) - 1 else ""
                    lines.append(
                        f"        {json.dumps(parameter, ensure_ascii=False)}: "
                        f"{json.dumps(params[parameter], ensure_ascii=False)}{comma}"
                    )
                lines.append("    }" + ("," if needs_comma else ""))
            else:
                lines.append(
                    f"    {json.dumps(role, ensure_ascii=False)}: "
                    f"{json.dumps(params, ensure_ascii=False)}"
                    + ("," if needs_comma else "")
                )

        if "disabled_rules" in data:
            lines.append("")
            lines.append(
                f'    "disabled_rules": '
                f'{json.dumps(data["disabled_rules"], ensure_ascii=False)}'
            )

        lines.append("}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def get_all(self) -> dict:
        return self.thresholds

# ─── Метаданные порогов для интерфейса оператора ────────────────────────
#
# Панель «Пороги правил» показывает параметры правил выбранной (главной)
# камеры, сгруппированные по правилам. Группировка и подписи живут здесь,
# чтобы backend и фронтенд не расходились в трактовке имён параметров.

PARAM_LABELS = {
    "false_positive_max_count": "Допустимо ложных срабатываний",
    "min_confidence": "Мин. уверенность",
    "window_min_confidence": "Мин. уверенность окна",
    "platform_min_confidence": "Мин. уверенность платформы",
    "contact_min_confidence": "Мин. уверенность контакта",
    "case_min_confidence": "Мин. уверенность корпуса",
    "case_central_min_confidence": "Мин. уверенность центра корпуса",
    "pin_min_confidence": "Мин. уверенность пина",
    "expected_count": "Ожидаемое количество",
    "top_px_min": "Верх зоны: мин, px",
    "top_px_max": "Верх зоны: макс, px",
    "bottom_px_min": "Низ зоны: мин, px",
    "bottom_px_max": "Низ зоны: макс, px",
    "center_zone_ratio": "Доля центральной зоны",
    "overlap_min_px": "Мин. заплыв, px",
    "line_deviation_ratio": "Допуск отклонения от линии",
    "max_level_slope": "Макс. наклон уровня",
    "omission_tilt_ratio_max": "Макс. наклон пропуска",
    "inscribed_rect_width_px": "Ширина впис. прямоугольника, px",
    "inscribed_rect_height_px": "Высота впис. прямоугольника, px",
    "y_filter_ratio": "Коэффициент фильтра по Y",
    "level_deviation_ratio": "Допуск отклонения уровня",
    "area_absolute_min": "Мин. площадь, px²",
    "allowed_thickness_px": "Допустимая толщина, px",
    "excess_component_min_px": "Мин. компонент излишка, px",
    "top_line_max_residual_px": "Макс. остаток верхней линии, px",
    "edge_distance_deviation_ratio": "Допуск отклонения края",
    "side_rect_width_px": "Боковая область: ширина, px",
    "side_rect_height_px": "Боковая область: высота, px",
    "edge_rect_width_px": "Краевая область: ширина, px",
    "edge_rect_height_px": "Краевая область: высота, px",
    "contact_inner_ratio": "Доля внутренней зоны контакта",
    "margin_px": "Запас области, px",
    "expand_x_ratio": "Расширение по X",
    "expand_y_ratio": "Расширение по Y",
}

# (rule_id, подпись в UI, префиксы имён параметров). Более специфичные
# префиксы идут раньше общих: TOP.top_platform_overlap_* не должен попадать
# в группу TOP.top_platform_*.
RULE_GROUPS = (
    ("input_part_presence",   "НАЛИЧИЕ ДЕТАЛИ",         ("input_part_presence_",)),
    ("input_window_geometry", "ГЕОМЕТРИЯ ВХОДНОГО ОКНА", ("input_window_geometry_",)),
    ("input_window_sinks",    "ЗАПЛАВЫ ВХОДНОГО ОКНА",  ("input_window_sinks_",)),
    ("spider_contacts_long",  "КОНТАКТЫ · ДЛИННЫЕ",     ("spider_contacts_long_",)),
    ("spider_long_omission",  "ПРОПУСК · ДЛИННЫЕ",      ("spider_long_omission_",)),
    ("spider_contacts_short", "КОНТАКТЫ · КОРОТКИЕ",    ("spider_contacts_short_",)),
    ("spider_short_omission", "ПРОПУСК · КОРОТКИЕ",     ("spider_short_omission_",)),
    ("top_contacts",          "КОНТАКТЫ СВЕРХУ",        ("top_contacts_",)),
    ("top_platform_overlap",  "ЗАПЛЫВ ПЛАТФОРМЫ",       ("top_platform_overlap_",)),
    ("top_platform",          "ПЛАТФОРМА СВЕРХУ",       ("top_platform_",)),
    ("top_sinks",             "ЗАПЛАВЫ СВЕРХУ",         ("top_sinks_",)),
    ("top_glass",             "СТЕКЛО СВЕРХУ",          ("top_glass_",)),
)

_RULE_GROUPS_SORTED = tuple(
    sorted(
        RULE_GROUPS,
        key=lambda group: -max(len(p) for p in group[2]),
    )
)


def _param_meta(key: str, value) -> dict:
    """Метаданные одного параметра для редактора: подпись и границы ввода."""
    # Более специфичные суффиксы (contact_min_confidence, platform_min_…)
    # должны побеждать общий суффикс min_confidence.
    label = next(
        (
            label
            for suffix, label in sorted(
                PARAM_LABELS.items(), key=lambda item: -len(item[0]),
            )
            if key.endswith(suffix)
        ),
        key,
    )
    meta = {"key": key, "label": label, "value": value}
    if key.endswith("_expected_count") or key.endswith("_false_positive_max_count"):
        meta.update({"step": 1, "min": 0, "max": 1000})
    elif key.endswith("_excess_component_min_px"):
        meta.update({"step": 1, "min": 1, "max": 1000})
    elif key.endswith("_area_absolute_min"):
        meta.update({"step": 1, "min": 0, "max": 1000000})
    elif key.endswith("_min_confidence"):
        meta.update({"step": 0.01, "min": 0, "max": 1})
    elif key.endswith("_y_filter_ratio"):
        meta.update({"step": 0.1, "min": 0, "max": 100})
    elif key.endswith("_center_zone_ratio") or key.endswith("_inner_ratio"):
        meta.update({"step": 0.01, "min": 0, "max": 1})
    elif key.endswith("_expand_x_ratio") or key.endswith("_expand_y_ratio"):
        meta.update({"step": 0.05, "min": 0, "max": 10})
    elif key.endswith("_margin_px"):
        meta.update({"step": 1, "min": -500, "max": 500})
    elif (
        key.endswith("_ratio")
        or key.endswith("_tilt_ratio_max")
        or key.endswith("_slope")
    ):
        meta.update({"step": 0.01, "min": 0, "max": 1})
    else:
        meta.update({"step": 0.1, "min": 0, "max": 5000})
    return meta


def describe_role_parameters(role: str, thresholds: dict) -> list:
    """Пороги роли, сгруппированные по правилам, в формате для UI.

    Возвращает список групп::

        [{"rule": "top_contacts", "label": "КОНТАКТЫ СВЕРХУ",
          "params": [{"key": ..., "label": ..., "value": ...,
                      "step": ..., "min": ..., "max": ...}]}, ...]
    """
    prefix = f"{role}."
    params = [
        (key[len(prefix):], value)
        for key, value in thresholds.items()
        if key.startswith(prefix) and key != "disabled_rules"
    ]

    groups = []
    matched = set()
    for rule_id, label, prefixes in _RULE_GROUPS_SORTED:
        group_params = [
            (name, value)
            for name, value in params
            if (
                name not in matched
                and any(name.startswith(prefix) for prefix in prefixes)
            )
        ]
        if not group_params:
            continue
        group_params.sort()
        groups.append({
            "rule": rule_id,
            "label": label,
            "params": [
                _param_meta(name, value)
                for name, value in group_params
            ],
        })
        matched.update(name for name, _ in group_params)

    leftovers = [(name, value) for name, value in params if name not in matched]
    if leftovers:
        leftovers.sort()
        groups.append({
            "rule": "other",
            "label": "ПРОЧИЕ ПОРОГИ",
            "params": [_param_meta(name, value) for name, value in leftovers],
        })
    return groups
