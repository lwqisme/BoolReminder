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
        self.assertEqual(matrix["weights"], {"return": 0.9, "drawdown": 0.1})


if __name__ == "__main__":
    unittest.main()
