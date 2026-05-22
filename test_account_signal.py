from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from drawdown.generate_drawdown_report import build_price_points_from_series

from account_signal.config import AccountSnapshot, SignalTarget, googl_inputs
from account_signal.engine import account_signal_status, run_account_signal
from account_signal.store import load_run_history, save_latest_run
from account_signal.state import AccountLot, AccountPosition, recover_position
from web.app import app


def points(*items: tuple[str, float]):
    return build_price_points_from_series([(datetime.fromisoformat(day), price) for day, price in items])


class AccountSignalTest(unittest.TestCase):
    def account(self):
        return AccountSnapshot(
            as_of="2026-05-21 21:30:00",
            currency="USD",
            cash=20000,
            buying_power=20000,
            net_liquidation=50000,
        )

    def targets(self):
        return {
            "GOOGL.US": SignalTarget("GOOGL.US", 10000, 1000, 100, True),
            "TSLA.US": SignalTarget("TSLA.US", 12000, 0, 100, True),
        }

    def test_recover_position_uses_fifo_lots_and_avg_cost(self):
        position = recover_position(
            "GOOGL.US",
            [
                {"trade_date": "2024-01-02", "side": "buy", "shares": 10, "price": 100, "amount": 1000},
                {"trade_date": "2024-01-03", "side": "buy", "shares": 10, "price": 120, "amount": 1200},
                {"trade_date": "2024-01-04", "side": "sell", "shares": 12, "price": 130, "amount": 1560},
            ],
        )

        self.assertAlmostEqual(position.shares, 8)
        self.assertEqual(len(position.lots), 1)
        self.assertAlmostEqual(position.lots[0].remaining_shares, 8)
        self.assertAlmostEqual(position.avg_cost, 120)
        self.assertEqual(position.cost_deleverage_marks, {"cost_1"})

    def test_googl_inputs_match_fixed_real_account_strategy(self):
        inputs = googl_inputs()

        self.assertEqual(inputs.cost_first_profit_pct, 15)
        self.assertEqual(inputs.cost_second_profit_pct, 25)
        self.assertEqual(inputs.cost_third_profit_pct, 40)
        self.assertEqual(inputs.cost_deleverage_cooldown_days, 30)
        self.assertEqual(inputs.sell_min_profit_pct, 15)
        self.assertEqual(inputs.dca_rearm_drawdown_pct, 0)
        self.assertEqual(inputs.sell_stage_rearm_drawdown_pct, 15)
        self.assertEqual(inputs.core_dip_timing_max_delay_days, 3)
        self.assertEqual(inputs.core_dip_timing_rise_threshold_pct, 1)
        self.assertEqual(inputs.core_dip_timing_near_low_pct, 2)

    def test_googl_cost_deleverage_thresholds(self):
        cases = [
            (114.9, [], None, None),
            (115.0, [], "cost_1", 4.0),
            (125.0, [{"trade_date": "2026-04-15", "side": "sell", "shares": 4, "price": 115, "amount": 460}], "cost_2", 1.8),
            (
                140.0,
                [
                    {"trade_date": "2026-04-15", "side": "sell", "shares": 4, "price": 115, "amount": 460},
                    {"trade_date": "2026-04-16", "side": "sell", "shares": 1.8, "price": 125, "amount": 225},
                ],
                "cost_3",
                0.84,
            ),
        ]

        for price, sells, expected_stage, expected_shares in cases:
            rows = [{"trade_date": "2026-04-01", "side": "buy", "shares": 10, "price": 100, "amount": 1000}]
            rows.extend(sells)
            position = recover_position("GOOGL.US", rows)
            market = {"GOOGL.US": points(("2026-05-18", 120), ("2026-05-19", 118), ("2026-05-20", price))}

            with self._patched_runtime({"GOOGL.US": position, "TSLA.US": recover_position("TSLA.US", [])}):
                result = run_account_signal(dry_run=True, symbols=["GOOGL.US"], price_points_by_symbol=market)

            sells = [signal for signal in result["signals"] if signal["action"] == "sell"]
            if expected_stage is None:
                self.assertEqual(sells, [])
            else:
                self.assertEqual(len(sells), 1)
                self.assertEqual(sells[0]["stage"], expected_stage)
                self.assertAlmostEqual(sells[0]["shares"], expected_shares)

    def test_googl_cost_deleverage_uses_real_sell_drawdown_context_before_fallback(self):
        position = recover_position(
            "GOOGL.US",
            [
                {"trade_date": "2026-04-01", "side": "buy", "shares": 10, "price": 100, "amount": 1000},
                {"trade_date": "2026-04-15", "side": "sell", "shares": 4, "price": 115, "amount": 460},
            ],
        )
        market = {
            "GOOGL.US": points(
                ("2026-04-01", 100),
                ("2026-04-15", 115),
                ("2026-05-18", 128),
                ("2026-05-19", 126),
                ("2026-05-20", 126),
            )
        }

        with self._patched_runtime({"GOOGL.US": position, "TSLA.US": recover_position("TSLA.US", [])}):
            result = run_account_signal(
                dry_run=True,
                symbols=["GOOGL.US"],
                price_points_by_symbol=market,
                include_debug=True,
            )

        sells = [signal for signal in result["signals"] if signal["action"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["stage"], "cost_2")
        self.assertEqual(result["debug"]["GOOGL.US"][0]["event"], "googl_cost_marks_retained")

        rearmed_market = {
            "GOOGL.US": points(
                ("2026-04-01", 100),
                ("2026-04-15", 115),
                ("2026-05-18", 150),
                ("2026-05-19", 126),
                ("2026-05-20", 125),
            )
        }
        with self._patched_runtime({"GOOGL.US": position, "TSLA.US": recover_position("TSLA.US", [])}):
            rearmed = run_account_signal(
                dry_run=True,
                symbols=["GOOGL.US"],
                price_points_by_symbol=rearmed_market,
                include_debug=True,
            )
        rearmed_sells = [signal for signal in rearmed["signals"] if signal["action"] == "sell"]
        self.assertEqual(len(rearmed_sells), 1)
        self.assertEqual(rearmed_sells[0]["stage"], "cost_1")
        self.assertEqual(rearmed["debug"]["GOOGL.US"][0]["event"], "googl_cost_marks_rearmed")

    def test_googl_historical_sells_restore_new_cost_stages(self):
        position = recover_position(
            "GOOGL.US",
            [
                {"trade_date": "2026-05-01", "side": "buy", "shares": 10, "price": 100, "amount": 1000},
                {"trade_date": "2026-05-10", "side": "sell", "shares": 1, "price": 114.9, "amount": 114.9},
                {"trade_date": "2026-05-11", "side": "sell", "shares": 1, "price": 115, "amount": 115},
                {"trade_date": "2026-05-12", "side": "sell", "shares": 1, "price": 125, "amount": 125},
                {"trade_date": "2026-05-13", "side": "sell", "shares": 1, "price": 140, "amount": 140},
            ],
        )

        self.assertEqual(position.cost_deleverage_marks, {"cost_1", "cost_2", "cost_3"})
        self.assertEqual(position.last_cost_deleverage_sell_date, "2026-05-13")

    def test_googl_same_day_buy_sell_disabled_by_default(self):
        position = recover_position(
            "GOOGL.US",
            [
                {"trade_date": "2026-05-01", "side": "buy", "shares": 100, "price": 100, "amount": 10000},
                {"trade_date": "2026-05-18", "side": "sell", "shares": 40, "price": 115, "amount": 4600},
            ],
        )
        market = {
            "GOOGL.US": points(
                ("2026-05-17", 140),
                ("2026-05-18", 115),
                ("2026-05-19", 121),
                ("2026-05-20", 128),
            )
        }
        account = AccountSnapshot("2026-05-20", "USD", 500, 500, 50000)
        runtime = SimpleNamespace(
            enabled=True,
            sync_stale_minutes=60,
            sell_allow_same_day_sell=False,
        )

        with patch.multiple(
            "account_signal.engine",
            get_runtime_config=lambda _manager: runtime,
            load_account_config=lambda: (account, self.targets(), [], {}),
            load_account_positions=lambda _symbols: {"GOOGL.US": position, "TSLA.US": recover_position("TSLA.US", [])},
            load_sent_signal_ids=lambda: set(),
            save_latest_run=lambda _payload: None,
        ):
            result = run_account_signal(dry_run=True, symbols=["GOOGL.US"], price_points_by_symbol=market)

        self.assertEqual([signal["action"] for signal in result["signals"]], ["buy"])

    def test_googl_same_day_buy_sell_estimate_when_enabled(self):
        position = recover_position(
            "GOOGL.US",
            [
                {"trade_date": "2026-03-01", "side": "buy", "shares": 100, "price": 100, "amount": 10000},
                {"trade_date": "2026-04-01", "side": "sell", "shares": 40, "price": 115, "amount": 4600},
            ],
        )
        market = {
            "GOOGL.US": points(
                ("2026-04-01", 115),
                ("2026-05-17", 140),
                ("2026-05-18", 115),
                ("2026-05-19", 121),
                ("2026-05-20", 128),
            )
        }
        account = AccountSnapshot("2026-05-20", "USD", 500, 500, 50000)
        runtime = SimpleNamespace(
            enabled=True,
            sync_stale_minutes=60,
            sell_allow_same_day_sell=True,
        )

        with patch.multiple(
            "account_signal.engine",
            get_runtime_config=lambda _manager: runtime,
            load_account_config=lambda: (account, self.targets(), [], {}),
            load_account_positions=lambda _symbols: {"GOOGL.US": position, "TSLA.US": recover_position("TSLA.US", [])},
            load_sent_signal_ids=lambda: set(),
            save_latest_run=lambda _payload: None,
        ):
            result = run_account_signal(dry_run=True, symbols=["GOOGL.US"], price_points_by_symbol=market)

        self.assertEqual([signal["action"] for signal in result["signals"]], ["buy", "sell"])
        self.assertEqual(result["signals"][1]["stage"], "cost_2")
        self.assertTrue(any("基于同日买入后估算" in item for item in result["signals"][1]["rationale"]))

    def test_account_signal_status_includes_strategy_summary(self):
        with self._patched_runtime({"GOOGL.US": recover_position("GOOGL.US", []), "TSLA.US": recover_position("TSLA.US", [])}):
            status = account_signal_status()

        googl = status["strategies"]["GOOGL.US"]
        self.assertIn("大涨1%", googl["buy_summary"])
        self.assertIn("近低2%", googl["buy_summary"])
        self.assertIn("盈利15/25/40%", googl["sell_summary"])
        self.assertIn("卖档重启15%回撤", googl["sell_summary"])
        self.assertEqual(googl["params"]["cost_profit_pcts"], [15, 25, 40])

    def test_tsla_linear_buy_skips_completed_thresholds_and_aggregates(self):
        position = recover_position(
            "TSLA.US",
            [{"trade_date": "2026-05-19", "side": "buy", "shares": 1, "price": 95, "amount": 95}],
        )
        market = {
            "TSLA.US": points(
                ("2026-05-18", 100),
                ("2026-05-19", 95),
                ("2026-05-20", 85),
            )
        }

        with self._patched_runtime({"GOOGL.US": recover_position("GOOGL.US", []), "TSLA.US": position}):
            result = run_account_signal(dry_run=True, symbols=["TSLA.US"], price_points_by_symbol=market)

        buys = [signal for signal in result["signals"] if signal["action"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["stage"], "dd_10_15")
        self.assertAlmostEqual(buys[0]["amount_usd"], 1090.91)
        self.assertEqual([event["stage"] for event in buys[0]["trigger_events"]], ["dd_10", "dd_15"])
        self.assertEqual(buys[0]["leaps"]["trigger_count"], 2)
        self.assertEqual(
            [event["threshold_pct"] for event in buys[0]["leaps"]["triggers"]],
            [10.0, 15.0],
        )

    def test_tsla_same_day_buy_sell_estimate_when_enabled(self):
        position = AccountPosition(
            symbol="TSLA.US",
            shares=100,
            lots=[
                AccountLot(
                    buy_date="2026-05-01",
                    buy_price=50,
                    initial_shares=100,
                    remaining_shares=100,
                    amount=5000,
                    buy_drawdown_pct=30.0,
                )
            ],
            buy_events=[],
        )
        market = {"TSLA.US": points(("2026-05-18", 100), ("2026-05-20", 85))}
        runtime = SimpleNamespace(
            enabled=True,
            sync_stale_minutes=60,
            sell_allow_same_day_sell=True,
        )

        with patch.multiple(
            "account_signal.engine",
            get_runtime_config=lambda _manager: runtime,
            load_account_config=lambda: (self.account(), self.targets(), [], {}),
            load_account_positions=lambda _symbols: {"GOOGL.US": recover_position("GOOGL.US", []), "TSLA.US": position},
            load_sent_signal_ids=lambda: set(),
            save_latest_run=lambda _payload: None,
        ):
            result = run_account_signal(dry_run=True, symbols=["TSLA.US"], price_points_by_symbol=market)

        self.assertEqual([signal["action"] for signal in result["signals"]], ["buy", "sell"])
        self.assertEqual(result["signals"][1]["strategy"], "grid_rebound")
        self.assertTrue(any("基于同日买入后估算" in item for item in result["signals"][1]["rationale"]))

    def test_tsla_linear_buy_filters_after_aggregation(self):
        small_targets = {
            "GOOGL.US": SignalTarget("GOOGL.US", 1000, 100, 100, True),
            "TSLA.US": SignalTarget("TSLA.US", 1000, 0, 100, True),
        }
        position = recover_position("TSLA.US", [])
        market = {"TSLA.US": points(("2026-05-18", 100), ("2026-05-19", 95))}

        with patch.multiple(
            "account_signal.engine",
            load_account_config=lambda: (self.account(), small_targets, [], {}),
            load_account_positions=lambda _symbols: {"GOOGL.US": recover_position("GOOGL.US", []), "TSLA.US": position},
            load_sent_signal_ids=lambda: set(),
            save_latest_run=lambda _payload: None,
        ):
            result = run_account_signal(dry_run=True, symbols=["TSLA.US"], price_points_by_symbol=market, include_debug=True)

        self.assertFalse([signal for signal in result["signals"] if signal["action"] == "buy"])
        self.assertEqual(result["debug"]["TSLA.US"][-1]["event"], "tsla_buy_filtered_min_amount")
        self.assertEqual(result["debug"]["TSLA.US"][-1]["suppressed_trigger_events"][0]["stage"], "dd_5")

    def test_dry_run_does_not_write_ledger_formal_run_does(self):
        position = recover_position("TSLA.US", [])
        market = {"TSLA.US": points(("2026-05-18", 100), ("2026-05-19", 90))}

        with self._patched_runtime({"GOOGL.US": recover_position("GOOGL.US", []), "TSLA.US": position}), \
            patch("account_signal.engine.append_sent_signals") as append:
            dry = run_account_signal(dry_run=True, symbols=["TSLA.US"], price_points_by_symbol=market)
            self.assertGreater(len(dry["new_signals"]), 0)
            append.assert_not_called()

        with self._patched_runtime({"GOOGL.US": recover_position("GOOGL.US", []), "TSLA.US": position}), \
            patch("account_signal.engine.append_sent_signals") as append:
            formal = run_account_signal(dry_run=False, send_email=False, symbols=["TSLA.US"], price_points_by_symbol=market)
            self.assertTrue(formal["ledger_written"])
            append.assert_called_once()

    def test_run_history_keeps_recent_runs_newest_first(self):
        with TemporaryDirectory() as tmp:
            history_path = __import__("pathlib").Path(tmp) / "run_history.jsonl"
            latest_path = __import__("pathlib").Path(tmp) / "latest_run.json"
            with patch("account_signal.store.DATA_DIR", __import__("pathlib").Path(tmp)), \
                patch("account_signal.store.RUN_HISTORY_PATH", history_path), \
                patch("account_signal.store.LATEST_RUN_PATH", latest_path):
                save_latest_run({"run_id": "old", "generated_at": "2026-05-20T00:00:00+00:00", "signals": []})
                save_latest_run({"run_id": "new", "generated_at": "2026-05-21T00:00:00+00:00", "signals": [{"symbol": "GOOGL.US"}]})

                history = load_run_history(limit=2)

        self.assertEqual([item["run_id"] for item in history], ["new", "old"])
        self.assertEqual(history[0]["signals"][0]["symbol"], "GOOGL.US")

    def test_trade_sync_accepts_account_and_signal_target_snapshots(self):
        payload = {
            "spreadsheet_id": "sheet-1",
            "exported_at": "2026-05-21T21:30:00+08:00",
            "rows": [{"symbol": "GOOGL", "trade_date": "2026-05-01", "side": "buy", "shares": 1, "price": 100}],
            "account": [{"as_of": "2026-05-21", "currency": "USD", "cash": 1000, "buying_power": 1000, "net_liquidation": 2000, "notes": ""}],
            "signal_targets": [
                {"symbol": "GOOGL", "initial_investment_usd": 1000, "monthly_contribution_usd": 100, "min_buy_amount_usd": 10, "enabled": True},
                {"symbol": "TSLA", "initial_investment_usd": 1000, "monthly_contribution_usd": 0, "min_buy_amount_usd": 10, "enabled": True},
            ],
        }
        with patch("web.app._check_trade_sync_auth", return_value=(True, "")), \
            patch("web.app.save_sync_payload", return_value={"success": True, "normalized_rows": 1}) as save_main, \
            patch("web.app.save_account_payload", return_value={"account_rows": 1}) as save_account, \
            patch("web.app.save_signal_targets_payload", return_value={"signal_target_rows": 2}) as save_targets, \
            patch("web.app.run_trade_sync_cleanup", return_value={}):
            response = app.test_client().post("/api/trade-sync", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["account_rows"], 1)
        self.assertEqual(body["signal_target_rows"], 2)
        save_main.assert_called_once()
        save_account.assert_called_once()
        save_targets.assert_called_once()

    def test_account_config_accepts_chinese_headers(self):
        from account_signal.config import _parse_account_snapshot, _parse_signal_targets

        errors = []
        account = _parse_account_snapshot(
            {
                "rows": [
                    {
                        "快照时间": "2026-05-21 21:30",
                        "币种": "USD",
                        "现金": 1000,
                        "购买力": 1200,
                        "净清算": 5000,
                        "备注": "test",
                    }
                ]
            },
            errors,
        )
        targets = _parse_signal_targets(
            {
                "rows": [
                    {"标的": "GOOGL", "初始投入": 10000, "每月投入": 1000, "最小买入金额": 100, "启用": "是"},
                    {"标的": "TSLA", "初始投入": 12000, "每月投入": 0, "最小买入金额": 100, "启用": "TRUE"},
                ]
            },
            errors,
        )

        self.assertEqual(errors, [])
        self.assertEqual(account.as_of, "2026-05-21 21:30")
        self.assertEqual(account.cash, 1000)
        self.assertEqual(account.buying_power, 1200)
        self.assertEqual(targets["GOOGL.US"].target_budget_usd, 10000)
        self.assertEqual(targets["GOOGL.US"].monthly_contribution_usd, 1000)
        self.assertTrue(targets["TSLA.US"].enabled)

    def _patched_runtime(self, positions):
        return patch.multiple(
            "account_signal.engine",
            load_account_config=lambda: (self.account(), self.targets(), [], {}),
            load_account_positions=lambda _symbols: positions,
            load_run_history=lambda limit=10: [],
            load_sent_signal_ids=lambda: set(),
            save_latest_run=lambda _payload: None,
        )


if __name__ == "__main__":
    unittest.main()
