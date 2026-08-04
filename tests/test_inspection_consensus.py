import unittest

import numpy as np

from core.production_cycle import ProductionCycle
from domain.defect_rules.base import RuleResult
from inspection.consensus import (
    InspectionConsensusError,
    combine_presence_results,
    combine_rule_results,
    describe_picture_run,
    select_picture_run,
)
from inspection.inspector import Inspector


class Recorder:
    def __init__(self):
        self.calls = []

    def process(self, **kwargs):
        self.calls.append(kwargs)
        return {role: frame.copy() for role, frame in kwargs["frames"].items()}


class ScriptedVision:
    def __init__(self, counts_by_run):
        self.counts_by_run = counts_by_run
        self.last_health = []
        self.calls = 0

    @staticmethod
    def _detection(name, run_id, index=0):
        x1 = 1 + index
        return {
            "class": name,
            "confidence": 0.9,
            "bbox": [x1, 1, x1 + 4, 5],
            "mask": [
                [x1, 1], [x1 + 4, 1],
                [x1 + 4, 5], [x1, 5],
            ],
            "run_id": run_id,
        }

    def process_all(self, frames):
        self.calls += 1
        run_id = int(next(iter(frames.values()))[0, 0, 0])
        left_count, right_count = self.counts_by_run[run_id]
        self.last_health = [
            {
                "role": role,
                "model": f"weights/{role}.pt",
                "ok": True,
                "elapsed_ms": 1.0,
                "detections": 1,
                "error": None,
            }
            for role in frames
        ]
        result = {}
        for role, count in (
            ("INPUT_LEFT", left_count),
            ("INPUT_RIGHT", right_count),
        ):
            if role not in frames:
                continue
            detections = [self._detection("marker", run_id)]
            detections.extend(
                self._detection("flatness", run_id, index + 1)
                for index in range(count)
            )
            result[role] = detections
        return result


class ScriptedDecision:
    thresholds = {
        "INPUT_LEFT.input_window_geometry_min_confidence": 0.4,
        "INPUT_RIGHT.input_window_geometry_min_confidence": 0.4,
        "INPUT_LEFT.input_part_presence_false_positive_max_count": 2,
        "INPUT_RIGHT.input_part_presence_false_positive_max_count": 2,
    }

    def __init__(self, trigger_by_run=None):
        self.trigger_by_run = trigger_by_run or {1: True, 2: False, 3: True}
        self.calls = 0

    def evaluate_all_detailed(self, vision_results, frames=None):
        self.calls += 1
        marker = next(
            detection
            for detections in vision_results.values()
            for detection in detections
            if detection.get("class") == "marker"
        )
        run_id = int(marker["run_id"])
        triggered = bool(self.trigger_by_run[run_id])
        return [RuleResult(
            "window_geometry",
            triggered,
            details={"run_id": run_id},
            drawings=[],
        )]


def input_frame_runs():
    return [
        {
            "INPUT_LEFT": np.full((20, 20, 3), run_id, dtype=np.uint8),
            "INPUT_RIGHT": np.full((20, 20, 3), run_id, dtype=np.uint8),
        }
        for run_id in (1, 2, 3)
    ]


class InspectionConsensusTests(unittest.TestCase):
    def test_each_rule_uses_independent_majority_two_of_three(self):
        runs = [
            [
                RuleResult("rule_a", True, details={"run": 1}),
                RuleResult("rule_b", False, details={"run": 1}),
            ],
            [
                RuleResult("rule_a", False, details={"run": 2}),
                RuleResult("rule_b", False, details={"run": 2}),
            ],
            [
                RuleResult("rule_a", True, details={"run": 3}),
                RuleResult("rule_b", True, details={"run": 3}),
            ],
        ]
        final, metadata, evidence_index = combine_rule_results(runs)
        self.assertEqual([result.triggered for result in final], [True, False])
        self.assertEqual(
            final[0].details["consensus"]["triggered_votes"],
            2,
        )
        self.assertEqual(final[1].details["consensus"]["normal_votes"], 2)
        self.assertEqual(metadata["rules"]["rule_a"]["states"], [True, False, True])
        self.assertIn(evidence_index, (0, 1, 2))

    def test_mismatched_rule_set_fails_instead_of_returning_good(self):
        runs = [
            [RuleResult("rule_a", False)],
            [RuleResult("rule_b", False)],
            [RuleResult("rule_a", False)],
        ]
        with self.assertRaisesRegex(
            InspectionConsensusError,
            "порядок или набор правил",
        ):
            combine_rule_results(runs)

    def test_presence_majority_is_neutral_and_tracks_all_votes(self):
        results = [
            RuleResult("part_presence", False, {"empty_tray": False}),
            RuleResult("part_presence", False, {"empty_tray": True}),
            RuleResult("part_presence", False, {"empty_tray": True}),
        ]
        final, metadata, evidence_index = combine_presence_results(results)
        self.assertTrue(final.details["empty_tray"])
        self.assertFalse(final.triggered)
        self.assertEqual(metadata["empty_votes"], 2)
        self.assertEqual(metadata["present_votes"], 1)
        self.assertEqual(evidence_index, 2)

    def test_input_runs_rules_three_times_after_presence_majority_present(self):
        vision = ScriptedVision({1: (3, 3), 2: (0, 0), 3: (3, 3)})
        decision = ScriptedDecision({1: True, 2: False, 3: True})
        recorder = Recorder()
        result = Inspector(vision, decision, recorder).inspect_input_consensus(
            part_id=1,
            step=1,
            frame_runs=input_frame_runs(),
        )
        self.assertFalse(result.is_empty_tray)
        self.assertEqual(vision.calls, 3)
        self.assertEqual(decision.calls, 3)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(result.defects, ["window_geometry"])
        self.assertEqual(
            result.consensus["part_presence"]["present_votes"],
            2,
        )
        self.assertEqual(
            result.consensus["rules"]["window_geometry"]["triggered_votes"],
            2,
        )
        self.assertEqual(len(result.model_health), 2)
        self.assertTrue(all(row["runs"] == 3 for row in result.model_health))
        self.assertTrue(all(
            len(row["detections_by_run"]) == 3
            for row in result.model_health
        ))

    def test_input_majority_empty_skips_all_defect_rules(self):
        vision = ScriptedVision({1: (3, 3), 2: (0, 0), 3: (1, 1)})
        decision = ScriptedDecision()
        result = Inspector(vision, decision, Recorder()).inspect_input_consensus(
            part_id=1,
            step=1,
            frame_runs=input_frame_runs(),
        )
        self.assertTrue(result.is_empty_tray)
        self.assertEqual(vision.calls, 3)
        self.assertEqual(decision.calls, 0)
        self.assertEqual(len(result.rule_results), 1)
        self.assertEqual(result.rule_results[0].rule_name, "part_presence")
        row = ProductionCycle._rule_report_row(result.rule_results[0])
        self.assertEqual(row["status_label"], "КОРПУС НЕ ОБНАРУЖЕН · 2/3")
        self.assertTrue(row["neutral"])

    def _omission_run(self, excess):
        """Один прогон правила long_omission с заданным «избытком» (порог 3)."""
        return RuleResult(
            "long_omission", excess > 3,
            details={"per_role": {"SPIDER_LEFT": {
                "triggered": excess > 3, "reason": None,
                "allowed_thickness_px": 20.0, "excess_pixels": excess,
                "largest_component_pixels": excess,
                "excess_component_min_px": 3, "max_excess_depth_px": 10.0,
                "top_line_actual_max_residual_px": 0.5,
                "top_line_max_residual_px": 3.0,
                "found": 5, "expected_count": 5,
            }}},
        )

    def test_rule_consensus_exposes_three_run_cards(self):
        """Голосование 2 из 3 несёт замеры каждого из трёх прогонов."""
        final, metadata, _ = combine_rule_results([
            [self._omission_run(5)],
            [self._omission_run(2)],
            [self._omission_run(340)],
        ])
        run_cards = final[0].details["consensus"]["run_cards"]
        self.assertEqual(len(run_cards), 3)
        self.assertTrue(any(
            metric["label"] == "избыток"
            for metric in run_cards[0][0]["metrics"]
        ))
        self.assertEqual(metadata["rules"]["long_omission"]["run_cards"], run_cards)

    def test_rule_consensus_exposes_run_status(self):
        """Статус прогонов («ОБЛАСТЬ НЕ ПОСТРОЕНА») попадает в consensus."""
        final, _, _ = combine_rule_results([
            [self._omission_run(5)],
            [self._omission_run(2)],
            [self._omission_run(340)],
        ])
        status = final[0].details["consensus"]["run_status"]
        self.assertEqual(len(status), 3)
        self.assertEqual(status[0][0]["status"], "ОТКЛОНЕНИЕ")
        self.assertEqual(status[1][0]["status"], "В НОРМЕ")

    def test_rule_consensus_marks_region_missing(self):
        """Прогон с непостроенной областью — «ОБЛАСТЬ НЕ ПОСТРОЕНА»."""
        runs = [
            [RuleResult("long_omission", True, details={
                "per_role": {"SPIDER_LEFT": {
                    "triggered": True, "reason": "no_detections",
                    "valid": False, "found": 0, "excess_pixels": None,
                }},
            })],
            [self._omission_run(0)],
            [self._omission_run(0)],
        ]
        final, _, _ = combine_rule_results(runs)
        status = final[0].details["consensus"]["run_status"]
        self.assertEqual(status[0][0]["status"], "ОБЛАСТЬ НЕ ПОСТРОЕНА")
        self.assertEqual(status[0][0]["reason"], "no_detections")
        self.assertEqual(status[1][0]["status"], "В НОРМЕ")

    def test_describe_picture_run_explains_selection(self):
        """picture_reason: правило, метрика, значение и порог."""
        final, _, _ = combine_rule_results([
            [self._omission_run(5)],
            [self._omission_run(2)],
            [self._omission_run(340)],
        ])
        reason = describe_picture_run(final, 1)  # run 2 (индекс 1)
        self.assertIn("избыток", reason)
        self.assertIn("2 px", reason)
        self.assertIn("порог 3 px", reason)
        self.assertIn("норма", reason)
        # Без числовых порогов — честный ответ по большинству голосов.
        no_limit = [RuleResult("rule_a", False, details={
            "consensus": {"runs": 3, "run_cards": [[
                {"role": "TOP", "metrics": [{
                    "label": "m", "value_raw": None, "limit_raw": None,
                    "ok": True}]},
            ]] * 3},
        })]
        self.assertIn("по большинству голосов",
                      describe_picture_run(no_limit, 0))

    def test_picture_reason_set_in_spider_consensus(self):
        """Inspector заполняет picture_run и picture_reason всегда."""
        vision = ScriptedVision({1: (3, 3), 2: (0, 0), 3: (3, 3)})
        decision = ScriptedDecision({1: True, 2: False, 3: True})
        # ScriptedDecision возвращает window_geometry без per_role — карточек
        # нет; picture_reason должен честно сказать про большинство голосов.
        result = Inspector(vision, decision, Recorder()).inspect_input_consensus(
            part_id=1, step=1, frame_runs=input_frame_runs(),
        )
        self.assertIn("picture_run", result.consensus)
        self.assertIn("picture_reason", result.consensus)
        self.assertTrue(result.consensus["picture_reason"])

    def test_spider_picture_run_prefers_normal_run_closest_to_threshold(self):
        """Сквозная проверка: картинка строится по прогону в норме, чей
        замер ближе всего к порогу; выбранный кадр реально уходит в результат."""

        class SpiderVision:
            last_health = []

            def process_all(self, frames):
                run_id = int(next(iter(frames.values()))[0, 0, 0])
                self.last_health = [
                    {"role": role, "model": f"w/{role}.pt", "ok": True,
                     "elapsed_ms": 1.0, "detections": 1, "error": None}
                    for role in frames
                ]
                return {role: [{"class": "marker", "confidence": 0.9,
                                "bbox": [1, 1, 5, 5], "run_id": run_id}]
                        for role in frames}

        class SpiderDecision:
            thresholds = {}
            excess = {1: 5, 2: 2, 3: 340}  # run 2 — норма, ближе всех к 3

            def evaluate_all_detailed(self, vision_results, frames=None):
                marker = next(d for ds in vision_results.values() for d in ds)
                run_id = int(marker["run_id"])
                excess = self.excess[run_id]
                return [RuleResult(
                    "long_omission", excess > 3,
                    details={"per_role": {"SPIDER_LEFT": {
                        "triggered": excess > 3, "reason": None,
                        "allowed_thickness_px": 20.0, "excess_pixels": excess,
                        "largest_component_pixels": excess,
                        "excess_component_min_px": 3,
                        "max_excess_depth_px": 10.0,
                        "top_line_actual_max_residual_px": 0.5,
                        "top_line_max_residual_px": 3.0,
                        "found": 5, "expected_count": 5,
                    }}},
                )]

        spider_roles = ("SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN",
                        "SPIDER_OUT", "TOP")
        frame_runs = [
            {role: np.full((16, 16, 3), run_id, dtype=np.uint8)
             for role in spider_roles}
            for run_id in (1, 2, 3)
        ]
        result = Inspector(
            SpiderVision(), SpiderDecision(), Recorder(),
        ).inspect_spider_consensus(part_id=1, step=1, frame_runs=frame_runs)

        self.assertEqual(result.consensus["picture_run"], 2)
        # В пиксель кадра записан номер прогона: в UI уезжает кадр №2.
        self.assertEqual(int(result.raw_frames["SPIDER_LEFT"][0, 0, 0]), 2)
        self.assertEqual(
            len(result.rule_results[0].details["consensus"]["run_cards"]),
            3,
        )
        # Все три набора кадров стадии доступны для переключения в UI.
        self.assertEqual(len(result.run_frames), 3)
        self.assertEqual(
            [int(run["SPIDER_LEFT"][0, 0, 0]) for run in result.run_frames],
            [1, 2, 3],
        )
        self.assertEqual(
            set(result.run_frames[0]),
            {"SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP"},
        )

    def test_empty_tray_picture_run_uses_closest_flatness(self):
        """Пустой лоток: картинка по самому пограничному flatness (ближе
        всего к порогу ложных срабатываний), а не по последнему прогону."""
        vision = ScriptedVision({1: (3, 3), 2: (0, 0), 3: (1, 1)})
        result = Inspector(
            vision, ScriptedDecision(), Recorder(),
        ).inspect_input_consensus(
            part_id=1, step=1, frame_runs=input_frame_runs(),
        )
        self.assertTrue(result.is_empty_tray)
        # Прогон 1: flatness 3 — ближе всего к порогу ложных 2 (и «не пусто»).
        self.assertEqual(result.consensus["picture_run"], 1)
        self.assertEqual(int(result.raw_frames["INPUT_LEFT"][0, 0, 0]), 1)
        # Кадры трёх прогонов доступны для переключения даже при пустом лотке.
        self.assertEqual(len(result.run_frames), 3)
        self.assertEqual(
            [int(run["INPUT_LEFT"][0, 0, 0]) for run in result.run_frames],
            [1, 2, 3],
        )
        # Правил по прогонам не выполнялось: разметка пустая.
        self.assertEqual(result.run_rule_results, [[], [], []])

    def test_select_picture_run_stays_within_decisive_metric(self):
        """Решающая метрика фиксируется сначала: вспомогательная метрика
        в норме (даже очень близко к порогу) не перебивает дефект."""
        def run(excess, residual):
            return [RuleResult(
                "long_omission", excess > 3,
                details={"per_role": {"SPIDER_LEFT": {
                    "triggered": excess > 3, "reason": None,
                    "allowed_thickness_px": 20.0, "excess_pixels": excess,
                    "largest_component_pixels": excess,
                    "excess_component_min_px": 3, "max_excess_depth_px": 10.0,
                    "top_line_actual_max_residual_px": residual,
                    "top_line_max_residual_px": 1.2,
                    "found": 5, "expected_count": 5,
                }}},
            )]
        final, _, _ = combine_rule_results([
            run(5, 1.19),    # избыток брак; отклонение 1.19/1.2 — норма
            run(30, 1.18),
            run(340, 1.17),  # отклонение 1.17 — ближе всех к порогу 1.2
        ])
        # Дефект — «избыток» (все три брак): выбираем ближайший брак по
        # этой метрике (прогон 1), а не прогон с самым близким «отклонением».
        self.assertEqual(select_picture_run(final), 0)
        reason = describe_picture_run(final, 0)
        self.assertIn("избыток", reason)
        self.assertIn("брак", reason)
        # select и describe согласованы: описывается та же метрика.
        self.assertNotIn("отклонение", reason)

    def test_select_picture_run_does_not_merge_cameras(self):
        """Одна метрика у разных камер не смешивается: решающая камера —
        та, где есть дефект (SPIDER_LEFT), а не «избыток 0» SPIDER_RIGHT."""
        def run(left_excess, right_excess):
            per_role = {}
            for role, excess in (("SPIDER_LEFT", left_excess),
                                 ("SPIDER_RIGHT", right_excess)):
                per_role[role] = {
                    "triggered": excess > 3, "reason": None,
                    "allowed_thickness_px": 20.0, "excess_pixels": excess,
                    "largest_component_pixels": excess,
                    "excess_component_min_px": 3, "max_excess_depth_px": 10.0,
                    "top_line_actual_max_residual_px": 0.5,
                    "top_line_max_residual_px": 3.0,
                    "found": 5, "expected_count": 5,
                }
            return [RuleResult("long_omission", left_excess > 3 or right_excess > 3,
                               details={"per_role": per_role})]

        final, _, _ = combine_rule_results([
            run(5, 0),    # левая брак, правая норма
            run(30, 0),
            run(340, 0),
        ])
        # Решающая метрика — «избыток» SPIDER_LEFT (все три брак):
        # ближайший брак = прогон 1 (5), а не «норма 0» правой камеры.
        self.assertEqual(select_picture_run(final), 0)
        reason = describe_picture_run(final, 0)
        self.assertIn("SPIDER_LEFT", reason)
        self.assertIn("5 px", reason)

    def test_select_picture_run_prefers_norma_closest_to_threshold(self):
        """Есть замеры в норме — картинка по ближайшему к порогу из них."""
        def card(value, ok):
            return [{"role": "TOP", "metrics": [{
                "label": "m", "value_raw": float(value),
                "limit_raw": 10.0, "ok": ok,
            }]}]
        result = RuleResult("rule_a", False, details={
            "consensus": {
                "runs": 3,
                "run_cards": [
                    card(9.9, True),   # норма, совсем рядом с порогом
                    card(2.0, True),   # норма, далеко
                    card(11.0, False), # брак, рядом
                ],
            },
        })
        self.assertEqual(select_picture_run([result]), 0)

    def test_select_picture_run_all_bad_uses_closest_defect(self):
        """Все три замера — брак: берём ближайший к порогу брак."""
        def card(value):
            return [{"role": "TOP", "metrics": [{
                "label": "m", "value_raw": float(value),
                "limit_raw": 10.0, "ok": False,
            }]}]
        result = RuleResult("rule_a", True, details={
            "consensus": {
                "runs": 3,
                "run_cards": [card(50.0), card(30.0), card(11.0)],
            },
        })
        self.assertEqual(select_picture_run([result]), 2)

    def test_select_picture_run_uses_only_triggered_rule_for_bad(self):
        """При браке решающее правило — сработавшее; его замеры в приоритете."""
        def card(value, ok):
            return [{"role": "TOP", "metrics": [{
                "label": "m", "value_raw": float(value),
                "limit_raw": 10.0, "ok": ok,
            }]}]
        triggered = RuleResult("bad_rule", True, details={
            "consensus": {
                "runs": 3,
                "run_cards": [card(9.0, False), card(9.5, False), card(50.0, False)],
            },
        })
        # Нормальное правило с замером ещё ближе к порогу не должно
        # перебивать сработавшее правило.
        normal = RuleResult("ok_rule", False, details={
            "consensus": {
                "runs": 3,
                "run_cards": [card(9.9, True), card(2.0, True), card(3.0, True)],
            },
        })
        self.assertEqual(select_picture_run([normal, triggered]), 1)

    def test_select_picture_run_returns_none_without_numeric_limits(self):
        result = RuleResult("rule_a", False, details={
            "consensus": {
                "runs": 3,
                "run_cards": [[{"role": "TOP", "metrics": [
                    {"label": "m", "value_raw": None, "limit_raw": None, "ok": True},
                ]}] for _ in range(3)],
            },
        })
        self.assertIsNone(select_picture_run([result]))

    def test_rule_report_exposes_vote_without_overlay_text(self):
        result = RuleResult(
            "window_sinks",
            True,
            details={
                "consensus": {
                    "runs": 3,
                    "required_votes": 2,
                    "triggered_votes": 2,
                    "normal_votes": 1,
                    "decision": "triggered",
                }
            },
        )
        row = ProductionCycle._rule_report_row(result)
        self.assertEqual(row["status_label"], "СРАБОТАЛО · 2/3")
        self.assertEqual(row["consensus"]["triggered_votes"], 2)


if __name__ == "__main__":
    unittest.main()
