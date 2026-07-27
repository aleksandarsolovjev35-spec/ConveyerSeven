import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from vision.overlay.debug_overlay import DebugOverlay


class RendererCompositionTests(unittest.TestCase):
    def frame(self):
        return np.zeros((240, 320, 3), dtype=np.uint8)

    def test_construction_errors_are_deduplicated_and_stacked(self):
        result = SimpleNamespace(drawings=[
            {
                "type": "construction_error",
                "role": "TOP",
                "message": "NO PLATFORM",
            },
            {
                "type": "construction_error",
                "role": "TOP",
                "message": "NO PLATFORM",
            },
            {
                "type": "construction_error",
                "role": "TOP",
                "message": "NO ORIENTATION",
            },
        ])
        with patch(
            "vision.overlay.debug_overlay.draw_construction_error"
        ) as draw_error:
            DebugOverlay.render_frame(self.frame(), "TOP", [result])
        self.assertEqual(draw_error.call_count, 2)
        drawings = [call.args[1] for call in draw_error.call_args_list]
        self.assertEqual(
            [drawing["message"] for drawing in drawings],
            ["NO PLATFORM", "NO ORIENTATION"],
        )
        self.assertEqual(
            [drawing["slot"] for drawing in drawings],
            [0, 1],
        )

    def test_top_reference_owners_suppress_dependent_duplicates(self):
        result = SimpleNamespace(drawings=[
            {"type": "top_contacts_item", "role": "TOP"},
            {"type": "top_platform_actual", "role": "TOP"},
            {"type": "platform_overlap_platform", "role": "TOP"},
            {"type": "top_sinks_references", "role": "TOP"},
            {"type": "top_glass_cleanup_references", "role": "TOP"},
            {"type": "top_glass_bad_references", "role": "TOP"},
        ])
        with (
            patch(
                "vision.overlay.debug_overlay.TopContactsRenderer.draw_item"
            ),
            patch(
                "vision.overlay.debug_overlay.TopPlatformRenderer.draw_actual"
            ),
            patch(
                "vision.overlay.debug_overlay.PlatformOverlapRenderer.draw_platform"
            ) as overlap_platform,
            patch(
                "vision.overlay.debug_overlay.TopSinksRenderer.draw_references"
            ) as sinks_references,
            patch(
                "vision.overlay.debug_overlay.TopGlassRenderer.draw_cleanup_references"
            ) as cleanup_references,
            patch(
                "vision.overlay.debug_overlay.TopGlassRenderer.draw_bad_references"
            ) as bad_references,
        ):
            DebugOverlay.render_frame(self.frame(), "TOP", [result])

        overlap_platform.assert_not_called()
        bad_references.assert_not_called()
        sinks_drawing = sinks_references.call_args.args[1]
        self.assertFalse(sinks_drawing["draw_platform_reference"])
        self.assertFalse(sinks_drawing["draw_contact_references"])
        cleanup_drawing = cleanup_references.call_args.args[1]
        self.assertFalse(cleanup_drawing["draw_platform_reference"])
        self.assertFalse(cleanup_drawing["draw_central_reference"])

    def test_window_sinks_reuses_window_geometry_reference(self):
        result = SimpleNamespace(drawings=[
            {"type": "window_geometry_item", "role": "INPUT_LEFT"},
            {"type": "window_sink_overlap", "role": "INPUT_LEFT"},
        ])
        with (
            patch(
                "vision.overlay.debug_overlay.WindowGeometryRenderer.draw_item"
            ),
            patch(
                "vision.overlay.debug_overlay.WindowSinksRenderer.draw_overlap"
            ) as draw_overlap,
        ):
            DebugOverlay.render_frame(
                self.frame(),
                "INPUT_LEFT",
                [result],
            )
        drawing = draw_overlap.call_args.args[1]
        self.assertFalse(drawing["draw_window_reference"])

    def test_dependent_reference_is_kept_when_owner_is_absent(self):
        result = SimpleNamespace(drawings=[
            {"type": "platform_overlap_platform", "role": "TOP"},
            {"type": "top_glass_bad_references", "role": "TOP"},
        ])
        with (
            patch(
                "vision.overlay.debug_overlay.PlatformOverlapRenderer.draw_platform"
            ) as overlap_platform,
            patch(
                "vision.overlay.debug_overlay.TopGlassRenderer.draw_bad_references"
            ) as bad_references,
        ):
            DebugOverlay.render_frame(self.frame(), "TOP", [result])
        overlap_platform.assert_called_once()
        bad_references.assert_called_once()


if __name__ == "__main__":
    unittest.main()
