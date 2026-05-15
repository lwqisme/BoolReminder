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
        self.assertIn("应用参数并看全量评分", html)
        self.assertIn('id="robustScoreMode"', html)
        self.assertIn("稳健榜口径", html)
        self.assertIn("收益优先 80/20", html)
        self.assertIn("robust_score_mode: document.getElementById('robustScoreMode').value", html)
        self.assertIn('id="scoreSellStrategy"', html)
        self.assertIn("score_sell_strategies: selectedSellStrategies()", html)
        self.assertIn('aria-label="解释 P25"', html)
        self.assertIn("document.getElementById('robustBoard')", html)
        self.assertIn("setFieldValue('stepPct', candidate.step_pct)", html)
        self.assertIn("setFieldValue('equalSliceAllocation', candidate.equal_slice_allocation_pct)", html)
        self.assertIn('id="dcaRearmDrawdown"', html)
        self.assertIn("dca_rearm_drawdown_pct: readNumber('dcaRearmDrawdown')", html)
        self.assertIn("default_dca_rearm_drawdown_pct: readNumber('dcaRearmDrawdown')", html)
        self.assertIn("setFieldValue('dcaRearmDrawdown', candidate.dca_rearm_drawdown_pct)", html)
        self.assertIn("setSelectValue('buyStrategy', 'all')", html)
        self.assertIn("setSelectValue('scoreSellStrategy', 'all')", html)
        self.assertIn("保持评分为全量策略", html)
        self.assertIn("DCA重启", html)
        self.assertIn("第 25 分位数", html)
        self.assertIn("强势命中率", html)
        self.assertIn("踩坑率", html)
        self.assertIn("defaultSellStrategyKeys = Object.keys(sellStrategyLabels);", html)
        self.assertIn("现在包含网格回弹卖出", html)

    def test_robust_top10_is_independent_and_shares_score_topics(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab").get_data(as_text=True)

        self.assertIn('data-tab="robust"', html)
        self.assertIn('id="robustWorkspace"', html)
        self.assertIn("共享题目矩阵", html)
        self.assertIn("function syncScorecardTopic(changedInput)", html)
        self.assertIn("function syncScorecardPeriod(changedInput)", html)
        self.assertIn("function selectedScorecardPeriods()", html)
        self.assertIn("enabled: !enabledEl || enabledEl.checked", html)
        self.assertIn("当前勾选题目会同时影响稳健 Top10 与策略评分", html)

        scan_panel = re.search(
            r'<div id="scanWorkspace".*?<div id="robustWorkspace"',
            html,
            re.S,
        )
        self.assertIsNotNone(scan_panel)
        self.assertNotIn("运行稳健 Top10", scan_panel.group(0))

    def test_salary_flow_dca_explanation_is_visible_in_strategy_reference(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab").get_data(as_text=True)

        self.assertIn("查看工资流定投说明", html)
        self.assertIn("月注入资金 × 标的权重 ÷ 4", html)
        self.assertIn("1.4x、2.0x、3.0x、4.0x", html)
        self.assertIn("余额扫入", html)
        self.assertIn("drawdown_boost", html)
        self.assertIn("DCA卖出重启回撤", html)


if __name__ == "__main__":
    unittest.main()
