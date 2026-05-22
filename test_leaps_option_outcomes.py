import unittest
from datetime import date
from unittest.mock import Mock, patch

import requests

from drawdown.leaps_option_outcomes import (
    NO_POLYGON_KEY,
    NO_STOCK_SELL,
    OptionBar,
    OptionContract,
    PolygonMonthlyOptionProvider,
    PolygonRequestRateLimiter,
    _polygon_retry_get,
    is_standard_monthly_expiration,
    replay_leaps_option_outcomes,
    summarize_outcomes,
)
from web.app import app


class FakeProvider:
    def __init__(self, contract=None, bars=None, reason=""):
        self.contract = contract or OptionContract("O:TSLA250815C110000000", "TSLA", date(2025, 8, 15), 110.0)
        self.bars = bars or [
            OptionBar(date(2024, 12, 9), 10.0),
            OptionBar(date(2024, 12, 20), 14.0),
        ]
        self.reason = reason

    def select_monthly_call(self, underlying, as_of, stock_price):
        return self.contract, self.reason

    def fetch_bars(self, ticker, start, end):
        return self.bars


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self.payload


class LeapsOptionOutcomesTest(unittest.TestCase):
    def test_monthly_expiration_filter_accepts_third_friday_and_holiday_thursday(self):
        self.assertTrue(is_standard_monthly_expiration(date(2025, 8, 15)))
        self.assertTrue(is_standard_monthly_expiration(date(2025, 4, 17)))
        self.assertFalse(is_standard_monthly_expiration(date(2025, 8, 22)))

    def test_provider_selects_monthly_contract_and_closest_otm_10_strike(self):
        provider = PolygonMonthlyOptionProvider("test-key")
        provider.fetch_contracts = lambda underlying, as_of, start, end: [
            {
                "ticker": "O:TSLA250822C110000000",
                "expiration_date": "2025-08-22",
                "strike_price": 110,
                "contract_type": "call",
            },
            {
                "ticker": "O:TSLA250815C115000000",
                "expiration_date": "2025-08-15",
                "strike_price": 115,
                "contract_type": "call",
            },
            {
                "ticker": "O:TSLA250815C109000000",
                "expiration_date": "2025-08-15",
                "strike_price": 109,
                "contract_type": "call",
            },
        ]

        contract, reason = provider.select_monthly_call("TSLA", date(2024, 12, 8), 100)

        self.assertEqual(reason, "")
        self.assertEqual(contract.ticker, "O:TSLA250815C109000000")
        self.assertEqual(contract.expiration, date(2025, 8, 15))
        self.assertEqual(contract.strike, 109)

    def test_replay_computes_roi_from_entry_and_exit_close(self):
        signal = {
            "signal_key": "sig-1",
            "date": "2024-12-08",
            "symbol": "TSLA.US",
            "stock_buy_price": 100,
            "next_stock_sell_date": "2024-12-20",
        }

        result = replay_leaps_option_outcomes([signal], api_key="", provider=FakeProvider())
        outcome = result["outcomes"][0]

        self.assertTrue(result["success"])
        self.assertEqual(outcome["status"], "success")
        self.assertEqual(outcome["entry_date"], "2024-12-09")
        self.assertEqual(outcome["exit_date"], "2024-12-20")
        self.assertAlmostEqual(outcome["roi_pct"], 40.0)
        self.assertAlmostEqual(result["summary"]["roi_mean_pct"], 40.0)
        self.assertEqual(result["summary"]["success_count"], 1)

    def test_missing_key_returns_clear_error_and_skipped_outcomes(self):
        result = replay_leaps_option_outcomes(
            [{"date": "2024-12-08", "symbol": "TSLA.US"}],
            api_key="",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["outcomes"][0]["skipped_reason"], NO_POLYGON_KEY)

    def test_no_stock_sell_is_skipped(self):
        result = replay_leaps_option_outcomes(
            [{"date": "2024-12-08", "symbol": "TSLA.US", "stock_buy_price": 100}],
            api_key="",
            provider=FakeProvider(),
        )

        self.assertEqual(result["outcomes"][0]["status"], "skipped")
        self.assertEqual(result["outcomes"][0]["skipped_reason"], NO_STOCK_SELL)

    def test_summary_uses_success_only_for_roi_and_counts_top_failure(self):
        summary = summarize_outcomes(
            [
                {"status": "success", "roi_pct": 20},
                {"status": "success", "roi_pct": -10},
                {"status": "skipped", "skipped_reason": "无可用入场价"},
                {"status": "skipped", "skipped_reason": "无可用入场价"},
            ]
        )

        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["success_count"], 2)
        self.assertEqual(summary["roi_mean_pct"], 5)
        self.assertEqual(summary["roi_median_pct"], 5)
        self.assertEqual(summary["top_failure_reason"], "无可用入场价")

    def test_endpoint_reports_missing_polygon_key(self):
        with patch("web.app._get_polygon_api_key", return_value=""):
            with app.test_client() as client:
                response = client.post(
                    "/api/strategy-lab/parameter-lab/leaps-option-outcomes",
                    json={"signals": [{"date": "2024-12-08", "symbol": "TSLA.US"}]},
                )

        payload = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["summary"]["top_failure_reason"], NO_POLYGON_KEY)

    def test_endpoint_rejects_batch_signals(self):
        with app.test_client() as client:
            response = client.post(
                "/api/strategy-lab/parameter-lab/leaps-option-outcomes",
                json={
                    "signals": [
                        {"date": "2024-12-08", "symbol": "TSLA.US"},
                        {"date": "2024-12-09", "symbol": "TSLA.US"},
                    ]
                },
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["success"])
        self.assertIn("1 条 signal", payload["message"])

    def test_endpoint_single_signal_returns_compatible_payload(self):
        signal = {
            "signal_key": "sig-1",
            "date": "2024-12-08",
            "symbol": "TSLA.US",
            "stock_buy_price": 100,
            "next_stock_sell_date": "2024-12-20",
        }
        with patch("web.app._get_polygon_api_key", return_value="test-key"):
            with patch("web.app._get_leaps_option_provider", return_value=FakeProvider()):
                with app.test_client() as client:
                    response = client.post(
                        "/api/strategy-lab/parameter-lab/leaps-option-outcomes",
                        json={"run_id": "run-1", "row_key": "row-1", "signals": [signal]},
                    )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["row_key"], "row-1")
        self.assertEqual(len(payload["outcomes"]), 1)
        self.assertEqual(payload["summary"]["success_count"], 1)

    def test_provider_reuses_contract_and_bar_cache(self):
        provider = PolygonMonthlyOptionProvider("test-key")

        def fake_polygon_get(url, params, timeout):
            if "/v3/reference/options/contracts" in url:
                return {
                    "results": [
                        {
                            "ticker": "O:TSLA250815C110000000",
                            "expiration_date": "2025-08-15",
                            "strike_price": 110,
                            "contract_type": "call",
                        }
                    ]
                }
            return {"results": [{"t": 1733702400000, "c": 10.0}]}

        with patch("drawdown.leaps_option_outcomes._polygon_retry_get", side_effect=fake_polygon_get) as fetch:
            provider.fetch_contracts("TSLA", date(2024, 12, 8), date(2025, 6, 26), date(2025, 10, 4))
            provider.fetch_contracts("TSLA", date(2024, 12, 8), date(2025, 6, 26), date(2025, 10, 4))
            provider.fetch_bars("O:TSLA250815C110000000", date(2024, 12, 8), date(2024, 12, 20))
            provider.fetch_bars("O:TSLA250815C110000000", date(2024, 12, 8), date(2024, 12, 20))

        self.assertEqual(fetch.call_count, 2)

    def test_polygon_retry_get_waits_before_second_request(self):
        limiter = PolygonRequestRateLimiter(interval_seconds=1.0)
        response = FakeResponse({"ok": True})

        with patch("drawdown.leaps_option_outcomes._POLYGON_RATE_LIMITER", limiter):
            with patch("drawdown.leaps_option_outcomes.time.monotonic", side_effect=[10.0, 10.2]):
                with patch("drawdown.leaps_option_outcomes.time.sleep") as sleep:
                    with patch("drawdown.leaps_option_outcomes.requests.get", return_value=response) as get:
                        _polygon_retry_get("https://api.polygon.io/v1/a", {"apiKey": "secret"}, 15)
                        _polygon_retry_get("https://api.polygon.io/v1/b", {"apiKey": "secret"}, 15)

        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.8)

    def test_polygon_retry_get_rate_limits_each_retry_after_429(self):
        limiter = Mock()
        limiter.wait.return_value = 0.0
        responses = [FakeResponse(status_code=429), FakeResponse({"ok": True})]

        with patch("drawdown.leaps_option_outcomes._POLYGON_RATE_LIMITER", limiter):
            with patch("drawdown.leaps_option_outcomes.time.sleep") as sleep:
                with patch("drawdown.leaps_option_outcomes.requests.get", side_effect=responses):
                    payload = _polygon_retry_get("https://api.polygon.io/v1/a", {"apiKey": "secret"}, 15)

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(limiter.wait.call_count, 2)
        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
