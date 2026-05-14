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
        self.assertIn('id="robustScoreMode"', html)
        self.assertIn("稳健榜口径", html)
        self.assertIn("收益优先 80/20", html)
        self.assertIn("robust_score_mode: document.getElementById('robustScoreMode').value", html)
        self.assertIn('aria-label="解释 P25"', html)
        self.assertIn("document.getElementById('robustBoard')", html)
        self.assertIn("setFieldValue('stepPct', candidate.step_pct)", html)
        self.assertIn("setFieldValue('equalSliceAllocation', candidate.equal_slice_allocation_pct)", html)
        self.assertIn("第 25 分位数", html)
        self.assertIn("强势命中率", html)
        self.assertIn("踩坑率", html)

    def test_salary_flow_dca_explanation_is_visible_in_strategy_reference(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab").get_data(as_text=True)

        self.assertIn("查看工资流定投说明", html)
        self.assertIn("月注入资金 × 标的权重 ÷ 4", html)
        self.assertIn("1.4x、2.0x、3.0x、4.0x", html)
        self.assertIn("余额扫入", html)
        self.assertIn("drawdown_boost", html)


if __name__ == "__main__":
    unittest.main()
