CATEGORY_GOOD    = "GOOD"
CATEGORY_BAD     = "BAD"
CATEGORY_CLEANUP = "CLEANUP"
CATEGORY_UNKNOWN = "UNKNOWN"

# Дефекты которые приводят на CLEANUP (если других дефектов нет)
CLEANUP_DEFECTS = {
    "glass",
    "glass_glare",
}


class Part:

    def __init__(self, part_id: int, step_created: int):
        self.id = part_id
        self.step_created = step_created

        self.input_defects: list = []
        self.spider_defects: list = []

        self.input_inspected: bool = False
        self.spider_inspected: bool = False

        self.final_decision: str = "none"
        self.route_category: str = CATEGORY_UNKNOWN

        # Аудит инспекции по стадиям input/spider.
        self.inspection_consensus: dict = {}

    def add_input_defect(self, defect: str):
        if defect:
            self.input_defects.append(defect)

    def add_spider_defect(self, defect: str):
        if defect:
            self.spider_defects.append(defect)

    def mark_input_done(self):
        """Зафиксировать завершение входной инспекции."""
        self.input_inspected = True
        self._recompute()

    def mark_spider_done(self):
        """Зафиксировать завершение spider-инспекции."""
        self.spider_inspected = True
        self._recompute()

    def get_all_defects(self) -> list:
        return self.input_defects + self.spider_defects

    @property
    def fully_inspected(self) -> bool:
        return self.input_inspected and self.spider_inspected

    def _recompute(self):
        """
        Пересчитать категорию и финальный дефект.
        Вызывается после каждой стадии инспекции.
        """
        defects = self.get_all_defects()

        if not defects:
            if self.fully_inspected:
                self.final_decision = "none"
                self.route_category = CATEGORY_GOOD
            else:
                self.final_decision = "none"
                self.route_category = CATEGORY_UNKNOWN
            return

        only_cleanup = all(d in CLEANUP_DEFECTS for d in defects)

        if only_cleanup:
            self.final_decision = defects[-1]
            self.route_category = CATEGORY_CLEANUP
            return

        for defect in defects:
            if defect not in CLEANUP_DEFECTS:
                self.final_decision = defect
                break
        else:
            self.final_decision = defects[-1]

        self.route_category = CATEGORY_BAD

    def __repr__(self):
        return (
            f"<Part #{self.id} "
            f"step={self.step_created} "
            f"category={self.route_category} "
            f"inspected={'full' if self.fully_inspected else 'partial'} "
            f"defects={self.get_all_defects()}>"
        )