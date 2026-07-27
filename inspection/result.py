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

    @property
    def has_defects(self) -> bool:
        return bool(self.defects)
