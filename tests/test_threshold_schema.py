import ast
import unittest
from pathlib import Path

from domain.threshold_loader import (
    FIXED_VALUE_PARAMETERS,
    PARAM_DESCRIPTIONS,
    PARAM_LABELS,
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
            expected_keys = {
                key.split(".", 1)[1]
                for key in thresholds
                if key.startswith(f"{role}.")
            }
            self.assertEqual(
                {param["key"] for param in params}, expected_keys,
                f"{role}: параметр потерян между конфигурацией и UI",
            )
            for param in params:
                # Подпись не должна совпадать с техническим именем и не
                # должна содержать snake_case (значит, не переведена).
                self.assertNotEqual(
                    param["label"], param["key"], param["key"],
                )
                self.assertNotIn("_", param["label"], param["key"])

    def test_operator_metadata_covers_every_current_rule_threshold(self):
        """Редактор получает не только подпись, но и смысл каждого порога.

        Это regression-проверка результата аудита: новый параметр нельзя
        добавить в thresholds.json так, чтобы он выглядел в HMI как
        непонятный технический ключ или имел старую неоднозначную единицу.
        """
        root = Path(__file__).resolve().parents[1]
        thresholds = ThresholdLoader(root / "thresholds.json").get_all()
        configured = {
            key.split(".", 1)[1]
            for key in thresholds
            if "." in key
        }
        self.assertEqual(configured - set(PARAM_LABELS), set())
        self.assertEqual(configured - set(PARAM_DESCRIPTIONS), set())

        for role in {
            key.split(".", 1)[0] for key in thresholds if "." in key
        }:
            params = {
                param["key"]: param
                for group in describe_role_parameters(role, thresholds)
                for param in group["params"]
            }
            for key in configured & set(params):
                self.assertTrue(params[key]["description"].strip(), key)

        # Критические уточнения смысла, которые раньше были неверно или
        # слишком общо переведены в интерфейсе.
        self.assertIn(
            "число пикселей", PARAM_LABELS[
                "spider_long_omission_excess_component_min_px"
            ].lower(),
        )
        self.assertIn(
            "опорной точки", PARAM_LABELS[
                "top_platform_overlap_contact_inner_ratio"
            ].lower(),
        )
        self.assertIn(
            "доля высоты", PARAM_LABELS[
                "spider_contacts_short_omission_tilt_ratio_max"
            ].lower(),
        )

    def test_editor_limits_match_rule_invariants(self):
        """Нельзя ввести значения, которые правило не умеет обработать."""
        root = Path(__file__).resolve().parents[1]
        thresholds = ThresholdLoader(root / "thresholds.json").get_all()

        def meta(role, key):
            for group in describe_role_parameters(role, thresholds):
                for param in group["params"]:
                    if param["key"] == key:
                        return param
            self.fail(f"{role}.{key} не попал в редактор")

        self.assertEqual(
            meta("INPUT_LEFT", "input_window_geometry_expected_count")["min"],
            1,
        )
        overlap = meta("INPUT_LEFT", "input_window_sinks_overlap_min_px")
        self.assertEqual(overlap["min"], 1)
        self.assertEqual(overlap["step"], 1)
        self.assertEqual(
            meta("SPIDER_LEFT", "spider_contacts_long_expected_count")["min"],
            2,
        )
        short_count = meta("SPIDER_IN", "spider_contacts_short_expected_count")
        self.assertTrue(short_count["readonly"])
        self.assertEqual(short_count["min"], 2)
        self.assertEqual(short_count["max"], 2)
        top_count = meta("TOP", "top_contacts_expected_count")
        self.assertTrue(top_count["readonly"])
        self.assertEqual(top_count["min"], 14)
        self.assertEqual(top_count["max"], 14)

        # У разрешённых правилами ratio/margin нет искусственного верхнего
        # потолка в HTML input.
        self.assertNotIn(
            "max",
            meta("SPIDER_LEFT", "spider_contacts_long_line_deviation_ratio"),
        )
        self.assertNotIn(
            "max", meta("TOP", "top_platform_overlap_margin_px"),
        )

    def test_invalid_editor_values_are_rejected_by_the_same_schema(self):
        root = Path(__file__).resolve().parents[1]
        loader = ThresholdLoader(root / "thresholds.json")
        base = loader.get_all()

        invalid_cases = (
            (
                "INPUT_LEFT.input_window_sinks_overlap_min_px", 0,
                "целым числом >= 1",
            ),
            (
                "INPUT_LEFT.input_window_sinks_overlap_min_px", 1.5,
                "целым числом >= 1",
            ),
            (
                "SPIDER_IN.spider_contacts_short_expected_count", 3,
                "должен быть равен 2",
            ),
            (
                "TOP.top_contacts_expected_count", 13,
                "должен быть равен 14",
            ),
        )
        for key, value, message in invalid_cases:
            candidate = dict(base)
            candidate[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ValueError, message):
                    ThresholdLoader.validate(candidate)

        self.assertEqual(
            FIXED_VALUE_PARAMETERS["spider_contacts_short_expected_count"], 2,
        )
        self.assertEqual(
            FIXED_VALUE_PARAMETERS["top_contacts_expected_count"], 14,
        )

    def test_editor_uses_rule_order_not_alphabetical_key_order(self):
        """Min/max и связанные параметры читаются сверху вниз естественно."""
        root = Path(__file__).resolve().parents[1]
        thresholds = ThresholdLoader(root / "thresholds.json").get_all()

        def group_keys(role, rule):
            group = next(
                item for item in describe_role_parameters(role, thresholds)
                if item["rule"] == rule
            )
            return [param["key"] for param in group["params"]]

        self.assertEqual(
            group_keys("INPUT_LEFT", "input_window_geometry")[:6],
            [
                "input_window_geometry_min_confidence",
                "input_window_geometry_expected_count",
                "input_window_geometry_top_px_min",
                "input_window_geometry_top_px_max",
                "input_window_geometry_bottom_px_min",
                "input_window_geometry_bottom_px_max",
            ],
        )
        self.assertEqual(
            [
                group["rule"]
                for group in describe_role_parameters("TOP", thresholds)
            ],
            [
                "top_contacts", "top_platform_overlap", "top_platform",
                "top_sinks", "top_glass",
            ],
        )


if __name__ == "__main__":
    unittest.main()
