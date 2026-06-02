"""Tests for LEAPS signal engine."""
import unittest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from drawdown.leaps_signal import (
    parse_option_contract,
    load_option_trades,
    OptionTrade,
    OpenPosition,
    LeapsSignalResult,
    EntrySignal,
    SellSignal,
    generate_leaps_signals_for_symbol,
    compute_open_positions,
    detect_entry_signals,
    detect_sell_signals,
)


class ParseOptionContractTest(unittest.TestCase):
    """Parse option contract codes like AMZN260618C00240000.US."""

    def test_call_contract(self):
        result = parse_option_contract("AMZN260618C00240000.US")
        self.assertEqual(result.underlying, "AMZN.US")
        self.assertEqual(result.expiration, date(2026, 6, 18))
        self.assertEqual(result.option_type, "call")
        self.assertAlmostEqual(result.strike, 240.0)
        self.assertEqual(result.contract_code, "AMZN260618C00240000.US")

    def test_put_contract(self):
        result = parse_option_contract("NVDA251219P00150000.US")
        self.assertEqual(result.underlying, "NVDA.US")
        self.assertEqual(result.expiration, date(2025, 12, 19))
        self.assertEqual(result.option_type, "put")
        self.assertAlmostEqual(result.strike, 150.0)

    def test_non_option_returns_none(self):
        result = parse_option_contract("AAPL.US")
        self.assertIsNone(result)
        result2 = parse_option_contract("")
        self.assertIsNone(result2)

    def test_option_with_leading_zeros_in_strike(self):
        result = parse_option_contract("TSLA260320C00080000.US")
        self.assertAlmostEqual(result.strike, 80.0)


class LoadOptionTradesTest(unittest.TestCase):
    """Load option trades from synced trade data."""

    def test_load_buys_and_sells(self):
        rows = [
            {
                "symbol": "AMZN260618C00240000.US",
                "trade_date": "2026-02-06",
                "side": "buy",
                "shares": 100,
                "price": 6.57,
                "amount": 657.0,
            },
            {
                "symbol": "AMZN260618C00240000.US",
                "trade_date": "2026-04-10",
                "side": "sell",
                "shares": 50,
                "price": 12.00,
                "amount": 600.0,
            },
        ]
        trades = load_option_trades(rows, "AMZN.US")
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0].side, "buy")
        self.assertEqual(trades[0].quantity, 100)
        self.assertEqual(trades[0].option_price, 6.57)
        self.assertEqual(trades[0].underlying, "AMZN.US")
        self.assertEqual(trades[1].side, "sell")

    def test_filters_non_option_rows(self):
        rows = [
            {"symbol": "AMZN.US", "trade_date": "2026-02-06", "side": "buy", "shares": 10, "price": 200},
            {
                "symbol": "AMZN260618C00240000.US",
                "trade_date": "2026-02-06",
                "side": "buy",
                "shares": 100,
                "price": 6.57,
                "amount": 657.0,
            },
        ]
        trades = load_option_trades(rows, "AMZN.US")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].contract_code, "AMZN260618C00240000.US")

    def test_filters_different_underlying(self):
        rows = [
            {
                "symbol": "NVDA251219C00150000.US",
                "trade_date": "2025-06-01",
                "side": "buy",
                "shares": 10,
                "price": 5.0,
                "amount": 50.0,
            },
        ]
        trades = load_option_trades(rows, "AMZN.US")
        self.assertEqual(len(trades), 0)

    def test_omits_zero_quantity_trades(self):
        rows = [
            {
                "symbol": "AMZN260618C00240000.US",
                "trade_date": "2026-02-06",
                "side": "buy",
                "shares": 0,
                "price": 6.57,
                "amount": 0.0,
            },
        ]
        trades = load_option_trades(rows, "AMZN.US")
        self.assertEqual(len(trades), 0)


class LeapsSignalResultTest(unittest.TestCase):
    """LeapsSignalResult dataclass."""

    def test_has_expected_fields(self):
        result = LeapsSignalResult(
            symbol="AMZN.US",
            preset_id="abc",
            signal_date=date(2026, 6, 2),
            entry_signals=[],
            sell_signals=[],
            open_positions=[],
            errors=[],
        )
        self.assertEqual(result.symbol, "AMZN.US")
        self.assertEqual(len(result.entry_signals), 0)


class OpenPositionsTest(unittest.TestCase):
    """Compute open positions from trade history."""

    def test_net_buy_positive_remaining(self):
        trades = [
            OptionTrade("AMZN260618C00240000.US", "AMZN.US",
                        date(2026, 2, 6), "buy", 100, 6.57, 657.0,
                        date(2026, 6, 18), 240.0, "call"),
        ]
        positions = compute_open_positions(trades)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].total_quantity, 100)

    def test_partial_sell_reduces_quantity(self):
        trades = [
            OptionTrade("AMZN260618C00240000.US", "AMZN.US",
                        date(2026, 2, 6), "buy", 100, 6.57, 657.0,
                        date(2026, 6, 18), 240.0, "call"),
            OptionTrade("AMZN260618C00240000.US", "AMZN.US",
                        date(2026, 4, 10), "sell", 50, 12.0, 600.0,
                        date(2026, 6, 18), 240.0, "call"),
        ]
        positions = compute_open_positions(trades)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].total_quantity, 50)

    def test_fully_sold_omitted(self):
        trades = [
            OptionTrade("AMZN260618C00240000.US", "AMZN.US",
                        date(2026, 2, 6), "buy", 100, 6.57, 657.0,
                        date(2026, 6, 18), 240.0, "call"),
            OptionTrade("AMZN260618C00240000.US", "AMZN.US",
                        date(2026, 4, 10), "sell", 100, 12.0, 1200.0,
                        date(2026, 6, 18), 240.0, "call"),
        ]
        positions = compute_open_positions(trades)
        self.assertEqual(len(positions), 0)

    def test_expired_contract_omitted(self):
        trades = [
            OptionTrade("AMZN250101C00240000.US", "AMZN.US",
                        date(2024, 11, 1), "buy", 100, 5.0, 500.0,
                        date(2025, 1, 1), 240.0, "call"),
        ]
        positions = compute_open_positions(trades)
        self.assertEqual(len(positions), 0)


class DetectEntrySignalsTest(unittest.TestCase):
    """Entry signal detection with cooldown."""

    def test_no_entry_when_no_drawdown(self):
        prices = build_test_prices(150.0, 60, start=date(2026, 1, 1))
        trades: list[OptionTrade] = []
        signals = detect_entry_signals(
            prices, trades, drawdown_threshold_pct=20,
            entry_mode="both", cooldown_days=5,
        )
        self.assertEqual(len(signals), 0, "No drawdown → no entry")

    def test_cooldown_blocks_entry(self):
        # Prices with a deep drawdown near the end
        prices = []
        d = date(2026, 1, 1)
        for i in range(140):
            prices.append((d, 200.0 if i < 130 else 100.0))
            d += timedelta(days=1)

        # Most recent buy was yesterday
        trades = [
            OptionTrade("AMZN260618C00240000.US", "AMZN.US",
                        d - timedelta(days=1), "buy", 100, 6.0, 600.0,
                        date(2026, 6, 18), 240.0, "call"),
        ]
        signals = detect_entry_signals(
            prices, trades, drawdown_threshold_pct=20,
            entry_mode="both", cooldown_days=5,
        )
        self.assertEqual(len(signals), 0, "Cooldown should block entry")


class DetectSellSignalsTest(unittest.TestCase):
    """Sell signal detection for open positions."""

    def test_sell_when_roi_exceeds_threshold(self):
        position = OpenPosition(
            contract_code="AMZN260618C00240000.US",
            underlying="AMZN.US",
            entry_date=date(2026, 2, 6),
            entry_stock_price=180.0,
            option_buy_price=6.57,
            total_quantity=100,
            expiration=date(2026, 6, 18),
            strike=240.0,
            option_type="call",
        )
        # Current stock is 220 → ~massive ROI on the call
        signals = detect_sell_signals(
            [position],
            current_stock_price=220.0,
            current_date=date(2026, 4, 15),
            stages=[(10, 60.0, 50.0), (30, 40.0, 50.0)],
        )
        # Should trigger stage 1 (10 day hold, 60% profit)
        self.assertGreater(len(signals), 0)

    def test_no_sell_when_hold_days_not_met(self):
        position = OpenPosition(
            contract_code="AMZN260618C00240000.US",
            underlying="AMZN.US",
            entry_date=date(2026, 4, 14),  # only 1 day ago
            entry_stock_price=180.0,
            option_buy_price=6.57,
            total_quantity=100,
            expiration=date(2026, 6, 18),
            strike=240.0,
            option_type="call",
        )
        signals = detect_sell_signals(
            [position],
            current_stock_price=220.0,
            current_date=date(2026, 4, 15),
            stages=[(10, 60.0, 50.0)],
        )
        self.assertEqual(len(signals), 0, "Should not sell before min hold days")

    def test_no_sell_when_roi_below_threshold(self):
        position = OpenPosition(
            contract_code="AMZN260618C00240000.US",
            underlying="AMZN.US",
            entry_date=date(2026, 2, 6),
            entry_stock_price=180.0,
            option_buy_price=6.57,
            total_quantity=100,
            expiration=date(2026, 6, 18),
            strike=240.0,
            option_type="call",
        )
        # Stock price flat → little or negative ROI
        signals = detect_sell_signals(
            [position],
            current_stock_price=185.0,
            current_date=date(2026, 4, 15),
            stages=[(10, 60.0, 50.0)],
        )
        self.assertEqual(len(signals), 0, "No sell when ROI below threshold")


# ── Test helpers ──

def build_test_prices(price: float, days: int, start: date | None = None) -> list[tuple[date, float]]:
    """Build flat price series for testing."""
    d = start or date(2026, 1, 1)
    return [(d + timedelta(days=i), price) for i in range(days)]


if __name__ == "__main__":
    unittest.main()
