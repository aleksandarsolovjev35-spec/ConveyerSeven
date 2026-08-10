"""Тесты domain.geometry: примитивы и подбор точек."""

import unittest

from domain.geometry.fitting import split_top_row
from domain.geometry.primitives import (
    bbox_intersect,
    bbox_intersection_rect,
    centroid_from_det,
    mask_area,
)


class PrimitivesTest(unittest.TestCase):
    def test_bbox_intersect(self):
        a = [0, 0, 10, 10]
        self.assertTrue(bbox_intersect(a, [5, 5, 15, 15]))
        self.assertFalse(bbox_intersect(a, [11, 11, 20, 20]))
        self.assertFalse(bbox_intersect(a, [10, 0, 20, 10]))  # касание — не пересечение

    def test_bbox_intersection_rect(self):
        self.assertEqual(
            bbox_intersection_rect([0, 0, 10, 10], [5, 5, 15, 15]),
            [5, 5, 10, 10],
        )
        self.assertIsNone(
            bbox_intersection_rect([0, 0, 5, 5], [10, 10, 15, 15]),
        )

    def test_centroid_from_bbox(self):
        det = {"bbox": [0, 0, 10, 20]}
        self.assertEqual(centroid_from_det(det), (5, 10))

    def test_centroid_from_mask(self):
        det = {"mask": [[0, 0], [10, 0], [0, 10]]}  # треугольник
        cx, cy = centroid_from_det(det)
        self.assertGreater(cx, 0)
        self.assertGreater(cy, 0)

    def test_centroid_missing(self):
        self.assertIsNone(centroid_from_det({}))

    def test_mask_area_from_bbox(self):
        det = {"bbox": [0, 0, 10, 20]}
        self.assertEqual(mask_area(det), 200)

    def test_mask_area_from_contour(self):
        det = {"mask": [[0, 0], [10, 0], [10, 10], [0, 10]]}
        self.assertEqual(mask_area(det), 100)


class SplitTopRowTest(unittest.TestCase):
    def test_less_than_expected_returns_all(self):
        points = [(0, 0), (10, 1), (20, 0)]
        best, rejected = split_top_row(points, expected_count=5)
        self.assertEqual(best, points)
        self.assertEqual(rejected, [])

    def test_linear_row_splits(self):
        # 6 точек на прямой: лучшая пятёрка = минимальная ошибка
        points = [(0, 0), (10, 0), (20, 0), (30, 0), (40, 0), (100, 50)]
        best, rejected = split_top_row(points, expected_count=5)
        self.assertEqual(len(best), 5)
        self.assertEqual(len(rejected), 1)
        self.assertIn((100, 50), rejected)
        self.assertNotIn((100, 50), best)

    def test_combinations_empty_on_failure(self):
        # Две точки с ожиданием 5 — нечего комбинировать
        best, rejected = split_top_row([(0, 0), (10, 10)], expected_count=5)
        self.assertEqual(best, [(0, 0), (10, 10)])


if __name__ == "__main__":
    unittest.main()
