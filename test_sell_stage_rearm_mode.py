#!/usr/bin/env python3
"""Tests for sell-stage rearm redesign.

Plan A: replace the ATH-relative drawdown threshold with a "drop from
last sell price" threshold, gated by a mode switch so the legacy
behavior remains the default until explicitly opted into.

Commit 1 (this file, initial tests): SymbolState gains a
`last_position_sell_price` field that records the price at which the
most recent position-level sell happened. No behavior change yet.
"""
from __future__ import annotations

import unittest
from datetime import datetime

from drawdown.generate_drawdown_report import build_price_points_from_series
from drawdown.position_strategy import (
    StrategyInputs,
    SymbolState,
    _sell_shares,
)


def _point(day: str, price: float):
    return build_price_points_from_series([(datetime.fromisoformat(day), price)])[0]


def _state_with_shares(shares: float, avg_cost: float) -> SymbolState:
    state = SymbolState(symbol="X.US", name="X", weight=100, budget=10000, cash=0)
    state.shares = shares
    state.invested = shares * avg_cost
    state.lots = []  # _sell_shares uses fifo lots; empty list ok for this minimal test
    return state


class LastPositionSellPriceFieldTest(unittest.TestCase):
    def test_field_defaults_to_none_on_fresh_state(self):
        state = SymbolState(symbol="X.US", name="X", weight=100, budget=1000, cash=0)
        self.assertIsNone(state.last_position_sell_price)

    def test_field_records_price_after_position_sell(self):
        state = _state_with_shares(shares=100.0, avg_cost=80.0)
        point = _point("2024-01-30", 120.0)
        inputs = StrategyInputs(max_drawdown_pct=50, sell_min_profit_pct=0)
        trade_log = []

        sold = _sell_shares(
            state, point, requested_shares=20.0, inputs=inputs,
            trade_log=trade_log, sell_strategy="cost_deleverage", trigger_value=10.0,
            sell_stage="cost_1",
        )

        self.assertTrue(sold)
        self.assertIsNotNone(state.last_position_sell_price)
        self.assertAlmostEqual(state.last_position_sell_price, 120.0, places=4)


if __name__ == "__main__":
    unittest.main()
