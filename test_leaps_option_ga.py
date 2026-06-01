"""Tests for LEAPS option genetic algorithm optimization."""

import unittest
from datetime import date, timedelta

from drawdown.leaps_option_ga import (
    LeapsEntrySignal,
    LeapsEvolutionConfig,
    LeapsIndividual,
    LeapsSellEvent,
    LeapsTrade,
    bollinger_lower_band,
    compute_sell_ladder,
    detect_leaps_entries,
    estimate_option_delta,
    evolve_leaps_parameters,
    leaps_crossover,
    leaps_fitness_fn,
    leaps_individual_key,
    leaps_mutate,
    proxy_option_roi,
    rolling_120d_high,
)


class LeapsOptionDeltaTest(unittest.TestCase):
    """Tracer bullet: option delta estimation works for LEAPS calls."""

    def test_atm_call_delta_around_point_seven(self):
        """ATM LEAPS call (250 DTE) should have delta ~0.7-0.8."""
        delta = estimate_option_delta(stock_price=100.0, strike=100.0, dte=250)
        self.assertGreater(delta, 0.60)
        self.assertLess(delta, 0.85)

    def test_otm_call_delta_lower_than_atm(self):
        """10% OTM call should have lower delta than ATM."""
        atm = estimate_option_delta(stock_price=100.0, strike=100.0, dte=250)
        otm = estimate_option_delta(stock_price=100.0, strike=110.0, dte=250)
        self.assertGreater(atm, otm)
        self.assertGreater(otm, 0.30)

    def test_shorter_dte_increases_delta_for_itm(self):
        """Shorter DTE pushes delta toward 1 for ITM, toward 0 for OTM."""
        delta_250 = estimate_option_delta(stock_price=110.0, strike=100.0, dte=250)
        delta_30 = estimate_option_delta(stock_price=110.0, strike=100.0, dte=30)
        self.assertGreater(delta_30, delta_250)

    def test_deep_otm_delta_positive(self):
        """Even very OTM still has positive delta for calls."""
        delta = estimate_option_delta(stock_price=50.0, strike=100.0, dte=250)
        self.assertGreater(delta, 0.0)
        self.assertLess(delta, 0.4)


class LeapsProxyRoiTest(unittest.TestCase):
    """proxy_option_roi produces sensible LEAPS return estimates."""

    def test_stock_up_20pct_gives_positive_option_roi(self):
        """Stock +20% → LEAPS call should return >>20% due to leverage."""
        entry_date = date(2025, 1, 15)
        exit_date = date(2025, 4, 15)  # 90 days later
        expiration = date(2025, 10, 15)  # ~270 DTE at entry
        roi = proxy_option_roi(
            entry_price=100.0, exit_price=120.0,
            entry_date=entry_date, exit_date=exit_date,
            expiration=expiration, strike_price=110.0,
        )
        # Delta ~0.5 at entry, stock +20% → option should return >50%
        self.assertGreater(roi, 30.0)
        self.assertLess(roi, 200.0)

    def test_stock_down_gives_negative_option_roi(self):
        """Stock drops → option ROI negative, amplified by leverage."""
        entry_date = date(2025, 1, 15)
        exit_date = date(2025, 4, 15)
        expiration = date(2025, 10, 15)
        roi = proxy_option_roi(
            entry_price=100.0, exit_price=90.0,
            entry_date=entry_date, exit_date=exit_date,
            expiration=expiration, strike_price=110.0,
        )
        self.assertLess(roi, -5.0)

    def test_longer_hold_reduces_roi_via_theta(self):
        """Same stock move, longer hold → lower ROI (theta decay)."""
        entry_date = date(2025, 1, 15)
        expiration = date(2025, 10, 15)
        roi_30d = proxy_option_roi(
            entry_price=100.0, exit_price=120.0,
            entry_date=entry_date, exit_date=date(2025, 2, 15),
            expiration=expiration, strike_price=110.0,
        )
        roi_180d = proxy_option_roi(
            entry_price=100.0, exit_price=120.0,
            entry_date=entry_date, exit_date=date(2025, 7, 15),
            expiration=expiration, strike_price=110.0,
        )
        self.assertGreater(roi_30d, roi_180d)


class LeapsRollingHighTest(unittest.TestCase):
    """rolling_120d_high computes correct rolling maximums."""

    def _make_prices(self, values: list[float], start_date: date) -> list[tuple[date, float]]:
        return [(start_date + timedelta(days=i), v) for i, v in enumerate(values)]

    def test_first_120_days_return_none(self):
        """Before 120 data points, rolling high is None."""
        prices = self._make_prices([100.0] * 119, date(2025, 1, 1))
        highs = rolling_120d_high(prices)
        self.assertEqual(len(highs), 119)
        # All should be None or 0 before 120 days
        for _, h in highs:
            self.assertIsNone(h)

    def test_after_120_days_shows_correct_high(self):
        """After day 120, rolling high is max of last 120 days."""
        # 100 days at 100, spike to 150, then back to 100
        values = [100.0] * 100 + [150.0] + [100.0] * 50
        prices = self._make_prices(values, date(2025, 1, 1))
        highs = rolling_120d_high(prices)
        # At index 120 (day 120 from start), should see 150 in window
        idx_120 = 120
        self.assertIsNotNone(highs[idx_120][1])
        self.assertGreaterEqual(highs[idx_120][1], 150.0)

    def test_rolling_window_moves(self):
        """Rolling high excludes values older than 120 days."""
        # Day 1: 200, days 2-121: 100
        values = [200.0] + [100.0] * 121
        prices = self._make_prices(values, date(2025, 1, 1))
        highs = rolling_120d_high(prices)
        # Index 121 (the 122nd element) should no longer include 200
        self.assertEqual(highs[121][1], 100.0)

    def test_highs_follow_dates(self):
        """Each high returned has correct date."""
        prices = self._make_prices([100.0] * 150, date(2025, 1, 1))
        highs = rolling_120d_high(prices)
        for i, (d, h) in enumerate(highs):
            if i >= 119:
                self.assertEqual(d, prices[i][0])
                self.assertAlmostEqual(h, 100.0)


class LeapsBollingerTest(unittest.TestCase):
    """bollinger_lower_band computes 22-day 2σ lower band."""

    def _make_prices(self, values: list[float], start_date: date) -> list[tuple[date, float]]:
        return [(start_date + timedelta(days=i), v) for i, v in enumerate(values)]

    def test_flat_prices_gives_band_at_price(self):
        """Flat prices → no stddev → band equals price."""
        prices = self._make_prices([100.0] * 30, date(2025, 1, 1))
        bands = bollinger_lower_band(prices, period=22, std_mult=2.0)
        # After 21 days of history, band should be ~100
        for i, (d, b) in enumerate(bands):
            if i >= 21:
                self.assertIsNotNone(b)
                self.assertAlmostEqual(b, 100.0, places=2)

    def test_drop_below_band(self):
        """Price dropping below MA → lower band below price."""
        # 22 days at 100, then drop to 95
        values = [100.0] * 22 + [95.0, 95.0]
        prices = self._make_prices(values, date(2025, 1, 1))
        bands = bollinger_lower_band(prices, period=22, std_mult=2.0)
        # At the drop point, band should be ~100 (near the MA before drop)
        # After multiple drops, the band drops too
        self.assertIsNotNone(bands[21][1])
        # The band at index 23 should reflect the new data
        self.assertIsNotNone(bands[23][1])

    def test_early_points_return_none(self):
        """Not enough data for period → None."""
        prices = self._make_prices([100.0] * 10, date(2025, 1, 1))
        bands = bollinger_lower_band(prices, period=22, std_mult=2.0)
        for _, b in bands:
            self.assertIsNone(b)

    def test_band_below_ma_when_volatile(self):
        """When prices have variance, band < MA."""
        # Oscillating prices
        values = [100.0, 105.0] * 15  # 30 points, alternating
        prices = self._make_prices(values, date(2025, 1, 1))
        bands = bollinger_lower_band(prices, period=22, std_mult=2.0)
        # After enough data, band should be below MA
        last_band = bands[-1][1]
        self.assertIsNotNone(last_band)
        self.assertLess(last_band, 102.5)  # MA is 102.5, band should be lower


class LeapsEntryDetectionTest(unittest.TestCase):
    """detect_leaps_entries finds entry signals from price data."""

    def _make_prices(self, values: list[float], start_date: date) -> list[tuple[date, float]]:
        return [(start_date + timedelta(days=i), v) for i, v in enumerate(values)]

    def test_no_entry_without_enough_history(self):
        """Short series produces no entries."""
        prices = self._make_prices([100.0] * 50, date(2025, 1, 1))
        entries = detect_leaps_entries(prices, drawdown_threshold_pct=20.0, entry_mode="touch")
        self.assertEqual(len(entries), 0)

    def test_touch_entry_when_price_hits_lower_band_and_drawdown(self):
        """When price is at/under Bollinger lower band AND drawdown >= threshold, trigger."""
        # 122 days stable at 100, then sharp drop creates drawdown + bollinger breach
        values = [100.0] * 122 + [95.0, 90.0, 87.0, 84.0, 81.0, 80.0, 78.0]
        prices = self._make_prices(values, date(2025, 1, 1))
        entries = detect_leaps_entries(prices, drawdown_threshold_pct=10.0, entry_mode="touch")
        self.assertGreater(len(entries), 0)
        # At least one entry should have substantial drawdown
        max_dd = max(e.drawdown_pct for e in entries)
        self.assertGreaterEqual(max_dd, 15.0)

    def test_no_entry_when_drawdown_below_threshold(self):
        """Drawdown below threshold → no signal, even at lower band."""
        values = [100.0] * 122 + [95.0] * 6
        prices = self._make_prices(values, date(2025, 1, 1))
        entries = detect_leaps_entries(prices, drawdown_threshold_pct=20.0, entry_mode="touch")
        self.assertEqual(len(entries), 0)

    def test_touch_mode_only_entries_are_at_or_below_band(self):
        """All touch entries should have price <= bollinger lower band (score >= 1)."""
        # Create oscillating data with a known drawdown event
        values = [100.0] * 122 + [95.0, 93.0, 91.0, 89.0, 87.0, 85.0]
        prices = self._make_prices(values, date(2025, 1, 1))
        entries = detect_leaps_entries(prices, drawdown_threshold_pct=12.0, entry_mode="touch")
        for entry in entries:
            self.assertGreaterEqual(entry.bollinger_score, 1.0,
                f"Entry at {entry.date} has bollinger_score={entry.bollinger_score}")

    def test_bounce_mode_only_triggers_after_recovery(self):
        """Bounce entries trigger when price was at band then recovers above it."""
        values = [100.0] * 122 + [85.0] * 5 + [90.0, 93.0, 95.0]
        prices = self._make_prices(values, date(2025, 1, 1))
        entries = detect_leaps_entries(prices, drawdown_threshold_pct=10.0, entry_mode="bounce")
        for entry in entries:
            self.assertLess(entry.bollinger_score, 1.0)

    def test_both_mode_finds_at_least_as_many_as_touch(self):
        """Both mode should find >= entries than touch alone."""
        values = [100.0] * 122 + [85.0] * 5 + [90.0, 93.0, 95.0, 98.0]
        prices = self._make_prices(values, date(2025, 1, 1))
        touch_entries = detect_leaps_entries(prices, drawdown_threshold_pct=10.0, entry_mode="touch")
        both_entries = detect_leaps_entries(prices, drawdown_threshold_pct=10.0, entry_mode="both")
        self.assertGreaterEqual(len(both_entries), len(touch_entries))


class LeapsSellLadderTest(unittest.TestCase):
    """compute_sell_ladder applies staged sell rules."""

    def _make_prices(self, values: list[float], start_date: date) -> list[tuple[date, float]]:
        return [(start_date + timedelta(days=i), v) for i, v in enumerate(values)]

    def test_no_sell_before_hold_period(self):
        """Entry on day 0, price doubles next day but hold=10 → no sell."""
        prices = self._make_prices([100.0] * 122 + [100.0, 200.0] + [200.0] * 200,
                                   date(2025, 1, 1))
        entry = LeapsEntrySignal(
            date=date(2025, 5, 3), price=100.0,
            drawdown_pct=20.0, bollinger_score=1.2, composite_score=0.6,
        )
        # Single stage: hold 10 days, profit > 50%, sell 100%
        stages = [(10, 50.0, 100.0)]
        trade = compute_sell_ladder(entry, prices, stages, expiration_days=190,
                                     strike_price=110.0)
        # First sell could happen at day 10+ (not day 1)
        if trade.sell_events:
            first_sell_date = trade.sell_events[0].date
            hold_days = (first_sell_date - entry.date).days
            self.assertGreaterEqual(hold_days, 10)

    def test_single_stage_sells_all_when_profit_met(self):
        """After hold period, profit threshold trips → sell 100%."""
        # Entry at 100, price rises to 150 after day 15
        values = [100.0] * 122 + [100.0] + [100.0] * 14 + [150.0] + [150.0] * 200
        start = date(2025, 1, 1)
        prices = self._make_prices(values, start)
        entry_date = start + timedelta(days=122)  # Day 122 = entry at 100
        entry = LeapsEntrySignal(
            date=entry_date, price=100.0,
            drawdown_pct=20.0, bollinger_score=1.2, composite_score=0.6,
        )
        stages = [(10, 30.0, 100.0)]  # hold 10, profit > 30%, sell all
        trade = compute_sell_ladder(entry, prices, stages, expiration_days=190,
                                     strike_price=110.0)
        self.assertGreaterEqual(len(trade.sell_events), 1)
        self.assertAlmostEqual(trade.sell_events[-1].pct_sold, 100.0)
        self.assertGreater(trade.total_roi_pct, 0)

    def test_expiration_force_sell(self):
        """If no profit trigger by expiration, force-sell remaining."""
        # Price stays flat, never triggers profit
        values = [100.0] * 400
        start = date(2025, 1, 1)
        prices = self._make_prices(values, start)
        entry_date = start + timedelta(days=122)
        entry = LeapsEntrySignal(
            date=entry_date, price=100.0,
            drawdown_pct=20.0, bollinger_score=1.2, composite_score=0.6,
        )
        stages = [(20, 50.0, 100.0)]  # hold 20, profit>50%, sell all
        trade = compute_sell_ladder(entry, prices, stages, expiration_days=190,
                                     strike_price=110.0)
        self.assertTrue(trade.expired)
        # Trade should have at least one sell event (expiration force-sell)
        self.assertGreaterEqual(len(trade.sell_events), 1)

    def test_two_stage_sells_reduce_position_progressively(self):
        """Two active stages: sell 50% at stage1, remaining 50% at stage2."""
        # Price rises gradually over time to trigger both stages
        values = [100.0] * 122 + [100.0] + list(range(100, 200))
        start = date(2025, 1, 1)
        prices = self._make_prices(values, start)
        entry_date = start + timedelta(days=122)
        entry = LeapsEntrySignal(
            date=entry_date, price=100.0,
            drawdown_pct=20.0, bollinger_score=1.2, composite_score=0.6,
        )
        stages = [(5, 10.0, 50.0), (10, 20.0, 100.0)]
        trade = compute_sell_ladder(entry, prices, stages, expiration_days=190,
                                     strike_price=110.0)
        self.assertGreaterEqual(len(trade.sell_events), 2)
        self.assertAlmostEqual(trade.sell_events[0].pct_sold, 50.0)
        self.assertAlmostEqual(trade.sell_events[1].pct_sold, 50.0)


class LeapsFullTradeTest(unittest.TestCase):
    """End-to-end: detect entries → sell ladder → ROI."""

    def _make_prices(self, values: list[float], start_date: date) -> list[tuple[date, float]]:
        return [(start_date + timedelta(days=i), v) for i, v in enumerate(values)]

    def test_full_cycle_from_detection_to_trade(self):
        """Detect entries from prices, then simulate a trade for each."""
        # Steep crash then recovery: fast enough to breach bollinger
        values = [100.0] * 122  # stable base
        # Sharp crash：100→70 in 10 days
        values += [95.0, 90.0, 85.0, 80.0, 78.0, 75.0, 74.0, 72.0, 71.0, 70.0]
        # Stay low for a bit
        values += [70.0] * 10
        # Recovery
        values += [75.0, 80.0, 85.0, 90.0, 95.0, 100.0]
        values += [105.0] * 100
        start = date(2025, 1, 1)
        prices = self._make_prices(values, start)

        entries = detect_leaps_entries(prices, drawdown_threshold_pct=15.0, entry_mode="both")
        self.assertGreater(len(entries), 0,
            f"Expected at least one LEAPS entry, got {len(entries)}")

        stages = [(15, 60.0, 50.0), (30, 100.0, 100.0)]
        for entry in entries[:3]:
            trade = compute_sell_ladder(entry, prices, stages, expiration_days=190,
                                         strike_price=entry.price * 1.10)
            self.assertIsInstance(trade, LeapsTrade)
            self.assertGreaterEqual(len(trade.sell_events), 1)


class LeapsIndividualTest(unittest.TestCase):
    """LeapsIndividual key generation and constraints."""

    def test_key_is_deterministic(self):
        ind1 = LeapsIndividual(
            drawdown_threshold_pct=20.0, entry_mode="touch",
            stage1_days=15, stage1_profit=80.0, stage1_sell=50.0,
            stage2_days=60, stage2_profit=60.0, stage2_sell=50.0,
        )
        ind2 = LeapsIndividual(
            drawdown_threshold_pct=20.0, entry_mode="touch",
            stage1_days=15, stage1_profit=80.0, stage1_sell=50.0,
            stage2_days=60, stage2_profit=60.0, stage2_sell=50.0,
        )
        self.assertEqual(ind1.key, ind2.key)

    def test_different_params_different_keys(self):
        ind1 = LeapsIndividual(
            drawdown_threshold_pct=20.0, entry_mode="touch",
            stage1_days=15, stage1_profit=80.0, stage1_sell=50.0,
            stage2_days=60, stage2_profit=60.0, stage2_sell=50.0,
        )
        ind2 = LeapsIndividual(
            drawdown_threshold_pct=25.0, entry_mode="touch",
            stage1_days=15, stage1_profit=80.0, stage1_sell=50.0,
            stage2_days=60, stage2_profit=60.0, stage2_sell=50.0,
        )
        self.assertNotEqual(ind1.key, ind2.key)


class LeapsGATest(unittest.TestCase):
    """GA operators: crossover, mutation, fitness."""

    def _make_individual(self, **overrides):
        params = {
            "drawdown_threshold_pct": 20.0, "entry_mode": "touch",
            "stage1_days": 15, "stage1_profit": 80.0, "stage1_sell": 50.0,
            "stage2_days": 60, "stage2_profit": 60.0, "stage2_sell": 50.0,
        }
        params.update(overrides)
        return LeapsIndividual(**params)

    def test_crossover_produces_valid_child(self):
        """Child respects s1_days < s2_days and s1_profit > s2_profit."""
        p1 = self._make_individual(stage1_days=10, stage1_profit=100.0,
                                    stage2_days=50, stage2_profit=60.0)
        p2 = self._make_individual(stage1_days=30, stage1_profit=70.0,
                                    stage2_days=80, stage2_profit=40.0)
        child = leaps_crossover(p1, p2)
        self.assertLess(child.stage1_days, child.stage2_days)
        self.assertGreater(child.stage1_profit, child.stage2_profit)

    def test_mutation_preserves_constraints(self):
        """After mutation, stage constraints still hold."""
        ind = self._make_individual(stage1_days=20, stage1_profit=80.0,
                                     stage2_days=60, stage2_profit=60.0)
        config = LeapsEvolutionConfig(mutation_rate=1.0)
        mutant = leaps_mutate(ind, config)
        self.assertLess(mutant.stage1_days, mutant.stage2_days)
        self.assertGreater(mutant.stage1_profit, mutant.stage2_profit)

    def test_zero_trades_gives_zero_fitness(self):
        """No trades → fitness = 0."""
        ind = self._make_individual()
        fitness = leaps_fitness_fn(ind, {})
        self.assertEqual(fitness, 0.0)

    def test_more_trades_beats_single_trade_same_total_roi(self):
        """Similar total ROI but more trades → higher fitness (density bonus)."""
        ind = self._make_individual(
            drawdown_threshold_pct=12.0, entry_mode="touch",
            stage1_days=3, stage1_profit=5.0, stage1_sell=100.0,
            stage2_days=60, stage2_profit=60.0, stage2_sell=100.0,
        )
        # 1 trade: single deep V, total ROI ~high
        values1 = [100.0] * 122 + [93.0, 88.0, 84.0, 80.0, 78.0]
        values1 += [85.0, 95.0, 105.0, 115.0]
        values1 += [115.0] * 200
        prices1 = [(date(2024, 1, 1) + timedelta(days=i), v) for i, v in enumerate(values1)]

        # 3 trades: three shallow Vs, total ROI similar but spread over more trades
        values3 = [100.0] * 122
        for _ in range(3):
            values3 += [94.0, 89.0, 85.0, 82.0, 88.0, 94.0, 100.0, 105.0]
            values3 += [105.0] * 20
        values3 += [105.0] * 50
        prices3 = [(date(2024, 1, 1) + timedelta(days=i), v) for i, v in enumerate(values3)]

        fit1 = leaps_fitness_fn(ind, {"A": prices1})
        fit3 = leaps_fitness_fn(ind, {"B": prices3})
        self.assertGreater(fit3, fit1,
            f"3-trade fitness ({fit3:.1f}) should beat 1-trade ({fit1:.1f})")


class LeapsFullEvolutionTest(unittest.TestCase):
    """Full GA evolution end-to-end."""

    def test_evolution_converges_on_price_data(self):
        """Run full GA on simple price data, verify results structure."""
        def _make_recovery_prices(start_val: float) -> list:
            values = [start_val] * 122
            values += [start_val * 0.95, start_val * 0.90, start_val * 0.87,
                      start_val * 0.85, start_val * 0.83, start_val * 0.80,
                      start_val * 0.78]
            values += [start_val * 0.85, start_val * 0.90, start_val * 0.95,
                      start_val * 1.00, start_val * 1.10, start_val * 1.20]
            values += [start_val * 1.20] * 200
            return [(date(2025, 1, 1) + timedelta(days=i), v)
                    for i, v in enumerate(values)]

        price_data = {
            "STOCK_A": _make_recovery_prices(100.0),
            "STOCK_B": _make_recovery_prices(50.0),
        }

        config = LeapsEvolutionConfig(
            population_size=10, generations=5,
            mutation_rate=0.15, crossover_rate=0.80,
            elitism_count=2, tournament_size=3, seed=42,
        )

        result = evolve_leaps_parameters(price_data, config)

        self.assertIn("best", result)
        self.assertIn("final_population", result)
        self.assertIn("snapshots", result)
        self.assertEqual(len(result["snapshots"]), 5)
        self.assertIsNotNone(result["best"])
        self.assertGreaterEqual(result["best"]["fitness"], 0.0)


if __name__ == "__main__":
    unittest.main()
