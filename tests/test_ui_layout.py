import unittest
from pathlib import Path

from tests.ui_assets import (
    css_paths,
    javascript_paths,
    load_css,
    load_html,
    load_javascript,
)


class UiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = load_html()
        cls.js = load_javascript()
        cls.css = load_css()

    def test_right_column_scrolls_without_overlapping_jog(self):
        self.assertIn("overflow-y: auto", self.css)
        self.assertIn("scrollbar-gutter: stable", self.css)
        self.assertIn(".jog-panel.jog-panel-embedded", self.css)
        self.assertIn("overflow: hidden", self.css)
        self.assertIn("isolation: isolate", self.css)

    def test_part_path_is_below_main_camera_and_distributor_is_right_panel(self):
        camera = self.html.index('class="camera-container"')
        process_line = self.html.index('class="process-line-panel"')
        history = self.html.index('class="history"')
        stats = self.html.index('id="stats-panel"')
        stats_summary = self.html.index('id="stats-summary"')
        defects = self.html.index('id="defects-section"')
        stats_service = self.html.index('id="stats-service"')
        distributor = self.html.index('id="distributor-diagnostics"')
        jog = self.html.index('id="jog-panel"')
        frame_analysis = self.html.index('id="frame-analysis-panel"')
        self.assertLess(camera, process_line)
        self.assertLess(process_line, history)
        self.assertLess(stats, stats_summary)
        self.assertLess(stats_summary, defects)
        self.assertLess(defects, stats_service)
        self.assertLess(stats_service, distributor)
        self.assertLess(distributor, jog)
        self.assertLess(jog, frame_analysis)
        self.assertEqual(self.html.count('id="line-cells"'), 1)
        self.assertEqual(self.html.count('id="dist1-blade"'), 1)
        self.assertEqual(self.html.count('id="dist2-blade"'), 1)

    def test_startup_error_remains_visible_and_can_be_closed(self):
        self.assertIn('id="splash-exit"', self.html)
        self.assertIn("els.splashExit.addEventListener", self.js)
        main_source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("waiting for operator to close the UI", main_source)
        self.assertIn("shutdown_requested.set()", main_source)
        self.assertIn("init_thread.join(timeout=INIT_JOIN_TIMEOUT)", main_source)
        self.assertIn("_ensure_initialization_active()", main_source)
        self.assertNotIn("time.sleep(delay)\n            monitor.close_window()", main_source)

    def test_every_motion_control_is_disabled_before_first_valid_status(self):
        self.assertIn('id="btn-start" class="btn btn-start" disabled', self.html)
        self.assertIn('id="btn-exit"  class="btn btn-exit" disabled', self.html)
        self.assertIn('id="analyze-selected-frame" disabled', self.html)
        self.assertIn('id="view-mode-raw" data-view-mode="RAW" disabled', self.html)
        self.assertIn('id="view-mode-rules" data-view-mode="RULES" disabled', self.html)
        for command in (
            "DIST1_HOME",
            "DIST1_OPEN",
            "DIST2_BAD",
            "DIST2_CLEANUP",
        ):
            marker = f'data-distributor-command="{command}" disabled'
            self.assertIn(marker, self.html)
        self.assertEqual(self.html.count('class="jog-dpad-btn jog-hold-btn"'), 2)
        self.assertGreaterEqual(self.html.count('data-direction="-" disabled'), 1)
        self.assertGreaterEqual(self.html.count('data-direction="+" disabled'), 1)

    def test_selected_live_camera_has_fps_and_on_demand_model_analysis(self):
        self.assertIn('id="mode-badge"', self.html)
        self.assertNotIn('id="selected-live-fps"', self.html)
        self.assertIn('id="analyze-selected-frame" disabled', self.html)
        self.assertIn("/api/diagnostics/selected/", self.js)
        self.assertIn("/api/diagnostics/selected/release", self.js)
        self.assertIn("selected_model_analysis", self.js)
        self.assertIn("selected_model_release", self.js)
        self.assertIn("ВЕРНУТЬ ПОТОК", self.js)
        self.assertIn("ПОТОК · ${formatFrameRate(state.liveFps)}", self.js)
        self.assertIn("КАДР/С", self.js)
        self.assertIn("mode-analysis", self.js)
        self.assertIn('id="view-mode-raw"', self.html)
        self.assertIn('id="view-mode-rules"', self.html)
        self.assertIn("setViewMode", self.js)
        self.assertIn("state.mainCamMode       = 'live-pull'", self.js)
        self.assertIn("LIVE_CAM_MIN_GAP     = 1000 / 30", self.js)
        self.assertIn("live=1&t=${Date.now()}", self.js)
        self.assertNotIn("`/stream/${desiredRole}", self.js)
        self.assertIn("pointer-events: none", self.css)
        self.assertNotIn("animation: pulse 1.2s", self.css)
        self.assertIn('id="frame-analysis-panel"', self.html)
        self.assertIn('id="frame-analysis-models"', self.html)
        self.assertIn('id="frame-analysis-rules"', self.html)
        production_source = (
            Path(__file__).resolve().parents[1] / "core/production_cycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn("АНАЛИЗ ТЕКУЩЕГО КАДРА", production_source)
        self.assertNotIn("АНАЛИЗ ТЕКУЩЕГО ЦИКЛА", production_source)
        self.assertIn("updateFrameAnalysisStatus", self.js)
        self.assertIn("frame-analysis-reason", self.js)
        self.assertIn("rule.triggered || rule.skipped || rule.show_detail", self.js)
        self.assertIn("rule.detail_lines", self.js)
        self.assertIn("rule.status_label ||", self.js)
        self.assertIn("${runCount} ПРОГОНА", self.js)
        self.assertIn("model.detections_by_run", self.js)
        self.assertIn("const stateClass = rule.neutral", self.js)
        self.assertIn(".frame-analysis-reason", self.css)
        self.assertIn("state.mainCamAnalysisKey === analysisKey", self.js)
        self.assertIn("state.frameVersions[role]", self.js)
        self.assertIn("img.dataset.frameKey === frameKey", self.js)
        self.assertNotIn("animateUiElement(img, 'ui-frame-change')", self.js)

    def test_right_panel_has_no_general_camera_or_neural_prestart_block(self):
        for element_id in (
            "prestart-diagnostics",
            "check-cameras",
            "check-vision-rules",
            "diagnostic-status",
            "diagnostic-details",
        ):
            self.assertNotIn(f'id="{element_id}"', self.html)
        self.assertIn('id="distributor-diagnostics"', self.html)

    def test_distributor_has_four_prestart_diagnostic_positions(self):
        for command in (
            "DIST1_HOME",
            "DIST1_OPEN",
            "DIST2_BAD",
            "DIST2_CLEANUP",
        ):
            self.assertIn(f'data-distributor-command="{command}"', self.html)
        dist1 = self.html.split('id="dist1-card"', 1)[1].split(
            'id="dist2-card"', 1
        )[0]
        dist2 = self.html.split('id="dist2-card"', 1)[1].split(
            'class="axis-action"', 1
        )[0]
        self.assertIn('data-distributor-command="DIST1_HOME"', dist1)
        self.assertIn('data-distributor-command="DIST1_OPEN"', dist1)
        self.assertNotIn('data-distributor-command="DIST2_', dist1)
        self.assertIn('data-distributor-command="DIST2_BAD"', dist2)
        self.assertIn('data-distributor-command="DIST2_CLEANUP"', dist2)
        self.assertNotIn('data-distributor-command="DIST1_', dist2)
        self.assertEqual(dist1.count('class="axis-route"'), 1)
        self.assertEqual(dist2.count('class="axis-route"'), 1)
        self.assertIn(".blade-card {", self.css)
        self.assertIn("flex-direction: column", self.css)
        self.assertIn("min-height: 108px", self.css)
        self.assertIn("min-height: 76px", self.css)
        self.assertIn("height: 14px", self.css)
        self.assertIn("button.dataset.distributorCommand", self.js)
        self.assertIn("diagnostic_allowed", self.js)
        self.assertIn("/api/distributor/diagnostic/", self.js)

    def test_ui_assets_are_modular_and_loaded_in_explicit_order(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "vision/ui/static/app.js").exists())
        self.assertFalse((root / "vision/ui/static/style.css").exists())
        js = javascript_paths()
        css = css_paths()
        self.assertEqual(
            [path.name for path in js],
            [
                "core.js", "boot.js", "diagnostics.js", "rule-summary.js",
                "status.js",
                "controls.js", "cameras.js", "jog.js", "history.js",
                "bootstrap.js",
            ],
        )
        self.assertGreaterEqual(len(css), 8)
        self.assertTrue(all(len(path.read_text(encoding="utf-8").splitlines()) < 600 for path in js))
        self.assertTrue(all(len(path.read_text(encoding="utf-8").splitlines()) < 500 for path in css))

    def test_strict_theme_has_no_rainbow_fills_or_cycle_animations(self):
        self.assertNotIn("gradient(", self.css)
        root = Path(__file__).resolve().parents[1]
        for name in (
            "camera.css",
            "axis.css",
            "history-strip.css",
            "stats.css",
            "jog.css",
            "process.css",
        ):
            module = (
                root / "vision/ui/static/css" / name
            ).read_text(encoding="utf-8")
            self.assertNotIn("animation:", module, name)
            self.assertNotIn("backdrop-filter", module, name)
        self.assertIn(".state-dot.state-running", self.css)
        self.assertIn("background: var(--ok)", self.css)
        self.assertIn(".state-dot.state-stopping", self.css)
        self.assertIn("background: var(--warn)", self.css)
        self.assertIn(".state-dot.state-fault", self.css)
        self.assertIn("background: var(--bad)", self.css)
        self.assertIn(".stats-value.good { color: var(--ok); }", self.css)
        self.assertIn(".stats-value.bad  { color: var(--bad); }", self.css)
        self.assertIn(".stats-value.warn { color: var(--warn); }", self.css)

    def test_operator_interface_is_russian_and_previews_refresh_frequently(self):
        for label in (
            "МОНИТОР ЛИНИИ",
            "ГОТОВА К ПУСКУ",
            "ШАГ:",
            "ВРЕМЯ РАБОТЫ:",
            "СТАТИСТИКА",
            "ПУСК",
            "СТОП",
            "ВЫХОД",
            "Пустые лотки",
            "Деталь №",
            "Дефекты:",
        ):
            self.assertIn(label, self.html)
        for old_label in (
            "LINE MONITOR",
            "STATISTICS",
            "RECENT:",
            "Empty trays",
            "TOP DEFECTS",
        ):
            self.assertNotIn(old_label, self.html)
        self.assertIn("CAMERA_ROLE_LABELS", self.js)
        self.assertIn("LINE_STATE_LABELS", self.js)
        self.assertIn("const PREVIEW_INTERVAL       = 180", self.js)
        live_preview = (
            Path(__file__).resolve().parents[1] / "core/live_preview.py"
        ).read_text(encoding="utf-8")
        self.assertIn("LIVE_TARGET_FPS = 30.0", live_preview)
        self.assertIn(
            "LIVE_FRAME_INTERVAL = 1.0 / LIVE_TARGET_FPS", live_preview
        )
        self.assertIn("LIVE_AUX_BATCH_INTERVAL = 0.20", live_preview)
        self.assertIn("target=self._auxiliary_loop", live_preview)
        self.assertIn("capture_roles(auxiliary_roles)", live_preview)

    def test_operator_terms_follow_one_unambiguous_naming_standard(self):
        self.assertIn(
            'data-distributor-command="DIST1_HOME" disabled>ПРОХОД</button>',
            self.html,
        )
        self.assertIn(
            'data-distributor-command="DIST1_OPEN" disabled>СБРОС</button>',
            self.html,
        )
        self.assertIn("DIST1 · ЗАСЛОНКА СБРОСА", self.html)
        self.assertIn("DIST2 · ВЫБОР КАНАЛА", self.html)
        self.assertIn("ПОСЛЕДНЯЯ КОМАНДА:", self.html)
        self.assertIn("РУЧНОЕ УПРАВЛЕНИЕ ЛЕНТОЙ", self.html)
        self.assertNotIn("jog-live-indicator", self.html)
        self.assertIn("КАТЕГОРИЯ: —", self.html)
        self.assertIn("РЕШЕНИЕ: —", self.html)
        self.assertIn("ВНУТРЕННИЙ ВИД", self.js)
        self.assertIn("НАРУЖНЫЙ ВИД", self.js)
        self.assertIn("STOPPING: 'ОСТАНОВКА ЛИНИИ'", self.js)
        self.assertNotIn("ОСТАНАВЛИВАЕТСЯ", self.js)

    def test_right_panel_switches_between_manual_and_production_information(self):
        for element_id in (
            "stats-summary",
            "frame-analysis-panel",
            "frame-analysis-models",
            "frame-analysis-rules",
            "distributor-diagnostics",
            "jog-panel",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('id="stats-body"', self.html)
        self.assertIn('class="accordion-chevron"', self.html)
        self.assertIn("updateOperationalAccordions", self.js)
        self.assertIn("controls-collapsed", self.js)
        self.assertIn("JOG_ALLOWED_STATES.includes(state.lineState)", self.js)
        self.assertIn("state.selectedAnalysisActive", self.js)
        self.assertIn("viewModeContextVisible", self.js)
        self.assertIn("['RUNNING', 'STOPPING']", self.js)
        self.assertIn("#frame-analysis-panel.ui-collapse { max-height: 1600px; }", self.css)
        self.assertNotIn("max-height: 176px", self.css)

    def test_professional_hmi_layout_uses_offline_fonts_and_compact_hierarchy(self):
        self.assertNotIn('fonts.googleapis.com', self.css)
        self.assertIn("--font-ui:", self.css)
        self.assertIn("--font-mono:", self.css)
        self.assertIn("--header-h:     56px", self.css)
        self.assertIn("--footer-h:     58px", self.css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) 360px", self.css)
        self.assertIn("grid-template-columns: repeat(6, minmax(0, 1fr))", self.css)
        self.assertIn('class="stats-row stats-half"', self.html)
        self.assertIn('class="stats-row stats-third"', self.html)
        self.assertIn('id="state-section"', self.html)

    def test_visual_transitions_are_smooth_and_safety_controls_remain_immediate(self):
        self.assertIn('motion.css', self.html)
        self.assertIn('.ui-collapse.is-collapsed', self.css)
        self.assertIn('max-height 240ms', self.css)
        self.assertIn('.camera-view-switch.is-faded', self.css)
        self.assertNotIn('.ui-frame-change', self.css)
        self.assertIn('.ui-content-change', self.css)
        self.assertIn('prefers-reduced-motion', self.css)
        self.assertIn("releaseJogHold('window blur')", self.js)
        self.assertIn("/api/jog/hold/release", self.js)

    def test_jog_buttons_are_equal_and_distributor_marker_is_smooth(self):
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", self.css)
        self.assertIn("height: 57px", self.css)
        self.assertIn("min-height: 57px", self.css)
        self.assertIn("max-height: 57px", self.css)
        self.assertIn("stroke: currentColor", self.css)
        self.assertEqual(self.html.count('class="jog-hold-arrow"'), 2)
        self.assertIn("M15 5 L8 12 L15 19", self.html)
        self.assertIn("M9 5 L16 12 L9 19", self.html)
        self.assertIn("box-shadow: inset 0 0 0 1px var(--accent)", self.css)

    def test_jsdom_interaction_suite_is_gated_out_of_production(self):
        self.assertIn("window.__TRANSPORTER_UI_TEST__ === true", self.js)
        self.assertIn("window.__TRANSPORTER_UI_TEST_API__", self.js)
        package = (Path(__file__).resolve().parents[1] / "package.json").read_text(
            encoding="utf-8"
        )
        self.assertIn('"test:ui"', package)
        self.assertIn('"jsdom": "24.1.3"', package)

    def test_ui_uses_backend_permissions_pending_lock_and_offline_lockout(self):
        for token in (
            "state.backendControls = ls.controls",
            "controls.start !== true",
            "controls.stop !== true",
            "controls.exit !== true",
            "state.controlPending",
            "STATUS_OFFLINE_AFTER",
            "markUiOffline",
            "Все команды заблокированы",
            "state.backendControls.jog_hold !== true",
            "bootFetchBusy",
            "statusFetchBusy",
            "camerasFetchBusy",
            "jogHeartbeatBusy",
            "activeCameraRequestBusy",
        ):
            self.assertIn(token, self.js)

    def test_part_path_has_cells_position_labels_and_live_blade_markers(self):
        for element_id in (
            "process-phase",
            "process-label",
            "process-conveyor-pos",
            "process-conveyor-target",
            "process-conveyor-moving",
            "process-conveyor-wait",
        ):
            self.assertNotIn(f'id="{element_id}"', self.html)
        self.assertNotIn('class="line-markers"', self.html)
        self.assertIn('class="line-position-labels"', self.html)
        self.assertIn("1 · ВХОД", self.html)
        self.assertIn("5 · КОНТРОЛЬ", self.html)
        self.assertIn("8 · СОРТИРОВКА", self.html)
        self.assertEqual(self.html.count('class="line-cell" data-pos='), 8)
        self.assertIn("process.positions", self.js)
        self.assertIn("setBladeMarkerPosition(els.dist1Blade", self.js)
        self.assertIn("setBladeMarkerPosition(els.dist2Blade", self.js)
        self.assertIn(
            "transition: left 100ms linear",
            self.css,
        )
        self.assertIn("STATUS_INTERVAL_MOTION = 60", self.js)
        self.assertIn("startStatusPolling()", self.js)


if __name__ == "__main__":
    unittest.main()
