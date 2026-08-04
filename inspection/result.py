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

    # True устанавливается только для INPUT после majority part_presence.
    is_empty_tray: bool = False

    # Production-метаданные строгого голосования 2 из 3. Для одиночной
    # диагностики/offline-анализа остаются пустыми.
    consensus: dict = field(default_factory=dict)
    model_health: list = field(default_factory=list)

    # Все три набора кадров стадии (по одному на прогон): UI может показать
    # на главной камере любой из трёх прогонов по клику. Каждый элемент —
    # dict {role: кадр}; только roles этой стадии (INPUT или SPIDER/TOP).
    run_frames: list = field(default_factory=list)

    # Правила, посчитанные по каждому прогону (до majority-слияния): кадр
    # run=N размечается drawings именно этого прогона, чтобы оверлей
    # совпадал с кадром. Каждый элемент — список RuleResult'ов прогона.
    run_rule_results: list = field(default_factory=list)

    # Детекции моделей по каждому прогону (list из 3 dict-ов)
    run_vision_results: list = field(default_factory=list)
