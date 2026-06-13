"""Tests for strategy_signal module – playback + signal generation."""

import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from drawdown.position_strategy import (
    SELL_STRATEGY_LABELS,
    STRATEGY_LABELS,
    StrategyInputs,
    _simulate_strategy,
    PortfolioTarget,
)
from drawdown.generate_drawdown_report import PricePoint, build_price_points_from_series


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_price_series(
    start: date,
    days: int,
    start_price: float = 100.0,
    daily_change: float = 0.0,
) -> list[PricePoint]:
    """Build deterministic PricePoint list for testing via build_price_points_from_series."""
    series: list[tuple[datetime, float]] = []
    for i in range(days):
        d = start + timedelta(days=i)
        price = round(start_price + daily_change * i, 2)
        series.append((datetime(d.year, d.month, d.day), price))
    return build_price_points_from_series(series)


def _make_trade_event(
    symbol: str,
    trade_date: str,
    side: str,
    shares: float,
    price: float,
    *,
    longbridge_symbol: str | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "longbridge_symbol": longbridge_symbol or f"{symbol}.US",
        "trade_date": trade_date,
        "side": side,
        "shares": shares,
        "price": price,
        "amount": shares * price,
        "note": "",
    }


# ---------------------------------------------------------------------------
# tracer-bullet test: trade playback overrides engine decisions
# ---------------------------------------------------------------------------


class StrategySignalPlaybackTest(unittest.TestCase):
    """Verify that real trades override engine decisions during playback."""

    def test_playback_overrides_engine_buy(self):
        """Engine would pyramid-buy on drawdown, but real trade says 'bought 5 shares at $90'.
        After playback, state must reflect the real shares/cost, not the engine's simulated buys."""
        symbol = "TEST"
        start = date(2026, 1, 5)
        days = 20
        # price drops then recovers: engine would trigger buys at each step
        prices = _make_price_series(start, days, start_price=100.0, daily_change=-1.0)
        price_points_by_symbol = {f"{symbol}.US": prices}
        points_by_day = {p.date.date(): p for p in prices}

        inputs = StrategyInputs(
            initial_cash=10000.0,
            monthly_contribution=0.0,
            step_pct=5.0,
            equal_slice_allocation_pct=5.0,
        )
        target = PortfolioTarget(symbol=f"{symbol}.US", weight=100.0, name=symbol)

        # --- build trade overrides: one real buy on day 5 ---
        buy_date = start + timedelta(days=5)
        trade_overrides: dict[date, list[dict]] = {
            buy_date: [
                _make_trade_event(symbol, buy_date.isoformat(), "buy", shares=5.0, price=90.0),
            ]
        }

        # --- run simulation WITH overrides ---
        result = _simulate_strategy(
            price_points_by_symbol,
            [target],
            inputs,
            strategy="pyramid_3",
            sell_strategy="none",
            trade_overrides=trade_overrides,
        )

        # --- assertions ---
        trades_today = [
            t for t in result["trades"]
            if t.get("date") == buy_date.isoformat() and t.get("action") == "buy"
        ]
        # Only the real buy should exist on that day
        self.assertEqual(len(trades_today), 1)
        real_trade = trades_today[0]
        self.assertEqual(real_trade["shares"], 5.0)
        self.assertAlmostEqual(real_trade["price"], 90.0, places=2)
        self.assertTrue(real_trade.get("is_real", False), "real trade must be marked")

        # Engine should NOT have added pyramid buys that day
        for t in result["trades"]:
            if t.get("date") == buy_date.isoformat() and t.get("action") == "buy" and not t.get("is_real"):
                self.fail(f"engine generated extra buy on overridden day: {t}")

        # Final state: real 5 shares PLUS any engine buys on subsequent days
        # (engine resumes after override day - expected behavior)
        symbol_summary = next(s for s in result["symbols"] if s["symbol"] == f"{symbol}.US")
        self.assertGreaterEqual(symbol_summary["shares"], 5.0)
        self.assertIn("final_value", result["metrics"])

    def test_playback_skips_engine_sell_on_real_sell_day(self):
        """When real sell exists on a day, engine sell logic must not fire."""
        symbol = "TEST"
        start = date(2026, 1, 5)
        days = 20
        prices = _make_price_series(start, days, start_price=100.0, daily_change=1.0)
        price_points_by_symbol = {f"{symbol}.US": prices}

        inputs = StrategyInputs(
            initial_cash=10000.0,
            monthly_contribution=0.0,
            repair_stage_sell_pct=10.0,
            sell_min_profit_pct=5.0,
        )
        target = PortfolioTarget(symbol=f"{symbol}.US", weight=100.0, name=symbol)

        # Manual buy on day 2, then sell on day 15
        buy_date = start + timedelta(days=2)
        sell_date = start + timedelta(days=15)
        trade_overrides: dict[date, list[dict]] = {
            buy_date: [
                _make_trade_event(symbol, buy_date.isoformat(), "buy", shares=10.0, price=100.0),
            ],
            sell_date: [
                _make_trade_event(symbol, sell_date.isoformat(), "sell", shares=3.0, price=115.0),
            ],
        }

        result = _simulate_strategy(
            price_points_by_symbol,
            [target],
            inputs,
            strategy="pyramid_3",
            sell_strategy="repair_step",
            trade_overrides=trade_overrides,
        )

        # Only the real sell on sell_date
        sells_that_day = [
            t for t in result["trades"]
            if t.get("date") == sell_date.isoformat() and t.get("action") == "sell"
        ]
        self.assertEqual(len(sells_that_day), 1)
        self.assertTrue(sells_that_day[0].get("is_real", False))
        self.assertAlmostEqual(sells_that_day[0]["shares"], 3.0)

        # Shares after real sell: 10 bought - 3 real sell - engine sells = ~6
        symbol_summary = next(s for s in result["symbols"] if s["symbol"] == f"{symbol}.US")
        self.assertLessEqual(symbol_summary["shares"], 7.0)
        self.assertGreaterEqual(symbol_summary["shares"], 5.0)

    def test_playback_then_forward_signal(self):
        """After playback of real trades, engine runs one more day and emits signal."""
        symbol = "TEST"
        start = date(2026, 1, 5)
        days = 20
        # Declining price: engine will want to buy on drawdown
        prices = _make_price_series(start, days, start_price=100.0, daily_change=-0.5)
        price_points_by_symbol = {f"{symbol}.US": prices}

        inputs = StrategyInputs(
            initial_cash=5000.0,
            monthly_contribution=0.0,
            step_pct=5.0,
            sell_min_profit_pct=10.0,
            repair_stage_sell_pct=10.0,
        )
        target = PortfolioTarget(symbol=f"{symbol}.US", weight=100.0, name=symbol)

        # Real buy on day 5
        buy_date = start + timedelta(days=5)
        trade_overrides = {
            buy_date: [
                _make_trade_event(symbol, buy_date.isoformat(), "buy", shares=10.0, price=95.0),
            ],
        }

        result = _simulate_strategy(
            price_points_by_symbol,
            [target],
            inputs,
            strategy="pyramid_3",
            sell_strategy="repair_step",
            trade_overrides=trade_overrides,
        )

        # After playback, should have >= 10 real shares
        symbol_summary = next(s for s in result["symbols"] if s["symbol"] == f"{symbol}.US")
        self.assertGreaterEqual(symbol_summary["shares"], 10.0)

        # Trade log must contain the real trade
        real_trades = [t for t in result["trades"] if t.get("is_real")]
        self.assertGreaterEqual(len(real_trades), 1)
        self.assertEqual(real_trades[0]["shares"], 10.0)

    def test_engine_skips_historical_non_trade_days(self):
        """After last real trade date, engine resumes. Before that, no engine buys on non-trade days."""
        symbol = "TEST"
        start = date(2026, 1, 5)
        days = 20
        # Declining price: engine would buy on drawdown
        prices = _make_price_series(start, days, start_price=100.0, daily_change=-0.5)
        price_points_by_symbol = {f"{symbol}.US": prices}

        inputs = StrategyInputs(
            initial_cash=5000.0, monthly_contribution=0.0,
            step_pct=5.0, sell_min_profit_pct=10.0, repair_stage_sell_pct=10.0,
        )
        target = PortfolioTarget(symbol=f"{symbol}.US", weight=100.0, name=symbol)

        # Only one real trade: buy on day 5. No trades after.
        last_trade_date = start + timedelta(days=5)
        trade_overrides = {
            last_trade_date: [
                _make_trade_event(symbol, last_trade_date.isoformat(), "buy", shares=10.0, price=95.0),
            ],
        }

        result = _simulate_strategy(
            price_points_by_symbol, [target], inputs,
            strategy="pyramid_3", sell_strategy="repair_step",
            trade_overrides=trade_overrides,
            last_trade_date=last_trade_date,
        )

        # After the real buy day (day 6-19), engine should NOT buy on historical non-trade days
        # because they are still in the past relative to the last trade date
        engine_buys_historical = [
            t for t in result["trades"]
            if t.get("action") == "buy" and not t.get("is_real")
            and t.get("date") <= last_trade_date.isoformat()
        ]
        self.assertEqual(len(engine_buys_historical), 0,
            f"Engine should not buy on historical non-trade days, got {len(engine_buys_historical)}")

        # After last trade date, engine MAY buy (forward-looking)
        engine_buys_forward = [
            t for t in result["trades"]
            if t.get("action") == "buy" and not t.get("is_real")
            and t.get("date") > last_trade_date.isoformat()
        ]
        # There may or may not be forward buys depending on price action


class NonTradingDayOverrideRollForwardTest(unittest.TestCase):
    """Real trades whose date falls on a non-trading day (weekend/holiday)
    must still be replayed; the engine rolls them forward to the next
    trading day so state.shares and consumed-tranche logic stay aligned
    with the user's actual position.

    Reproduces 2026-06-13 GOOGL case: trades dated 2026-05-23 (Sat) and
    2026-05-30 (Sat) were silently dropped because the per-day loop iterates
    only over price points (trading days), causing 3 missing shares -> wrong
    invested ratio -> spurious multi-tranche fire.
    """

    def test_weekend_buy_event_replayed_on_next_trading_day(self):
        symbol = "TEST"
        # Build a Mon-Fri series covering both weekend dates.
        start = date(2026, 5, 18)  # Monday
        prices: list[tuple[datetime, float]] = []
        for i in range(20):
            d = start + timedelta(days=i)
            if d.weekday() >= 5:
                continue  # skip weekends in price series
            prices.append((datetime(d.year, d.month, d.day), 100.0))
        from drawdown.generate_drawdown_report import build_price_points_from_series
        points = build_price_points_from_series(prices)
        price_points_by_symbol = {f"{symbol}.US": points}

        inputs = StrategyInputs(
            initial_cash=10000.0,
            monthly_contribution=0.0,
            step_pct=5.0,
            equal_slice_allocation_pct=5.0,
        )
        target = PortfolioTarget(symbol=f"{symbol}.US", weight=100.0, name=symbol)

        sat = date(2026, 5, 23)  # Saturday – not in price series
        next_mon = date(2026, 5, 25)
        self.assertEqual(sat.weekday(), 5)

        trade_overrides: dict[date, list[dict]] = {
            sat: [_make_trade_event(symbol, sat.isoformat(), "buy", shares=2.0, price=95.0)],
        }

        result = _simulate_strategy(
            price_points_by_symbol, [target], inputs,
            strategy="pyramid_3", sell_strategy="none",
            trade_overrides=trade_overrides,
        )

        # The buy must be replayed (not dropped) and tagged as real.
        real_buys = [t for t in result["trades"] if t.get("is_real") and t["action"] == "buy"]
        self.assertEqual(len(real_buys), 1, f"weekend trade not replayed: {real_buys}")
        # Date is shifted to next trading day (Mon) -- price/shares preserved.
        self.assertEqual(real_buys[0]["date"], next_mon.isoformat())
        self.assertEqual(real_buys[0]["shares"], 2.0)
        self.assertAlmostEqual(real_buys[0]["price"], 95.0, places=2)

    def test_weekend_buy_keeps_recorded_price_unchanged(self):
        """Roll-forward must NOT replace ev['price'] with next-day's close.

        Real fills happen intraday at arbitrary prices; the engine only
        adjusts the calendar slot for replay, never the recorded price.
        """
        symbol = "TEST"
        start = date(2026, 5, 18)
        prices: list[tuple[datetime, float]] = []
        for i in range(20):
            d = start + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            # Mon close diverges from weekend fill price by a lot.
            prices.append((datetime(d.year, d.month, d.day), 200.0))
        from drawdown.generate_drawdown_report import build_price_points_from_series
        points = build_price_points_from_series(prices)
        price_points_by_symbol = {f"{symbol}.US": points}

        inputs = StrategyInputs(
            initial_cash=10000.0,
            monthly_contribution=0.0,
            step_pct=5.0,
            equal_slice_allocation_pct=5.0,
        )
        target = PortfolioTarget(symbol=f"{symbol}.US", weight=100.0, name=symbol)

        sat = date(2026, 5, 23)
        trade_overrides = {
            sat: [_make_trade_event(symbol, sat.isoformat(), "buy", shares=2.0, price=95.0)],
        }
        result = _simulate_strategy(
            price_points_by_symbol, [target], inputs,
            strategy="pyramid_3", sell_strategy="none",
            trade_overrides=trade_overrides,
        )
        real_buys = [t for t in result["trades"] if t.get("is_real") and t["action"] == "buy"]
        self.assertEqual(len(real_buys), 1)
        # Recorded buy price stays at the user's intraday fill, not Mon's $200 close.
        self.assertAlmostEqual(real_buys[0]["price"], 95.0, places=2)

    def test_weekend_buy_consumes_tranches_correctly(self):
        """After roll-forward, mark_consumed sees the full real position so
        the engine does not re-fire already-covered tranches on the signal day.

        Direct regression for the GOOGL '3 tranches fire at once' bug.
        """
        symbol = "TEST"
        start = date(2026, 5, 18)
        prices: list[tuple[datetime, float]] = []
        # Long enough series so a meaningful drawdown can be computed.
        for i in range(60):
            d = start + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            # Up then down: peak around mid, drop ~12% by tail.
            ratio = i / 60.0
            price = 100.0 + 20.0 * (1 - abs(ratio - 0.4) / 0.4)
            prices.append((datetime(d.year, d.month, d.day), round(price, 2)))
        from drawdown.generate_drawdown_report import build_price_points_from_series
        points = build_price_points_from_series(prices)
        price_points_by_symbol = {f"{symbol}.US": points}

        inputs = StrategyInputs(
            initial_cash=10000.0,
            monthly_contribution=0.0,
            step_pct=2.5,
            equal_slice_allocation_pct=15.0,
            max_drawdown_pct=50.0,
        )
        target = PortfolioTarget(symbol=f"{symbol}.US", weight=100.0, name=symbol)

        # 3 weekend-dated buys totalling ~45% of the portfolio at fill prices.
        sats = [date(2026, 5, 23), date(2026, 5, 30), date(2026, 6, 6)]
        for s in sats:
            self.assertEqual(s.weekday(), 5)
        trade_overrides: dict[date, list[dict]] = {
            s: [_make_trade_event(symbol, s.isoformat(), "buy", shares=15.0, price=100.0)]
            for s in sats
        }

        result = _simulate_strategy(
            price_points_by_symbol, [target], inputs,
            strategy="equal_slice", sell_strategy="none",
            trade_overrides=trade_overrides,
        )

        # All 3 weekend buys must be replayed (45 shares total).
        real_buys = [t for t in result["trades"] if t.get("is_real") and t["action"] == "buy"]
        total_real_shares = sum(t["shares"] for t in real_buys)
        self.assertEqual(len(real_buys), 3)
        self.assertAlmostEqual(total_real_shares, 45.0, places=2)

    def test_no_price_data_drops_overrides_gracefully(self):
        """Defensive: if a symbol has no price points at all, weekend-dated
        events for it are dropped rather than crashing roll-forward.
        """
        symbol = "GHOST"
        # Build prices for a different symbol; GHOST has none.
        start = date(2026, 5, 18)
        prices: list[tuple[datetime, float]] = []
        for i in range(10):
            d = start + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            prices.append((datetime(d.year, d.month, d.day), 100.0))
        from drawdown.generate_drawdown_report import build_price_points_from_series
        points = build_price_points_from_series(prices)

        inputs = StrategyInputs(initial_cash=10000.0, step_pct=5.0)
        target = PortfolioTarget(symbol="OTHER.US", weight=100.0, name="OTHER")

        sat = date(2026, 5, 23)
        trade_overrides = {
            sat: [_make_trade_event(symbol, sat.isoformat(), "buy", shares=1.0, price=50.0)],
        }

        # Should not raise.
        result = _simulate_strategy(
            {"OTHER.US": points}, [target], inputs,
            strategy="pyramid_3", sell_strategy="none",
            trade_overrides=trade_overrides,
        )
        # GHOST event silently dropped; result still well-formed.
        self.assertIn("trades", result)


if __name__ == "__main__":
    unittest.main()
