"""End-to-end regression tests verifying account-signal removal.

These tests confirm that after removing the account-signal page, API routes,
and supporting code, the rest of the system still functions correctly.
"""

import unittest

from web.app import app


class AccountSignalRemovalTest(unittest.TestCase):
    """Verify that all account-signal routes return 404 after removal."""

    ROUTES = [
        ("GET", "/account-signal"),
        ("GET", "/api/account-signal/status"),
        ("GET", "/api/account-signal/profiles"),
        ("POST", "/api/account-signal/profile-candidates"),
        ("POST", "/api/account-signal/profiles/assign"),
        ("POST", "/api/account-signal/profiles/promote"),
        ("POST", "/api/account-signal/run"),
        ("POST", "/api/account-signal/backtest"),
    ]

    def test_account_signal_page_returns_404(self):
        with app.test_client() as client:
            resp = client.get("/account-signal")
        self.assertEqual(resp.status_code, 404)

    def test_account_signal_status_api_returns_404(self):
        with app.test_client() as client:
            resp = client.get("/api/account-signal/status")
        self.assertEqual(resp.status_code, 404)

    def test_account_signal_profiles_api_returns_404(self):
        with app.test_client() as client:
            resp = client.get("/api/account-signal/profiles")
        self.assertEqual(resp.status_code, 404)

    def test_account_signal_profile_candidates_api_returns_404(self):
        with app.test_client() as client:
            resp = client.post("/api/account-signal/profile-candidates", json={})
        self.assertEqual(resp.status_code, 404)

    def test_account_signal_profiles_assign_api_returns_404(self):
        with app.test_client() as client:
            resp = client.post("/api/account-signal/profiles/assign", json={})
        self.assertEqual(resp.status_code, 404)

    def test_account_signal_profiles_promote_api_returns_404(self):
        with app.test_client() as client:
            resp = client.post("/api/account-signal/profiles/promote", json={})
        self.assertEqual(resp.status_code, 404)

    def test_account_signal_run_api_returns_404(self):
        with app.test_client() as client:
            resp = client.post("/api/account-signal/run", json={})
        self.assertEqual(resp.status_code, 404)

    def test_account_signal_backtest_api_returns_404(self):
        with app.test_client() as client:
            resp = client.post("/api/account-signal/backtest", json={})
        self.assertEqual(resp.status_code, 404)


class StrategyLabRegressionTest(unittest.TestCase):
    """Verify strategy-lab pages work without account-signal references."""

    def test_strategy_lab_page_loads_without_account_signal_button(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab").get_data(as_text=True)
        self.assertNotIn("晋升到真实账户提醒", html)
        self.assertNotIn("promoteRobustCandidateToAccountSignal", html)
        self.assertNotIn("/api/account-signal/profile-candidates", html)
        self.assertNotIn("可到真实账户页面选择股票并启用", html)
        # Confirm page still loads core content
        self.assertIn("策略实验室", html)

    def test_strategy_parameter_lab_page_loads_without_account_signal_button(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab/parameter-lab").get_data(as_text=True)
        self.assertNotIn("晋升到真实账户提醒", html)
        self.assertNotIn("promoteParameterRowToAccountSignal", html)
        self.assertNotIn("/api/account-signal/profile-candidates", html)
        self.assertNotIn("可到真实账户页面选择股票并启用", html)
        # Confirm page still loads core content
        self.assertIn("参数实验室", html)

    def test_ga_packet_endpoint_still_works(self):
        with app.test_client() as client:
            resp = client.post(
                "/api/strategy-lab/parameter-lab/ga-packet",
                json={
                    "ga_buy_strategy": "pyramid_3",
                    "ga_sell_strategy": "none",
                    "start": "2026-01-01",
                    "end": "2026-05-01",
                    "ga_population_size": 3,
                },
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])



class DrawdownPageRegressionTest(unittest.TestCase):
    """Verify drawdown page still works."""

    def test_drawdown_page_loads(self):
        with app.test_client() as client:
            resp = client.get("/drawdown")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Drawdown", html)


class IndexPageRegressionTest(unittest.TestCase):
    """Verify index page still works."""

    def test_index_page_loads(self):
        with app.test_client() as client:
            resp = client.get("/")
        self.assertEqual(resp.status_code, 200)


class StrategyRulesConsistencyTest(unittest.TestCase):
    """Verify strategy_rules still export shared functions after account_signal removal."""

    def test_shared_strategy_rules_functions_still_exported(self):
        from drawdown.strategy_rules import (
            core_dip_boost_ratio,
            core_dip_cash_reserve_ratio,
            core_dip_timing_allows_buy,
            grid_rebound_stages,
            point_drawdown_pct,
            sell_stage_rearm_drawdown_pct,
        )
        # All functions must be callable
        self.assertTrue(callable(point_drawdown_pct))
        self.assertTrue(callable(core_dip_boost_ratio))
        self.assertTrue(callable(core_dip_cash_reserve_ratio))
        self.assertTrue(callable(core_dip_timing_allows_buy))
        self.assertTrue(callable(grid_rebound_stages))
        self.assertTrue(callable(sell_stage_rearm_drawdown_pct))


if __name__ == "__main__":
    unittest.main()
