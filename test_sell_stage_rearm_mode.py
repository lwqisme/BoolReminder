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

    def test_default_mode_is_drop_from_last_sell(self):
        # Plan A commit 4: the rearm logic now defaults to the price-anchored
        # "drop_from_last_sell" mode. Users who want the old ATH-based behavior
        # can opt in by selecting "legacy" in the UI.
        inputs = StrategyInputs()
        self.assertEqual(inputs.sell_stage_rearm_mode, "drop_from_last_sell")

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

    def test_unknown_mode_raises_value_error(self):
        state = SymbolState(
            symbol="X.US", name="X", weight=100, budget=10000, cash=0,
            sell_marks={"cost_1"},
        )
        inputs = StrategyInputs(
            max_drawdown_pct=50,
            dca_rearm_drawdown_pct=5,
            sell_stage_rearm_drawdown_pct=15,
            sell_stage_rearm_mode="bogus_mode_xyz",
        )
        with self.assertRaises(ValueError):
            _rearm_position_sell_cycle_after_dca_buy(state, 20, inputs, "cost_deleverage")


class DropFromLastSellModeTest(unittest.TestCase):
    """Plan A core: rearm fires when current price has dropped >= threshold%
    below the last position-sell price (cost-anchored, not ATH-anchored)."""

    def _state_with_last_sell(self, last_sell_price: float, marks):
        state = SymbolState(
            symbol="X.US", name="X", weight=100, budget=10000, cash=0,
            sell_marks=set(marks),
        )
        state.last_position_sell_price = last_sell_price
        return state

    def _inputs(self, threshold: float):
        return StrategyInputs(
            max_drawdown_pct=50,
            dca_rearm_drawdown_pct=5,
            sell_stage_rearm_drawdown_pct=threshold,
            sell_stage_rearm_mode="drop_from_last_sell",
        )

    def test_no_rearm_when_drop_below_threshold(self):
        # Sold at $100; current $95 = 5% drop, threshold 16.12% → not enough.
        state = self._state_with_last_sell(last_sell_price=100.0, marks={"cost_1"})
        rearmed = _rearm_position_sell_cycle_after_dca_buy(
            state, current_price=95.0, drawdown_pct=99.0, inputs=self._inputs(16.12),
            sell_strategy="cost_deleverage",
        )
        self.assertFalse(rearmed)
        self.assertEqual(state.sell_marks, {"cost_1"})

    def test_rearm_when_drop_meets_threshold(self):
        # Sold at $100; current $80 = 20% drop, threshold 16.12% → rearmed.
        state = self._state_with_last_sell(last_sell_price=100.0, marks={"cost_1", "cost_2"})
        rearmed = _rearm_position_sell_cycle_after_dca_buy(
            state, current_price=80.0, drawdown_pct=0.0, inputs=self._inputs(16.12),
            sell_strategy="cost_deleverage",
        )
        self.assertTrue(rearmed)
        self.assertEqual(state.sell_marks, set())
        self.assertIsNone(state.last_position_sell_price)

    def test_no_rearm_when_last_sell_price_is_none(self):
        # Marks present but no recorded sell price (e.g., loaded from old state).
        state = self._state_with_last_sell(last_sell_price=100.0, marks={"cost_1"})
        state.last_position_sell_price = None
        rearmed = _rearm_position_sell_cycle_after_dca_buy(
            state, current_price=10.0, drawdown_pct=99.0, inputs=self._inputs(16.12),
            sell_strategy="cost_deleverage",
        )
        self.assertFalse(rearmed)
        self.assertEqual(state.sell_marks, {"cost_1"})

    def test_grid_rebound_clears_cycle_anchor_on_rearm(self):
        state = self._state_with_last_sell(last_sell_price=100.0, marks={"grid_1"})
        state.grid_rebound_cycle_anchor_drawdown_pct = 30.0
        state.grid_rebound_last_sell_drawdown_pct = 20.0
        rearmed = _rearm_position_sell_cycle_after_dca_buy(
            state, current_price=80.0, drawdown_pct=0.0, inputs=self._inputs(16.12),
            sell_strategy="grid_rebound",
        )
        self.assertTrue(rearmed)
        self.assertIsNone(state.grid_rebound_cycle_anchor_drawdown_pct)
        self.assertIsNone(state.grid_rebound_last_sell_drawdown_pct)

    def test_drop_threshold_uses_dca_rearm_when_sell_stage_left_blank(self):
        # If sell_stage_rearm_drawdown_pct is None or <= dca, fall back to dca
        # (matching the legacy semantics, just measured on price drop instead
        # of ATH drawdown).
        state = self._state_with_last_sell(last_sell_price=100.0, marks={"cost_1"})
        inputs = StrategyInputs(
            max_drawdown_pct=50,
            dca_rearm_drawdown_pct=10,
            sell_stage_rearm_drawdown_pct=None,
            sell_stage_rearm_mode="drop_from_last_sell",
        )
        # 8% drop: not enough vs 10% dca threshold
        rearmed = _rearm_position_sell_cycle_after_dca_buy(
            state, current_price=92.0, drawdown_pct=0.0, inputs=inputs,
            sell_strategy="cost_deleverage",
        )
        self.assertFalse(rearmed)
        # 12% drop: meets 10% dca threshold
        rearmed = _rearm_position_sell_cycle_after_dca_buy(
            state, current_price=88.0, drawdown_pct=0.0, inputs=inputs,
            sell_strategy="cost_deleverage",
        )
        self.assertTrue(rearmed)


class StrategyInputsPayloadTest(unittest.TestCase):
    """Regression: the JS worker only knows what the Python serializer emits.

    Bug found 2026-06-12: _strategy_inputs_payload (and the inline copy at
    drawdown/position_strategy.py:464) did not include sell_stage_rearm_mode,
    so packet.inputs.sell_stage_rearm_mode was undefined in the worker, which
    fell back to 'legacy' via `inputs.sell_stage_rearm_mode || 'legacy'`. GA
    runs therefore never used drop_from_last_sell even when the UI selected it.
    """

    def test_payload_emits_sell_stage_rearm_mode(self):
        from drawdown.position_strategy import _strategy_inputs_payload

        inputs = StrategyInputs(sell_stage_rearm_mode="drop_from_last_sell")
        payload = _strategy_inputs_payload(inputs)
        self.assertIn("sell_stage_rearm_mode", payload)
        self.assertEqual(payload["sell_stage_rearm_mode"], "drop_from_last_sell")

    def test_payload_emits_legacy_mode_when_selected(self):
        from drawdown.position_strategy import _strategy_inputs_payload

        inputs = StrategyInputs(sell_stage_rearm_mode="legacy")
        payload = _strategy_inputs_payload(inputs)
        self.assertEqual(payload["sell_stage_rearm_mode"], "legacy")

if __name__ == "__main__":
    unittest.main()
