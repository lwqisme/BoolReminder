"""
Characterization tests for the Parameter Lab refactoring.
These assert that key DOM elements, JS functions, API endpoints, and
template variables survive the tab-extraction refactor unchanged.

Run: python -m unittest test_parameter_lab_refactor -v
"""

import re
import unittest

from web.app import app


def _get_parameter_lab_html():
    """Fetch the Parameter Lab page HTML once per test class."""
    with app.test_client() as client:
        return client.get("/strategy-lab/parameter-lab").get_data(as_text=True)


class ParameterLabCharacterizationTest(unittest.TestCase):
    """Snapshot of pre-refactor behaviour — all these MUST still pass after refactor."""

    @classmethod
    def setUpClass(cls):
        cls.html = _get_parameter_lab_html()

    # ── Preset Management DOM ────────────────────────────────────────

    def test_preset_management_panel_exists(self):
        self.assertIn('id="presetMgmtPanel"', self.html)

    def test_preset_tab_buttons_exist(self):
        self.assertIn('id="presetTabLeaps"', self.html)
        self.assertIn('id="presetTabStock"', self.html)

    def test_preset_lists_exist(self):
        self.assertIn('id="presetLeapsList"', self.html)
        self.assertIn('id="presetStockList"', self.html)

    def test_preset_management_heading(self):
        self.assertIn("💾 预设管理", self.html)

    # ── Signal Config DOM ────────────────────────────────────────────

    def test_signal_config_section_exists(self):
        self.assertIn('id="signalConfigSection"', self.html)

    def test_signal_config_heading(self):
        self.assertIn("🔔 策略信号配置", self.html)

    def test_signal_config_body_exists(self):
        self.assertIn('id="signalConfigBody"', self.html)

    def test_signal_bindings_list_exists(self):
        self.assertIn('id="signalBindingsList"', self.html)

    def test_signal_results_container_exists(self):
        self.assertIn('id="signalResults"', self.html)

    # ── Preset Management JS Functions ───────────────────────────────

    def test_switch_preset_tab_function(self):
        self.assertIn("function switchPresetTab(", self.html)

    def test_load_presets_function(self):
        self.assertIn("async function loadPresets(", self.html)

    def test_is_leaps_preset_function(self):
        self.assertIn("function isLeapsPreset(", self.html)

    def test_rename_preset_function(self):
        self.assertIn("async function renamePreset(", self.html)

    def test_delete_preset_function(self):
        self.assertIn("async function deletePreset(", self.html)

    def test_render_preset_leaps_list_function(self):
        self.assertIn("function renderPresetLeapsList(", self.html)

    def test_render_preset_stock_list_function(self):
        self.assertIn("function renderPresetStockList(", self.html)

    # ── Signal Config JS Functions ───────────────────────────────────

    def test_toggle_signal_config_function(self):
        self.assertIn("function toggleSignalConfig(", self.html)

    def test_add_signal_binding_function(self):
        self.assertIn("function addSignalBinding(", self.html)

    def test_run_signal_dry_run_function(self):
        self.assertIn("async function runSignalDryRun(", self.html)

    def test_load_signal_bindings_function(self):
        self.assertIn("async function loadSignalBindings(", self.html)

    def test_save_signal_bindings_function(self):
        self.assertIn("async function saveSignalBindings(", self.html)

    def test_load_preset_list_function(self):
        self.assertIn("async function loadPresetList(", self.html)

    def test_preset_select_html_function(self):
        self.assertIn("function presetSelectHtml(", self.html)

    def test_render_signal_bindings_function(self):
        self.assertIn("function renderSignalBindings(", self.html)

    # ── API Endpoints (must remain callable) ─────────────────────────

    def test_preset_api_endpoint(self):
        self.assertIn("/api/strategy-lab/presets", self.html)

    def test_signal_bindings_api_endpoint(self):
        self.assertIn("/api/strategy-lab/signals/bindings", self.html)

    def test_leaps_simulate_api_endpoint(self):
        self.assertIn("/api/strategy-lab/parameter-lab/leaps-simulate", self.html)

    def test_stock_simulate_api_endpoint(self):
        self.assertIn("/api/strategy-lab/parameter-lab/stock-simulate", self.html)

    # ── Matrix / Core DOM (must remain on parameter-lab page) ────────

    def test_matrix_table_exists(self):
        self.assertIn('id="matrixTable"', self.html)
        self.assertIn('id="matrixHead"', self.html)
        self.assertIn('id="matrixBody"', self.html)

    def test_run_button_area_exists(self):
        self.assertIn("runWorkerPool(", self.html)

    def test_parameter_lab_worker_url(self):
        self.assertIn("parameterLabWorkerUrl", self.html)

    def test_cost_details_exists(self):
        self.assertIn('id="costDetails"', self.html)

    # ── GA Panel DOM ─────────────────────────────────────────────────

    def test_leaps_ga_panel_exists(self):
        self.assertIn('id="leapsGaMainPanel"', self.html)

    def test_stock_ga_panel_in_sidebar(self):
        """Stock GA panel is in the sidebar."""
        self.assertIn("遗传算法 (GA)", self.html)

    # ── Global JS utilities (must remain accessible) ─────────────────

    def test_escape_html_utility(self):
        self.assertIn("function escapeHtml(", self.html)

    def test_money_utility(self):
        self.assertIn("function money(", self.html)

    def test_pct_utility(self):
        self.assertIn("function pct(", self.html)

    # ── Template variables (verify rendered output, not Jinja2 names) ─────

    def test_default_config_reflects_in_html(self):
        """default_config values must be injected into form fields."""
        self.assertIn('id="maxDrawdown"', self.html)
        self.assertIn('id="stepPct"', self.html)
        self.assertIn('id="reservePosition"', self.html)

    def test_default_portfolio_reflects_in_html(self):
        """default_portfolio JSON must be in the portfolio textarea."""
        self.assertIn('id="portfolioText"', self.html)

    def test_synced_symbols_reflects_in_html(self):
        """synced_symbols JS constant must exist for signal binding."""
        self.assertIn('const SYNCD_SYMBOLS', self.html)

    def test_strategy_labels_in_html(self):
        """Buy/sell strategy labels must be rendered in check-grid."""
        self.assertIn('id="buyStrategies"', self.html)
        self.assertIn('id="sellStrategies"', self.html)


class ParameterLabTabStructureTest(unittest.TestCase):
    """Tests for the new tab navigation structure.
    These will FAIL initially (RED), then pass once tabs are implemented (GREEN)."""

    @classmethod
    def setUpClass(cls):
        cls.html = _get_parameter_lab_html()

    def test_tab_navigation_bar_exists(self):
        """Page must have a tab navigation bar with 3 tabs."""
        self.assertIn('id="mainTabNav"', self.html)

    def test_backtest_tab_button_exists(self):
        self.assertIn('data-tab="backtest"', self.html)
        self.assertIn("📊 参数回测", self.html)

    def test_preset_signal_tab_button_exists(self):
        self.assertIn('data-tab="preset-signal"', self.html)
        self.assertIn("💾 预设 & 信号", self.html)

    def test_leaps_ga_tab_button_exists(self):
        self.assertIn('data-tab="leaps-ga"', self.html)
        self.assertIn("🧬 LEAPS GA", self.html)

    def test_backtest_tab_panel_exists(self):
        self.assertIn('id="tabBacktest"', self.html)

    def test_preset_signal_tab_panel_exists(self):
        self.assertIn('id="tabPresetSignal"', self.html)

    def test_leaps_ga_tab_panel_exists(self):
        self.assertIn('id="tabLeapsGa"', self.html)

    def test_preset_management_inside_preset_signal_tab(self):
        """💾 预设管理 must be inside the preset-signal tab panel."""
        # Find the tab panel, then verify presetMgmtPanel is inside it
        tab_start = self.html.find('id="tabPresetSignal"')
        self.assertGreater(tab_start, 0, "tabPresetSignal panel must exist")
        preset_pos = self.html.find('id="presetMgmtPanel"', tab_start)
        self.assertGreater(preset_pos, tab_start,
                           "presetMgmtPanel must be inside tabPresetSignal")

    def test_signal_config_inside_preset_signal_tab(self):
        """🔔 策略信号配置 must be inside the preset-signal tab panel."""
        tab_start = self.html.find('id="tabPresetSignal"')
        self.assertGreater(tab_start, 0, "tabPresetSignal panel must exist")
        signal_pos = self.html.find('id="signalConfigSection"', tab_start)
        self.assertGreater(signal_pos, tab_start,
                           "signalConfigSection must be inside tabPresetSignal")

    def test_matrix_inside_backtest_tab(self):
        """Matrix table must be inside the backtest tab panel."""
        tab_start = self.html.find('id="tabBacktest"')
        self.assertGreater(tab_start, 0, "tabBacktest panel must exist")
        matrix_pos = self.html.find('id="matrixTable"', tab_start)
        self.assertGreater(matrix_pos, tab_start,
                           "matrixTable must be inside tabBacktest")

    def test_leaps_ga_inside_leaps_ga_tab(self):
        """LEAPS GA panel must be inside the LEAPS GA tab panel."""
        tab_start = self.html.find('id="tabLeapsGa"')
        self.assertGreater(tab_start, 0, "tabLeapsGa panel must exist")
        ga_pos = self.html.find('id="leapsGaMainPanel"', tab_start)
        self.assertGreater(ga_pos, tab_start,
                           "leapsGaMainPanel must be inside tabLeapsGa")

    def test_mobile_drawer_toggle_exists(self):
        """Mobile settings drawer toggle button must exist."""
        self.assertIn('id="mobileSettingsToggle"', self.html)

    def test_mobile_settings_drawer_exists(self):
        """Mobile settings drawer overlay must exist."""
        self.assertIn('id="mobileSettingsDrawer"', self.html)


if __name__ == "__main__":
    unittest.main()
