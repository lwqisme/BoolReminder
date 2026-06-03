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
    compute_leaps_sell_signals,
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


# ── Golden file sell signal tests ──────────────────────────────────────

class GoldenSellSignalTest(unittest.TestCase):
    """Verify signal sell logic matches GA compute_sell_ladder using golden data."""

    @classmethod
    def setUpClass(cls):
        import json
        from pathlib import Path
        golden_path = Path(__file__).resolve().parent / "test_data" / "leaps_signal_golden.json"
        with open(golden_path) as f:
            cls.golden = json.load(f)

    def _prices_for(self, window_key: str) -> list[tuple[date, float]]:
        raw = self.golden["price_slices"][window_key]
        return [(date.fromisoformat(d), p) for d, p in raw]

    def _make_position(self, window_key: str, partial_sell_pct: float = 0.0) -> OpenPosition:
        """Create an OpenPosition for a window, optionally with partial prior sells."""
        ga = self.golden["expected_ga_results"][window_key]
        entry_date = date.fromisoformat(ga["entry_date"])
        entry_price = ga["entry_price"]
        # entry + 190 days = expiration
        expiration = entry_date + timedelta(days=190)
        # strike = entry * 1.10 (same as GA default)
        strike = entry_price * 1.10
        total_qty = 100.0
        total_sold = total_qty * partial_sell_pct / 100.0

        return OpenPosition(
            contract_code=f"NVDA{entry_date.strftime('%y%m%d')}C{int(strike*1000):08d}.US",
            underlying="NVDA.US",
            entry_date=entry_date,
            entry_stock_price=entry_price,
            option_buy_price=entry_price * 0.05,  # rough
            total_quantity=total_qty - total_sold,
            expiration=expiration,
            strike=strike,
            option_type="call",
            total_sold=total_sold,
        )

    def test_s1_triggers_with_no_prior_trades(self):
        """Tracer bullet: no real trades, S1 condition met → recommend S1 full."""
        ga = self.golden["expected_ga_results"]["window_2"]
        s1_expected = ga["sell_events"][0]  # S1: 31% @ 2024-05-14
        s1_date = date.fromisoformat(s1_expected["date"])

        position = self._make_position("window_2", partial_sell_pct=0.0)
        prices = self._prices_for("window_2")
        stages = [(d, p, s) for d, p, s in self.golden["stages"]]

        signals = compute_leaps_sell_signals(
            position, prices, [], stages, s1_date,
        )

        self.assertEqual(len(signals), 1, "Should have 1 sell signal")
        sig = signals[0]
        self.assertEqual(sig.stage, 1, "Should be stage 1")
        self.assertAlmostEqual(sig.pct_to_sell, 31.0, delta=0.1)
        self.assertAlmostEqual(sig.stock_price, s1_expected["price"], delta=1.0)

    def test_s2_triggers_after_s1_fully_executed(self):
        """S1 fully executed (31%), S2 condition met → recommend S2."""
        ga = self.golden["expected_ga_results"]["window_2"]
        s2_expected = ga["sell_events"][1]  # S2: 30% @ 2024-06-14
        s2_date = date.fromisoformat(s2_expected["date"])

        position = self._make_position("window_2", partial_sell_pct=0.0)
        prices = self._prices_for("window_2")
        stages = [(d, p, s) for d, p, s in self.golden["stages"]]

        # Simulate: S1 was fully executed on 2024-05-14
        s1_date = date.fromisoformat(ga["sell_events"][0]["date"])
        s1_trades = [
            OptionTrade(
                position.contract_code, "NVDA.US",
                s1_date, "sell",
                position.total_quantity * 0.31,  # sold 31% of original qty
                0.0, 0.0,
                position.expiration, position.strike, "call",
            )
        ]

        signals = compute_leaps_sell_signals(
            position, prices, s1_trades, stages, s2_date,
        )

        self.assertEqual(len(signals), 1, "Should have 1 sell signal for S2")
        sig = signals[0]
        self.assertEqual(sig.stage, 2, "Should be stage 2")
        self.assertAlmostEqual(sig.pct_to_sell, 30.0, delta=0.1)
        self.assertAlmostEqual(sig.stock_price, s2_expected["price"], delta=1.0)

    def test_s1_partial_execution_still_recommends_s1_remainder(self):
        """方案 B: S1 partially executed (15/31%), S1 still meets conditions → recommend remaining 16%."""
        ga = self.golden["expected_ga_results"]["window_2"]
        s1_expected = ga["sell_events"][0]  # S1: 31% @ 2024-05-14
        s1_date = date.fromisoformat(s1_expected["date"])

        position = self._make_position("window_2", partial_sell_pct=0.0)
        prices = self._prices_for("window_2")
        stages = [(d, p, s) for d, p, s in self.golden["stages"]]

        # Simulate: user only sold 15% of S1's 31% target on s1_date
        s1_trades = [
            OptionTrade(
                position.contract_code, "NVDA.US",
                s1_date, "sell",
                position.total_quantity * 0.15,  # partial: 15% instead of 31%
                0.0, 0.0,
                position.expiration, position.strike, "call",
            )
        ]

        signals = compute_leaps_sell_signals(
            position, prices, s1_trades, stages, s1_date,
        )

        self.assertEqual(len(signals), 1, "Should still have S1 signal")
        sig = signals[0]
        self.assertEqual(sig.stage, 1, "Should remain stage 1")
        self.assertAlmostEqual(sig.pct_to_sell, 16.0, delta=0.2, msg=f"Expected ~16% remaining, got {sig.pct_to_sell}")

    def test_no_signal_when_s2_not_met_after_s1_executed(self):
        """S1 fully executed, but date is before S2 conditions are met → no signal."""
        ga = self.golden["expected_ga_results"]["window_2"]
        s1_date = date.fromisoformat(ga["sell_events"][0]["date"])  # 2024-05-14
        check_date = s1_date + timedelta(days=5)  # 2024-05-19 — well before S2 (2024-06-14)

        position = self._make_position("window_2", partial_sell_pct=0.0)
        prices = self._prices_for("window_2")
        stages = [(d, p, s) for d, p, s in self.golden["stages"]]

        s1_trades = [
            OptionTrade(position.contract_code, "NVDA.US",
                        s1_date, "sell", position.total_quantity * 0.31,
                        0.0, 0.0, position.expiration, position.strike, "call"),
        ]

        signals = compute_leaps_sell_signals(
            position, prices, s1_trades, stages, check_date,
        )

        self.assertEqual(len(signals), 0, f"No signal expected on {check_date}, got {[(s.stage, s.pct_to_sell) for s in signals]}")

    def test_s3_triggers_after_s1_s2_executed(self):
        """S1 and S2 fully executed, S3 (remaining) triggers at hard_cutoff or when conditions met."""
        ga = self.golden["expected_ga_results"]["window_2"]
        s3_expected = ga["sell_events"][2]  # S3: 39% @ 2024-08-28
        s3_date = date.fromisoformat(s3_expected["date"])

        position = self._make_position("window_2", partial_sell_pct=0.0)
        prices = self._prices_for("window_2")
        stages = [(d, p, s) for d, p, s in self.golden["stages"]]

        # Simulate S1 and S2 fully executed
        s1_date = date.fromisoformat(ga["sell_events"][0]["date"])
        s2_date = date.fromisoformat(ga["sell_events"][1]["date"])
        s1_trades = [
            OptionTrade(position.contract_code, "NVDA.US",
                        s1_date, "sell", position.total_quantity * 0.31,
                        0.0, 0.0, position.expiration, position.strike, "call"),
            OptionTrade(position.contract_code, "NVDA.US",
                        s2_date, "sell", position.total_quantity * 0.30,
                        0.0, 0.0, position.expiration, position.strike, "call"),
        ]

        signals = compute_leaps_sell_signals(
            position, prices, s1_trades, stages, s3_date,
        )

        self.assertEqual(len(signals), 1, "Should have S3 sell signal")
        sig = signals[0]
        self.assertEqual(sig.stage, 3, "Should be stage 3 (remaining)")
        self.assertAlmostEqual(sig.pct_to_sell, 39.0, delta=0.2)
        self.assertAlmostEqual(sig.stock_price, s3_expected["price"], delta=1.0)

    def test_window_1_force_sell_at_hard_cutoff(self):
        """Window 1: S1/S2 never met (ROI too low), force-sell 100% at hard_cutoff."""
        ga = self.golden["expected_ga_results"]["window_1"]
        sell_date = date.fromisoformat(ga["sell_events"][0]["date"])  # 2023-12-18

        position = self._make_position("window_1", partial_sell_pct=0.0)
        prices = self._prices_for("window_1")
        stages = [(d, p, s) for d, p, s in self.golden["stages"]]

        signals = compute_leaps_sell_signals(
            position, prices, [], stages, sell_date,
        )

        self.assertEqual(len(signals), 1, "Should have force-sell signal")
        sig = signals[0]
        self.assertAlmostEqual(sig.pct_to_sell, 100.0, delta=0.2)
        self.assertAlmostEqual(sig.stock_price, ga["sell_events"][0]["price"], delta=1.0)

    def test_window_3_same_day_s1_s2(self):
        """Window 3: S1 and S2 trigger on same day → two signals for that day."""
        ga = self.golden["expected_ga_results"]["window_3"]
        s1_date = date.fromisoformat(ga["sell_events"][0]["date"])  # 2025-05-14
        # S1 and S2 both on 2025-05-14

        position = self._make_position("window_3", partial_sell_pct=0.0)
        prices = self._prices_for("window_3")
        stages = [(d, p, s) for d, p, s in self.golden["stages"]]

        signals = compute_leaps_sell_signals(
            position, prices, [], stages, s1_date,
        )

        self.assertEqual(len(signals), 2, f"Should have 2 signals (S1+S2 same day), got {len(signals)}")
        stages_found = {s.stage for s in signals}
        self.assertEqual(stages_found, {1, 2}, f"Should be stages 1 and 2, got {stages_found}")
        total_sold = sum(s.pct_to_sell for s in signals)
        self.assertAlmostEqual(total_sold, 61.0, delta=0.2)  # 31 + 30


# ── Test helpers ──

def build_test_prices(price: float, days: int, start: date | None = None) -> list[tuple[date, float]]:
    """Build flat price series for testing."""
    d = start or date(2026, 1, 1)
    return [(d + timedelta(days=i), price) for i in range(days)]


class AppendRealtimePriceTest(unittest.TestCase):
    """Real-time price append/replace for intraday signal generation."""

    def _mock_quote_ctx(self, *, last_done: float, market_state: str = "normal"):
        """Build a mock QuoteContext with quote() and trading_session()."""
        ctx = MagicMock()
        quote_resp = MagicMock()
        quote_resp.last_done = last_done
        ctx.quote.return_value = [quote_resp]

        session_resp = MagicMock()
        session_resp.market_state = market_state
        ctx.trading_session.return_value = [session_resp]
        return ctx

    def test_append_when_market_open_and_daily_ends_before_today(self):
        """Market open, daily data ends yesterday → append today's realtime price."""
        from drawdown.leaps_signal import append_realtime_price

        today = date(2026, 6, 3)
        daily = [
            (date(2026, 5, 1), 400.0),
            (date(2026, 6, 2), 370.0),
        ]
        ctx = self._mock_quote_ctx(last_done=361.85, market_state="normal")

        result = append_realtime_price(daily, ctx, "GOOGL.US", today)

        self.assertEqual(len(result), 3)
        self.assertEqual(result[-1][0], today)
        self.assertAlmostEqual(result[-1][1], 361.85)

    def test_replace_when_market_open_and_daily_has_today(self):
        """Market open, daily data already has today → replace last price."""
        from drawdown.leaps_signal import append_realtime_price

        today = date(2026, 6, 3)
        daily = [
            (date(2026, 5, 1), 400.0),
            (date(2026, 6, 2), 370.0),
            (date(2026, 6, 3), 365.0),
        ]
        ctx = self._mock_quote_ctx(last_done=361.85, market_state="normal")

        result = append_realtime_price(daily, ctx, "GOOGL.US", today)

        self.assertEqual(len(result), 3)
        self.assertEqual(result[-1][0], today)
        self.assertAlmostEqual(result[-1][1], 361.85)

    def test_skip_when_market_closed(self):
        """Market closed (holiday/weekend) → return daily data unchanged."""
        from drawdown.leaps_signal import append_realtime_price

        today = date(2026, 6, 3)
        daily = [
            (date(2026, 5, 1), 400.0),
            (date(2026, 6, 2), 370.0),
        ]
        ctx = self._mock_quote_ctx(last_done=361.85, market_state="closed")

        result = append_realtime_price(daily, ctx, "GOOGL.US", today)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[-1][0], date(2026, 6, 2))
        self.assertAlmostEqual(result[-1][1], 370.0)


if __name__ == "__main__":
    unittest.main()
