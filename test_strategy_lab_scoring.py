import unittest

from drawdown.strategy_lab_scoring import score_parameter_matrix, score_topic_observations


class StrategyLabScoringTest(unittest.TestCase):
    def test_topic_score_uses_return_90_drawdown_10(self):
        scored = score_topic_observations(
            [
                {"candidate_key": "fast", "topic_key": "one_year", "return_pct": 30, "max_drawdown_pct": -40},
                {"candidate_key": "steady", "topic_key": "one_year", "return_pct": 20, "max_drawdown_pct": -10},
            ]
        )
        by_key = {item["candidate_key"]: item for item in scored}

        self.assertAlmostEqual(by_key["fast"]["return_score"], 100.0)
        self.assertAlmostEqual(by_key["fast"]["drawdown_score"], 0.0)
        self.assertAlmostEqual(by_key["fast"]["topic_score"], 90.0)
        self.assertAlmostEqual(by_key["steady"]["return_score"], 0.0)
        self.assertAlmostEqual(by_key["steady"]["drawdown_score"], 100.0)
        self.assertAlmostEqual(by_key["steady"]["topic_score"], 10.0)
        self.assertEqual(by_key["fast"]["topic_rank"], 1)

    def test_sell_quality_weight_zero_is_backward_compat(self):
        """sell_quality_weight=0 (default) produces same scores as before."""
        observations = [
            {"candidate_key": "fast", "topic_key": "one_year", "return_pct": 30, "max_drawdown_pct": -40, "sell_quality_score": 20},
            {"candidate_key": "steady", "topic_key": "one_year", "return_pct": 20, "max_drawdown_pct": -10, "sell_quality_score": 80},
        ]
        scored = score_topic_observations(observations, sell_quality_weight=0.0)
        by_key = {item["candidate_key"]: item for item in scored}
        self.assertAlmostEqual(by_key["fast"]["topic_score"], 90.0)
        self.assertAlmostEqual(by_key["steady"]["topic_score"], 10.0)

    def test_sell_quality_weight_changes_topic_scores(self):
        """With sell_quality_weight > 0, sell_quality_score affects topic_score."""
        observations = [
            {"candidate_key": "a", "topic_key": "t1", "return_pct": 20, "max_drawdown_pct": -15, "sell_quality_score": 70},
            {"candidate_key": "b", "topic_key": "t1", "return_pct": 22, "max_drawdown_pct": -14, "sell_quality_score": 10},
            {"candidate_key": "c", "topic_key": "t1", "return_pct": 19, "max_drawdown_pct": -12, "sell_quality_score": 90},
        ]
        without = score_topic_observations(observations, sell_quality_weight=0.0)
        with_sq = score_topic_observations(observations, sell_quality_weight=0.25)
        by_key_w = {item["candidate_key"]: item["topic_score"] for item in without}
        by_key_s = {item["candidate_key"]: item["topic_score"] for item in with_sq}
        # scores must differ when sell_quality_weight is non-zero
        self.assertFalse(
            all(abs(by_key_w[k] - by_key_s[k]) < 0.01 for k in by_key_w),
            "sell_quality_weight=0.25 should change topic_scores"
        )
        # sell_quality_score is normalized and appears in output
        for item in with_sq:
            self.assertIn("sell_quality_score", item)

    def test_parameter_matrix_aggregates_topic_scores(self):
        matrix = score_parameter_matrix(
            [
                {"key": "a", "label": "A"},
                {"key": "b", "label": "B"},
            ],
            [
                {"candidate_key": "a", "topic_key": "t1", "return_pct": 30, "max_drawdown_pct": -40},
                {"candidate_key": "b", "topic_key": "t1", "return_pct": 20, "max_drawdown_pct": -10},
                {"candidate_key": "a", "topic_key": "t2", "return_pct": 5, "max_drawdown_pct": -8},
                {"candidate_key": "b", "topic_key": "t2", "return_pct": 10, "max_drawdown_pct": -12},
            ],
        )

        rows = {item["key"]: item for item in matrix["rows"]}
        self.assertAlmostEqual(rows["a"]["final_score"], 50.0)
        self.assertAlmostEqual(rows["b"]["final_score"], 50.0)
        self.assertEqual(rows["a"]["topic_count"], 2)
        self.assertIn(rows["a"]["final_rank"], {1, 2})
        self.assertAlmostEqual(matrix["weights"]["return"], 0.9)
        self.assertAlmostEqual(matrix["weights"]["drawdown"], 0.1)
        self.assertAlmostEqual(matrix["weights"]["sell_quality"], 0.0)

    def test_sell_quality_flips_ranking_when_return_gap_narrow(self):
        """With 3+ candidates (diluted normalization), sell_quality can flip rank."""
        observations = [
            {"candidate_key": "low_quality", "topic_key": "t1", "return_pct": 20, "max_drawdown_pct": -10, "sell_quality_score": 5},
            {"candidate_key": "high_quality", "topic_key": "t1", "return_pct": 19.8, "max_drawdown_pct": -10.2, "sell_quality_score": 95},
            {"candidate_key": "worst", "topic_key": "t1", "return_pct": 5, "max_drawdown_pct": -30, "sell_quality_score": 50},
        ]
        without = score_topic_observations(observations, sell_quality_weight=0.0)
        with_sq = score_topic_observations(observations, sell_quality_weight=0.35)
        by_key_wo = {item["candidate_key"]: item["topic_rank"] for item in without}
        self.assertEqual(by_key_wo["low_quality"], 1, "low_quality has best return/drawdown → rank 1 without sell quality")
        by_key_sq = {item["candidate_key"]: item["topic_rank"] for item in with_sq}
        self.assertEqual(by_key_sq["high_quality"], 1,
                         f"sell_quality_weight=0.35 should promote high_quality, got rank {by_key_sq['high_quality']}")


if __name__ == "__main__":
    unittest.main()
