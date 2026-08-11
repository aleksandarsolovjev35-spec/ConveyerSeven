"""DecisionEngine: оркестратор правил дефектов.

DecisionEngine загружает все правила, фильтрует disabled_rules и
выполняет их по vision_results. Тесты проверяют:

* конструктор: disabled_rules фильтрует, все disabled → RuntimeError;
* evaluate_all_detailed: вызывает check каждого правила;
* rules_for_role: фильтрует правила по ROLES;
* evaluate_rules_detailed: пустые vision_results → пустой список.
"""

from core.decision_engine import DecisionEngine
from domain.defect_rules.base import RuleResult


class FakeRule:
    """Минимальное правило с заданным именем и ROLES."""

    def __init__(self, name, roles=(), triggered=False, enabled=True):
        self.name = name
        self.ROLES = roles
        self._triggered = triggered
        self._enabled = enabled
        self.check_calls = 0

    @property
    def enabled(self):
        return self._enabled

    def check(self, vision_results, **kwargs):
        self.check_calls += 1
        return RuleResult(
            rule_name=self.name,
            triggered=self._triggered,
            details={},
            drawings=[],
        )


def _make_thresholds(disabled_rules=None):
    """Минимальные thresholds для DecisionEngine."""
    return {
        "disabled_rules": disabled_rules or [],
        "INPUT_LEFT.input_window_geometry_min_confidence": 0.3,
        "INPUT_RIGHT.input_window_geometry_min_confidence": 0.3,
        "INPUT_LEFT.input_window_geometry_expected_count": 7,
        "INPUT_RIGHT.input_window_geometry_expected_count": 7,
        "INPUT_LEFT.input_window_geometry_top_px_min": 10,
        "INPUT_LEFT.input_window_geometry_top_px_max": 100,
        "INPUT_LEFT.input_window_geometry_bottom_px_min": 10,
        "INPUT_LEFT.input_window_geometry_bottom_px_max": 100,
        "INPUT_LEFT.input_window_geometry_center_zone_ratio": 0.5,
        "INPUT_RIGHT.input_window_geometry_top_px_min": 10,
        "INPUT_RIGHT.input_window_geometry_top_px_max": 100,
        "INPUT_RIGHT.input_window_geometry_bottom_px_min": 10,
        "INPUT_RIGHT.input_window_geometry_bottom_px_max": 100,
        "INPUT_RIGHT.input_window_geometry_center_zone_ratio": 0.5,
        "input_window_sinks_min_confidence": 0.3,
        "input_window_sinks_window_min_confidence": 0.3,
        "input_window_sinks_overlap_min_px": 5,
        "spider_contacts_long_min_confidence": 0.3,
        "spider_contacts_long_expected_count": 5,
        "spider_contacts_long_damper_open_max_px": 10,
        "spider_contacts_long_gap_dev_max_px": 10,
        "spider_contacts_long_inscribed_rect_width_px": 38,
        "spider_contacts_long_inscribed_rect_height_px": 18,
        "spider_contacts_long_y_filter_ratio": 3.0,
        "spider_long_omission_min_confidence": 0.3,
        "spider_long_omission_allowed_thickness_px": 20,
        "spider_long_omission_excess_component_min_px": 50,
        "spider_long_omission_top_line_max_residual_px": 5,
        "spider_long_omission_top_line_min_inlier_ratio": 0.5,
        "spider_contacts_short_min_confidence": 0.3,
        "spider_contacts_short_expected_count": 2,
        "spider_contacts_short_damper_open_max_px": 10,
        "spider_contacts_short_inscribed_rect_width_px": 20,
        "spider_contacts_short_inscribed_rect_height_px": 10,
        "spider_contacts_short_area_absolute_min": 100,
        "spider_contacts_short_y_filter_ratio": 3.0,
        "spider_short_omission_min_confidence": 0.3,
        "spider_short_omission_allowed_thickness_px": 20,
        "spider_short_omission_excess_component_min_px": 50,
        "spider_short_omission_top_line_max_residual_px": 5,
        "spider_short_omission_top_line_min_inlier_ratio": 0.5,
        "top_contacts_min_confidence": 0.3,
        "top_contacts_expected_count": 14,
        "top_contacts_platform_min_confidence": 0.3,
        "top_contacts_edge_distance_deviation_ratio": 0.3,
        "top_contacts_side_rect_width_px": 10,
        "top_contacts_side_rect_height_px": 10,
        "top_platform_min_confidence": 0.3,
        "top_platform_inscribed_rect_width_px": 100,
        "top_platform_inscribed_rect_height_px": 80,
        "top_sinks_min_confidence": 0.3,
        "top_sinks_case_min_confidence": 0.3,
        "top_sinks_central_min_confidence": 0.3,
        "top_sinks_platform_min_confidence": 0.3,
        "top_sinks_forbidden_margin_px": 5,
        "top_sinks_central_forbidden_margin_px": 5,
        "top_glass_min_confidence": 0.3,
        "top_glass_platform_min_confidence": 0.3,
        "top_glass_pin_min_confidence": 0.3,
        "top_glass_case_min_confidence": 0.3,
        "top_glass_central_min_confidence": 0.3,
        "top_glass_contacts_min_confidence": 0.3,
        "top_glass_platform_margin_px": 5,
        "top_glass_pin_margin_px": 5,
        "top_glass_ring_margin_px": 5,
        "top_glass_on_contacts_min_confidence": 0.3,
        "top_glass_on_contacts_pin_min_confidence": 0.3,
        "top_glass_on_contacts_contacts_min_confidence": 0.3,
        "top_glass_on_contacts_overlap_min_px": 5,
        "top_platform_overlap_min_confidence": 0.3,
        "top_platform_overlap_contacts_min_confidence": 0.3,
        "top_platform_overlap_platform_min_confidence": 0.3,
        "top_platform_overlap_expand_x_ratio": 1.1,
        "top_platform_overlap_expand_y_ratio": 1.1,
        "top_platform_overlap_boundary_width_px": 5,
        "top_platform_overlap_boundary_height_px": 5,
        "top_platform_overlap_excess_component_min_px": 50,
    }


class TestConstructor:
    def test_все_правила_активны(self):
        engine = DecisionEngine(thresholds=_make_thresholds())
        assert len(engine.rules) == 12

    def test_disabled_rules_фильтруются(self):
        thresholds = _make_thresholds(disabled_rules=["window_geometry"])
        engine = DecisionEngine(thresholds=thresholds)
        names = [r.name for r in engine.rules]
        assert "window_geometry" not in names
        assert len(engine.rules) == 11

    def test_все_disabled_бросает_ошибку(self):
        all_names = [
            "window_geometry", "window_sinks",
            "contacts_long", "long_omission",
            "contacts_short", "short_omission",
            "top_contacts", "top_platform", "sinks",
            "glass", "glass_on_contacts", "platform_contacts_overlap",
        ]
        thresholds = _make_thresholds(disabled_rules=all_names)
        try:
            DecisionEngine(thresholds=thresholds)
            raise AssertionError("должен был бросить RuntimeError")
        except RuntimeError as exc:
            assert "No active defect rules" in str(exc)

    def test_thresholds_сохраняются(self):
        thresholds = _make_thresholds()
        engine = DecisionEngine(thresholds=thresholds)
        assert engine.thresholds is thresholds


class TestEvaluateAllDetailed:
    def test_вызывает_check_каждого_правила(self):
        engine = DecisionEngine(thresholds=_make_thresholds())
        # Заменяем правила на фейки для подсчёта вызовов
        fake_rules = [FakeRule(f"rule_{i}") for i in range(3)]
        engine.rules = fake_rules
        vision_results = {"TOP": []}
        engine.evaluate_all_detailed(vision_results)
        for rule in fake_rules:
            assert rule.check_calls == 1

    def test_возвращает_rule_results(self):
        engine = DecisionEngine(thresholds=_make_thresholds())
        fake_rules = [
            FakeRule("rule_ok", triggered=False),
            FakeRule("rule_fail", triggered=True),
        ]
        engine.rules = fake_rules
        results = engine.evaluate_all_detailed({"TOP": []})
        assert len(results) == 2
        assert results[0].rule_name == "rule_ok"
        assert results[0].triggered is False
        assert results[1].rule_name == "rule_fail"
        assert results[1].triggered is True

    def test_frames_передаются_в_check(self):
        engine = DecisionEngine(thresholds=_make_thresholds())

        class FramesRule:
            name = "test"
            enabled = True
            ROLES = ()

            def __init__(self):
                self.received_frames = None

            def check(self, vision_results, **kwargs):
                self.received_frames = kwargs.get("frames")
                return RuleResult("test", False)

        rule = FramesRule()
        engine.rules = [rule]
        frames = {"TOP": "frame_data"}
        engine.evaluate_all_detailed({"TOP": []}, frames=frames)
        assert rule.received_frames is frames


class TestRulesForRole:
    def test_фильтрует_по_ROLES(self):
        engine = DecisionEngine(thresholds=_make_thresholds())
        fake_rules = [
            FakeRule("input_rule", roles=("INPUT_LEFT", "INPUT_RIGHT")),
            FakeRule("spider_rule", roles=("SPIDER_LEFT", "SPIDER_RIGHT")),
            FakeRule("top_rule", roles=("TOP",)),
        ]
        engine.rules = fake_rules

        input_rules = engine.rules_for_role("INPUT_LEFT")
        assert len(input_rules) == 1
        assert input_rules[0].name == "input_rule"

        top_rules = engine.rules_for_role("TOP")
        assert len(top_rules) == 1
        assert top_rules[0].name == "top_rule"

    def test_пустой_результат_для_неизвестной_роли(self):
        engine = DecisionEngine(thresholds=_make_thresholds())
        fake_rules = [FakeRule("input_rule", roles=("INPUT_LEFT",))]
        engine.rules = fake_rules
        assert engine.rules_for_role("UNKNOWN_ROLE") == []


class TestEvaluateRulesDetailed:
    def test_пустые_vision_results_возвращают_пустой_список(self):
        engine = DecisionEngine(thresholds=_make_thresholds())
        fake_rules = [FakeRule("rule_1")]
        engine.rules = fake_rules
        result = engine.evaluate_rules_detailed(fake_rules, {})
        assert result == []

    def test_none_vision_results_возвращают_пустой_список(self):
        engine = DecisionEngine(thresholds=_make_thresholds())
        fake_rules = [FakeRule("rule_1")]
        engine.rules = fake_rules
        result = engine.evaluate_rules_detailed(fake_rules, None)
        assert result == []

    def test_выполняет_только_указанные_правила(self):
        engine = DecisionEngine(thresholds=_make_thresholds())
        rule_a = FakeRule("a")
        rule_b = FakeRule("b")
        rule_c = FakeRule("c")
        engine.rules = [rule_a, rule_b, rule_c]
        # Выполняем только rule_a и rule_c
        result = engine.evaluate_rules_detailed(
            [rule_a, rule_c], {"TOP": []}
        )
        assert len(result) == 2
        assert rule_a.check_calls == 1
        assert rule_b.check_calls == 0
        assert rule_c.check_calls == 1
