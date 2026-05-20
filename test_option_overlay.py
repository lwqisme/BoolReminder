#!/usr/bin/env python3
"""Offline checks for option overlay simulation."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from drawdown.option_overlay import OptionOverlaySettings, batch_fetch_option_data, replay_option_wallet
from drawdown.option_overlay import _build_strategy_option_overlay
from drawdown.option_provider import OptionBar, OptionDataProvider


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
                    "gross_amount": 10000,
                }
            ]
        }
        overlay = _build_strategy_option_overlay(
            strategy,
            FakeOptionClient(),
            OptionOverlaySettings(
                enabled=True,
                wallet_pct=20,
                trade_allocation_pct=30,
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
        self.assertAlmostEqual(position["premium"], 2000)
        self.assertAlmostEqual(position["contracts"], 2)
        self.assertEqual(position["exits"][0]["reason"], "profit_take")
        self.assertGreater(position["return_pct"], 0)

    def test_batch_fetch_selects_contract_closest_to_target_inside_dte_window(self):
        class Provider(OptionDataProvider):
            def get_option_chain(self, *args, **kwargs):
                return None

            def fetch_option_contracts(self, underlying, as_of, start_expiration, end_expiration):
                base = date(2026, 1, 1)
                return [
                    {"ticker": "TSLA240", "expiration_date": (base + timedelta(days=240)).isoformat(), "strike_price": 110},
                    {"ticker": "TSLA249", "expiration_date": (base + timedelta(days=249)).isoformat(), "strike_price": 130},
                    {"ticker": "TSLA251", "expiration_date": (base + timedelta(days=251)).isoformat(), "strike_price": 111},
                    {"ticker": "TSLA310", "expiration_date": (base + timedelta(days=310)).isoformat(), "strike_price": 110},
                ]

            def get_option_history(self, ticker, start, end):
                return [OptionBar(date(2026, 1, 1), 1, 1, 1, 1)]

        lookup, warnings = batch_fetch_option_data(
            [{"action": "buy", "date": "2026-01-01", "symbol": "TSLA.US", "price": 100}],
            OptionOverlaySettings(),
            Provider(),
            ["otm_10"],
            [250],
            date(2026, 12, 31),
            dte_windows=[(200, 250, 300)],
        )

        self.assertEqual(warnings, [])
        selected = lookup[("TSLA", "2026-01-01", "otm_10", 250)]["contract"]
        self.assertEqual(selected.ticker, "TSLA251")
        self.assertEqual((selected.expiration - date(2026, 1, 1)).days, 251)

    def test_wallet_buys_on_each_stock_buy_when_contracts_and_cash_are_available(self):
        lookup = {
            ("TSLA", "2026-01-01", "otm_10", 250): {
                "contract": _contract("TSLA1", date(2026, 9, 8), 110),
                "bars": [
                    OptionBar(date(2026, 1, 1), 8, 8, 8, 8),
                    OptionBar(date(2026, 2, 1), 8, 8, 8, 8),
                ],
            },
            ("TSLA", "2026-02-02", "otm_10", 250): {
                "contract": _contract("TSLA2", date(2026, 10, 10), 110),
                "bars": [
                    OptionBar(date(2026, 2, 2), 8, 8, 8, 8),
                    OptionBar(date(2026, 3, 1), 8, 8, 8, 8),
                ],
            },
        }

        result = replay_option_wallet(
            [
                {"action": "buy", "date": "2026-01-01", "symbol": "TSLA.US", "price": 100, "gross_amount": 1000},
                {"action": "buy", "date": "2026-02-02", "symbol": "TSLA.US", "price": 100, "gross_amount": 1000},
            ],
            lookup,
            OptionOverlaySettings(
                enabled=True,
                wallet_pct=100,
                trade_allocation_pct=50,
                target_dte=250,
                min_dte=200,
                max_dte=300,
                moneyness="otm_10",
                trade_fee=0,
            ),
            {"initial_cash": 4000, "monthly_contribution": 0},
            date(2026, 3, 2),
        )

        self.assertEqual(len(result["positions"]), 2)
        self.assertEqual(result["skipped"], [])

    def test_profit_take_cash_is_available_for_later_stock_buy(self):
        lookup = {
            ("TSLA", "2026-01-01", "otm_10", 250): {
                "contract": _contract("TSLA1", date(2026, 9, 8), 110),
                "bars": [
                    OptionBar(date(2026, 1, 1), 8, 8, 8, 8),
                    OptionBar(date(2026, 2, 1), 20, 20, 20, 20),
                ],
            },
            ("TSLA", "2026-02-02", "otm_10", 250): {
                "contract": _contract("TSLA2", date(2026, 10, 10), 110),
                "bars": [OptionBar(date(2026, 2, 2), 8, 8, 8, 8)],
            },
        }

        result = replay_option_wallet(
            [
                {"action": "buy", "date": "2026-01-01", "symbol": "TSLA.US", "price": 100, "gross_amount": 1000},
                {"action": "buy", "date": "2026-02-02", "symbol": "TSLA.US", "price": 100, "gross_amount": 1000},
            ],
            lookup,
            OptionOverlaySettings(
                enabled=True,
                wallet_pct=100,
                trade_allocation_pct=100,
                target_dte=250,
                min_dte=200,
                max_dte=300,
                moneyness="otm_10",
                profit_take_pct=100,
                profit_take_sell_pct=100,
                trade_fee=0,
            ),
            {"initial_cash": 1000, "monthly_contribution": 0},
            date(2026, 2, 3),
        )

        self.assertEqual(len(result["positions"]), 2)
        self.assertEqual(result["positions"][0]["exits"][0]["reason"], "profit_take")
        self.assertEqual(result["skipped"], [])
        self.assertAlmostEqual(result["metrics"]["total_value"], 3600)
        self.assertAlmostEqual(result["metrics"]["return_pct"], 50)


def _contract(ticker: str, expiration: date, strike: float):
    from drawdown.option_provider import OptionContractInfo

    return OptionContractInfo(
        ticker=ticker,
        underlying="TSLA",
        expiration=expiration,
        strike=strike,
        contract_type="call",
    )


if __name__ == "__main__":
    unittest.main()
