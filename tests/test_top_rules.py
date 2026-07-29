import unittest
from pathlib import Path

import numpy as np

from domain.defect_rules.rule_top_contacts import TopContactsRule
from domain.defect_rules.rule_top_glass import TopGlassRule
from domain.defect_rules.rule_top_glass_on_contacts import TopGlassOnContactsRule
from domain.defect_rules.rule_top_platform import TopPlatformRule
from domain.defect_rules.rule_top_platform_overlap import TopPlatformOverlapRule
from domain.defect_rules.rule_top_sinks import TopSinksRule
from domain.threshold_loader import ThresholdLoader
from vision.overlay.debug_overlay import DebugOverlay


def rect(class_name, x1, y1, x2, y2, confidence=0.99):
    return {
        "class": class_name,
        "confidence": confidence,
        "bbox": [x1, y1, x2, y2],
        "mask": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
    }


def top_platform():
    return rect("platform", 300, 200, 700, 500)


def top_contacts():
    contacts = []
    for center_y in (230, 290, 350, 410, 470):
        contacts.append(rect("contacts", 230, center_y - 20, 270, center_y + 20))
        contacts.append(rect("contacts", 730, center_y - 20, 770, center_y + 20))
    contacts.extend([
        rect("contacts", 380, 130, 420, 170),
        rect("contacts", 580, 130, 620, 170),
        rect("contacts", 380, 530, 420, 570),
        rect("contacts", 580, 530, 620, 570),
    ])
    return contacts


def glass_references():
    platform = top_platform()
    contacts = top_contacts()
    pins = [
        rect("pin", 40 + index * 12, 40, 48 + index * 12, 48)
        for index in range(14)
    ]
    case = rect("case", 800, 200, 1000, 400)
    central = rect("case_central", 850, 250, 950, 350)
    return [platform, *contacts, *pins, case, central]


class TopRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.thresholds = ThresholdLoader("thresholds.json").get_all()

    def test_contacts_exact_layout_and_rectangles_pass(self):
        result = TopContactsRule(self.thresholds).check({
            "TOP": [top_platform(), *top_contacts()],
        })
        details = result.details["per_role"]["TOP"]
        self.assertFalse(result.triggered)
        self.assertEqual(details["group_counts"], {"L": 5, "R": 5, "T": 2, "B": 2})
        self.assertEqual(details["rectangle_fail_indices"], [])
        self.assertEqual(details["side_rect_px"], [28.0, 35.0])
        self.assertEqual(details["edge_rect_px"], [30.0, 28.0])
        self.assertEqual(sum(
            drawing.get("type") == "top_contact_inscribed_rect"
            for drawing in result.drawings
        ), 14)

    def test_contacts_wrong_count_missing_platform_and_missing_mask_fail(self):
        rule = TopContactsRule(self.thresholds)
        wrong = rule.check({"TOP": [top_platform(), *top_contacts()[:-1]]})
        self.assertTrue(wrong.triggered)
        self.assertIn("wrong_count", wrong.details["per_role"]["TOP"]["reason"])
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "CONTACTS 13/14"
            for drawing in wrong.drawings
        ))

        missing_platform = rule.check({"TOP": top_contacts()})
        self.assertTrue(missing_platform.triggered)
        self.assertEqual(
            missing_platform.details["per_role"]["TOP"]["reason"],
            "no_valid_platform",
        )
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "NO PLATFORM"
            for drawing in missing_platform.drawings
        ))

        contacts = top_contacts()
        contacts[0] = dict(contacts[0], mask=None)
        missing_mask = rule.check({"TOP": [top_platform(), *contacts]})
        self.assertTrue(missing_mask.triggered)
        self.assertEqual(
            missing_mask.details["per_role"]["TOP"]["reason"],
            "insufficient_valid_contact_masks",
        )
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and str(drawing.get("message", "")).startswith("NO CONTACT MASK")
            for drawing in missing_mask.drawings
        ))

    def test_contacts_layout_distance_and_rectangle_failures(self):
        invalid_layout = top_contacts()
        invalid_layout[0] = rect("contacts", 310, 210, 350, 250)
        layout_result = TopContactsRule(self.thresholds).check({
            "TOP": [top_platform(), *invalid_layout],
        })
        self.assertTrue(layout_result.triggered)
        self.assertEqual(
            layout_result.details["per_role"]["TOP"]["reason"],
            "layout_groups_failed",
        )
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and str(drawing.get("message", "")).startswith("LAYOUT")
            for drawing in layout_result.drawings
        ))

        contacts = top_contacts()
        # Сдвигаем один L-контакт дальше от platform bbox, сохраняя сторону.
        contacts[0] = rect("contacts", 195, 215, 225, 245)
        layout = TopContactsRule(self.thresholds).check({
            "TOP": [top_platform(), *contacts],
        })
        self.assertTrue(layout.triggered)
        self.assertIn("L", layout.details["per_role"]["TOP"]["failed_groups"])
        self.assertTrue(any(
            drawing.get("type") == "top_contacts_distance"
            and drawing.get("triggered")
            for drawing in layout.drawings
        ))

        contacts = top_contacts()
        contacts[0] = rect("contacts", 245, 225, 255, 235)
        size = TopContactsRule(self.thresholds).check({
            "TOP": [top_platform(), *contacts],
        })
        self.assertTrue(size.triggered)
        self.assertIn(1, size.details["per_role"]["TOP"]["rectangle_fail_indices"])

    def test_contacts_select_best_fourteen_and_render_full_geometry(self):
        contacts = top_contacts()
        extra = rect("contacts", 130, 210, 170, 250, confidence=0.95)
        result = TopContactsRule(self.thresholds).check({
            "TOP": [top_platform(), *contacts, extra],
        })
        details = result.details["per_role"]["TOP"]
        self.assertFalse(result.triggered)
        self.assertEqual(details["found_raw"], 15)
        self.assertEqual(details["selected"], 14)
        self.assertEqual(details["ignored"], 1)
        self.assertEqual(sum(
            drawing.get("type") == "top_contacts_ignored"
            for drawing in result.drawings
        ), 1)
        self.assertEqual(sum(
            drawing.get("type") == "top_contacts_group_reference"
            for drawing in result.drawings
        ), 4)
        self.assertEqual(sum(
            drawing.get("type") == "top_contacts_distance"
            for drawing in result.drawings
        ), 14)
        self.assertEqual(sum(
            drawing.get("type") == "top_contacts_platform_bbox"
            for drawing in result.drawings
        ), 1)
        source = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/top_contacts.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("cv2.fillPoly", source)
        self.assertNotIn("cv2.putText", source)
        self.assertNotIn("draw_text_with_bg", source)

        invalid_extra = dict(extra, mask=None)
        with_invalid_extra = TopContactsRule(self.thresholds).check({
            "TOP": [top_platform(), *contacts, invalid_extra],
        })
        invalid_details = with_invalid_extra.details["per_role"]["TOP"]
        self.assertFalse(with_invalid_extra.triggered)
        self.assertEqual(invalid_details["selected"], 14)
        self.assertEqual(invalid_details["ignored"], 1)

    def test_platform_is_independent_and_uses_260_by_120_px(self):
        rule = TopPlatformRule(self.thresholds)
        good = rule.check({"TOP": [top_platform()]})
        details = good.details["per_role"]["TOP"]
        self.assertFalse(good.triggered)
        self.assertEqual(details["rect_width_px"], 260.0)
        self.assertEqual(details["rect_height_px"], 120.0)
        self.assertEqual(details["placement"], "centered")
        self.assertAlmostEqual(details["shift_distance_px"], 0.0)
        self.assertTrue(any(
            drawing.get("type") == "top_platform_centers"
            for drawing in good.drawings
        ))

        bad = rule.check({"TOP": [rect("platform", 0, 0, 100, 40)]})
        self.assertTrue(bad.triggered)
        self.assertTrue(bad.details["per_role"]["TOP"]["inscribe_fail"])
        self.assertEqual(
            bad.details["per_role"]["TOP"]["placement"],
            "not_fitted",
        )

        shifted_platform = {
            "class": "platform",
            "confidence": 0.99,
            "bbox": [300, 200, 700, 500],
            "mask": [
                [300, 200], [480, 200], [480, 360], [520, 360],
                [520, 200], [700, 200], [700, 500], [300, 500],
            ],
        }
        shifted = rule.check({"TOP": [shifted_platform]})
        shifted_details = shifted.details["per_role"]["TOP"]
        self.assertFalse(shifted.triggered)
        self.assertEqual(shifted_details["placement"], "shifted")
        self.assertGreater(shifted_details["shift_distance_px"], 0.0)

        missing = rule.check({"TOP": []})
        self.assertTrue(missing.triggered)
        self.assertEqual(missing.details["per_role"]["TOP"]["reason"], "no_valid_platform")
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "NO PLATFORM"
            for drawing in missing.drawings
        ))

        invalid_orientation = {
            "class": "platform",
            "confidence": 0.99,
            "bbox": [0, 0, 0.5, 100],
            "mask": [[0, 0], [0.5, 0], [0.5, 100], [0, 100]],
        }
        invalid = rule.check({"TOP": [invalid_orientation]})
        self.assertTrue(invalid.triggered)
        self.assertEqual(
            invalid.details["per_role"]["TOP"]["reason"],
            "invalid_platform_orientation",
        )
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "NO ORIENTATION"
            for drawing in invalid.drawings
        ))
        source = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/top_platform.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("cv2.fillPoly", source)
        self.assertNotIn("cv2.putText", source)
        self.assertNotIn("draw_text_with_bg", source)

    def test_platform_overlap_rule_keeps_existing_module_boundary(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((
            root / "domain/defect_rules/rule_top_platform_overlap.py"
        ).is_file())
        self.assertTrue((
            root / "vision/overlay/renderers/platform_overlap.py"
        ).is_file())
        self.assertFalse((
            root / "domain/defect_rules/rule_top_platform_overflow.py"
        ).exists())
        self.assertFalse((
            root / "vision/overlay/renderers/platform_overflow.py"
        ).exists())
        contacts_source = (
            root / "domain/defect_rules/rule_top_contacts.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("overlap_mask", contacts_source)
        self.assertNotIn("platform_overlap_region", contacts_source)

    def test_platform_overlap_area_is_built_from_contact_centers(self):
        rule = TopPlatformOverlapRule(self.thresholds)
        self.assertEqual(rule.name, "platform_contacts_overlap")
        contacts = top_contacts()
        clean = rule.check({"TOP": [top_platform(), *contacts]})
        details = clean.details["per_role"]["TOP"]
        self.assertFalse(clean.triggered)
        self.assertEqual(details["anchor"], "contacts_rectangle")
        self.assertEqual(details["contact_inner_ratio"], 0.5)
        self.assertEqual(details["used_contacts"], 14)
        self.assertEqual(
            details["contact_groups"], {"L": 5, "R": 5, "T": 2, "B": 2},
        )
        # Границы проходят через центры контактов: 250..750 x 150..550.
        self.assertAlmostEqual(details["boundary_width_px"], 500.0, places=1)
        self.assertAlmostEqual(details["boundary_height_px"], 400.0, places=1)
        np.testing.assert_allclose(
            details["boundary_center"], [500.0, 350.0], atol=0.51,
        )
        boundary = next(
            drawing for drawing in clean.drawings
            if drawing.get("type") == "platform_overlap_boundary"
        )
        self.assertEqual(boundary["anchor"], "contacts_rectangle")
        self.assertEqual(len(boundary["points"]), 4)
        anchors = next(
            drawing for drawing in clean.drawings
            if drawing.get("type") == "platform_overlap_contact_anchors"
        )
        self.assertEqual(len(anchors["points"]), 14)

    def test_platform_crossing_contact_area_triggers_and_marks_excess(self):
        rule = TopPlatformOverlapRule(self.thresholds)
        overflow_platform = {
            "class": "platform",
            "confidence": 0.99,
            "bbox": [300, 200, 800, 500],
            "mask": [
                [300, 200], [700, 200], [700, 330], [800, 330],
                [800, 370], [700, 370], [700, 500], [300, 500],
            ],
        }
        hit = rule.check({"TOP": [overflow_platform, *top_contacts()]})
        details = hit.details["per_role"]["TOP"]
        self.assertTrue(hit.triggered)
        self.assertEqual(details["anchor"], "contacts_rectangle")
        self.assertGreaterEqual(details["largest_component_pixels"], 3)
        self.assertGreaterEqual(details["excess_pixels"], 3)
        region = next(
            drawing for drawing in hit.drawings
            if drawing.get("type") == "platform_overlap_region"
        )
        self.assertTrue(region["triggered"])
        self.assertTrue(region["contours"])

        rendered = DebugOverlay.render_frame(
            np.zeros((720, 1280, 3), dtype=np.uint8),
            "TOP",
            [hit],
        )
        # Вышедшая за границу область закрашена красным.
        self.assertGreater(int(np.count_nonzero(rendered[:, :, 2])), 0)
        # Сама область выделена заметным пурпурным прямоугольником.
        self.assertGreater(int(np.count_nonzero(np.all(
            rendered == np.asarray([255, 0, 255], dtype=np.uint8),
            axis=2,
        ))), 0)

    def test_platform_overlap_reports_missing_contacts_and_platform(self):
        rule = TopPlatformOverlapRule(self.thresholds)
        no_contacts = rule.check({"TOP": [top_platform()]})
        details = no_contacts.details["per_role"]["TOP"]
        self.assertTrue(no_contacts.triggered)
        self.assertEqual(details["reason"], "contact_boundary_not_built")
        self.assertEqual(details["used_contacts"], 0)
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "NO CONTACT RECT"
            for drawing in no_contacts.drawings
        ))
        self.assertFalse(any(
            drawing.get("type") == "platform_overlap_boundary"
            for drawing in no_contacts.drawings
        ))

        partial = rule.check({
            "TOP": [
                top_platform(),
                rect("contacts", 230, 280, 270, 320),
                rect("contacts", 730, 280, 770, 320),
            ],
        })
        partial_details = partial.details["per_role"]["TOP"]
        self.assertTrue(partial.triggered)
        self.assertEqual(
            partial_details["reason"], "contact_boundary_not_built",
        )
        self.assertEqual(
            partial_details["contact_groups"],
            {"L": 1, "R": 1, "T": 0, "B": 0},
        )

        missing = rule.check({"TOP": []})
        self.assertTrue(missing.triggered)
        self.assertEqual(
            missing.details["per_role"]["TOP"]["reason"],
            "no_valid_platform",
        )
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "NO PLATFORM"
            for drawing in missing.drawings
        ))
        source = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/platform_overlap.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("PLATFORM OVERFLOW", source)
        self.assertNotIn("draw_text_with_bg", source)
        self.assertNotIn("cv2.putText", source)
        self.assertNotIn("cv2.fillPoly", source)

    def test_platform_overlap_area_respects_margin_and_expand(self):
        thresholds = dict(self.thresholds)
        thresholds["TOP.top_platform_overlap_margin_px"] = 10
        thresholds["TOP.top_platform_overlap_expand_x_ratio"] = 1.2
        thresholds["TOP.top_platform_overlap_expand_y_ratio"] = 0.5
        result = TopPlatformOverlapRule(thresholds).check({
            "TOP": [top_platform(), *top_contacts()],
        })
        details = result.details["per_role"]["TOP"]
        # (500 + 2*10) * 1.2 и (400 + 2*10) * 0.5
        self.assertAlmostEqual(details["boundary_width_px"], 624.0, places=1)
        self.assertAlmostEqual(details["boundary_height_px"], 210.0, places=1)
        self.assertTrue(result.triggered)

    def test_platform_overlap_boundary_ignores_only_one_or_two_connected_pixels(self):
        outside = np.zeros((12, 12), dtype=np.uint8)
        outside[1, 1:3] = 255
        measurement = TopPlatformOverlapRule._measure_components(outside, 3)
        self.assertEqual(measurement["raw_excess_pixels"], 2)
        self.assertEqual(measurement["confirmed_components"], 0)
        self.assertEqual(measurement["ignored_noise_components"], 1)
        self.assertEqual(measurement["ignored_noise_pixels"], 2)

        outside[5:8, 5] = 255
        measurement = TopPlatformOverlapRule._measure_components(outside, 3)
        self.assertEqual(measurement["confirmed_components"], 1)
        self.assertEqual(measurement["excess_pixels"], 3)

    def test_sinks_use_pixelwise_forbidden_region(self):
        rule = TopSinksRule(self.thresholds)
        central = rect("case_central", 800, 200, 1000, 400)
        defect = rect("shells", 850, 250, 855, 255)
        result = rule.check({
            "TOP": [central, top_platform(), *top_contacts(), defect],
        })
        details = result.details["per_role"]["TOP"]
        self.assertTrue(result.triggered)
        self.assertEqual(details["defect_sinks"], 1)
        self.assertGreater(details["hits"][0]["forbidden_pixels"], 0)
        self.assertEqual(details["contacts_used"], 14)
        self.assertTrue(any(
            drawing.get("type") == "top_sinks_references"
            for drawing in result.drawings
        ))
        self.assertTrue(any(
            drawing.get("type") == "top_sink_forbidden_region"
            for drawing in result.drawings
        ))

        outside = rect("shells", 1050, 450, 1055, 455)
        ignored = rule.check({
            "TOP": [central, top_platform(), *top_contacts(), outside],
        })
        self.assertFalse(ignored.triggered)
        self.assertEqual(ignored.drawings, [])

        # Часть shell лежит на platform, но пиксели слева от platform остаются
        # внутри case_central и должны браковать по новой pixelwise-логике.
        wide_central = rect("case_central", 250, 100, 900, 600)
        partial = rect("shells", 290, 250, 310, 270)
        partial_result = rule.check({
            "TOP": [wide_central, top_platform(), *top_contacts(), partial],
        })
        partial_hit = partial_result.details["per_role"]["TOP"]["hits"][0]
        self.assertTrue(partial_result.triggered)
        self.assertGreater(partial_hit["platform_overlap_px"], 0)
        self.assertGreater(partial_hit["forbidden_pixels"], 0)

    def test_sinks_reference_fail_closed_only_when_shells_exist(self):
        rule = TopSinksRule(self.thresholds)
        no_sinks = rule.check({"TOP": []})
        self.assertFalse(no_sinks.triggered)
        self.assertEqual(no_sinks.drawings, [])

        shell = rect("shells", 850, 250, 855, 255)
        with_sink = rule.check({"TOP": [shell]})
        self.assertTrue(with_sink.triggered)
        self.assertEqual(
            with_sink.details["per_role"]["TOP"]["reason"],
            "invalid_case_central_reference",
        )
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and str(drawing.get("message", "")).startswith("CASE CENTRAL")
            for drawing in with_sink.drawings
        ))

        central = rect("case_central", 800, 200, 1000, 400)
        missing_contact = rule.check({
            "TOP": [central, top_platform(), *top_contacts()[:-1], shell],
        })
        missing_details = missing_contact.details["per_role"]["TOP"]
        self.assertTrue(missing_contact.triggered)
        self.assertEqual(
            missing_details["reason"], "insufficient_valid_contacts",
        )
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "CONTACTS REF 13/14"
            for drawing in missing_contact.drawings
        ))

        invalid_shell = dict(shell, mask=None)
        invalid = rule.check({"TOP": [invalid_shell]})
        self.assertTrue(invalid.triggered)
        self.assertEqual(
            invalid.details["per_role"]["TOP"]["reason"],
            "invalid_sink_masks",
        )
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "NO SHELL MASK #1"
            for drawing in invalid.drawings
        ))
        source = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/top_sinks.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("cv2.fillPoly", source)
        self.assertNotIn("cv2.putText", source)
        self.assertNotIn("draw_text_with_bg", source)

    def test_glass_no_detection_does_not_require_references(self):
        cleanup = TopGlassRule(self.thresholds).check({"TOP": []})
        bad = TopGlassOnContactsRule(self.thresholds).check({"TOP": []})
        self.assertFalse(cleanup.triggered)
        self.assertFalse(bad.triggered)

    def test_glass_on_platform_cleanup_and_on_contact_bad(self):
        refs = glass_references()
        on_platform = rect("glass", 400, 300, 410, 310)
        cleanup = TopGlassRule(self.thresholds).check({
            "TOP": [*refs, on_platform],
        })
        bad = TopGlassOnContactsRule(self.thresholds).check({
            "TOP": [*refs, on_platform],
        })
        self.assertTrue(cleanup.triggered)
        self.assertFalse(bad.triggered)
        cleanup_details = cleanup.details["per_role"]["TOP"]
        self.assertEqual(cleanup_details["cleanup_hits"], 1)
        self.assertEqual(cleanup_details["hits"][0]["route"], "CLEANUP")
        self.assertGreater(
            cleanup_details["hits"][0]["platform_overlap_px"], 0,
        )
        self.assertTrue(any(
            drawing.get("type") == "top_glass_cleanup_references"
            for drawing in cleanup.drawings
        ))
        self.assertTrue(any(
            drawing.get("type") == "top_glass_cleanup_region"
            for drawing in cleanup.drawings
        ))

        outside = rect("glass", 1100, 600, 1110, 610)
        outside_result = TopGlassRule(self.thresholds).check({
            "TOP": [*refs, outside],
        })
        self.assertFalse(outside_result.triggered)
        self.assertEqual(outside_result.drawings, [])

        invalid_extra_contact = dict(
            rect("contacts", 50, 500, 70, 520),
            mask=None,
        )
        with_extra = TopGlassRule(self.thresholds).check({
            "TOP": [*refs, invalid_extra_contact, on_platform],
        })
        self.assertTrue(with_extra.triggered)

        contact = top_contacts()[0]
        on_contact = rect(
            "glass",
            contact["bbox"][0], contact["bbox"][1],
            contact["bbox"][0] + 5, contact["bbox"][1] + 5,
        )
        cleanup = TopGlassRule(self.thresholds).check({
            "TOP": [*refs, on_contact],
        })
        bad = TopGlassOnContactsRule(self.thresholds).check({
            "TOP": [*refs, on_contact],
        })
        self.assertFalse(cleanup.triggered)
        self.assertTrue(bad.triggered)
        self.assertEqual(
            cleanup.details["per_role"]["TOP"]["on_contacts_indices"],
            [1],
        )
        self.assertEqual(cleanup.drawings, [])
        bad_details = bad.details["per_role"]["TOP"]
        self.assertEqual(len(bad_details["pairs"]), 1)
        self.assertEqual(bad_details["pairs"][0]["glass_index"], 1)
        self.assertEqual(bad_details["pairs"][0]["contact_index"], 1)
        self.assertGreater(bad_details["pairs"][0]["overlap_pixels"], 0)
        self.assertTrue(any(
            drawing.get("type") == "top_glass_bad_references"
            for drawing in bad.drawings
        ))
        self.assertTrue(any(
            drawing.get("type") == "top_glass_contact_overlap"
            for drawing in bad.drawings
        ))

        two_contacts = rect("glass", 230, 210, 270, 310)
        multi_bad = TopGlassOnContactsRule(self.thresholds).check({
            "TOP": [*refs, two_contacts],
        })
        multi_pairs = multi_bad.details["per_role"]["TOP"]["pairs"]
        self.assertTrue(multi_bad.triggered)
        self.assertEqual(len(multi_pairs), 2)
        self.assertEqual(
            {pair["contact_index"] for pair in multi_pairs},
            {1, 2},
        )

        glass_renderer_source = (
            Path(__file__).resolve().parents[1]
            / "vision/overlay/renderers/top_glass.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("cv2.fillPoly", glass_renderer_source)
        self.assertNotIn("cv2.putText", glass_renderer_source)
        self.assertNotIn("draw_text_with_bg", glass_renderer_source)
        self.assertNotIn("top_glass_defect", glass_renderer_source)

    def test_glass_reference_failure_routes_bad_not_cleanup(self):
        glass = rect("glass", 10, 10, 20, 20)
        cleanup = TopGlassRule(self.thresholds).check({"TOP": [glass]})
        bad = TopGlassOnContactsRule(self.thresholds).check({"TOP": [glass]})
        self.assertFalse(cleanup.triggered)
        self.assertTrue(cleanup.details["per_role"]["TOP"]["skipped"])
        self.assertTrue(bad.triggered)
        self.assertTrue(bad.details["per_role"]["TOP"]["reference_fail"])
        self.assertTrue(any(
            drawing.get("type") == "top_glass_bad_glass"
            for drawing in bad.drawings
        ))
        self.assertTrue(any(
            drawing.get("type") == "construction_error"
            and drawing.get("message") == "NO PLATFORM"
            for drawing in bad.drawings
        ))

    def test_rules_render_without_embedded_statistics(self):
        contacts_result = TopContactsRule(self.thresholds).check({
            "TOP": [top_platform(), *top_contacts()],
        })
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        rendered = DebugOverlay.render_frame(frame, "TOP", [contacts_result])
        self.assertGreater(int(rendered.sum()), 0)


if __name__ == "__main__":
    unittest.main()
