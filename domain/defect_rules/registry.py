"""
Единый источник истины по правилам дефектов.

До этого карты правил жили в трёх местах:
- RULE_THRESHOLD_GROUPS в vision/ui/server/server.py
- RULE_GROUPS в domain/threshold_loader.py
- RULE_CAMERA_ROLES, DETAILED_RULES, HUMAN_CAUSE_MAP в core/rule_report.py

Теперь каноническое описание в одном модуле — остальные импортируют отсюда.
Добавление нового правила требует правки только здесь и реализации самого правила.
"""

# rule_name (как в RuleResult) -> группа порогов (префикс в thresholds.json)
RULE_THRESHOLD_GROUPS = {
    "part_presence": "input_part_presence",
    "window_geometry": "input_window_geometry",
    "window_sinks": "input_window_sinks",
    "contacts_long": "spider_contacts_long",
    "contacts_short": "spider_contacts_short",
    "long_omission": "spider_long_omission",
    "short_omission": "spider_short_omission",
    "top_contacts": "top_contacts",
    "top_platform": "top_platform",
    "platform_contacts_overlap": "top_platform_overlap",
    "sinks": "top_sinks",
    "glass": "top_glass",
    "glass_on_contacts": "top_glass",  # использует те же пороги, что и glass
}

# rule_name -> кортеж камер, для которых правило имеет смысл
RULE_CAMERA_ROLES = {
    "part_presence": ("INPUT_LEFT", "INPUT_RIGHT"),
    "window_geometry": ("INPUT_LEFT", "INPUT_RIGHT"),
    "window_sinks": ("INPUT_LEFT", "INPUT_RIGHT"),
    "contacts_long": ("SPIDER_LEFT", "SPIDER_RIGHT"),
    "long_omission": ("SPIDER_LEFT", "SPIDER_RIGHT"),
    "contacts_short": ("SPIDER_IN", "SPIDER_OUT"),
    "short_omission": ("SPIDER_IN", "SPIDER_OUT"),
    "top_contacts": ("TOP",),
    "top_platform": ("TOP",),
    "platform_contacts_overlap": ("TOP",),
    "sinks": ("TOP",),
    "glass": ("TOP",),
    "glass_on_contacts": ("TOP",),
}

# Правила с развёрнутой построчной телеметрией
DETAILED_RULES = (
    "window_geometry",
    "contacts_long",
    "contacts_short",
    "top_contacts",
    "top_platform",
    "platform_contacts_overlap",
    "long_omission",
    "short_omission",
)

# Человекочитаемые причины для оператора
HUMAN_CAUSE_MAP = {
    ("window_geometry", True): "НЕПРАВИЛЬНАЯ ГЕОМЕТРИЯ ОКОН",
    ("window_sinks", True): "РАКОВИНА В ОКНЕ",
    ("contacts_long", True): "НАКЛОН / СМЕЩЕНИЕ ДЛИННЫХ КОНТАКТОВ",
    ("contacts_short", True): "НАКЛОН / СМЕЩЕНИЕ КОРОТКИХ КОНТАКТОВ",
    ("long_omission", True): "ИЗБЫТОЧНАЯ ТОЛЩИНА ДЛИННОЙ ПОЛОСЫ ПРОПУСКА",
    ("short_omission", True): "ИЗБЫТОЧНАЯ ТОЛЩИНА КОРОТКОЙ ПОЛОСЫ ПРОПУСКА",
    ("top_contacts", True): "СМЕЩЕНИЕ КОНТАКТОВ НА ПЛАТФОРМЕ",
    ("top_platform", True): "ПЛАТФОРМА НЕ ВПИСАЛАСЬ",
    ("platform_contacts_overlap", True): "ЗАПЛЫВ ПЛАТФОРМЫ",
    ("sinks", True): "РАКОВИНА ВНУТРИ КОРПУСА",
    ("glass", True): "СТЕКЛО НА ПЛАТФОРМЕ / ШТИФТАХ",
    ("glass_on_contacts", True): "СТЕКЛО НА КОНТАКТАХ",
}

# Соответствие группы порогов -> список префиксов параметров
# Используется ThresholdLoader для группировки в UI редактора.
THRESHOLD_GROUP_PREFIXES = {
    "input_part_presence": ("input_part_presence_",),
    "input_window_geometry": ("input_window_geometry_",),
    "input_window_sinks": ("input_window_sinks_",),
    "spider_contacts_long": ("spider_contacts_long_",),
    "spider_long_omission": ("spider_long_omission_",),
    "spider_contacts_short": ("spider_contacts_short_",),
    "spider_short_omission": ("spider_short_omission_",),
    "top_contacts": ("top_contacts_",),
    "top_platform_overlap": ("top_platform_overlap_",),
    "top_platform": ("top_platform_",),
    "top_sinks": ("top_sinks_",),
    "top_glass": ("top_glass_",),
}

# UI-метки групп порогов (как в threshold_loader)
THRESHOLD_GROUP_LABELS = {
    "input_part_presence": "НАЛИЧИЕ ДЕТАЛИ",
    "input_window_geometry": "ГЕОМЕТРИЯ ВХОДНОГО ОКНА",
    "input_window_sinks": "РАКОВИНЫ В ОКНАХ",
    "spider_contacts_long": "КОНТАКТЫ · ДЛИННЫЕ",
    "spider_long_omission": "ПОЛОСА ПРОПУСКА · ДЛИННАЯ",
    "spider_contacts_short": "КОНТАКТЫ · КОРОТКИЕ",
    "spider_short_omission": "ПОЛОСА ПРОПУСКА · КОРОТКАЯ",
    "top_contacts": "КОНТАКТЫ СВЕРХУ",
    "top_platform_overlap": "ЗАПЛЫВ ПЛАТФОРМЫ",
    "top_platform": "ПЛАТФОРМА СВЕРХУ",
    "top_sinks": "РАКОВИНЫ КОРПУСА",
    "top_glass": "СТЕКЛО СВЕРХУ",
}
