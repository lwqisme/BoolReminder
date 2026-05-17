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
        self.assertIn("收益榜口径：收益 90% / 回撤 10%", html)
        self.assertIn("ranking_formula: 'return_90_drawdown_10'", html)
        self.assertNotIn("robust_score_mode", html)
        self.assertNotIn('id="robustScoreMode"', html)
        self.assertNotIn("综合排序", html)
        self.assertNotIn("收益优先 80/20", html)
        self.assertIn('id="scoreSellStrategy"', html)
        self.assertIn("score_sell_strategies: selectedSellStrategies()", html)
        self.assertNotIn('aria-label="解释 P25"', html)
        self.assertNotIn("Top10%", html)
        self.assertNotIn("Bottom10%", html)
        self.assertIn("document.getElementById('robustBoard')", html)
        self.assertIn("setFieldValue('stepPct', candidate.step_pct)", html)
        self.assertIn("setFieldValue('equalSliceAllocation', candidate.equal_slice_allocation_pct)", html)
        self.assertIn('id="coreDipInitialCorePct"', html)
        self.assertIn('id="coreDipWeeklyCorePct"', html)
        self.assertIn('id="coreDipCashReservePct"', html)
        self.assertIn('id="coreDipStartDrawdownPct"', html)
        self.assertIn('id="coreDipFullDrawdownPct"', html)
        self.assertIn('id="coreDipTimingEnabled"', html)
        self.assertIn('id="coreDipTimingMaxDelayDays"', html)
        self.assertIn('id="coreDipTimingRiseThresholdPct"', html)
        self.assertIn('id="coreDipTimingNearLowPct"', html)
        self.assertIn("core_dip_initial_core_pct: readNumber('coreDipInitialCorePct')", html)
        self.assertIn("core_dip_timing_enabled: document.getElementById('coreDipTimingEnabled').value === 'true'", html)
        self.assertIn("default_core_dip_initial_core_pct: readNumber('coreDipInitialCorePct')", html)
        self.assertIn("default_core_dip_timing_enabled: document.getElementById('coreDipTimingEnabled').value === 'true'", html)
        self.assertIn("setFieldValue('coreDipInitialCorePct', candidate.core_dip_initial_core_pct)", html)
        self.assertIn("function coreTimingAllowsBuy", html)
        self.assertIn("买点优化 延迟${number(candidate.core_dip_timing_max_delay_days)}日", html)
        self.assertIn("bits.push('买点优化 关闭')", html)
        self.assertIn("setSelectValue('coreDipTimingEnabled', String(Boolean(candidate.core_dip_timing_enabled)))", html)
        self.assertIn('id="robustCoreDipTimingFilter"', html)
        self.assertIn("core_dip_timing_filter: document.getElementById('robustCoreDipTimingFilter').value", html)
        self.assertIn('id="universeGrid"', html)
        self.assertIn("function initUniverseRows", html)
        self.assertIn("function saveUniverseSymbols", html)
        self.assertIn("default_investment_universe: readInvestmentUniverse()", html)
        self.assertIn("investment_universe: readInvestmentUniverse()", html)
        self.assertIn("const scorecardSymbolKeys =", html)
        self.assertIn("return scorecardSymbolKeys[normalized]", html)
        self.assertNotIn('value="symbol_tsm_us"', html)
        self.assertNotIn('value="symbol_googl_us"', html)
        self.assertNotIn('value="symbol_tsla_us"', html)
        self.assertIn("全局投资标的库", html)
        self.assertIn("function robustCoreDipBuyParams(candidate)", html)
        self.assertIn('id="dcaRearmDrawdown"', html)
        self.assertIn("dca_rearm_drawdown_pct: readNumber('dcaRearmDrawdown')", html)
        self.assertIn("default_dca_rearm_drawdown_pct: readNumber('dcaRearmDrawdown')", html)
        self.assertIn("setFieldValue('dcaRearmDrawdown', candidate.dca_rearm_drawdown_pct)", html)
        self.assertIn('id="gridReboundStep"', html)
        self.assertIn('id="gridFirstSellPct"', html)
        self.assertIn('id="gridSecondSellPct"', html)
        self.assertIn('id="gridMinSellAmount"', html)
        self.assertIn("grid_rebound_step_pct: readNumber('gridReboundStep')", html)
        self.assertIn("default_grid_rebound_step_pct: readNumber('gridReboundStep')", html)
        self.assertIn("setFieldValue('gridReboundStep', candidate.grid_rebound_step_pct)", html)
        self.assertIn("function robustGridSellParams(candidate)", html)
        self.assertIn("candidate.grid_rebound_step_pct ?? readNumber('gridReboundStep')", html)
        self.assertIn("网格回弹步长", html)
        self.assertIn("解释网格第一档卖出", html)
        self.assertIn("解释网格第二档卖出", html)
        self.assertIn("解释网格最小卖出额", html)
        self.assertIn('id="costFirstProfitPct"', html)
        self.assertIn('id="costMinSellAmount"', html)
        self.assertIn("cost_first_profit_pct: readNumber('costFirstProfitPct')", html)
        self.assertIn("default_cost_first_profit_pct: readNumber('costFirstProfitPct')", html)
        self.assertIn("setFieldValue('costFirstProfitPct', candidate.cost_first_profit_pct)", html)
        self.assertIn("function robustCostSellParams(candidate)", html)
        self.assertIn("解释成本盈利档位", html)
        self.assertIn("解释成本卖出比例", html)
        self.assertIn("解释成本最小卖出额", html)
        self.assertIn('id="robustEstimate"', html)
        self.assertNotIn('id="robustComputeMode"', html)
        self.assertNotIn("robust_compute_mode: document.getElementById('robustComputeMode').value", html)
        self.assertNotIn("computeMode === 'full'", html)
        self.assertNotIn("candidate_neighborhoods", html)
        self.assertNotIn("fineByKey", html)
        self.assertIn("function scoreCandidateLikeScorecardPage", html)
        self.assertIn("scoreCandidatesLikeScorecardPage", html)
        self.assertIn("scorecardMultiplier", html)
        self.assertIn("评分只比较收益率和最大回撤，固定按收益 90% / 回撤 10% 排名", html)
        self.assertIn("client_full_candidate_return_90_drawdown_10_leaderboard", html)
        self.assertIn("client_parallel_full_candidate_return_90_drawdown_10_leaderboard", html)
        self.assertIn("全候选全题", html)
        self.assertIn("function estimateRobustWorkload()", html)
        self.assertIn('id="robustBuyStrategies" role="group"', html)
        self.assertIn('id="robustSellStrategies" role="group"', html)
        self.assertIn('name="robustBuyStrategy"', html)
        self.assertIn('name="robustSellStrategy"', html)
        self.assertIn("robust-strategy-option", html)
        self.assertIn("querySelectorAll('input[type=\"checkbox\"]:checked')", html)
        self.assertNotIn('id="robustBuyStrategies" multiple', html)
        self.assertNotIn('id="robustSellStrategies" multiple', html)
        self.assertIn("function selectedRobustBuyStrategies()", html)
        self.assertIn("function selectedRobustSellStrategies()", html)
        self.assertIn("buy_strategies: selectedRobustBuyStrategies()", html)
        self.assertIn("sell_strategies: selectedRobustSellStrategies()", html)
        self.assertIn("score_sell_strategies: selectedRobustSellStrategies()", html)
        self.assertIn('id="jobPauseButton"', html)
        self.assertIn('id="jobResumeButton"', html)
        self.assertIn('id="jobCancelButton"', html)
        self.assertIn("async function pauseStrategyJob()", html)
        self.assertIn("async function resumeStrategyJob()", html)
        self.assertIn("async function cancelStrategyJob()", html)
        self.assertIn("${encodeURIComponent(activeStrategyJob.id)}/${action}", html)
        self.assertIn("function clientRobustWorkerSource()", html)
        self.assertIn("new Worker(URL.createObjectURL(new Blob", html)
        self.assertIn("并发自检", html)
        self.assertIn("function runRobustConcurrencyProbe()", html)
        self.assertIn("worker合计", html)
        self.assertIn("/api/strategy-lab/robust/client-packet", html)
        self.assertIn("本机计算 Top10", html)
        self.assertIn("小规模服务端 Top10", html)
        self.assertIn("buyStrategy === 'core_dip_dca'", html)
        self.assertIn("const gridVariantCount = 5 * 4 * 4;", html)
        self.assertIn("return gridVariantCount * rearmMultiplier;", html)
        self.assertIn("预估计算量", html)
        self.assertIn("实际演算", html)
        self.assertIn("解释本次遍历参数", html)
        self.assertIn("解释卖后重启", html)
        self.assertIn("三档金字塔：整仓/网格/成本卖出后", html)
        self.assertIn("解释候选组合", html)
        self.assertNotIn("解释粗筛候选", html)
        self.assertNotIn("解释局部加密候选", html)
        self.assertNotIn("解释最终验证候选", html)
        self.assertIn("function normalizedRangeRatio(value, values)", html)
        self.assertIn("function heatmapFillStyle(ratio, cssVar = '--scan-fill')", html)
        self.assertIn("function scorecardCellHeatStyle(strategy, cells)", html)
        self.assertIn("const scoreRatio = normalizedRangeRatio(strategy.score, scoreValues);", html)
        self.assertIn("style: heatmapFillStyle(scoreRatio, '--score-fill')", html)
        self.assertIn("颜色分位", html)
        self.assertNotIn("returnGap / 18", html)
        self.assertNotIn("drawdownGap / 8", html)
        self.assertIn("setSelectValue('buyStrategy', 'all')", html)
        self.assertIn("setSelectValue('scoreSellStrategy', 'all')", html)
        self.assertIn("保持评分为全量策略", html)
        self.assertIn("卖后重启", html)
        self.assertNotIn("第 25 分位数", html)
        self.assertNotIn("强势命中率", html)
        self.assertNotIn("踩坑率", html)
        self.assertIn("defaultSellStrategyKeys = Object.keys(sellStrategyLabels);", html)
        self.assertIn("现在包含网格回弹卖出", html)
        self.assertIn("核心定投+回撤加仓", html)
        self.assertNotIn("平方递增加权细切", html)

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
        self.assertIn("当前勾选题目会同时影响收益 Top10 与策略评分", html)

        scan_panel = re.search(
            r'<div id="scanWorkspace".*?<div id="robustWorkspace"',
            html,
            re.S,
        )
        self.assertIsNotNone(scan_panel)
        self.assertNotIn("运行收益 Top10", scan_panel.group(0))

    def test_score_matrix_heatmap_uses_linear_question_range(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab").get_data(as_text=True)

        normalized = re.search(
            r"function normalizedRangeRatio\(value, values\) \{(?P<body>.*?)\n        function heatmapFillStyle",
            html,
            re.S,
        )
        self.assertIsNotNone(normalized)
        self.assertIn("(Number(value || 0) - min) / (max - min)", normalized.group("body"))

        score_style = re.search(
            r"function scorecardCellHeatStyle\(strategy, cells\) \{(?P<body>.*?)\n        function showScoreTooltip",
            html,
            re.S,
        )
        self.assertIsNotNone(score_style)
        body = score_style.group("body")
        self.assertIn("normalizedRangeRatio(strategy.return_pct, returnValues)", body)
        self.assertIn("normalizedRangeRatio(strategy.max_drawdown_pct, drawdownValues)", body)
        self.assertIn("normalizedRangeRatio(strategy.score, scoreValues)", body)
        self.assertIn("heatmapFillStyle(scoreRatio, '--score-fill')", body)

    def test_salary_flow_dca_explanation_is_visible_in_strategy_reference(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab").get_data(as_text=True)

        self.assertIn("查看工资流定投说明", html)
        self.assertIn("月注入资金 × 标的权重 ÷ 4", html)
        self.assertIn("1.4x、2.0x、3.0x、4.0x", html)
        self.assertIn("余额扫入", html)
        self.assertIn("drawdown_boost", html)
        self.assertIn("卖后重启回撤", html)

    def test_parameter_lab_page_exposes_full_matrix_worker_flow(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab/parameter-lab").get_data(as_text=True)

        self.assertIn("Strategy + params full lab", html)
        self.assertIn("/api/strategy-lab/parameter-lab/packet", html)
        self.assertIn("new Worker('/static/strategy_parameter_lab_worker.js')", html)
        self.assertIn("function runWorkerPool(packet, startedAt)", html)
        self.assertIn("function scoreParameterResults(packet, partialRows, workerStats, wallMs)", html)
        self.assertIn("chunks_completed_per_worker", html)
        self.assertIn("cpu_work_estimate_ms", html)
        self.assertIn('id="matrixHead"', html)
        self.assertIn('id="matrixBody"', html)
        self.assertIn('id="candidateGridSummary"', html)
        self.assertIn("function renderCandidateGridSummary()", html)
        self.assertIn("步长候选", html)
        self.assertIn("每步投入", html)
        self.assertIn("卖后重启", html)
        self.assertIn("topic_rank", html)
        self.assertIn("topic_score", html)
        self.assertIn("parameter_snapshot", html)
        self.assertIn("strategyLabPendingParameterCandidate", html)
        self.assertIn("应用到主实验室", html)

    def test_strategy_lab_can_consume_parameter_lab_apply_payload(self):
        with app.test_client() as client:
            html = client.get("/strategy-lab").get_data(as_text=True)

        self.assertIn('/strategy-lab/parameter-lab', html)
        self.assertIn("function applyPendingParameterLabCandidate()", html)
        self.assertIn("localStorage.getItem('strategyLabPendingParameterCandidate')", html)
        self.assertIn("applyRunConfigPayload(payload)", html)
        self.assertIn("已应用 Parameter Lab 参数，并恢复原评分题目与周期", html)


if __name__ == "__main__":
    unittest.main()
