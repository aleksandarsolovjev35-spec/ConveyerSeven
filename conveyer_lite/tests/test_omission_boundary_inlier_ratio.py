"""Проверка критерия валидности верхней линии omission по доле точек.

Раньше линия объявлялась невалидной по ХУДШЕЙ точке кромки:
любой одиночный зубец растеризованной маски (обычный шум сегментации,
±3-4 px между кадрами) заваливал весь замер — правило выдавало
«NO VALID OMISSION» при визуально нормальной области.

Теперь проверяется ОБЩАЯ КАРТИНА: замер валиден, если доля сэмплов
верхней кромки в пределах ``top_line_max_residual_px`` от опорной линии
не ниже ``top_line_min_inlier_ratio`` (например, 9 из 10 точек). При этом
массивное искажение кромки (кривая/ступень, затрагивающая много
колонок) по-прежнему отклоняется fail-closed.
"""

import unittest

import numpy as np

from domain.defect_rules.omission_boundary import (
    OmissionBoundaryMixin,
    measure_omission_boundary,
)
from domain.defect_rules.rule_spider_long_omission import (
    SpiderLongOmissionRule,
)

KW = dict(
    allowed_thickness_px=20.0,
    excess_component_min_px=3,
    top_line_max_residual_px=3.0,
    top_line_min_inlier_ratio=0.9,
)

X0, X1 = 100, 700


def _strip(top_fn, bottom_y=116.0, step=10):
    """Полигон-полоса: верхняя кромка top_fn(x), низ — горизонталь."""
    xs = np.arange(X0, X1 + 1, step, dtype=float)
    top = [(float(x), float(top_fn(x))) for x in xs]
    bot = [(float(x), bottom_y) for x in xs[::-1]]
    return np.array(top + bot, dtype=np.float32).tolist()


def _detection(mask):
    return {"class": "omission-long", "confidence": 0.9, "mask": mask}


class RatioGateTest(unittest.TestCase):
    def test_single_spike_does_not_invalidate(self):
        """Одиночный зубец 8 px: раньше max-остаток 8 > 3 валил замер."""
        def top_fn(x):
            return 100.0 if abs(x - 400.0) > 5 else 92.0
        result = measure_omission_boundary([_detection(_strip(top_fn))], **KW)
        self.assertTrue(result["valid"])
        self.assertLess(result["top_line_actual_inlier_ratio"], 1.0)
        self.assertGreaterEqual(
            result["top_line_actual_inlier_ratio"],
            KW["top_line_min_inlier_ratio"],
        )
        self.assertGreater(result["top_line_actual_max_residual_px"], 3.0)

    def test_edge_noise_band_is_stable(self):
        """Равномерный шум кромки ±3.5 px (рябь сегментации) — валиден."""
        rng = np.random.default_rng(21)
        noise = {x: rng.uniform(-3.5, 3.5) for x in range(X0, X1 + 1, 10)}
        result = measure_omission_boundary(
            [_detection(_strip(lambda x: 100.0 + noise[x]))], **KW,
        )
        self.assertTrue(result["valid"])
        self.assertFalse(result["confirmed_components"] > 0)

    def test_broad_distortion_still_fails_closed(self):
        """Дуга 7 px: большая часть кромки вне допуска — невалидно."""
        def top_fn(x):
            return 100.0 - 7.0 * np.sin((x - X0) / (X1 - X0) * np.pi)
        result = measure_omission_boundary([_detection(_strip(top_fn))], **KW)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "top_line_residual_too_large")

    def test_stricter_ratio_restores_old_strictness(self):
        """При ratio=1.0 критерий эквивалентен старому (все точки у линии)."""
        def top_fn(x):
            return 100.0 if abs(x - 400.0) > 5 else 92.0
        result = measure_omission_boundary(
            [_detection(_strip(top_fn))],
            **{**KW, "top_line_min_inlier_ratio": 1.0},
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "top_line_residual_too_large")


class ThresholdsWiringTest(unittest.TestCase):
    def _rule(self, ratio=0.9):
        thresholds = {
            "SPIDER_LEFT.spider_long_omission_min_confidence": 0.1,
            "SPIDER_LEFT.spider_long_omission_allowed_thickness_px": 20.0,
            "SPIDER_LEFT.spider_long_omission_excess_component_min_px": 3,
            "SPIDER_LEFT.spider_long_omission_top_line_max_residual_px": 3,
            "SPIDER_LEFT.spider_long_omission_top_line_min_inlier_ratio": ratio,
            "SPIDER_RIGHT.spider_long_omission_min_confidence": 0.1,
            "SPIDER_RIGHT.spider_long_omission_allowed_thickness_px": 20.0,
            "SPIDER_RIGHT.spider_long_omission_excess_component_min_px": 3,
            "SPIDER_RIGHT.spider_long_omission_top_line_max_residual_px": 3,
            "SPIDER_RIGHT.spider_long_omission_top_line_min_inlier_ratio": ratio,
        }
        return SpiderLongOmissionRule(thresholds=thresholds)

    def test_rule_passes_with_spike(self):
        def top_fn(x):
            return 100.0 if abs(x - 400.0) > 5 else 92.0
        result = self._rule().check(
            {"SPIDER_LEFT": [_detection(_strip(top_fn))]}
        )
        role = result.details["per_role"]["SPIDER_LEFT"]
        self.assertTrue(role["valid"])
        self.assertFalse(result.triggered)
        self.assertEqual(role["top_line_min_inlier_ratio"], 0.9)
        self.assertIsNotNone(role["top_line_actual_inlier_ratio"])

    def test_rule_rejects_bad_ratio(self):
        for bad in (0.0, -0.5, 1.5, "0.9"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self._rule(bad).check({"SPIDER_LEFT": []})

    def test_rule_requires_ratio_key(self):
        rule = self._rule()
        for key in list(rule.thresholds):
            if key.endswith("top_line_min_inlier_ratio"):
                del rule.thresholds[key]
        with self.assertRaises(ValueError):
            rule.check({"SPIDER_LEFT": []})


class AllSamplePointsReturnedTest(unittest.TestCase):
    def test_fit_returns_full_edge_trace(self):
        from domain.defect_rules.omission_reference import (
            fit_omission_top_line,
        )
        mask = _strip(lambda x: 100.0)
        reference = fit_omission_top_line(
            [_detection(mask)], x_start=X0, x_end=X1,
        )
        self.assertIsNotNone(reference)
        self.assertIn("all_sample_points", reference)
        self.assertGreaterEqual(
            len(reference["all_sample_points"]),
            len(reference["sample_points"]),
        )


if __name__ == "__main__":
    unittest.main()
