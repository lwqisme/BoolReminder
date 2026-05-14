import re
import unittest

from web.app import app


class StrategyLabFrontendTest(unittest.TestCase):
    def test_scorecard_detail_button_opens_visible_results_detail(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab").get_data(as_text=True)

        self.assertIn("onclick=\"loadScorecardDetail('${question.key}', '${item.buy_strategy}', '${item.sell_strategy}')\"", html)
        detail_function = re.search(
            r"async function loadScorecardDetail\(questionKey, buyStrategy, sellStrategy\) \{(?P<body>.*?)\n        async function runScorecard",
            html,
            re.S,
        )
        self.assertIsNotNone(detail_function)
        body = detail_function.group("body")
        self.assertIn("activateTab('results')", body)
        self.assertNotIn("activateTab('scorecard')", body)

    def test_scorecard_payload_uses_selected_strategy_filters(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab").get_data(as_text=True)

        self.assertIn("buy_strategies: selectedStrategies('buyStrategy', buyStrategyLabels)", html)
        self.assertIn("sell_strategies: selectedSellStrategies()", html)
        self.assertIn("function applyRobustCandidate(candidateKey)", html)
        self.assertIn("应用并看评分", html)
        self.assertIn("第 25 分位数", html)
        self.assertIn("强势命中率", html)
        self.assertIn("踩坑率", html)


if __name__ == "__main__":
    unittest.main()
