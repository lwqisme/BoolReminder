import json
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import drawdown.leaps_option_outcomes as leaps_module
import web.app as web_app
from drawdown.leaps_option_outcomes import (
    ALPACA_DATA_UNAVAILABLE_BEFORE_2024_02,
    ALPACA_PERMISSION_DENIED,
    NO_EXIT_PRICE,
    NO_POLYGON_KEY,
    NO_STOCK_SELL,
    AlpacaMonthlyOptionProvider,
    OutcomeCache,
    OptionBar,
    OptionContract,
    POLYGON_PERMISSION_DENIED,
    PolygonMonthlyOptionProvider,
    PolygonRequestRateLimiter,
    _polygon_retry_get,
    is_standard_monthly_expiration,
    replay_leaps_option_outcomes,
    replay_leaps_option_outcomes_batch,
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
        self.fetch_requests = []

    def select_monthly_call(self, underlying, as_of, stock_price):
        return self.contract, self.reason

    def fetch_bars(self, ticker, start, end):
        self.fetch_requests.append((ticker, start, end))
        return [bar for bar in self.bars if start <= bar.date <= end]


class FakeAlpacaProvider(FakeProvider):
    provider_name = "alpaca"
    provider_label = "Alpaca"
    cache_provider_id = "alpaca-indicative"
    permission_denied_reason = ALPACA_PERMISSION_DENIED
    provider_config = {"option_data_feed": "indicative"}
    min_signal_date = date(2024, 2, 1)


class CountingProvider(FakeProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.select_count = 0

    def select_monthly_call(self, underlying, as_of, stock_price):
        self.select_count += 1
        return super().select_monthly_call(underlying, as_of, stock_price)


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self.payload


def utc_ms(year, month, day):
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


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

    def test_alpaca_pre_2024_02_signal_skips_without_external_api(self):
        provider = CountingProvider()
        provider.provider_name = "alpaca"
        provider.provider_label = "Alpaca"
        provider.cache_provider_id = "alpaca-indicative"
        provider.permission_denied_reason = ALPACA_PERMISSION_DENIED
        provider.provider_config = {"option_data_feed": "indicative"}
        provider.min_signal_date = date(2024, 2, 1)

        result = replay_leaps_option_outcomes(
            [{"date": "2024-01-31", "symbol": "TSLA.US", "stock_buy_price": 100}],
            api_key="",
            provider=provider,
            outcome_cache=OutcomeCache(cache_enabled=False),
        )

        self.assertTrue(result["success"])
        self.assertEqual(provider.select_count, 0)
        self.assertEqual(result["outcomes"][0]["status"], "skipped")
        self.assertEqual(result["outcomes"][0]["skipped_reason"], ALPACA_DATA_UNAVAILABLE_BEFORE_2024_02)
        self.assertEqual(result["outcomes"][0]["provider"], "alpaca")

    def test_alpaca_bar_response_converts_to_option_bars_and_computes_roi(self):
        class FakeAlpacaClient:
            def get_option_bars(self, request):
                return {
                    "bars": {
                        "TSLA250815C00110000": [
                            {"t": "2024-12-09T05:00:00Z", "c": 10.0},
                            {"t": "2024-12-20T05:00:00Z", "c": 15.0},
                        ]
                    }
                }

        provider = AlpacaMonthlyOptionProvider("key", "secret", client=FakeAlpacaClient())
        provider.select_monthly_call = lambda underlying, as_of, stock_price: (
            OptionContract("TSLA250815C00110000", "TSLA", date(2025, 8, 15), 110.0),
            "",
        )
        signal = {
            "signal_key": "sig-1",
            "date": "2024-12-08",
            "symbol": "TSLA.US",
            "stock_buy_price": 100,
            "next_stock_sell_date": "2024-12-20",
        }

        result = replay_leaps_option_outcomes([signal], api_key="", provider=provider, outcome_cache=OutcomeCache(cache_enabled=False))
        outcome = result["outcomes"][0]

        self.assertEqual(outcome["status"], "success")
        self.assertEqual(outcome["provider"], "alpaca")
        self.assertEqual(outcome["contract"], "TSLA250815C00110000")
        self.assertEqual(outcome["entry_date"], "2024-12-09")
        self.assertEqual(outcome["exit_date"], "2024-12-20")
        self.assertAlmostEqual(outcome["roi_pct"], 50.0)

    def test_missing_key_returns_clear_error_and_skipped_outcomes(self):
        result = replay_leaps_option_outcomes(
            [{"date": "2024-12-08", "symbol": "TSLA.US"}],
            api_key="",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["outcomes"][0]["skipped_reason"], NO_POLYGON_KEY)

    def test_no_stock_sell_uses_latest_option_close_as_holding(self):
        provider = FakeProvider(
            bars=[
                OptionBar(date(2024, 12, 9), 10.0),
                OptionBar(date(2024, 12, 20), 14.0),
                OptionBar(date(2025, 1, 10), 16.0),
            ]
        )

        with patch("drawdown.leaps_option_outcomes._utc_today", return_value=date(2025, 1, 11)):
            result = replay_leaps_option_outcomes(
                [{"date": "2024-12-08", "symbol": "TSLA.US", "stock_buy_price": 100}],
                api_key="",
                provider=provider,
            )

        outcome = result["outcomes"][0]
        self.assertEqual(outcome["status"], "success")
        self.assertEqual(outcome["exit_status"], "holding")
        self.assertEqual(outcome["stock_sell_date"], "")
        self.assertEqual(outcome["exit_date"], "2025-01-10")
        self.assertAlmostEqual(outcome["roi_pct"], 60.0)
        self.assertEqual(result["summary"]["success_count"], 1)
        self.assertEqual(provider.fetch_requests[0][1:], (date(2024, 12, 8), date(2025, 1, 10)))

    def test_expired_contract_without_stock_sell_uses_last_bar_before_expiration(self):
        provider = FakeProvider(
            contract=OptionContract("O:TSLA250117C110000000", "TSLA", date(2025, 1, 17), 110.0),
            bars=[
                OptionBar(date(2024, 12, 9), 10.0),
                OptionBar(date(2025, 1, 16), 7.0),
                OptionBar(date(2025, 1, 21), 12.0),
            ],
        )

        with patch("drawdown.leaps_option_outcomes._utc_today", return_value=date(2025, 5, 22)):
            result = replay_leaps_option_outcomes(
                [{"date": "2024-12-08", "symbol": "TSLA.US", "stock_buy_price": 100}],
                api_key="",
                provider=provider,
            )

        outcome = result["outcomes"][0]
        self.assertEqual(outcome["status"], "success")
        self.assertEqual(outcome["exit_status"], "expired_without_stock_sell")
        self.assertEqual(outcome["stock_sell_date"], "")
        self.assertEqual(outcome["exit_date"], "2025-01-16")
        self.assertAlmostEqual(outcome["roi_pct"], -30.0)
        self.assertEqual(provider.fetch_requests[0][1:], (date(2024, 12, 8), date(2025, 1, 17)))

    def test_no_stock_sell_without_exit_bar_is_skipped(self):
        provider = FakeProvider(bars=[OptionBar(date(2024, 12, 9), 10.0)])

        with patch("drawdown.leaps_option_outcomes._utc_today", return_value=date(2024, 12, 10)):
            result = replay_leaps_option_outcomes(
                [{"date": "2024-12-08", "symbol": "TSLA.US", "stock_buy_price": 100}],
                api_key="",
                provider=provider,
            )

        outcome = result["outcomes"][0]
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["skipped_reason"], NO_EXIT_PRICE)
        self.assertEqual(outcome["entry_price"], 10.0)

    def test_invalid_stock_sell_date_is_skipped(self):
        result = replay_leaps_option_outcomes(
            [{"date": "2024-12-08", "symbol": "TSLA.US", "stock_buy_price": 100, "next_stock_sell_date": "2024-12-08"}],
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

    def test_endpoint_provider_factory_prefers_alpaca_credentials(self):
        with patch("web.app._leaps_option_provider", None):
            with patch("web.app._leaps_option_provider_api_key", ""):
                with patch("web.app._get_alpaca_config", return_value={"api_key": "ak", "secret_key": "sk", "option_data_feed": "indicative"}):
                    with patch("web.app.AlpacaMonthlyOptionProvider") as alpaca_cls:
                        alpaca_cls.return_value = FakeAlpacaProvider()
                        provider = web_app._get_leaps_option_provider("polygon-key")

        self.assertIs(provider, alpaca_cls.return_value)
        alpaca_cls.assert_called_once_with("ak", "sk", option_data_feed="indicative")

    def test_endpoint_provider_factory_falls_back_to_polygon_without_alpaca_credentials(self):
        with patch("web.app._leaps_option_provider", None):
            with patch("web.app._leaps_option_provider_api_key", ""):
                with patch("web.app._get_alpaca_config", return_value={"api_key": "", "secret_key": "", "option_data_feed": "indicative"}):
                    with patch("web.app.PolygonMonthlyOptionProvider") as polygon_cls:
                        polygon_cls.return_value = FakeProvider()
                        provider = web_app._get_leaps_option_provider("polygon-key")

        self.assertIs(provider, polygon_cls.return_value)
        polygon_cls.assert_called_once_with("polygon-key")

    def test_provider_status_endpoint_reports_selected_provider_without_secrets(self):
        with patch("web.app._get_alpaca_config", return_value={"api_key": "ak", "secret_key": "sk", "option_data_feed": "opra"}):
            with patch("web.app._get_polygon_api_key", return_value="polygon-key"):
                with app.test_client() as client:
                    response = client.get("/api/strategy-lab/parameter-lab/leaps-option-provider")

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["provider"], "alpaca")
        self.assertTrue(payload["alpaca_configured"])
        self.assertEqual(payload["alpaca_option_data_feed"], "opra")
        self.assertNotIn("ak", json.dumps(payload))
        self.assertNotIn("sk", json.dumps(payload))

    def test_provider_status_endpoint_reports_polygon_fallback_when_alpaca_missing(self):
        with patch("web.app._get_alpaca_config", return_value={"api_key": "", "secret_key": "", "option_data_feed": "indicative"}):
            with patch("web.app._get_polygon_api_key", return_value="polygon-key"):
                with app.test_client() as client:
                    response = client.get("/api/strategy-lab/parameter-lab/leaps-option-provider")

        payload = response.get_json()
        self.assertEqual(payload["provider"], "polygon")
        self.assertFalse(payload["alpaca_configured"])
        self.assertIn("Alpaca API key/secret 未配置", payload["fallback_reason"])

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

    def test_batch_endpoint_returns_ordered_cloned_outcomes(self):
        signals = [
            {
                "signal_key": "sig-1",
                "date": "2024-12-08",
                "symbol": "TSLA.US",
                "stock_buy_price": 100,
                "next_stock_sell_date": "2024-12-20",
            },
            {
                "signal_key": "sig-2",
                "date": "2024-12-08",
                "symbol": "TSLA.US",
                "stock_buy_price": 100,
                "next_stock_sell_date": "2024-12-20",
            },
        ]
        with patch("web.app._get_polygon_api_key", return_value="test-key"):
            with patch("web.app._get_leaps_option_provider", return_value=CountingProvider()):
                with patch("web.app._leaps_option_outcome_cache", OutcomeCache(cache_enabled=False)):
                    with app.test_client() as client:
                        response = client.post(
                            "/api/strategy-lab/parameter-lab/leaps-option-outcomes/batch",
                            json={"run_id": "run-1", "row_key": "row-1", "signals": signals},
                        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual([item["signal_key"] for item in payload["outcomes"]], ["sig-1", "sig-2"])
        self.assertEqual(payload["summary"]["success_count"], 2)
        self.assertIn("cache_stats", payload)

    def test_batch_endpoint_short_circuits_after_polygon_403(self):
        signal = {
            "signal_key": "sig-1",
            "date": "2024-12-08",
            "symbol": "TSLA.US",
            "stock_buy_price": 100,
            "next_stock_sell_date": "2024-12-20",
        }
        denied_result = {
            "success": True,
            "outcomes": [{**signal, "status": "skipped", "skipped_reason": POLYGON_PERMISSION_DENIED}],
            "summary": {
                "total": 1,
                "success_count": 0,
                "skipped_count": 1,
                "top_failure_reason": POLYGON_PERMISSION_DENIED,
                "failure_reasons": {POLYGON_PERMISSION_DENIED: 1},
            },
            "cache_stats": {"polygon_requests": 1},
        }

        with patch("web.app._leaps_option_polygon_permission_denied_until_by_key", {}):
            with patch("web.app._get_polygon_api_key", return_value="test-key"):
                with patch("web.app.replay_leaps_option_outcomes_batch", return_value=denied_result) as replay:
                    with app.test_client() as client:
                        first = client.post(
                            "/api/strategy-lab/parameter-lab/leaps-option-outcomes/batch",
                            json={"signals": [signal]},
                        )
                        second = client.post(
                            "/api/strategy-lab/parameter-lab/leaps-option-outcomes/batch",
                            json={"signals": [signal]},
                        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(replay.call_count, 1)
        payload = second.get_json()
        self.assertTrue(payload["permission_circuit_open"])
        self.assertEqual(payload["cache_stats"]["polygon_requests"], 0)
        self.assertEqual(payload["summary"]["top_failure_reason"], POLYGON_PERMISSION_DENIED)

    def test_permission_circuit_still_serves_cached_outcomes(self):
        signal = {
            "signal_key": "sig-1",
            "date": "2026-02-17",
            "symbol": "GOOGL.US",
            "stock_buy_price": 286,
            "next_stock_sell_date": "2026-03-25",
        }
        outcome = {
            **signal,
            "status": "success",
            "contract": "O:GOOGL261120C00315000",
            "entry_date": "2026-02-17",
            "exit_date": "2026-03-25",
            "entry_price": 35.28,
            "exit_price": 26.6,
            "roi_pct": -24.6,
            "skipped_reason": "",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = OutcomeCache(cache_dir=Path(tmpdir))
            cache.write(signal, outcome, provider_id="polygon")
            with patch("web.app._leaps_option_outcome_cache", cache):
                with patch("web.app._leaps_option_polygon_permission_denied_until_by_key", {"polygon:test": time.monotonic() + 60}):
                    with patch("web.app._leaps_option_permission_fingerprint", return_value="polygon:test"):
                        with patch("web.app._get_polygon_api_key", return_value="test-key"):
                            with patch("web.app.replay_leaps_option_outcomes_batch") as replay:
                                with app.test_client() as client:
                                    response = client.post(
                                        "/api/strategy-lab/parameter-lab/leaps-option-outcomes/batch",
                                        json={"signals": [signal]},
                                    )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["permission_circuit_open"])
        self.assertEqual(replay.call_count, 0)
        self.assertEqual(payload["summary"]["success_count"], 1)
        self.assertEqual(payload["outcomes"][0]["status"], "success")
        self.assertEqual(payload["outcomes"][0]["contract"], "O:GOOGL261120C00315000")

    def test_batch_replay_dedupes_duplicate_signals(self):
        provider = CountingProvider()
        signals = [
            {
                "signal_key": "sig-1",
                "date": "2024-12-08",
                "symbol": "TSLA.US",
                "stock_buy_price": 100,
                "next_stock_sell_date": "2024-12-20",
            },
            {
                "signal_key": "sig-2",
                "date": "2024-12-08",
                "symbol": "TSLA.US",
                "stock_buy_price": 100,
                "next_stock_sell_date": "2024-12-20",
            },
        ]

        result = replay_leaps_option_outcomes_batch(signals, api_key="", provider=provider, outcome_cache=OutcomeCache(cache_enabled=False))

        self.assertTrue(result["success"])
        self.assertEqual(provider.select_count, 1)
        self.assertEqual([item["signal_key"] for item in result["outcomes"]], ["sig-1", "sig-2"])

    def test_batch_replay_circuit_breaks_after_repeated_429s(self):
        signals = [
            {
                "signal_key": f"sig-{index}",
                "date": f"2024-12-{index + 1:02d}",
                "symbol": "TSLA.US",
                "stock_buy_price": 100 + index,
                "next_stock_sell_date": "2024-12-20",
            }
            for index in range(5)
        ]

        def raise_429(provider, signal):
            leaps_module._increment_cache_stat("polygon_429s", amount=3)
            raise requests.HTTPError(response=FakeResponse(status_code=429))

        with patch("drawdown.leaps_option_outcomes.replay_signal", side_effect=raise_429) as replay:
            result = replay_leaps_option_outcomes_batch(
                signals,
                api_key="",
                provider=FakeProvider(),
                outcome_cache=OutcomeCache(cache_enabled=False),
            )

        self.assertEqual(replay.call_count, 2)
        reasons = [item["skipped_reason"] for item in result["outcomes"]]
        self.assertEqual(reasons[:2], ["API 限流/超时", "API 限流/超时"])
        self.assertTrue(all("熔断" in reason for reason in reasons[2:]))

    def test_batch_replay_circuit_breaks_after_polygon_403(self):
        signals = [
            {
                "signal_key": f"sig-{index}",
                "date": f"2024-12-{index + 1:02d}",
                "symbol": "TSLA.US",
                "stock_buy_price": 100 + index,
                "next_stock_sell_date": "2024-12-20",
            }
            for index in range(5)
        ]

        def raise_403(provider, signal):
            raise requests.HTTPError(response=FakeResponse(status_code=403))

        with patch("drawdown.leaps_option_outcomes.replay_signal", side_effect=raise_403) as replay:
            result = replay_leaps_option_outcomes_batch(
                signals,
                api_key="",
                provider=FakeProvider(),
                outcome_cache=OutcomeCache(cache_enabled=False),
            )

        self.assertEqual(replay.call_count, 1)
        reasons = [item["skipped_reason"] for item in result["outcomes"]]
        self.assertEqual(reasons, [POLYGON_PERMISSION_DENIED] * len(signals))
        self.assertEqual(result["summary"]["top_failure_reason"], POLYGON_PERMISSION_DENIED)

    def test_batch_replay_circuit_breaks_after_alpaca_permission_error(self):
        signals = [
            {
                "signal_key": f"sig-{index}",
                "date": f"2024-12-{index + 1:02d}",
                "symbol": "TSLA.US",
                "stock_buy_price": 100 + index,
                "next_stock_sell_date": "2024-12-20",
            }
            for index in range(3)
        ]

        def raise_permission(provider, signal):
            raise leaps_module.OptionProviderPermissionError(ALPACA_PERMISSION_DENIED)

        with patch("drawdown.leaps_option_outcomes.replay_signal", side_effect=raise_permission) as replay:
            result = replay_leaps_option_outcomes_batch(
                signals,
                api_key="",
                provider=FakeAlpacaProvider(),
                outcome_cache=OutcomeCache(cache_enabled=False),
            )

        self.assertEqual(replay.call_count, 1)
        reasons = [item["skipped_reason"] for item in result["outcomes"]]
        self.assertEqual(reasons, [ALPACA_PERMISSION_DENIED] * len(signals))
        self.assertEqual(result["summary"]["top_failure_reason"], ALPACA_PERMISSION_DENIED)

    def test_outcome_cache_hit_skips_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = OutcomeCache(cache_dir=Path(tmpdir))
            signal = {
                "signal_key": "sig-1",
                "date": "2024-12-08",
                "symbol": "TSLA.US",
                "stock_buy_price": 100,
                "next_stock_sell_date": "2024-12-20",
            }
            first_provider = CountingProvider()
            first = replay_leaps_option_outcomes_batch([signal], api_key="", provider=first_provider, outcome_cache=cache)
            second_provider = CountingProvider()
            second = replay_leaps_option_outcomes_batch([{**signal, "signal_key": "sig-2"}], api_key="", provider=second_provider, outcome_cache=cache)

        self.assertEqual(first_provider.select_count, 1)
        self.assertEqual(second_provider.select_count, 0)
        self.assertEqual(second["outcomes"][0]["signal_key"], "sig-2")
        self.assertEqual(first["outcomes"][0]["roi_pct"], second["outcomes"][0]["roi_pct"])

    def test_no_sell_outcome_cache_uses_latest_completed_market_day(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = OutcomeCache(cache_dir=Path(tmpdir))
            signal = {
                "signal_key": "sig-1",
                "date": "2024-12-08",
                "symbol": "TSLA.US",
                "stock_buy_price": 100,
            }
            first_provider = CountingProvider()
            with patch("drawdown.leaps_option_outcomes._utc_today", return_value=date(2025, 1, 10)):
                first = replay_leaps_option_outcomes_batch([signal], api_key="", provider=first_provider, outcome_cache=cache)
            second_provider = CountingProvider()
            with patch("drawdown.leaps_option_outcomes._utc_today", return_value=date(2025, 1, 11)):
                second = replay_leaps_option_outcomes_batch([{**signal, "signal_key": "sig-2"}], api_key="", provider=second_provider, outcome_cache=cache)

        self.assertEqual(first_provider.select_count, 1)
        self.assertEqual(second_provider.select_count, 0)
        self.assertEqual(first["outcomes"][0]["exit_status"], "holding")
        self.assertEqual(second["outcomes"][0]["signal_key"], "sig-2")
        self.assertEqual(first["outcomes"][0]["roi_pct"], second["outcomes"][0]["roi_pct"])

    def test_provider_reuses_contract_and_bar_memory_cache(self):
        provider = PolygonMonthlyOptionProvider("test-key", cache_enabled=False)

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

    def test_provider_reuses_persistent_contract_and_bar_cache_across_instances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))

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
                return {"results": [{"t": utc_ms(2024, 12, 9), "c": 10.0}]}

            with patch("drawdown.leaps_option_outcomes._polygon_retry_get", side_effect=fake_polygon_get) as fetch:
                contracts = provider.fetch_contracts("TSLA", date(2024, 12, 8), date(2025, 6, 26), date(2025, 10, 4))
                bars = provider.fetch_bars("O:TSLA250815C110000000", date(2024, 12, 8), date(2024, 12, 20))

            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(contracts[0]["ticker"], "O:TSLA250815C110000000")
            self.assertEqual(bars[0].close, 10.0)

            new_provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))
            with patch("drawdown.leaps_option_outcomes._polygon_retry_get") as fetch:
                cached_contracts = new_provider.fetch_contracts("TSLA", date(2024, 12, 8), date(2025, 6, 26), date(2025, 10, 4))
                cached_bars = new_provider.fetch_bars("O:TSLA250815C110000000", date(2024, 12, 8), date(2024, 12, 20))

            fetch.assert_not_called()
            self.assertEqual(cached_contracts[0]["ticker"], "O:TSLA250815C110000000")
            self.assertEqual(cached_bars[0], OptionBar(date(2024, 12, 9), 10.0))

    def test_provider_refetches_and_merges_bars_when_disk_cache_range_is_partial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))
            ticker = "O:TSLA250815C110000000"

            with patch(
                "drawdown.leaps_option_outcomes._polygon_retry_get",
                return_value={"results": [{"t": utc_ms(2024, 12, 9), "c": 10.0}]},
            ) as fetch:
                provider.fetch_bars(ticker, date(2024, 12, 8), date(2024, 12, 20))
            self.assertEqual(fetch.call_count, 1)

            new_provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))
            with patch(
                "drawdown.leaps_option_outcomes._polygon_retry_get",
                return_value={"results": [{"t": utc_ms(2024, 12, 25), "c": 12.0}]},
            ) as fetch:
                expanded = new_provider.fetch_bars(ticker, date(2024, 12, 1), date(2024, 12, 31))

            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(expanded, [OptionBar(date(2024, 12, 9), 10.0), OptionBar(date(2024, 12, 25), 12.0)])

            third_provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))
            with patch("drawdown.leaps_option_outcomes._polygon_retry_get") as fetch:
                cached = third_provider.fetch_bars(ticker, date(2024, 12, 1), date(2024, 12, 31))

            fetch.assert_not_called()
            self.assertEqual(cached, expanded)

    def test_provider_persists_empty_bar_range_coverage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))
            ticker = "O:TSLA250815C110000000"

            with patch("drawdown.leaps_option_outcomes._polygon_retry_get", return_value={"results": []}) as fetch:
                bars = provider.fetch_bars(ticker, date(2024, 12, 8), date(2024, 12, 20))

            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(bars, [])

            new_provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))
            with patch("drawdown.leaps_option_outcomes._polygon_retry_get") as fetch:
                cached = new_provider.fetch_bars(ticker, date(2024, 12, 8), date(2024, 12, 20))

            fetch.assert_not_called()
            self.assertEqual(cached, [])

    def test_provider_contract_cache_lock_prevents_concurrent_duplicate_fetches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))

            def fake_polygon_get(url, params, timeout):
                time.sleep(0.02)
                return {"results": [{"ticker": "O:TSLA250815C110000000"}]}

            results = []
            with patch("drawdown.leaps_option_outcomes._polygon_retry_get", side_effect=fake_polygon_get) as fetch:
                threads = [
                    threading.Thread(
                        target=lambda: results.append(
                            provider.fetch_contracts("TSLA", date(2024, 12, 8), date(2025, 6, 26), date(2025, 10, 4))
                        )
                    )
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])

    def test_provider_reuses_historical_cache_even_when_cache_date_is_old(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))
            ticker = "O:TSLA250815C110000000"
            path = provider._bars_cache_path(ticker)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cache_date": "2020-01-01",
                        "ticker": ticker,
                        "covered_ranges": [{"start": "2024-12-08", "end": "2024-12-20"}],
                        "bars": [{"date": "2024-12-09", "close": 10.0}],
                    }
                ),
                encoding="utf-8",
            )

            with patch("drawdown.leaps_option_outcomes._utc_today", return_value=date(2026, 5, 22)):
                with patch("drawdown.leaps_option_outcomes._polygon_retry_get") as fetch:
                    bars = provider.fetch_bars(ticker, date(2024, 12, 8), date(2024, 12, 20))

            fetch.assert_not_called()
            self.assertEqual(bars, [OptionBar(date(2024, 12, 9), 10.0)])

    def test_provider_refetches_today_bars_when_cache_date_is_old(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))
            ticker = "O:TSLA260522C110000000"
            path = provider._bars_cache_path(ticker)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cache_date": "2026-05-21",
                        "ticker": ticker,
                        "covered_ranges": [{"start": "2026-05-20", "end": "2026-05-22"}],
                        "bars": [{"date": "2026-05-20", "close": 8.0}],
                    }
                ),
                encoding="utf-8",
            )

            with patch("drawdown.leaps_option_outcomes._utc_today", return_value=date(2026, 5, 22)):
                with patch(
                    "drawdown.leaps_option_outcomes._polygon_retry_get",
                    return_value={"results": [{"t": utc_ms(2026, 5, 22), "c": 9.0}]},
                ) as fetch:
                    bars = provider.fetch_bars(ticker, date(2026, 5, 20), date(2026, 5, 22))

            self.assertEqual(fetch.call_count, 1)
            self.assertIn("/2026-05-22/2026-05-22", fetch.call_args.args[0])
            self.assertEqual(bars, [OptionBar(date(2026, 5, 20), 8.0), OptionBar(date(2026, 5, 22), 9.0)])

    def test_provider_refetches_today_contracts_when_cache_date_is_old(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))
            path = provider._contracts_cache_path("TSLA", date(2026, 5, 22), date(2026, 12, 8), date(2027, 3, 18))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cache_date": "2026-05-21",
                        "query": {
                            "underlying": "TSLA",
                            "as_of": "2026-05-22",
                            "expiration_start": "2026-12-08",
                            "expiration_end": "2027-03-18",
                        },
                        "contracts": [{"ticker": "OLD"}],
                    }
                ),
                encoding="utf-8",
            )

            with patch("drawdown.leaps_option_outcomes._utc_today", return_value=date(2026, 5, 22)):
                with patch(
                    "drawdown.leaps_option_outcomes._polygon_retry_get",
                    return_value={"results": [{"ticker": "NEW"}]},
                ) as fetch:
                    contracts = provider.fetch_contracts("TSLA", date(2026, 5, 22), date(2026, 12, 8), date(2027, 3, 18))

            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(contracts, [{"ticker": "NEW"}])

    def test_provider_ignores_corrupt_contract_cache_and_rewrites(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = PolygonMonthlyOptionProvider("test-key", cache_dir=Path(tmpdir))
            path = provider._contracts_cache_path("TSLA", date(2024, 12, 8), date(2025, 6, 26), date(2025, 10, 4))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{bad json", encoding="utf-8")

            with patch(
                "drawdown.leaps_option_outcomes._polygon_retry_get",
                return_value={
                    "results": [
                        {
                            "ticker": "O:TSLA250815C110000000",
                            "expiration_date": "2025-08-15",
                            "strike_price": 110,
                            "contract_type": "call",
                        }
                    ]
                },
            ) as fetch:
                contracts = provider.fetch_contracts("TSLA", date(2024, 12, 8), date(2025, 6, 26), date(2025, 10, 4))

            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(contracts[0]["ticker"], "O:TSLA250815C110000000")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1)

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

    def test_polygon_retry_get_redacts_api_key_in_failure_logs(self):
        limiter = Mock()
        limiter.wait.return_value = 0.0
        error = requests.HTTPError(
            "429 Client Error: Too Many Requests for url: https://api.polygon.io/v1/a?apiKey=secret-token"
        )
        error.response = FakeResponse(status_code=429)

        with patch("drawdown.leaps_option_outcomes._POLYGON_RATE_LIMITER", limiter):
            with patch("drawdown.leaps_option_outcomes.time.sleep"):
                with patch("drawdown.leaps_option_outcomes.requests.get", side_effect=error):
                    with self.assertLogs("drawdown.leaps_option_outcomes", level="INFO") as logs:
                        with self.assertRaises(requests.HTTPError):
                            _polygon_retry_get("https://api.polygon.io/v1/a", {"apiKey": "secret-token"}, 15)

        output = "\n".join(logs.output)
        self.assertNotIn("secret-token", output)
        self.assertIn("apiKey=<redacted>", output)


if __name__ == "__main__":
    unittest.main()
