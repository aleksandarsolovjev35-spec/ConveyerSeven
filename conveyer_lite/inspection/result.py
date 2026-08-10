from dataclasses import dataclass, field


@dataclass
class InspectionResult:
    """Результат одной стадии инспекции."""

    stage: str
    defects: list = field(default_factory=list)
    vision_results: dict = field(default_factory=dict)
    rule_results: list = field(default_factory=list)
    annotated: dict = field(default_factory=dict)
    raw_frames: dict = field(default_factory=dict)
    raw_overlay_frames: dict = field(default_factory=dict)

    # True устанавливается только для INPUT по part_presence.
    is_empty_tray: bool = False

    # Production-метаданные прогона. Для одиночной диагностики/offline-
    # анализа остаются пустыми.
    consensus: dict = field(default_factory=dict)
    model_health: list = field(default_factory=list)

    # Набор кадров стадии (один элемент): dict {role: кадр}; только roles
    # этой стадии (INPUT или SPIDER/TOP).
    run_frames: list = field(default_factory=list)

    # Правила стадии: кадр размечается drawings этих правил, чтобы оверлей
    # совпадал с кадром. Один элемент — список RuleResult'ов.
    run_rule_results: list = field(default_factory=list)

    # Детекции моделей по каждому прогону
    run_vision_results: list = field(default_factory=list)
