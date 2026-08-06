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
    "spider_contacts_long_damper_open_max_px",
    "spider_contacts_long_gap_dev_max_px",
    "spider_contacts_long_inscribed_rect_width_px",
    "spider_contacts_long_inscribed_rect_height_px",
    "spider_contacts_long_y_filter_ratio",
)
SHORT_CONTACT_PARAMETER_NAMES = (
    "spider_contacts_short_min_confidence",
    "spider_contacts_short_expected_count",
    "spider_contacts_short_damper_open_max_px",
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
    "top_line_min_inlier_ratio",
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

    def __init__(self, path: str = "thresholds.json"):
        self.path = path
        # Понятные названия порогов для оператора: ROLE.parameter -> строка.
        # Хранятся в thresholds.json как "_label.<parameter>": "Название".
        self.labels: dict = {}
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
        data, labels = self._flatten_sections(raw_data)
        self.labels = labels
        self.validate(data, labels)
        return data

    @classmethod
    def validate(cls, data: dict, labels: dict | None = None) -> None:
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

        ``labels`` — понятные названия порогов для оператора (ROLE.parameter
        -> строка). Названия не влияют на логику правил, только на отображение.
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

        if labels is not None:
            if not isinstance(labels, dict):
                raise ValueError("Названия порогов должны быть объектом")
            for key, name in labels.items():
                if not isinstance(key, str):
                    raise ValueError("Ключ названия порога должен быть строкой")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        f"Название порога {key} должно быть непустой строкой"
                    )

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
            if key.endswith("_overlap_min_px") and (
                type(value) is not int or value < 1
            ):
                raise ValueError(f"{key} должен быть целым числом >= 1")
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
            # short rule реализован строго для пары: при другом количестве
            # он не мог бы честно проверить разность двух уровней.
            if key.endswith("spider_contacts_short_expected_count") and value != 2:
                raise ValueError(f"{key} должен быть равен 2 (пара контактов)")
            # long rule строит линии и тренд расстояний, для чего нужны хотя
            # бы две точки. Большее число контактов остаётся поддержанным.
            if key.endswith("spider_contacts_long_expected_count") and value < 2:
                raise ValueError(f"{key} должен быть целым числом >= 2")
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
            ratio_min = data[prefix + "top_line_min_inlier_ratio"]
            if (
                type(ratio_min) not in (int, float)
                or not math.isfinite(float(ratio_min))
                or not 0.0 < float(ratio_min) <= 1.0
            ):
                raise ValueError(
                    f"{prefix}top_line_min_inlier_ratio должен быть числом "
                    "> 0 и <= 1"
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
            # Топология top_contacts фиксирована в коде как 5L+5R+2T+2B.
            # Любое другое число в UI раньше создавало ложное впечатление,
            # что раскладка будет пересчитана, хотя правило этого не делает.
            if key == "TOP.top_contacts_expected_count" and value != 14:
                raise ValueError(
                    "TOP.top_contacts_expected_count должен быть равен 14 "
                    "(5L+5R+2T+2B)"
                )
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
    def _flatten_sections(raw_data: dict) -> tuple[dict, dict]:
        """Преобразовать читаемые секции камер в ROLE.parameter.

        Ключи `_comment*` являются допустимыми комментариями JSON и полностью
        игнорируются загрузчиком. Дополнительные параметры в секциях камер
        сохраняются: новые пороги подхватываются при запуске и показываются
        в панели «Пороги правил» (группа «Прочие пороги»).

        Ключи `_label.<parameter>` — понятные названия порогов для оператора:
        они не попадают в значения, а собираются в отдельный словарь
        ``ROLE.parameter -> название`` (self.labels).
        """
        flattened: dict = {}
        labels: dict = {}
        for key, value in raw_data.items():
            if str(key).startswith("_comment"):
                continue
            if str(key).startswith("_label."):
                # Служебный ключ названия вне секции камеры: некуда привязать,
                # игнорируем (названия живут внутри секций ролей).
                continue
            if key in ROLE_SECTIONS:
                if not isinstance(value, dict):
                    raise ValueError(f"Секция {key} должна быть объектом")
                for parameter, parameter_value in value.items():
                    if str(parameter).startswith("_comment"):
                        continue
                    if str(parameter).startswith("_label."):
                        param_name = str(parameter)[len("_label."):]
                        if (
                            isinstance(parameter_value, str)
                            and parameter_value.strip()
                        ):
                            labels[f"{key}.{param_name}"] = (
                                parameter_value.strip()
                            )
                        continue
                    if isinstance(parameter_value, (dict, list)):
                        raise ValueError(
                            f"{key}.{parameter} должен быть простым значением"
                        )
                    flattened[f"{key}.{parameter}"] = parameter_value
                continue
            flattened[key] = value
        return flattened, labels

    @staticmethod
    def save_file(path: str, data: dict, labels: dict | None = None) -> None:
        """Сохранить плоский dict порогов в файл секциями по ролям.

        Формат повторяет читаемый вручную вид thresholds.json: секция камеры
        с параметрами и пустая строка между секциями. ``disabled_rules``
        записывается в конец. Перед сохранением вызывающий обязан выполнить
        :meth:`validate`, чтобы в файл не попали некорректные значения.

        ``labels`` — понятные названия порогов (ROLE.parameter -> строка);
        записываются в секции камеры как ``"_label.<parameter>": "Название"``.
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
                role_label_keys = sorted(
                    key[len(role) + 1:]
                    for key in (labels or {})
                    if key.startswith(f"{role}.")
                )
                entries = [
                    (False, parameter) for parameter in params
                ] + [
                    (True, parameter) for parameter in role_label_keys
                ]
                for p_index, (is_label, parameter) in enumerate(entries):
                    comma = "," if p_index < len(entries) - 1 else ""
                    if is_label:
                        full_key = f"{role}.{parameter}"
                        lines.append(
                            f"        {json.dumps('_label.' + parameter, ensure_ascii=False)}: "
                            f"{json.dumps(labels[full_key], ensure_ascii=False)}{comma}"
                        )
                    else:
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
# Ниже — точный перевод каждого порога на русский, максимально близкий
# к смыслу (что именно проверяет правило).

# ─── Операторские названия и пояснения порогов ──────────────────────────
#
# Значение порога само по себе часто недостаточно: например, часть величин
# является долей высоты контакта, а ``*_component_min_px`` — не длиной, а
# количеством пикселей в связной компоненте. Поэтому название и пояснение
# держим рядом с логикой схемы. UI получает оба поля через _param_meta().

PARAM_LABELS = {
    # ── INPUT: наличие детали ──────────────────────────────────────────
    "input_part_presence_false_positive_max_count": (
        "Допустимое число ложных срабатываний, шт."
    ),

    # ── INPUT: геометрия окон ──────────────────────────────────────────
    "input_window_geometry_min_confidence": (
        "Мин. уверенность обнаружения окон"
    ),
    "input_window_geometry_expected_count": "Ожидаемое число окон, шт.",
    "input_window_geometry_top_px_min": "T до перекладины: мин., px",
    "input_window_geometry_top_px_max": "T до перекладины: макс., px",
    "input_window_geometry_bottom_px_min": "B после перекладины: мин., px",
    "input_window_geometry_bottom_px_max": "B после перекладины: макс., px",
    "input_window_geometry_center_zone_ratio": (
        "Ширина центральной зоны измерения, доля"
    ),

    # ── INPUT: раковины в окнах ────────────────────────────────────────
    "input_window_sinks_min_confidence": "Мин. уверенность раковин",
    "input_window_sinks_window_min_confidence": (
        "Мин. уверенность окон для проверки раковин"
    ),
    "input_window_sinks_overlap_min_px": (
        "Мин. число общих пикселей раковины и окна, px"
    ),

    # ── SPIDER: длинные контакты ───────────────────────────────────────
    "spider_contacts_long_min_confidence": (
        "Мин. уверенность длинных контактов"
    ),
    "spider_contacts_long_expected_count": (
        "Ожидаемое число длинных контактов, шт."
    ),
    "spider_contacts_long_damper_open_max_px": (
        "Макс. перепад заслонки по ряду, px"
    ),
    "spider_contacts_long_gap_dev_max_px": (
        "Макс. разброс расстояний до пропуска, px"
    ),
    "spider_contacts_long_inscribed_rect_width_px": (
        "Эталон длинного контакта: ширина, px"
    ),
    "spider_contacts_long_inscribed_rect_height_px": (
        "Эталон длинного контакта: высота, px"
    ),
    "spider_contacts_long_y_filter_ratio": (
        "Допуск отбора контактов по Y, доля высоты"
    ),

    # ── SPIDER: короткие контакты ──────────────────────────────────────
    "spider_contacts_short_min_confidence": (
        "Мин. уверенность коротких контактов"
    ),
    "spider_contacts_short_expected_count": (
        "Фиксированное число коротких контактов, шт."
    ),
    "spider_contacts_short_damper_open_max_px": (
        "Макс. открытие заслонки, px"
    ),
    "spider_contacts_short_inscribed_rect_width_px": (
        "Эталон короткого контакта: ширина, px"
    ),
    "spider_contacts_short_inscribed_rect_height_px": (
        "Эталон короткого контакта: высота, px"
    ),
    "spider_contacts_short_area_absolute_min": (
        "Мин. площадь короткого контакта, px²"
    ),
    "spider_contacts_short_y_filter_ratio": (
        "Допуск отбора контактов по Y, доля высоты"
    ),

    # ── SPIDER: контроль полосы пропуска ───────────────────────────────
    "spider_long_omission_min_confidence": (
        "Мин. уверенность длинной полосы пропуска"
    ),
    "spider_long_omission_allowed_thickness_px": (
        "Допустимая толщина длинной полосы пропуска, px"
    ),
    "spider_long_omission_excess_component_min_px": (
        "Мин. число пикселей в компоненте избытка, px"
    ),
    "spider_long_omission_top_line_max_residual_px": (
        "Макс. остаточное отклонение верхней линии, px"
    ),
    "spider_long_omission_top_line_min_inlier_ratio": (
        "Мин. доля точек верхней линии в допуске, доля"
    ),
    "spider_short_omission_min_confidence": (
        "Мин. уверенность короткой полосы пропуска"
    ),
    "spider_short_omission_allowed_thickness_px": (
        "Допустимая толщина короткой полосы пропуска, px"
    ),
    "spider_short_omission_excess_component_min_px": (
        "Мин. число пикселей в компоненте избытка, px"
    ),
    "spider_short_omission_top_line_max_residual_px": (
        "Макс. остаточное отклонение верхней линии, px"
    ),
    "spider_short_omission_top_line_min_inlier_ratio": (
        "Мин. доля точек верхней линии в допуске, доля"
    ),

    # ── TOP: контакты ─────────────────────────────────────────────────
    "top_contacts_min_confidence": (
        "Мин. уверенность контактов сверху"
    ),
    "top_contacts_expected_count": "Фиксированное число контактов сверху, шт.",
    "top_contacts_platform_min_confidence": (
        "Мин. уверенность платформы для контактов"
    ),
    "top_contacts_edge_distance_deviation_ratio": (
        "Допуск разброса отступа до края, доля размера контакта"
    ),
    "top_contacts_side_rect_width_px": (
        "Эталон контактов L/R: ширина, px"
    ),
    "top_contacts_side_rect_height_px": (
        "Эталон контактов L/R: высота, px"
    ),
    "top_contacts_edge_rect_width_px": (
        "Эталон контактов T/B: ширина, px"
    ),
    "top_contacts_edge_rect_height_px": (
        "Эталон контактов T/B: высота, px"
    ),

    # ── TOP: заплыв платформы ─────────────────────────────────────────
    "top_platform_overlap_platform_min_confidence": (
        "Мин. уверенность платформы для границы"
    ),
    "top_platform_overlap_excess_component_min_px": (
        "Мин. число пикселей в компоненте заплыва, px"
    ),
    "top_platform_overlap_contact_min_confidence": (
        "Мин. уверенность контактов для границы"
    ),
    "top_platform_overlap_contact_inner_ratio": (
        "Положение опорной точки контакта (0…1)"
    ),
    "top_platform_overlap_margin_px": "Внешний отступ границы, px",
    "top_platform_overlap_expand_x_ratio": "Масштаб границы по X",
    "top_platform_overlap_expand_y_ratio": "Масштаб границы по Y",

    # ── TOP: платформа ─────────────────────────────────────────────────
    "top_platform_min_confidence": "Мин. уверенность платформы",
    "top_platform_inscribed_rect_width_px": (
        "Вписываемый эталон платформы: ширина, px"
    ),
    "top_platform_inscribed_rect_height_px": (
        "Вписываемый эталон платформы: высота, px"
    ),

    # ── TOP: раковины корпуса ─────────────────────────────────────────
    "top_sinks_min_confidence": "Мин. уверенность раковин корпуса",
    "top_sinks_platform_min_confidence": (
        "Мин. уверенность платформы для раковин"
    ),
    "top_sinks_case_central_min_confidence": (
        "Мин. уверенность центральной области корпуса"
    ),

    # ── TOP: стекло ───────────────────────────────────────────────────
    "top_glass_min_confidence": "Мин. уверенность стекла",
    "top_glass_platform_min_confidence": (
        "Мин. уверенность платформы для стекла"
    ),
    "top_glass_case_min_confidence": (
        "Мин. уверенность внешней области корпуса"
    ),
    "top_glass_case_central_min_confidence": (
        "Мин. уверенность центральной области корпуса"
    ),
    "top_glass_pin_min_confidence": "Мин. уверенность штифтов",
}

# Короткая подсказка открывается наведением на подпись в интерфейсе и
# одновременно доступна интеграциям через GET /api/thresholds. Она снимает
# неоднозначности единиц и показывает общие пороги между правилами.
PARAM_DESCRIPTIONS = {
    "input_part_presence_false_positive_max_count": (
        "До этого числа обнаружений окон включительно лоток считается "
        "пустым. Деталь подтверждается только если порог превышен на обеих "
        "INPUT-камерах."
    ),
    "input_window_geometry_min_confidence": (
        "Минимальная уверенность YOLO для обнаружения окон. Этот общий порог также "
        "использует правило «Наличие детали»."
    ),
    "input_window_geometry_expected_count": (
        "Сколько окон должно войти в выбранный ряд."
    ),
    "input_window_geometry_top_px_min": (
        "Нижняя граница диапазона T: от верха маски окна до нижнего края "
        "перекладины."
    ),
    "input_window_geometry_top_px_max": (
        "Верхняя граница диапазона T: от верха маски окна до нижнего края "
        "перекладины."
    ),
    "input_window_geometry_bottom_px_min": (
        "Нижняя граница диапазона B: от нижнего края перекладины до низа "
        "маски окна."
    ),
    "input_window_geometry_bottom_px_max": (
        "Верхняя граница диапазона B: от нижнего края перекладины до низа "
        "маски окна."
    ),
    "input_window_geometry_center_zone_ratio": (
        "Доля ширины ограничивающего прямоугольника окна, в центральной полосе которой ищется нижний "
        "край перекладины; значение должно быть больше нуля."
    ),
    "input_window_sinks_min_confidence": (
        "Минимальная уверенность YOLO для обнаружения раковин "
        "во входном окне."
    ),
    "input_window_sinks_window_min_confidence": (
        "Минимальная уверенность обнаружений окон, используемых как маски окон "
        "только для проверки раковин."
    ),
    "input_window_sinks_overlap_min_px": (
        "Брак возникает при числе общих пикселей растровых масок раковины и окна "
        "не меньше этого целого порога."
    ),
    "spider_contacts_long_min_confidence": (
        "Минимальная уверенность YOLO для длинных контактов."
    ),
    "spider_contacts_long_expected_count": (
        "Число длинных контактов в контролируемом ряду."
    ),
    "spider_contacts_long_damper_open_max_px": (
        "«Заслонка» — линия через центры вписанных эталонов. Перепад её "
        "высоты относительно опорной линии пропуска на размахе ряда не "
        "должен превышать это значение; наклон всей детали на замер не "
        "влияет."
    ),
    "spider_contacts_long_gap_dev_max_px": (
        "«Стены» — перпендикуляры от центров контактов к опорной линии "
        "пропуска. Отклонение длины любой стены от медианной не должно "
        "превышать это значение; ловит одиночный торчащий контакт."
    ),
    "spider_contacts_long_inscribed_rect_width_px": (
        "Ширина прямоугольника, который обязан целиком поместиться в маску "
        "каждого длинного контакта."
    ),
    "spider_contacts_long_inscribed_rect_height_px": (
        "Высота прямоугольника, который обязан целиком поместиться в маску "
        "каждого длинного контакта."
    ),
    "spider_contacts_long_y_filter_ratio": (
        "При лишних обнаружениях оставляет кандидатов около медианного Y: "
        "допуск равен медианной высоте × этот коэффициент."
    ),
    "spider_contacts_short_min_confidence": (
        "Минимальная уверенность YOLO для коротких контактов."
    ),
    "spider_contacts_short_expected_count": (
        "Контрольная геометрия реализована строго для пары контактов, "
        "поэтому значение фиксировано: 2."
    ),
    "spider_contacts_short_damper_open_max_px": (
        "«Заслонка» — отрезок между центрами вписанных эталонов, «стены» — "
        "перпендикуляры от центров к опорной линии пропуска. Открытие "
        "заслонки (разница длин стен, px) не должно превышать это значение; "
        "наклон всей детали на замер не влияет."
    ),
    "spider_contacts_short_inscribed_rect_width_px": (
        "Ширина прямоугольника, который обязан целиком поместиться в маску "
        "каждого короткого контакта."
    ),
    "spider_contacts_short_inscribed_rect_height_px": (
        "Высота прямоугольника, который обязан целиком поместиться в маску "
        "каждого короткого контакта."
    ),
    "spider_contacts_short_area_absolute_min": (
        "Минимальная площадь маски кандидата; меньшие маски "
        "исключаются до выбора пары."
    ),
    "spider_contacts_short_y_filter_ratio": (
        "При лишних кандидатах оставляет контакты около медианного Y: "
        "допуск равен медианной высоте × этот коэффициент."
    ),
    "spider_long_omission_min_confidence": (
        "Минимальная уверенность YOLO для длинной полосы пропуска. Этот порог также "
        "использует проверка наклона длинных контактов."
    ),
    "spider_long_omission_allowed_thickness_px": (
        "Перпендикулярное расстояние от верхней опорной линии до контрольной линии "
        "полосы пропуска."
    ),
    "spider_long_omission_excess_component_min_px": (
        "Минимальное число пикселей растровой маски в 8-связной компоненте ниже "
        "контрольной линии; меньшие компоненты считаются шумом."
    ),
    "spider_long_omission_top_line_max_residual_px": (
        "Максимальный остаток точек верхнего контура относительно устойчивой "
        "опорной линии. Точка считается «у линии», если её остаток не "
        "превышает это значение."
    ),
    "spider_long_omission_top_line_min_inlier_ratio": (
        "Минимальная доля точек верхнего контура, обязанных лежать «у "
        "линии» (в пределах макс. остатка); меньшая доля делает измерение "
        "невалидным. Единичные зубцы маски на замер не влияют."
    ),
    "spider_short_omission_min_confidence": (
        "Минимальная уверенность YOLO для короткой полосы пропуска. Этот порог также "
        "использует проверка наклона коротких контактов."
    ),
    "spider_short_omission_allowed_thickness_px": (
        "Перпендикулярное расстояние от верхней опорной линии до контрольной линии "
        "короткой полосы пропуска."
    ),
    "spider_short_omission_excess_component_min_px": (
        "Минимальное число пикселей растровой маски в 8-связной компоненте ниже "
        "контрольной линии; меньшие компоненты считаются шумом."
    ),
    "spider_short_omission_top_line_max_residual_px": (
        "Максимальный остаток точек верхнего контура относительно устойчивой "
        "опорной линии. Точка считается «у линии», если её остаток не "
        "превышает это значение."
    ),
    "spider_short_omission_top_line_min_inlier_ratio": (
        "Минимальная доля точек верхнего контура, обязанных лежать «у "
        "линии» (в пределах макс. остатка); меньшая доля делает измерение "
        "невалидным. Единичные зубцы маски на замер не влияют."
    ),
    "top_contacts_min_confidence": (
        "Минимальная уверенность YOLO для контактов. Этот общий порог также "
        "используют правила раковин и стекла сверху."
    ),
    "top_contacts_expected_count": (
        "Топология правила фиксирована: 5L + 5R + 2T + 2B, поэтому "
        "значение фиксировано: 14."
    ),
    "top_contacts_platform_min_confidence": (
        "Минимальная уверенность платформы, границы которой нужны для проверки "
        "контактов сверху."
    ),
    "top_contacts_edge_distance_deviation_ratio": (
        "Допуск разброса расстояний до стороны границы платформы: медианный "
        "размер контакта × этот коэффициент."
    ),
    "top_contacts_side_rect_width_px": (
        "Ширина эталонного прямоугольника для контактов у левой и правой "
        "сторон платформы."
    ),
    "top_contacts_side_rect_height_px": (
        "Высота эталонного прямоугольника для контактов у левой и правой "
        "сторон платформы."
    ),
    "top_contacts_edge_rect_width_px": (
        "Ширина эталонного прямоугольника для контактов у верхней и нижней "
        "сторон платформы."
    ),
    "top_contacts_edge_rect_height_px": (
        "Высота эталонного прямоугольника для контактов у верхней и нижней "
        "сторон платформы."
    ),
    "top_platform_overlap_platform_min_confidence": (
        "Минимальная уверенность платформы для проверки её выхода за границу, "
        "построенную по контактам."
    ),
    "top_platform_overlap_excess_component_min_px": (
        "Минимальное число пикселей растровой маски в 8-связной компоненте платформы "
        "за границей; меньшие компоненты считаются шумом."
    ),
    "top_platform_overlap_contact_min_confidence": (
        "Минимальная уверенность контактов, из которых строится граница "
        "вокруг платформы."
    ),
    "top_platform_overlap_contact_inner_ratio": (
        "Положение опорной координаты внутри ограничивающего прямоугольника контакта: 0 — кромка к "
        "платформе, 0.5 — центр, 1 — внешняя кромка."
    ),
    "top_platform_overlap_margin_px": (
        "Отступ, на который граница по контактам расширяется наружу с каждой "
        "стороны; отрицательное значение сжимает её."
    ),
    "top_platform_overlap_expand_x_ratio": (
        "Множитель ширины построенной по контактам границы; значение должно "
        "быть больше нуля."
    ),
    "top_platform_overlap_expand_y_ratio": (
        "Множитель высоты построенной по контактам границы; значение должно "
        "быть больше нуля."
    ),
    "top_platform_min_confidence": (
        "Минимальная уверенность YOLO для платформы в правиле вписывания "
        "эталонного прямоугольника."
    ),
    "top_platform_inscribed_rect_width_px": (
        "Ширина прямоугольника, который обязан целиком поместиться в маску "
        "платформы."
    ),
    "top_platform_inscribed_rect_height_px": (
        "Высота прямоугольника, который обязан целиком поместиться в маску "
        "платформы."
    ),
    "top_sinks_min_confidence": (
        "Минимальная уверенность YOLO для раковин корпуса."
    ),
    "top_sinks_platform_min_confidence": (
        "Минимальная уверенность платформы, которая исключается из запрещённой "
        "области раковины."
    ),
    "top_sinks_case_central_min_confidence": (
        "Минимальная уверенность центральной области корпуса, внутри которой "
        "проверяется раковина."
    ),
    "top_glass_min_confidence": (
        "Минимальная уверенность YOLO для стекла."
    ),
    "top_glass_platform_min_confidence": (
        "Минимальная уверенность платформы в общем контексте проверки стекла."
    ),
    "top_glass_case_min_confidence": (
        "Минимальная уверенность внешней маски корпуса в общем контексте стекла."
    ),
    "top_glass_case_central_min_confidence": (
        "Минимальная уверенность центральной маски корпуса в общем контексте стекла."
    ),
    "top_glass_pin_min_confidence": (
        "Минимальная уверенность YOLO для 14 штифтов в общем контексте стекла."
    ),
}

# Запасной перевод по суффиксу — для порогов, добавленных вручную,
# которых ещё нет в PARAM_LABELS.
SUFFIX_LABELS = {
    "min_confidence": "Мин. уверенность",
    "window_min_confidence": "Мин. уверенность окна",
    "platform_min_confidence": "Мин. уверенность платформы",
    "contact_min_confidence": "Мин. уверенность контакта",
    "case_min_confidence": "Мин. уверенность корпуса",
    "case_central_min_confidence": "Мин. уверенность центра корпуса",
    "pin_min_confidence": "Мин. уверенность штифта",
    "expected_count": "Ожидаемое количество, шт.",
    "top_px_min": "T: мин., px",
    "top_px_max": "T: макс., px",
    "bottom_px_min": "B: мин., px",
    "bottom_px_max": "B: макс., px",
    "center_zone_ratio": "Ширина центральной зоны, доля",
    "overlap_min_px": "Мин. число общих пикселей, px",
    "damper_open_max_px": "Макс. открытие заслонки, px",
    "gap_dev_max_px": "Макс. разброс расстояний, px",
    "inscribed_rect_width_px": "Ширина вписываемого прямоугольника, px",
    "inscribed_rect_height_px": "Высота вписываемого прямоугольника, px",
    "y_filter_ratio": "Допуск фильтра по Y, доля",
    "area_absolute_min": "Мин. площадь, px²",
    "allowed_thickness_px": "Допустимая толщина, px",
    "excess_component_min_px": "Мин. число пикселей в компоненте, px",
    "top_line_max_residual_px": "Макс. остаточное отклонение линии, px",
    "edge_distance_deviation_ratio": "Допуск разброса до края, доля",
    "side_rect_width_px": "Эталон L/R: ширина, px",
    "side_rect_height_px": "Эталон L/R: высота, px",
    "edge_rect_width_px": "Эталон T/B: ширина, px",
    "edge_rect_height_px": "Эталон T/B: высота, px",
    "contact_inner_ratio": "Положение опорной точки контакта (0…1)",
    "margin_px": "Внешний отступ, px",
    "expand_x_ratio": "Масштаб по X",
    "expand_y_ratio": "Масштаб по Y",
}


# (rule_id, подпись в UI, префиксы имён параметров). Более специфичные
# префиксы идут раньше общих: TOP.top_platform_overlap_* не должен попадать
# в группу TOP.top_platform_*.
RULE_GROUPS = (
    ("input_part_presence",   "НАЛИЧИЕ ДЕТАЛИ",         ("input_part_presence_",)),
    ("input_window_geometry", "ГЕОМЕТРИЯ ВХОДНОГО ОКНА", ("input_window_geometry_",)),
    ("input_window_sinks",    "РАКОВИНЫ В ОКНАХ",        ("input_window_sinks_",)),
    ("spider_contacts_long",  "КОНТАКТЫ · ДЛИННЫЕ",     ("spider_contacts_long_",)),
    ("spider_long_omission",  "ПОЛОСА ПРОПУСКА · ДЛИННАЯ", ("spider_long_omission_",)),
    ("spider_contacts_short", "КОНТАКТЫ · КОРОТКИЕ",    ("spider_contacts_short_",)),
    ("spider_short_omission", "ПОЛОСА ПРОПУСКА · КОРОТКАЯ", ("spider_short_omission_",)),
    ("top_contacts",          "КОНТАКТЫ СВЕРХУ",        ("top_contacts_",)),
    ("top_platform_overlap",  "ЗАПЛЫВ ПЛАТФОРМЫ",       ("top_platform_overlap_",)),
    ("top_platform",          "ПЛАТФОРМА СВЕРХУ",       ("top_platform_",)),
    ("top_sinks",             "РАКОВИНЫ КОРПУСА",        ("top_sinks_",)),
    ("top_glass",             "СТЕКЛО СВЕРХУ",          ("top_glass_",)),
)

_RULE_GROUPS_SORTED = tuple(
    sorted(
        RULE_GROUPS,
        key=lambda group: -max(len(p) for p in group[2]),
    )
)
# _RULE_GROUPS_SORTED нужен только чтобы общий top_platform_* не поглотил
# top_platform_overlap_*. В интерфейсе карточки возвращаем в естественном
# порядке выполнения правил, заданном в RULE_GROUPS.
_RULE_GROUP_DISPLAY_INDEX = {
    rule_id: index for index, (rule_id, _label, _prefixes) in enumerate(RULE_GROUPS)
}

# Эти два значения описывают фиксированную конструкцию детали, а не
# калибруемый допуск. Показываем их в интерфейсе для прозрачности, но не
# даём оператору сохранить значение, которое правило не умеет обработать.
FIXED_VALUE_PARAMETERS = {
    "spider_contacts_short_expected_count": 2,
    "top_contacts_expected_count": 14,
}

# Порядок строк в карточке — порядок настройки правила, а не алфавитный
# порядок технических ключей. Так min всегда стоит перед max, а оператор
# сначала видит отбор модели/количество, затем геометрию и фильтры.
PARAMETER_DISPLAY_ORDER = (
    "input_part_presence_false_positive_max_count",
    "input_window_geometry_min_confidence",
    "input_window_geometry_expected_count",
    "input_window_geometry_top_px_min",
    "input_window_geometry_top_px_max",
    "input_window_geometry_bottom_px_min",
    "input_window_geometry_bottom_px_max",
    "input_window_geometry_center_zone_ratio",
    "input_window_sinks_min_confidence",
    "input_window_sinks_window_min_confidence",
    "input_window_sinks_overlap_min_px",
    "spider_contacts_long_min_confidence",
    "spider_contacts_long_expected_count",
    "spider_contacts_long_damper_open_max_px",
    "spider_contacts_long_gap_dev_max_px",
    "spider_contacts_long_inscribed_rect_width_px",
    "spider_contacts_long_inscribed_rect_height_px",
    "spider_contacts_long_y_filter_ratio",
    "spider_long_omission_min_confidence",
    "spider_long_omission_allowed_thickness_px",
    "spider_long_omission_excess_component_min_px",
    "spider_long_omission_top_line_max_residual_px",
    "spider_long_omission_top_line_min_inlier_ratio",
    "spider_contacts_short_min_confidence",
    "spider_contacts_short_expected_count",
    "spider_contacts_short_damper_open_max_px",
    "spider_contacts_short_inscribed_rect_width_px",
    "spider_contacts_short_inscribed_rect_height_px",
    "spider_contacts_short_area_absolute_min",
    "spider_contacts_short_y_filter_ratio",
    "spider_short_omission_min_confidence",
    "spider_short_omission_allowed_thickness_px",
    "spider_short_omission_excess_component_min_px",
    "spider_short_omission_top_line_max_residual_px",
    "spider_short_omission_top_line_min_inlier_ratio",
    "top_contacts_min_confidence",
    "top_contacts_expected_count",
    "top_contacts_platform_min_confidence",
    "top_contacts_edge_distance_deviation_ratio",
    "top_contacts_side_rect_width_px",
    "top_contacts_side_rect_height_px",
    "top_contacts_edge_rect_width_px",
    "top_contacts_edge_rect_height_px",
    "top_platform_overlap_platform_min_confidence",
    "top_platform_overlap_excess_component_min_px",
    "top_platform_overlap_contact_min_confidence",
    "top_platform_overlap_contact_inner_ratio",
    "top_platform_overlap_margin_px",
    "top_platform_overlap_expand_x_ratio",
    "top_platform_overlap_expand_y_ratio",
    "top_platform_min_confidence",
    "top_platform_inscribed_rect_width_px",
    "top_platform_inscribed_rect_height_px",
    "top_sinks_min_confidence",
    "top_sinks_platform_min_confidence",
    "top_sinks_case_central_min_confidence",
    "top_glass_min_confidence",
    "top_glass_platform_min_confidence",
    "top_glass_case_min_confidence",
    "top_glass_case_central_min_confidence",
    "top_glass_pin_min_confidence",
)
_PARAMETER_DISPLAY_INDEX = {
    key: index for index, key in enumerate(PARAMETER_DISPLAY_ORDER)
}


def _param_meta(key: str, value) -> dict:
    """Метаданные одного параметра для редактора.

    Границы ввода следуют реальной валидации :class:`ThresholdLoader`, а
    не произвольному «безопасному» диапазону. Для строго положительных
    значений UI использует наименьший практический шаг редактора вместо 0;
    верхний предел не задаётся, если его нет в правилах.
    """
    # Точный перевод по имени параметра; для незнакомых (добавленных вручную)
    # порогов — запасной перевод по суффиксу, иначе техническое имя.
    label = PARAM_LABELS.get(key)
    if label is None:
        label = next(
            (
                suffix_label
                for suffix, suffix_label in sorted(
                    SUFFIX_LABELS.items(), key=lambda item: -len(item[0]),
                )
                if key.endswith(suffix)
            ),
            key,
        )
    description = PARAM_DESCRIPTIONS.get(
        key,
        "Дополнительный числовой порог. Технический ключ: " + key,
    )
    meta = {
        "key": key,
        "label": label,
        "description": description,
        "value": value,
    }
    fixed_value = FIXED_VALUE_PARAMETERS.get(key)
    if fixed_value is not None:
        meta.update({
            "step": 1,
            "min": fixed_value,
            "max": fixed_value,
            "readonly": True,
        })
        return meta

    # Целые счётчики.
    if key.endswith("spider_contacts_long_expected_count"):
        meta.update({"step": 1, "min": 2})
    elif key.endswith("_expected_count"):
        meta.update({"step": 1, "min": 1})
    elif key.endswith("_false_positive_max_count"):
        meta.update({"step": 1, "min": 0})
    elif key.endswith("_excess_component_min_px"):
        meta.update({"step": 1, "min": 1})
    elif key.endswith("_overlap_min_px"):
        # Перекрытие — число raster-пикселей. Ноль сделал бы дефектом даже
        # пару masks без общих пикселей, поэтому рабочий минимум — один.
        meta.update({"step": 1, "min": 1})
    elif key.endswith("_area_absolute_min"):
        meta.update({"step": 1, "min": 0})

    # Нормированные пороги.
    elif key.endswith("_min_confidence"):
        meta.update({"step": 0.01, "min": 0, "max": 1})
    elif key.endswith("_center_zone_ratio") or key.endswith("_inlier_ratio"):
        meta.update({"step": 0.01, "min": 0.01, "max": 1})
    elif key.endswith("_inner_ratio"):
        meta.update({"step": 0.01, "min": 0, "max": 1})
    elif key.endswith("_expand_x_ratio") or key.endswith("_expand_y_ratio"):
        meta.update({"step": 0.05, "min": 0.01})
    elif key.endswith("_y_filter_ratio"):
        meta.update({"step": 0.1, "min": 0})
    elif (
        key.endswith("_ratio")
        or key.endswith("_tilt_ratio_max")
        or key.endswith("_slope")
    ):
        # В схеме нет верхней границы: коэффициент/наклон больше единицы
        # допустим, если его действительно нужно настроить под изделие.
        meta.update({"step": 0.01, "min": 0})

    # Геометрические величины. margin — единственный штатный отрицательный
    # параметр (сжимает область), остальные пиксельные значения неотрицательны.
    elif key.endswith("_margin_px"):
        meta.update({"step": 0.1})
    elif "inscribed_rect_" in key:
        meta.update({"step": 0.1, "min": 0.1})
    else:
        meta.update({"step": 0.1, "min": 0})
    return meta


def describe_role_parameters(role: str, thresholds: dict) -> list:
    """Пороги роли, сгруппированные по правилам, в формате для UI.

    Возвращает список групп::

        [{"rule": "top_contacts", "label": "КОНТАКТЫ СВЕРХУ",
          "params": [{"key": ..., "label": ..., "description": ...,
                      "value": ..., "step": ..., "min": ..., "max": ...,
                      "readonly": ...}]}, ...]
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
        group_params.sort(
            key=lambda item: (
                _PARAMETER_DISPLAY_INDEX.get(item[0], len(PARAMETER_DISPLAY_ORDER)),
                item[0],
            ),
        )
        groups.append({
            "rule": rule_id,
            "label": label,
            "params": [
                _param_meta(name, value)
                for name, value in group_params
            ],
        })
        matched.update(name for name, _ in group_params)

    groups.sort(
        key=lambda group: _RULE_GROUP_DISPLAY_INDEX.get(
            group["rule"], len(_RULE_GROUP_DISPLAY_INDEX),
        ),
    )
    leftovers = [(name, value) for name, value in params if name not in matched]
    if leftovers:
        leftovers.sort()
        groups.append({
            "rule": "other",
            "label": "ПРОЧИЕ ПОРОГИ",
            "params": [_param_meta(name, value) for name, value in leftovers],
        })
    return groups
