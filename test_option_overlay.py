#!/usr/bin/env python3
"""Offline checks for option overlay simulation."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from drawdown.option_overlay import OptionOverlaySettings, _build_strategy_option_overlay


class FakeOptionClient:
    def choose_call_contract(self, underlying, as_of, underlying_price, settings):
        return {
            "ticker": f"O:{underlying}270115C00100000",
            "expiration_date": (as_of + timedelta(days=365)).isoformat(),
            "strike_price": 100,
            "contract_type": "call",
        }

    def option_history(self, ticker, start_date, end_date):
        return [
            {"date": start_date, "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100},
            {"date": start_date + timedelta(days=10), "open": 21, "high": 21, "low": 21, "close": 21, "volume": 100},
            {"date": start_date + timedelta(days=20), "open": 16, "high": 16, "low": 16, "close": 16, "volume": 100},
        ]


class OptionOverlayTest(unittest.TestCase):
    def test_option_overlay_buys_on_stock_buy_and_profit_takes(self):
        strategy = {
            "trades": [
                {
                    "action": "buy",
                    "date": "2026-01-02",
                    "symbol": "TSLA.US",
                    "price": 100,
                    "gross_amount": 1000,
                }
            ]
        }
        overlay = _build_strategy_option_overlay(
            strategy,
            FakeOptionClient(),
            OptionOverlaySettings(
                enabled=True,
                allocation_pct=20,
                profit_take_pct=100,
                profit_take_sell_pct=50,
                trade_fee=0,
            ),
            date(2026, 2, 1),
            [],
        )

        self.assertEqual(overlay["metrics"]["position_count"], 1)
        position = overlay["positions"][0]
        self.assertEqual(position["underlying"], "TSLA")
        self.assertAlmostEqual(position["premium"], 200)
        self.assertAlmostEqual(position["contracts"], 0.2)
        self.assertEqual(position["exits"][0]["reason"], "profit_take")
        self.assertGreater(position["return_pct"], 0)


if __name__ == "__main__":
    unittest.main()
