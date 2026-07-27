import unittest

from tests.ui_assets import load_css, load_html, load_javascript


class JogUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = load_html()
        cls.js = load_javascript()
        cls.css = load_css()

    def test_jog_has_only_two_hold_buttons_without_slider_or_software_estop(self):
        jog_section = self.html.split('id="jog-panel"', 1)[1].split(
            'class="jog-action"', 1
        )[0]
        self.assertEqual(jog_section.count("jog-hold-btn"), 2)
        self.assertIn('data-direction="-"', jog_section)
        self.assertIn('data-direction="+"', jog_section)
        self.assertNotIn('type="range"', jog_section)
        self.assertNotIn('data-action="estop"', jog_section)
        self.assertNotIn("jog-presets", jog_section)
        self.assertNotIn(".jog-slider", self.css)
        self.assertNotIn(".jog-presets", self.css)
        self.assertNotIn(".jog-dpad-center", self.css)

    def test_hold_control_stops_on_every_ui_loss_path(self):
        for token in (
            "pointerdown",
            "pointerup",
            "pointercancel",
            "lostpointercapture",
            "pointerleave",
            "window blur",
            "page hidden",
            "document hidden",
            "key released",
            "/api/jog/hold/start",
            "/api/jog/hold/heartbeat",
            "/api/jog/hold/release",
        ):
            self.assertIn(token, self.js)
        self.assertNotIn("/api/jog/params", self.js)
        self.assertNotIn("/api/jog/move", self.js)


if __name__ == "__main__":
    unittest.main()
