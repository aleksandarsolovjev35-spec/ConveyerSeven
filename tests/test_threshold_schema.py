import ast
import unittest
from pathlib import Path

from domain.threshold_loader import ThresholdLoader


DIRECT_PARAMETER_NAMES = {
    "input_window_geometry_min_confidence",
    "input_window_geometry_expected_count",
    "input_window_geometry_top_px_min",
    "input_window_geometry_top_px_max",
    "input_window_geometry_bottom_px_min",
    "input_window_geometry_bottom_px_max",
    "input_window_geometry_center_zone_ratio",
    "spider_long_omission_min_confidence",
    "spider_long_omission_allowed_thickness_px",
    "spider_long_omission_excess_component_min_px",
    "spider_long_omission_top_line_max_residual_px",
    "spider_short_omission_min_confidence",
    "spider_short_omission_allowed_thickness_px",
    "spider_short_omission_excess_component_min_px",
    "spider_short_omission_top_line_max_residual_px",
}


class ThresholdSchemaTests(unittest.TestCase):
    def test_every_threshold_is_used_and_every_rule_parameter_is_explicit(self):
        root = Path(__file__).resolve().parents[1]
        used = set(DIRECT_PARAMETER_NAMES)
        for path in (root / "domain" / "defect_rules").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    continue
                used.add(node.args[0].value)

        thresholds = ThresholdLoader(root / "thresholds.json").get_all()
        configured = {
            key.split(".", 1)[1] if "." in key else key
            for key in thresholds
            if key != "disabled_rules"
        }
        self.assertEqual(configured - used, set(), "unused threshold parameters")
        self.assertEqual(used - configured, set(), "hidden fallback parameters")


if __name__ == "__main__":
    unittest.main()
