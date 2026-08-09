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
    "presence_min_count",
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
    """Versioned operator threshold store.

    Threshold values are deliberately not range-clamped here.  Calibration is
    owned by the developer/nalaďчик and the operator API only guarantees that
    values are finite JSON numbers.
    """

    SCHEMA_VERSION = 2
    LEGACY_SCHEMA_VERSION = 1

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
        self.schema_version = self.SCHEMA_VERSION
        self.backup_path = f"{self.path}.bak"
        self.thresholds = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            raise RuntimeError(f"Файл не найден: {self.path}")
        try:
            with open(self.path, encoding="utf-8") as stream:
                raw_data = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ошибка чтения {self.path}: {exc}") from exc
        if not isinstance(raw_data, dict):
            raise ValueError("thresholds.json должен содержать объект")

        migrated, labels, changed = self.migrate(raw_data)
        data, labels = self._flatten_sections(migrated)
        data.pop("schema_version", None)
        self.labels = labels
        self.validate(data, labels)
        if changed:
            # Migration is all-or-nothing.  ``save_file`` validates the full
            # set again and creates the required backup before replacement.
            self.save_file(self.path, data, labels)
        return data

    @classmethod
    def validate(cls, data: dict, labels: dict | None = None) -> None:
        """Validate a complete threshold set without hidden calibration rules.

        The write contract is intentionally strict about *shape* and JSON
        types, but permissive about numeric values: every existing threshold
        may be any finite number, including negative or fractional values.
        This prevents an unnoticed clamp or min/max swap in the operator API.
        """
        if not isinstance(data, dict):
            raise ValueError("Пороги должны быть объектом")
        for key in cls.REQUIRED_KEYS:
            if key not in data:
                raise ValueError(f"Отсутствует ключ в thresholds.json: {key}")

        for key, value in data.items():
            if not isinstance(key, str):
                raise ValueError("Ключ порога должен быть строкой")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{key} должен быть конечным JSON-числом")

        if labels is not None:
            if not isinstance(labels, dict):
                raise ValueError("Названия порогов должны быть объектом")
            for key, name in labels.items():
                if not isinstance(key, str) or not isinstance(name, str) or not name.strip():
                    raise ValueError(f"Некорректное название порога: {key!r}")

    @classmethod
    def operator_update(cls, current: dict, role: str, values: dict) -> dict:
        """Validate the narrow HMI write contract and return a full copy."""
        if not isinstance(current, dict) or not isinstance(role, str) or not role:
            raise ValueError("invalid threshold update envelope")
        if not isinstance(values, dict) or not values:
            raise ValueError("values must be a non-empty object")
        prefix = role + "."
        updated = dict(current)
        for key, value in values.items():
            full_key = key if key.startswith(prefix) else prefix + key
            if full_key not in current:
                raise ValueError(f"unknown operator threshold: {full_key}")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{full_key} must be a finite JSON number")
            updated[full_key] = value
        cls.validate(updated)
        return updated

    @classmethod
    def migrate(cls, raw_data: dict) -> tuple[dict, dict, bool]:
        """Migrate the old presence key and return ``(data, labels, changed)``."""
        if not isinstance(raw_data, dict):
            raise ValueError("thresholds.json должен содержать объект")
        data = dict(raw_data)
        changed = data.get("schema_version") != cls.SCHEMA_VERSION
        # Work on a copy of role sections so a failed migration cannot mutate
        # the caller's decoded document.
        for role in ("INPUT_LEFT", "INPUT_RIGHT"):
            section = data.get(role)
            if not isinstance(section, dict):
                continue
            section = dict(section)
            legacy = section.pop("input_part_presence_false_positive_max_count", None)
            if "presence_min_count" not in section:
                # Legacy 2 means ``count > 2``; the new production recipe is
                # expressed directly as ``count >= 3``.
                section["presence_min_count"] = 3
                changed = True
            elif legacy is not None:
                changed = True
            data[role] = section
        data["schema_version"] = cls.SCHEMA_VERSION
        labels = {}
        flattened, labels = cls._flatten_sections(data)
        flattened.pop("schema_version", None)
        return data, labels, changed

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
            if str(key) in ("schema_version", "_schema_version") or str(key).startswith("_comment"):
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
        """Atomically save a complete versioned threshold document.

        Validation happens before any file operation.  The previous file is
        copied to ``.bak`` and the new document is fsynced before replacement.
        """
        ThresholdLoader.validate(data, labels)
        destination = os.path.abspath(path)
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
        if os.path.exists(destination):
            backup = destination + ".bak"
            with open(destination, "rb") as source, open(backup, "wb") as target:
                target.write(source.read())
                target.flush()
                os.fsync(target.fileno())
        data = dict(data)
        data.pop("schema_version", None)
        data["schema_version"] = ThresholdLoader.SCHEMA_VERSION

        grouped: dict = {}
        for key, value in data.items():
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
        last_index = len(ordered_keys) - 1
        for index, role in enumerate(ordered_keys):
            if index:
                lines.append("")
            params = grouped[role]
            needs_comma = index < last_index
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

        lines.append("}")
        temp_path = destination + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, destination)

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
    "presence_min_count": (
        "Минимальное число flatness для присутствия, шт."
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
    "presence_min_count": (
        "Деталь подтверждается при count >= этого порога отдельно на каждой "
        "INPUT-камере; production требует обе роли."
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
    "presence_min_count",
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
    """Return display metadata without imposing a calibration range.

    The operator editor may send any finite JSON number.  ``step`` is only a
    keyboard/UI hint; no min/max or readonly flag is emitted for thresholds.
    """
    label = PARAM_LABELS.get(key)
    if label is None:
        label = next(
            (
                suffix_label
                for suffix, suffix_label in sorted(
                    SUFFIX_LABELS.items(), key=lambda item: -len(item[0])
                )
                if key.endswith(suffix)
            ),
            key,
        )
    description = PARAM_DESCRIPTIONS.get(
        key, "Числовой production-порог. Технический ключ: " + key
    )
    return {
        "key": key,
        "label": label,
        "description": description,
        "value": value,
        "step": 0.1,
    }

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
        if key.startswith(prefix)
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
