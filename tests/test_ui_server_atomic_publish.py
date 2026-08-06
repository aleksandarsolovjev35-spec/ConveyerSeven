"""Тесты атомарной публикации снимка в UIServer.

После синхронизации «сначала монитор, потом UI» версия ``_cache_version``
(в UI — ``frame_version``) растёт только при реальном изменении
визуального содержимого. Повторные публикации тех же объектов (REVIEW /
PUBLISH) не должны перерисовывать UI и давать ложные сигналы фронтенду.
"""

import unittest

import numpy as np

from domain.defect_rules.base import RuleResult
from vision.ui.server.server import UIServer


def _frame(value=0, h=8, w=8):
    return np.full((h, w, 3), value, dtype=np.uint8)


class UIServerAtomicPublishTest(unittest.TestCase):
    def setUp(self):
        self.server = UIServer()

    def test_publish_bumps_version_once(self):
        frame = _frame()
        version = self.server._cache_version
        self.server.update(
            frames={"A": frame},
            run_frames=[{"A": frame}],
            run_rule_results=[[{"name": "x"}], [], []],
            line_status={"state": "RUNNING"},
        )
        self.assertEqual(self.server._cache_version, version + 1)
        self.assertEqual(self.server.get_frame_count(), 1)
        self.assertIs(self.server.frames["A"], frame)
        self.assertEqual(self.server.run_rule_results, [[{"name": "x"}], [], []])

    def test_same_frames_do_not_bump(self):
        frame = _frame()
        self.server.update(frames={"A": frame})
        version = self.server._cache_version
        self.server.update(frames={"A": frame})
        self.assertEqual(self.server._cache_version, version)

    def test_new_frame_bumps(self):
        frame = _frame(0)
        self.server.update(frames={"A": frame})
        version = self.server._cache_version
        self.server.update(frames={"A": _frame(255)})
        self.assertEqual(self.server._cache_version, version + 1)

    def test_equal_rule_results_do_not_bump(self):
        rule = {"name": "window_geometry", "triggered": False}
        self.server.update(rule_results=[rule])
        version = self.server._cache_version
        # Новый список, но равное содержимое — визуально ничего не изменилось
        self.server.update(rule_results=[{"name": "window_geometry",
                                          "triggered": False}])
        self.assertEqual(self.server._cache_version, version)

    def test_changed_rule_results_bump(self):
        self.server.update(rule_results=[{"name": "a", "triggered": False}])
        version = self.server._cache_version
        self.server.update(rule_results=[{"name": "a", "triggered": True}])
        self.assertEqual(self.server._cache_version, version + 1)

    def test_same_vision_results_object_do_not_bump(self):
        vision = {"A": [{"class": "flatness"}]}
        self.server.update(vision_results=vision)
        version = self.server._cache_version
        self.server.update(vision_results=vision)
        self.assertEqual(self.server._cache_version, version)

    def test_same_run_frames_do_not_bump(self):
        frame = _frame()
        run_frames = [{"A": frame}]
        run_rules = [[{"name": "x"}]]
        self.server.update(run_frames=run_frames, run_rule_results=run_rules)
        version = self.server._cache_version
        self.server.update(run_frames=run_frames, run_rule_results=run_rules)
        self.assertEqual(self.server._cache_version, version)

    def test_new_run_frames_bump_and_clear_old_rules(self):
        frame = _frame(0)
        self.server.update(
            run_frames=[{"A": frame}],
            run_rule_results=[[{"name": "old"}]],
        )
        version = self.server._cache_version
        self.server.update(
            run_frames=[{"A": _frame(255)}],
            run_rule_results=[[{"name": "new"}]],
        )
        self.assertEqual(self.server._cache_version, version + 1)
        self.assertEqual(self.server.run_rule_results, [[{"name": "new"}]])

    def test_run_frames_change_without_rules_resets_rules(self):
        frame = _frame(0)
        self.server.update(
            run_frames=[{"A": frame}],
            run_rule_results=[[{"name": "old"}]],
        )
        self.server.update(run_frames=[{"A": _frame(1)}])
        self.assertEqual(self.server.run_rule_results, [])
        self.assertEqual(self.server.get_frame_count(), 1)

    def test_clear_overlays_bumps(self):
        frame = _frame()
        self.server.update(frames={"A": frame})
        version = self.server._cache_version
        # clear_overlays() публикует пустые результаты — контент изменился
        self.server.update(vision_results={}, rule_results=[], run_frames=[])
        self.assertEqual(self.server._cache_version, version + 1)
        self.assertEqual(self.server.run_frames, [])
        self.assertEqual(self.server.run_rule_results, [])

    def test_same_run_frames_helper(self):
        a, b = _frame(1), _frame(2)
        self.assertTrue(UIServer._same_run_frames([{"A": a}], [{"A": a}]))
        self.assertFalse(UIServer._same_run_frames([{"A": a}], [{"A": b}]))
        self.assertFalse(UIServer._same_run_frames([{"A": a}], [{"A": a}, {"B": a}]))
        self.assertTrue(UIServer._same_frames({"A": a}, {"A": a}))
        self.assertFalse(UIServer._same_frames({"A": a}, {"B": b}))

    def test_review_publish_does_not_bump_with_numpy_rules(self):
        """REVIEW/PUBLISH повторно публикуют те же объекты правил — версия
        не должна расти. В details/drawings правил numpy-массивы: наивное
        ``!=`` падало бы и давало ложный бамп (перевзвод гейта UI)."""
        rule = RuleResult(
            rule_name="window_geometry",
            triggered=False,
            details={"per_role": {
                "INPUT_LEFT": {"valid": True, "threshold": np.float64(12.5)},
            }},
            drawings=[{"type": "window_geometry_item", "role": "INPUT_LEFT",
                       "pts": np.array([[1, 2], [3, 4]])}],
        )
        frame = _frame()
        vision = {"A": [{"class": "flatness"}]}
        rules = [rule]

        # Анализ — новая публикация
        self.server.update(
            frames={"A": frame},
            vision_results=vision,
            rule_results=rules,
            run_frames=[{"A": frame}],
            run_rule_results=[[rule]],
        )
        version = self.server._cache_version

        # REVIEW/PUBLISH: те же объекты кадров/правил/vision
        self.server.update(frames={"A": frame}, vision_results=vision,
                           rule_results=rules)
        self.server.update(frames={"A": frame}, vision_results=vision,
                           rule_results=rules)
        self.assertEqual(self.server._cache_version, version)

        # Реальное изменение (другой объект с triggered=True) — бамп
        changed = RuleResult(
            rule_name="window_geometry",
            triggered=True,
            details={"per_role": {
                "INPUT_LEFT": {"valid": True, "threshold": np.float64(18.0)},
            }},
            drawings=[{"type": "window_geometry_item", "role": "INPUT_LEFT",
                       "pts": np.array([[1, 2], [3, 4]])}],
        )
        self.server.update(frames={"A": frame}, vision_results=vision,
                           rule_results=[changed])
        self.assertEqual(self.server._cache_version, version + 1)

    def test_rules_equal_numpy_safe(self):
        a = RuleResult(
            "r", False,
            details={"v": np.float64(1.5)},
            drawings=[{"pts": np.array([[1, 2]])}],
        )
        b = RuleResult(
            "r", False,
            details={"v": np.float64(1.5)},
            drawings=[{"pts": np.array([[1, 2]])}],
        )
        c = RuleResult(
            "r", True,
            details={"v": np.float64(1.5)},
            drawings=[{"pts": np.array([[1, 2]])}],
        )
        self.assertTrue(UIServer._rules_equal(a, a))
        self.assertTrue(UIServer._rules_equal(a, b))
        self.assertFalse(UIServer._rules_equal(a, c))
        self.assertTrue(UIServer._rules_equal(np.float64(1.5), np.float64(1.5)))
        self.assertFalse(UIServer._rules_equal(np.array([1, 2]), np.array([1, 3])))
        self.assertTrue(UIServer._rules_equal(np.array([1, 2]), np.array([1, 2])))


if __name__ == "__main__":
    unittest.main()
