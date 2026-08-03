import ast
import unittest
from pathlib import Path

from domain.threshold_loader import (
    ThresholdLoader,
    describe_role_parameters,
)


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
    def test_every_rule_parameter_is_explicitly_configured(self):
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
        # Параметры правил должны быть явно сконфигурированы в файле —
        # без скрытых дефолтов в коде.
        self.assertEqual(used - configured, set(), "hidden fallback parameters")
        # Обратное направление теперь разрешено: в thresholds.json можно
        # добавлять новые пороги вручную, они подхватываются при запуске и
        # редактируются в панели «Пороги правил» (группа «Прочие пороги»).

    def test_every_configured_parameter_has_russian_operator_label(self):
        """Каждый порог в файле получает понятную русскую подпись."""
        root = Path(__file__).resolve().parents[1]
        thresholds = ThresholdLoader(root / "thresholds.json").get_all()
        roles = sorted({
            key.split(".", 1)[0]
            for key in thresholds
            if "." in key
        })
        self.assertTrue(roles)
        for role in roles:
            groups = describe_role_parameters(role, thresholds)
            params = [
                param
                for group in groups
                for param in group["params"]
            ]
            self.assertTrue(params, role)
            for param in params:
                # Подпись не должна совпадать с техническим именем и не
                # должна содержать snake_case (значит, не переведена).
                self.assertNotEqual(
                    param["label"], param["key"], param["key"],
                )
                self.assertNotIn("_", param["label"], param["key"])


if __name__ == "__main__":
    unittest.main()
