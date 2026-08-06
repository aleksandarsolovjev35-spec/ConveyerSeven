"""Проверка ровности контактов по схеме «дымоход-заслонка».

Раньше ровность мерялась отклонением центров от линии, проведённой
наименьшими квадратами по тем же центрам: сдвинутый контакт двигал
линию к себе, сдвиг «размазывался» по всему ряду (краевой контакт на
16 px был невидим, средний ловился только от ~10 px).

Теперь (схема оператора): опорная линия строится по верху полосы
omission, от неё опускаются перпендикуляры («стены») до центров
вписанных эталонов, а линия через центры — «заслонка». Каждая стена
измеряется независимо, поэтому торчащий контакт виден в полный рост,
а наклон всей детали на вердикт не влияет (дымоход наклоняется вместе
с деталью).
"""

import unittest

import numpy as np

from domain.defect_rules.rule_spider_contacts_long import SpiderContactsLongRule

TOP_Y = 300.0


def _contact_mask(cx, cy, w=44.0, h=22.0, step=4.0):
    x1, x2 = cx - w / 2, cx + w / 2
    y1, y2 = cy - h / 2, cy + h / 2
    pts = (
        [(float(x), y1) for x in np.arange(x1, x2 + 0.1, step)]
        + [(x2, float(y)) for y in np.arange(y1 + step, y2 + 0.1, step)]
        + [(float(x), y2) for x in np.arange(x2, x1 - 0.1, -step)]
        + [(x1, float(y)) for y in np.arange(y2 - step, y1 - 0.1, -step)]
    )
    return np.array(pts, dtype=np.float32).tolist()


def _omission_strip(x1, x2, y_top, thickness=14.0, tilt=0.0):
    return [
        [float(x1), float(y_top)],
        [float(x2), float(y_top + tilt)],
        [float(x2), float(y_top + tilt + thickness)],
        [float(x1), float(y_top + thickness)],
    ]


def _det(cls, mask, conf=0.9):
    arr = np.asarray(mask, dtype=np.float64)
    return {
        "class": cls,
        "confidence": conf,
        "bbox": [
            float(arr[:, 0].min()), float(arr[:, 1].min()),
            float(arr[:, 0].max()), float(arr[:, 1].max()),
        ],
        "mask": mask,
    }


def _long_thresholds(damper=5.0, gap=4.0):
    thresholds = {}
    for role in ("SPIDER_LEFT", "SPIDER_RIGHT"):
        thresholds[f"{role}.spider_contacts_long_min_confidence"] = 0.3
        thresholds[f"{role}.spider_contacts_long_expected_count"] = 5
        thresholds[f"{role}.spider_contacts_long_damper_open_max_px"] = damper
        thresholds[f"{role}.spider_contacts_long_gap_dev_max_px"] = gap
        thresholds[f"{role}.spider_contacts_long_inscribed_rect_width_px"] = 38
        thresholds[f"{role}.spider_contacts_long_inscribed_rect_height_px"] = 18
        thresholds[f"{role}.spider_contacts_long_y_filter_ratio"] = 3
        thresholds[f"{role}.spider_long_omission_min_confidence"] = 0.3
    return thresholds


def _long_vision(shifts, xs=(200, 400, 600, 800, 1000)):
    """5 контактов в ряд; shifts[i] — вертикальный сдвиг i-го контакта.

    Полоса omission строится горизонтально над самым высоким контактом.
    """
    detections = []
    for x, shift in zip(xs, shifts, strict=True):
        detections.append(
            _det("contacts-long", _contact_mask(float(x), TOP_Y + shift))
        )
    om_y = min(TOP_Y + shift for shift in shifts) - 44.0
    detections.append(_det("omission-long", _omission_strip(190, 1010, om_y)))
    return {"SPIDER_LEFT": detections}


class LongDamperTest(unittest.TestCase):
    def setUp(self):
        self.rule = SpiderContactsLongRule(thresholds=_long_thresholds())

    def _role(self, vision):
        return (
            self.rule.check(vision).details["per_role"]["SPIDER_LEFT"]
        )

    def test_perfect_row_passes(self):
        role = self._role(_long_vision([0, 0, 0, 0, 0]))
        self.assertFalse(role["triggered"])
        self.assertEqual(role["reason"], None)
        self.assertEqual(role["damper_open_px"], 0.0)
        self.assertEqual(role["gap_dev_px"], 0.0)

    def test_edge_contact_full_shift_visible(self):
        """Старый код: крайний +16 px был невидим (0.4×16=6.4<7.7)."""
        for pos in (0, 4):
            with self.subTest(position=pos):
                shifts = [0, 0, 0, 0, 0]
                shifts[pos] = 16
                role = self._role(_long_vision(shifts))
                self.assertTrue(role["triggered"])
                self.assertTrue(role["gap_fail"])
                deviations = [g["deviation_px"] for g in role["gaps"]]
                # Видно именно виновный контакт, соседи чистые.
                self.assertAlmostEqual(deviations[pos], 16.0, places=1)
                for index, dev in enumerate(deviations):
                    if index != pos:
                        self.assertAlmostEqual(dev, 0.0, places=1)

    def test_middle_contact_caught_from_gap_threshold(self):
        """Старый код: средний +6 px проходил (0.8×6=4.8<7.7)."""
        role = self._role(_long_vision([0, 0, 6, 0, 0]))
        self.assertTrue(role["triggered"])
        self.assertAlmostEqual(role["gap_dev_px"], 6.0, places=1)

    def test_small_shift_within_gap_threshold_passes(self):
        role = self._role(_long_vision([0, 0, 3, 0, 0]))
        self.assertFalse(role["triggered"])

    def test_whole_part_tilt_ignored(self):
        """Контакты наклонены 24 px/800 px, полоса omission — так же.

        Дымоход наклонён вместе с деталью, заслонка закрыта.
        """
        xs = (200, 400, 600, 800, 1000)
        shifts = [(x - 200) * 24.0 / 800.0 for x in xs]
        vision = _long_vision(shifts)
        # наклоним и полосу так же: +24 px слева направо
        vision["SPIDER_LEFT"][-1]["mask"] = _omission_strip(
            190, 1010, TOP_Y - 44.0, tilt=24.0,
        )
        role = self._role(vision)
        self.assertFalse(role["triggered"])
        self.assertLessEqual(
            role["damper_open_px"], role["damper_open_max_px"],
        )

    def test_missing_omission_fails_closed(self):
        vision = _long_vision([0, 0, 0, 0, 0])
        vision["SPIDER_LEFT"] = vision["SPIDER_LEFT"][:-1]  # без omission
        role = self._role(vision)
        self.assertTrue(role["triggered"])
        self.assertEqual(role["reason"], "no_valid_omission_top_line")
        self.assertIsNone(role["damper_open_px"])

    def test_short_omission_reference_fails_closed(self):
        """Полоса покрывает ~20% ряда — экстраполяция запрещена."""
        vision = _long_vision([0, 0, 0, 0, 0])
        vision["SPIDER_LEFT"][-1]["mask"] = _omission_strip(190, 350, 256)
        role = self._role(vision)
        self.assertTrue(role["triggered"])
        self.assertEqual(role["reason"], "omission_reference_too_short")

    def test_requires_positive_thresholds(self):
        rule = SpiderContactsLongRule(
            thresholds={**_long_thresholds(damper=0.0)}
        )
        with self.assertRaises(ValueError):
            rule.check(_long_vision([0, 0, 0, 0, 0]))


def _short_thresholds(damper=6.0):
    thresholds = {}
    for role in ("SPIDER_IN", "SPIDER_OUT"):
        thresholds[f"{role}.spider_contacts_short_min_confidence"] = 0.2
        thresholds[f"{role}.spider_contacts_short_expected_count"] = 2
        thresholds[f"{role}.spider_contacts_short_damper_open_max_px"] = damper
        thresholds[f"{role}.spider_contacts_short_inscribed_rect_width_px"] = 30
        thresholds[f"{role}.spider_contacts_short_inscribed_rect_height_px"] = 18
        thresholds[f"{role}.spider_contacts_short_area_absolute_min"] = 400
        thresholds[f"{role}.spider_contacts_short_y_filter_ratio"] = 3
        thresholds[f"{role}.spider_short_omission_min_confidence"] = 0.3
    return thresholds


def _short_vision(dy_b, om_tilt=0.0):
    contacts = [
        _det("flatness_short", _contact_mask(300.0, TOP_Y, w=34.0)),
        _det("flatness_short", _contact_mask(700.0, TOP_Y + dy_b, w=34.0)),
    ]
    om_y = TOP_Y - 44.0 + min(0.0, dy_b)
    contacts.append(
        _det("omission-short", _omission_strip(280, 720, om_y, tilt=om_tilt))
    )
    return {"SPIDER_IN": contacts}


class ShortDamperTest(unittest.TestCase):
    def setUp(self):
        from domain.defect_rules.rule_spider_contacts_short import (
            SpiderContactsShortRule,
        )
        self.rule = SpiderContactsShortRule(thresholds=_short_thresholds())

    def _role(self, vision):
        return self.rule.check(vision).details["per_role"]["SPIDER_IN"]

    def test_level_pair_passes(self):
        role = self._role(_short_vision(0.0))
        self.assertFalse(role["triggered"])
        self.assertEqual(role["damper_open_px"], 0.0)

    def test_open_damper_below_threshold_passes(self):
        role = self._role(_short_vision(4.0))
        self.assertFalse(role["triggered"])
        self.assertAlmostEqual(role["damper_open_px"], 4.0, places=1)

    def test_open_damper_fails(self):
        role = self._role(_short_vision(8.0))
        self.assertTrue(role["triggered"])
        self.assertTrue(role["damper_fail"])
        self.assertAlmostEqual(role["damper_open_px"], 8.0, places=1)

    def test_whole_pair_tilt_with_omission_ignored(self):
        """Оба контакта и полоса наклонены одинаково — заслонка закрыта."""
        role = self._role(_short_vision(12.0, om_tilt=12.0))
        self.assertFalse(role["triggered"])

    def test_missing_omission_fails_closed(self):
        vision = _short_vision(0.0)
        vision["SPIDER_IN"] = vision["SPIDER_IN"][:-1]
        role = self._role(vision)
        self.assertTrue(role["triggered"])
        self.assertEqual(role["reason"], "no_valid_omission_top_line")


if __name__ == "__main__":
    unittest.main()
