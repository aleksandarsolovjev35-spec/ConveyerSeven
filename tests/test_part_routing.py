"""Логика маршрутизации Part: категория пересчитывается после каждой стадии.

Категория не должна становиться окончательной, пока не пройдены обе стадии
инспекции: на ней строится вся механика распределителя.
"""

from domain.part import (
    CATEGORY_BAD,
    CATEGORY_CLEANUP,
    CATEGORY_GOOD,
    CATEGORY_UNKNOWN,
    CLEANUP_DEFECTS,
    Part,
)


def test_cleanup_дефекты_это_ровно_стекло():
    # От этого множества зависит маршрут CLEANUP; расширение — осознанное
    # решение, а не побочный эффект правки.
    assert CLEANUP_DEFECTS == {"glass"}


def test_до_завершения_стадий_категория_unknown():
    part = Part(1, 0)
    assert part.route_category == CATEGORY_UNKNOWN
    part.mark_input_done()
    # Без дефектов, но spider ещё не пройден — решения нет.
    assert part.route_category == CATEGORY_UNKNOWN
    assert part.final_decision == "none"
    assert not part.fully_inspected


def test_без_дефектов_после_обеих_стадий_good():
    part = Part(1, 0)
    part.mark_input_done()
    part.mark_spider_done()
    assert part.fully_inspected
    assert part.route_category == CATEGORY_GOOD
    assert part.final_decision == "none"


def test_только_стекло_как_на_входе_так_и_на_spider_даёт_cleanup():
    part = Part(1, 0)
    part.add_input_defect("glass")
    part.mark_input_done()
    assert part.route_category == CATEGORY_CLEANUP
    assert part.final_decision == "glass"

    # Второй glass не меняет маршрут.
    part2 = Part(2, 0)
    part2.add_input_defect("glass")
    part2.add_spider_defect("glass")
    part2.mark_input_done()
    part2.mark_spider_done()
    assert part2.route_category == CATEGORY_CLEANUP


def test_стекло_плюс_любой_другой_дефект_это_bad():
    part = Part(1, 0)
    part.add_input_defect("glass")
    part.mark_input_done()
    part.add_spider_defect("window_geometry")
    part.mark_spider_done()
    assert part.route_category == CATEGORY_BAD
    # Финальным дефектом становится первый НЕ cleanup-дефект.
    assert part.final_decision == "window_geometry"


def test_порядок_дефектов_не_меняет_магистральный_маршрут():
    part = Part(1, 0)
    part.add_input_defect("top_contacts")
    part.add_spider_defect("glass")
    part.mark_input_done()
    part.mark_spider_done()
    assert part.route_category == CATEGORY_BAD
    assert part.final_decision == "top_contacts"


def test_пустые_дефекты_отбрасываются():
    part = Part(1, 0)
    part.add_input_defect("")
    part.add_input_defect(None)
    part.mark_input_done()
    part.mark_spider_done()
    assert part.get_all_defects() == []
    assert part.route_category == CATEGORY_GOOD


def test_дефекты_стадий_суммируются_для_архива():
    part = Part(1, 0)
    part.add_input_defect("window_geometry")
    part.add_spider_defect("glass")
    assert part.get_all_defects() == ["window_geometry", "glass"]
