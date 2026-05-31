"""Tests for strategy_signal module – signal generation end-to-end."""

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from drawdown.strategy_lab_config import StrategyLabConfig


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_test_config(**overrides) -> dict:
    """Build a legacy defaults dict for StrategyLabConfig.from_saved_defaults."""
    base = StrategyLabConfig.from_saved_defaults({
        "default_initial_cash": 5000.0,
        "default_monthly_contribution": 0.0,
        "default_buy_strategy": "pyramid_3",
        "default_sell_strategy": "none",
    }).to_legacy_defaults()
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class GenerateSignalTest(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)

    def test_generate_signal_no_preset(self):
        from drawdown.strategy_signal import generate_signal
        with patch("drawdown.strategy_signal.load_experiment_preset", return_value=None):
            result = generate_signal("FOO", "nonexistent")
            self.assertIn("error", result)
            self.assertEqual(result["symbol"], "FOO")

    def test_generate_signal_no_trades(self):
        from drawdown.strategy_signal import generate_signal
        config = _make_test_config()
        preset = {"id": "20260101_abcdef01", "config_payload": config}
        with patch("drawdown.strategy_signal.load_experiment_preset", return_value=preset):
            with patch("drawdown.strategy_signal.load_symbol_snapshot", return_value=None):
                result = generate_signal("GOOGL", "20260101_abcdef01")
                self.assertIn("error", result)

    def test_generate_signal_empty_trades(self):
        from drawdown.strategy_signal import generate_signal
        config = _make_test_config()
        preset = {"id": "20260101_abcdef01", "config_payload": config}
        with patch("drawdown.strategy_signal.load_experiment_preset", return_value=preset):
            with patch("drawdown.strategy_signal.load_symbol_snapshot", return_value={"rows": []}):
                result = generate_signal("GOOGL", "20260101_abcdef01")
                self.assertIn("error", result)

    @patch("drawdown.strategy_signal.fetch_longbridge_daily_candles")
    @patch("drawdown.strategy_signal.build_longbridge_quote_context")
    def test_generate_signal_success(self, mock_ctx, mock_fetch):
        from drawdown.strategy_signal import generate_signal

        mock_ctx.return_value = None

        config = _make_test_config(
            default_sell_strategy="repair_step",
            default_slice_step_pct=5.0,
            default_sell_min_profit_pct=10.0,
            default_repair_stage_sell_pct=10.0,
            default_repair_sell_cooldown_days=0,
        )

        preset = {"id": "20260101_abcdef01", "config_payload": config, "name": "test"}

        class MockCandle:
            def __init__(self, dt: datetime, close: float):
                self.timestamp = dt
                self.close = close

        mock_fetch.return_value = [
            MockCandle(datetime(2026, 1, d), 150.0 + d * 0.5)
            for d in range(1, 32)
        ]

        trades = [{
            "symbol": "GOOGL", "longbridge_symbol": "GOOGL.US",
            "trade_date": "2026-01-15", "side": "buy",
            "shares": 10.0, "price": 150.0, "amount": 1500.0, "note": "",
        }]

        with patch("drawdown.strategy_signal.load_experiment_preset", return_value=preset):
            with patch("drawdown.strategy_signal.load_symbol_snapshot", return_value={"rows": trades}):
                with patch("drawdown.strategy_signal._load_target_allocation", return_value=5000.0):
                    result = generate_signal("GOOGL", "20260101_abcdef01", dry_run=True)

        self.assertNotIn("error", result)
        self.assertEqual(result["symbol"], "GOOGL")
        self.assertEqual(result["preset_id"], "20260101_abcdef01")
        self.assertIn("signals", result)
        self.assertIn("current_state", result)
        self.assertTrue(result.get("dry_run"))


class SignalBindingsTest(unittest.TestCase):
    """Test that bindings save/load round-trips correctly, and generate_all_signals uses them."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)

    def test_bindings_save_and_load(self):
        from drawdown.strategy_signal import load_signal_bindings, save_signal_bindings, _signal_bindings_path

        with patch("drawdown.strategy_signal._signal_bindings_path", return_value=self.tmp / "signal_bindings.json"):
            save_signal_bindings({"GOOGL": "preset_aaa", "TSLA": "preset_bbb"})
            loaded = load_signal_bindings()
            self.assertEqual(loaded, {"GOOGL": "preset_aaa", "TSLA": "preset_bbb"})

    def test_bindings_empty_by_default(self):
        from drawdown.strategy_signal import load_signal_bindings, _signal_bindings_path

        with patch("drawdown.strategy_signal._signal_bindings_path", return_value=self.tmp / "nonexistent.json"):
            loaded = load_signal_bindings()
            self.assertEqual(loaded, {})

    def test_generate_all_signals_uses_bindings(self):
        from drawdown.strategy_signal import generate_all_signals, save_signal_bindings, _signal_bindings_path

        with patch("drawdown.strategy_signal._signal_bindings_path", return_value=self.tmp / "signal_bindings.json"):
            save_signal_bindings({"GOOGL": "preset_test"})
            with patch("drawdown.strategy_signal.generate_signal") as mock_gen:
                mock_gen.return_value = {"symbol": "GOOGL", "signals": [], "dry_run": True}
                results = generate_all_signals(dry_run=True)
                mock_gen.assert_called_once_with("GOOGL", "preset_test", signal_date=None, dry_run=True)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["symbol"], "GOOGL")

    def test_generate_all_signals_empty_bindings(self):
        from drawdown.strategy_signal import generate_all_signals, _signal_bindings_path

        with patch("drawdown.strategy_signal._signal_bindings_path", return_value=self.tmp / "nonexistent.json"):
            results = generate_all_signals()
            self.assertEqual(results, [])

    def test_generate_all_signals_catches_errors(self):
        from drawdown.strategy_signal import generate_all_signals, save_signal_bindings, _signal_bindings_path

        with patch("drawdown.strategy_signal._signal_bindings_path", return_value=self.tmp / "signal_bindings.json"):
            save_signal_bindings({"BAD": "bad_preset"})
            with patch("drawdown.strategy_signal.generate_signal", side_effect=RuntimeError("boom")):
                results = generate_all_signals(dry_run=True)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["symbol"], "BAD")
                self.assertIn("boom", results[0]["error"])


if __name__ == "__main__":
    unittest.main()
