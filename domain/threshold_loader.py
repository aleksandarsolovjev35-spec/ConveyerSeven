import json
import math
import os

ROLE_SECTIONS = (
    "INPUT_LEFT", "INPUT_RIGHT",
    "SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT",
    "TOP",
)

# (rule_id, UI label, parameter prefixes). More specific prefixes first.
RULE_GROUPS = (
    ("input_part_presence", "НАЛИЧИЕ ДЕТАЛИ", ("input_part_presence_",)),
    ("input_window_geometry", "ГЕОМЕТРИЯ ВХОДНОГО ОКНА", ("input_window_geometry_",)),
    ("input_window_sinks", "РАКОВИНЫ В ОКНАХ", ("input_window_sinks_",)),
    ("spider_contacts_long", "КОНТАКТЫ · ДЛИННЫЕ", ("spider_contacts_long_",)),
    ("spider_long_omission", "ПОЛОСА ПРОПУСКА · ДЛИННАЯ", ("spider_long_omission_",)),
    ("spider_contacts_short", "КОНТАКТЫ · КОРОТКИЕ", ("spider_contacts_short_",)),
    ("spider_short_omission", "ПОЛОСА ПРОПУСКА · КОРОТКАЯ", ("spider_short_omission_",)),
    ("top_contacts", "КОНТАКТЫ СВЕРХУ", ("top_contacts_",)),
    ("top_platform_overlap", "ЗАПЛЫВ ПЛАТФОРМЫ", ("top_platform_overlap_",)),
    ("top_platform", "ПЛАТФОРМА СВЕРХУ", ("top_platform_",)),
    ("top_sinks", "РАКОВИНЫ КОРПУСА", ("top_sinks_",)),
    ("top_glass", "СТЕКЛО СВЕРХУ", ("top_glass_",)),
)
_RULE_GROUPS_SORTED = tuple(sorted(RULE_GROUPS, key=lambda g: -max(len(p) for p in g[2])))
_RULE_GROUP_INDEX = {rule_id: i for i, (rule_id, _, _) in enumerate(RULE_GROUPS)}

PARAM_LABELS = {
    "input_part_presence_false_positive_max_count": "Допустимое число ложных срабатываний, шт.",
    "input_window_geometry_min_confidence": "Мин. уверенность обнаружения окон",
    "input_window_geometry_expected_count": "Ожидаемое число окон, шт.",
    "input_window_geometry_top_px_min": "T до перекладины: мин., px",
    "input_window_geometry_top_px_max": "T до перекладины: макс., px",
    "input_window_geometry_bottom_px_min": "B после перекладины: мин., px",
    "input_window_geometry_bottom_px_max": "B после перекладины: макс., px",
    "input_window_geometry_center_zone_ratio": "Ширина центральной зоны измерения, доля",
    "input_window_sinks_min_confidence": "Мин. уверенность раковин",
    "input_window_sinks_window_min_confidence": "Мин. уверенность окон для проверки раковин",
    "input_window_sinks_overlap_min_px": "Мин. число общих пикселей раковины и окна, px",
    "spider_contacts_long_min_confidence": "Мин. уверенность длинных контактов",
    "spider_contacts_long_expected_count": "Ожидаемое число длинных контактов, шт.",
    "spider_contacts_long_damper_open_max_px": "Макс. перепад заслонки по ряду, px",
    "spider_contacts_long_gap_dev_max_px": "Макс. разброс расстояний до пропуска, px",
    "spider_contacts_long_inscribed_rect_width_px": "Эталон длинного контакта: ширина, px",
    "spider_contacts_long_inscribed_rect_height_px": "Эталон длинного контакта: высота, px",
    "spider_contacts_long_y_filter_ratio": "Допуск отбора контактов по Y, доля высоты",
    "spider_contacts_short_min_confidence": "Мин. уверенность коротких контактов",
    "spider_contacts_short_expected_count": "Фиксированное число коротких контактов, шт.",
    "spider_contacts_short_damper_open_max_px": "Макс. открытие заслонки, px",
    "spider_contacts_short_inscribed_rect_width_px": "Эталон короткого контакта: ширина, px",
    "spider_contacts_short_inscribed_rect_height_px": "Эталон короткого контакта: высота, px",
    "spider_contacts_short_area_absolute_min": "Мин. площадь короткого контакта, px²",
    "spider_contacts_short_y_filter_ratio": "Допуск отбора контактов по Y, доля высоты",
    "spider_long_omission_min_confidence": "Мин. уверенность длинной полосы пропуска",
    "spider_long_omission_allowed_thickness_px": "Допустимая толщина длинной полосы, px",
    "spider_long_omission_excess_component_min_px": "Мин. размер компоненты избытка, px",
    "spider_long_omission_top_line_max_residual_px": "Макс. остаточное отклонение верхней линии, px",
    "spider_long_omission_top_line_min_inlier_ratio": "Мин. доля точек верхней линии в допуске",
    "spider_short_omission_min_confidence": "Мин. уверенность короткой полосы пропуска",
    "spider_short_omission_allowed_thickness_px": "Допустимая толщина короткой полосы, px",
    "spider_short_omission_excess_component_min_px": "Мин. размер компоненты избытка, px",
    "spider_short_omission_top_line_max_residual_px": "Макс. остаточное отклонение верхней линии, px",
    "spider_short_omission_top_line_min_inlier_ratio": "Мин. доля точек верхней линии в допуске",
    "top_contacts_min_confidence": "Мин. уверенность контактов сверху",
    "top_contacts_expected_count": "Фиксированное число контактов сверху, шт.",
    "top_contacts_platform_min_confidence": "Мин. уверенность платформы для контактов",
    "top_contacts_edge_distance_deviation_ratio": "Допуск разброса отступа до края, доля",
    "top_contacts_side_rect_width_px": "Эталон контактов L/R: ширина, px",
    "top_contacts_side_rect_height_px": "Эталон контактов L/R: высота, px",
    "top_contacts_edge_rect_width_px": "Эталон контактов T/B: ширина, px",
    "top_contacts_edge_rect_height_px": "Эталон контактов T/B: высота, px",
    "top_platform_overlap_platform_min_confidence": "Мин. уверенность платформы для границы",
    "top_platform_overlap_excess_component_min_px": "Мин. размер компоненты заплыва, px",
    "top_platform_overlap_contact_min_confidence": "Мин. уверенность контактов для границы",
    "top_platform_overlap_contact_inner_ratio": "Положение опорной точки контакта (0…1)",
    "top_platform_overlap_margin_px": "Внешний отступ границы, px",
    "top_platform_overlap_expand_x_ratio": "Масштаб границы по X",
    "top_platform_overlap_expand_y_ratio": "Масштаб границы по Y",
    "top_platform_min_confidence": "Мин. уверенность платформы",
    "top_platform_inscribed_rect_width_px": "Вписываемый эталон платформы: ширина, px",
    "top_platform_inscribed_rect_height_px": "Вписываемый эталон платформы: высота, px",
    "top_sinks_min_confidence": "Мин. уверенность раковин корпуса",
    "top_sinks_platform_min_confidence": "Мин. уверенность платформы для раковин",
    "top_sinks_case_central_min_confidence": "Мин. уверенность центральной области корпуса",
    "top_glass_min_confidence": "Мин. уверенность стекла",
    "top_glass_platform_min_confidence": "Мин. уверенность платформы для стекла",
    "top_glass_case_min_confidence": "Мин. уверенность внешней области корпуса",
    "top_glass_case_central_min_confidence": "Мин. уверенность центральной области корпуса",
    "top_glass_pin_min_confidence": "Мин. уверенность штифтов",
}
SUFFIX_LABELS = {
    "min_confidence": "Мин. уверенность", "expected_count": "Ожидаемое количество, шт.",
    "top_px_min": "T: мин., px", "top_px_max": "T: макс., px",
    "bottom_px_min": "B: мин., px", "bottom_px_max": "B: макс., px",
    "center_zone_ratio": "Ширина центральной зоны, доля", "overlap_min_px": "Мин. число общих пикселей, px",
    "damper_open_max_px": "Макс. открытие заслонки, px", "gap_dev_max_px": "Макс. разброс расстояний, px",
    "inscribed_rect_width_px": "Ширина вписываемого прямоугольника, px",
    "inscribed_rect_height_px": "Высота вписываемого прямоугольника, px",
    "y_filter_ratio": "Допуск фильтра по Y, доля", "area_absolute_min": "Мин. площадь, px²",
    "allowed_thickness_px": "Допустимая толщина, px", "excess_component_min_px": "Мин. размер компоненты, px",
    "top_line_max_residual_px": "Макс. остаточное отклонение линии, px",
    "edge_distance_deviation_ratio": "Допуск разброса до края, доля",
    "side_rect_width_px": "Эталон L/R: ширина, px", "side_rect_height_px": "Эталон L/R: высота, px",
    "edge_rect_width_px": "Эталон T/B: ширина, px", "edge_rect_height_px": "Эталон T/B: высота, px",
    "contact_inner_ratio": "Положение опорной точки контакта (0…1)",
    "margin_px": "Внешний отступ, px", "expand_x_ratio": "Масштаб по X", "expand_y_ratio": "Масштаб по Y",
}
FIXED_VALUES = {"spider_contacts_short_expected_count": 2, "top_contacts_expected_count": 14}
DISPLAY_ORDER = tuple(PARAM_LABELS)
_DISPLAY_INDEX = {k: i for i, k in enumerate(DISPLAY_ORDER)}


def _finite_number(value):
    return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(float(value))


class ThresholdLoader:
    REQUIRED_KEYS = tuple(
        f"{role}.{name}"
        for role in ROLE_SECTIONS
        for name in PARAM_LABELS
        if (role.startswith("INPUT_") and name.startswith("input_"))
        or (role in ("SPIDER_LEFT", "SPIDER_RIGHT") and name.startswith(("spider_contacts_long", "spider_long_omission")))
        or (role in ("SPIDER_IN", "SPIDER_OUT") and name.startswith(("spider_contacts_short", "spider_short_omission")))
        or (role == "TOP" and name.startswith("top_"))
    )

    def __init__(self, path="thresholds.json"):
        self.path = path
        self.labels = {}
        self.thresholds = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            raise RuntimeError(f"Файл не найден: {self.path}")
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ошибка чтения {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("thresholds.json должен содержать объект")
        data, labels = self._flatten_sections(raw)
        self.labels = labels
        self.validate(data, labels)
        return data

    @classmethod
    def validate(cls, data, labels=None):
        if not isinstance(data, dict):
            raise ValueError("Пороги должны быть объектом")
        for key in cls.REQUIRED_KEYS:
            if key not in data:
                raise ValueError(f"Отсутствует ключ в thresholds.json: {key}")

        for key, value in data.items():
            if key == "disabled_rules":
                continue
            if not _finite_number(value):
                raise ValueError(f"{key} должен быть конечным числом")
            name = key.split(".", 1)[1] if "." in key else key
            v = float(value)
            if name.endswith("_min_confidence") and not 0 <= v <= 1:
                raise ValueError(f"{key} должен быть 0..1")
            if name.endswith("_ratio") and not name.endswith(("_expand_x_ratio", "_expand_y_ratio")):
                if v < 0:
                    raise ValueError(f"{key} должен быть >= 0")
                if not name.endswith(("_y_filter_ratio", "_deviation_ratio")) and v > 1:
                    raise ValueError(f"{key} должен быть 0..1")
            if name.endswith("_expected_count"):
                if type(value) is not int or value <= 0:
                    raise ValueError(f"{key} должен быть целым > 0")
                if name == "spider_contacts_short_expected_count" and value != 2:
                    raise ValueError(f"{key} должен быть равен 2")
                if name == "top_contacts_expected_count" and value != 14:
                    raise ValueError(f"{key} должен быть равен 14")
            if name.endswith(("false_positive_max_count", "excess_component_min_px", "overlap_min_px", "area_absolute_min")):
                if type(value) is not int or value < 0:
                    raise ValueError(f"{key} должен быть неотрицательным целым")
            if name.endswith(("excess_component_min_px", "overlap_min_px")) and value < 1:
                raise ValueError(f"{key} должен быть >= 1")
            if "inscribed_rect_" in name and v <= 0:
                raise ValueError(f"{key} должен быть > 0")
            if not name.endswith("_margin_px") and v < 0 and not name.endswith("confidence"):
                raise ValueError(f"{key} должен быть >= 0")

        # Pairwise ranges for INPUT windows.
        for role in ("INPUT_LEFT", "INPUT_RIGHT"):
            for axis in ("top", "bottom"):
                mn = data[f"{role}.input_window_geometry_{axis}_px_min"]
                mx = data[f"{role}.input_window_geometry_{axis}_px_max"]
                if mn > mx:
                    raise ValueError(f"{role}: {axis}_px_min не может превышать {axis}_px_max")
        if data.get("top_platform_overlap_expand_x_ratio", 1) <= 0 or data.get("top_platform_overlap_expand_y_ratio", 1) <= 0:
            raise ValueError("top_platform_overlap_expand_*_ratio должны быть > 0")

        disabled = data.get("disabled_rules", [])
        if not isinstance(disabled, list) or any(not isinstance(x, str) for x in disabled):
            raise ValueError("disabled_rules должен быть списком строк")
        if "part_presence" in disabled:
            raise ValueError("part_presence нельзя отключать")
        if labels is not None:
            if not isinstance(labels, dict) or any(not isinstance(k, str) or not str(v).strip() for k, v in labels.items()):
                raise ValueError("Названия порогов должны быть непустыми строками")

    @staticmethod
    def _flatten_sections(raw):
        flat, labels = {}, {}
        for key, value in raw.items():
            if str(key).startswith("_comment"):
                continue
            if key not in ROLE_SECTIONS:
                flat[key] = value
                continue
            if not isinstance(value, dict):
                raise ValueError(f"Секция {key} должна быть объектом")
            for pkey, pval in value.items():
                if str(pkey).startswith("_comment"):
                    continue
                if str(pkey).startswith("_label."):
                    if isinstance(pval, str) and pval.strip():
                        labels[f"{key}.{str(pkey)[7:]}"] = pval.strip()
                    continue
                if isinstance(pval, (dict, list)):
                    raise ValueError(f"{key}.{pkey} должен быть простым значением")
                flat[f"{key}.{pkey}"] = pval
        return flat, labels

    @staticmethod
    def save_file(path, data, labels=None):
        grouped = {}
        for key, value in data.items():
            if key == "disabled_rules":
                continue
            role, dot, param = key.partition(".")
            if dot and role in ROLE_SECTIONS:
                grouped.setdefault(role, {})[param] = value
            else:
                grouped[key] = value
        lines = ["{"]
        ordered = [r for r in ROLE_SECTIONS if r in grouped] + [k for k in grouped if k not in ROLE_SECTIONS]
        last = len(ordered) - 1
        has_disabled = "disabled_rules" in data
        for i, role in enumerate(ordered):
            if i:
                lines.append("")
            params = grouped[role]
            comma = "," if i < last or has_disabled else ""
            if isinstance(params, dict):
                lines.append(f'    "{role}": {{')
                role_labels = sorted(k[len(role)+1:] for k in (labels or {}) if k.startswith(role + "."))
                entries = [(False, p) for p in params] + [(True, p) for p in role_labels]
                for j, (is_label, p) in enumerate(entries):
                    c = "," if j < len(entries)-1 else ""
                    if is_label:
                        lines.append(f'        "_label.{p}": {json.dumps((labels or {})[role+"."+p], ensure_ascii=False)}{c}')
                    else:
                        lines.append(f'        "{p}": {json.dumps(params[p], ensure_ascii=False)}{c}')
                lines.append("    }" + comma)
            else:
                lines.append(f'    {json.dumps(role, ensure_ascii=False)}: {json.dumps(params, ensure_ascii=False)}{comma}')
        if has_disabled:
            lines.append("")
            lines.append(f'    "disabled_rules": {json.dumps(data["disabled_rules"], ensure_ascii=False)}')
        lines.append("}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def get_all(self):
        return self.thresholds


def _param_meta(key, value):
    label = PARAM_LABELS.get(key) or next((v for s, v in sorted(SUFFIX_LABELS.items(), key=lambda x: -len(x[0])) if key.endswith(s)), key)
    meta = {"key": key, "label": label, "description": "", "value": value}
    if key in FIXED_VALUES:
        meta.update({"step": 1, "min": FIXED_VALUES[key], "max": FIXED_VALUES[key], "readonly": True})
        return meta
    if key.endswith("expected_count"):
        meta.update({"step": 1, "min": 2 if key.endswith("long_expected_count") else 1})
    elif key.endswith(("false_positive_max_count", "excess_component_min_px", "overlap_min_px", "area_absolute_min")):
        meta.update({"step": 1, "min": 0 if key.endswith("area_absolute_min") else 1 if "min_px" in key else 0})
    elif key.endswith("min_confidence"):
        meta.update({"step": 0.01, "min": 0, "max": 1})
    elif key.endswith(("center_zone_ratio", "inlier_ratio", "inner_ratio")):
        meta.update({"step": 0.01, "min": 0.01, "max": 1})
    elif key.endswith(("expand_x_ratio", "expand_y_ratio")):
        meta.update({"step": 0.05, "min": 0.01})
    elif key.endswith("y_filter_ratio"):
        meta.update({"step": 0.1, "min": 0})
    elif key.endswith("ratio"):
        meta.update({"step": 0.01, "min": 0})
    elif key.endswith("margin_px"):
        meta.update({"step": 0.1})
    elif "inscribed_rect_" in key:
        meta.update({"step": 0.1, "min": 0.1})
    else:
        meta.update({"step": 0.1, "min": 0})
    return meta


def describe_role_parameters(role, thresholds):
    prefix = role + "."
    params = [(k[len(prefix):], v) for k, v in thresholds.items() if k.startswith(prefix)]
    groups, matched = [], set()
    for rule_id, label, prefixes in _RULE_GROUPS_SORTED:
        gp = [(n, v) for n, v in params if n not in matched and any(n.startswith(p) for p in prefixes)]
        if gp:
            gp.sort(key=lambda x: (_DISPLAY_INDEX.get(x[0], len(DISPLAY_ORDER)), x[0]))
            groups.append({"rule": rule_id, "label": label, "params": [_param_meta(n, v) for n, v in gp]})
            matched.update(n for n, _ in gp)
    groups.sort(key=lambda g: _RULE_GROUP_INDEX.get(g["rule"], len(_RULE_GROUP_INDEX)))
    leftovers = [(n, v) for n, v in params if n not in matched]
    if leftovers:
        leftovers.sort()
        groups.append({"rule": "other", "label": "ПРОЧИЕ ПОРОГИ", "params": [_param_meta(n, v) for n, v in leftovers]})
    return groups
