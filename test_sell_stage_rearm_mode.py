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
    _rearm_position_sell_cycle_after_dca_buy,
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


class SellStageRearmModeSwitchTest(unittest.TestCase):
    """Mode switch (Commit 2): default "legacy" preserves existing behavior;
    "drop_from_last_sell" is reserved for the new logic in Commit 3."""

    def test_default_mode_is_legacy(self):
        inputs = StrategyInputs()
        self.assertEqual(inputs.sell_stage_rearm_mode, "legacy")

    def test_legacy_mode_preserves_existing_rearm_behavior(self):
        # Same setup as the existing _rearm_position_sell_cycle_after_dca_buy contract:
        # threshold = 15%, drawdown = 5% → not enough; drawdown = 15% → rearmed.
        state = SymbolState(
            symbol="X.US", name="X", weight=100, budget=10000, cash=0,
            sell_marks={"cost_1"},
        )
        inputs = StrategyInputs(
            max_drawdown_pct=50,
            dca_rearm_drawdown_pct=5,
            sell_stage_rearm_drawdown_pct=15,
            sell_stage_rearm_mode="legacy",
        )
        shallow = _rearm_position_sell_cycle_after_dca_buy(state, 5, inputs, "cost_deleverage")
        self.assertFalse(shallow)
        deep = _rearm_position_sell_cycle_after_dca_buy(state, 15, inputs, "cost_deleverage")
        self.assertTrue(deep)
        self.assertEqual(state.sell_marks, set())

    def test_unknown_mode_raises_not_implemented_error(self):
        state = SymbolState(
            symbol="X.US", name="X", weight=100, budget=10000, cash=0,
            sell_marks={"cost_1"},
        )
        inputs = StrategyInputs(
            max_drawdown_pct=50,
            dca_rearm_drawdown_pct=5,
            sell_stage_rearm_drawdown_pct=15,
            sell_stage_rearm_mode="drop_from_last_sell",
        )
        with self.assertRaises(NotImplementedError):
            _rearm_position_sell_cycle_after_dca_buy(state, 20, inputs, "cost_deleverage")


if __name__ == "__main__":
    unittest.main()
