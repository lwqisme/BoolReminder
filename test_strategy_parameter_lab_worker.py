import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from drawdown.position_strategy import StrategyInputs
from drawdown.strategy_parameter_registry import expand_strategy_candidate_payloads


WORKER_JS = Path(__file__).resolve().parent / "web/static/strategy_parameter_lab_worker.js"
PARAMETER_LAB_HTML = Path(__file__).resolve().parent / "web/templates/strategy_parameter_lab.html"


class StrategyParameterLabWorkerTest(unittest.TestCase):
    def test_worker_inflates_v3_packet_and_precomputes_task_contexts(self):
        source = WORKER_JS.read_text(encoding="utf-8")

        self.assertIn("function rebuildPricePoints(dates, closes, start, end)", source)
        self.assertIn("function buildTaskContexts(packet)", source)
        self.assertIn("function inflateCandidate(packet, candidateRow)", source)
        self.assertIn("function buildCandidateKey(buyStrategy, sellStrategy, buyParams, sellParams)", source)
        self.assertIn("const candidateRows = Array.isArray(message.candidate_rows) ? message.candidate_rows : []", source)
        self.assertIn("packet.market_data", source)
        self.assertIn("monthlyContributionDays(allDays)", source)
        self.assertIn("dcaDays[symbol] = weeklyDcaDays(points)", source)

    def test_worker_returns_candidate_id_key_rows_without_full_candidate_object(self):
        source = WORKER_JS.read_text(encoding="utf-8")

        self.assertIn("rows.push({ candidate_id: candidate.candidate_id, candidate_key: candidate.key, observations })", source)
        self.assertNotIn("rows.push({ candidate, observations })", source)
        self.assertIn("type: 'batch_done'", source)
        self.assertIn("leaps_signal: summarizeLeapsSignals(tradeLog, inputs, Boolean(workerState?.include_leaps_signal_details))", source)

    def test_worker_trade_log_and_series_are_detail_only(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript worker detail payload check")

        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const messages = [];
const context = {
  console: { info() {}, warn() {}, error() {} },
  postMessage(message) { messages.push(message); },
  performance: { now: () => 0 },
  setTimeout
};
context.self = context;
vm.createContext(context);
vm.runInContext(source, context);

const buyFields = [
  'step_pct', 'equal_slice_allocation_pct', 'core_dip_initial_core_pct',
  'core_dip_weekly_core_pct', 'core_dip_cash_reserve_pct',
  'core_dip_start_drawdown_pct', 'core_dip_full_drawdown_pct',
  'core_dip_timing_enabled', 'core_dip_timing_max_delay_days',
  'core_dip_timing_rise_threshold_pct', 'core_dip_timing_near_low_pct'
];
const sellFields = [
  'sell_min_profit_pct', 'repair_sell_cooldown_days', 'repair_stage_sell_pct',
  'grid_rebound_step_pct', 'grid_first_sell_pct', 'grid_second_sell_pct',
  'grid_min_sell_amount', 'cost_first_profit_pct', 'cost_second_profit_pct',
  'cost_third_profit_pct', 'cost_first_sell_pct', 'cost_second_sell_pct',
  'cost_third_sell_pct', 'cost_deleverage_cooldown_days',
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct'
];
function packet(flags) {
  return {
    run_id: flags.run_id,
    inputs: {
      initial_cash: 1000,
      monthly_contribution: 0,
      max_drawdown_pct: 50,
      drawdown_basis: 'ath',
      trade_fee: 0,
      hkd_to_usd: 0.128,
      reserve_position_pct: 0,
      sell_min_profit_pct: 0,
      sell_allow_same_day_sell: false,
      dca_rearm_drawdown_pct: 0
    },
    tasks: [{
      key: 'googl_3y',
      portfolio_key: 'googl_100',
      portfolio_label: 'GOOGL',
      period_key: 'three_years',
      period_label: '近三年',
      start: '2025-09-01',
      end: '2025-09-08',
      symbols: ['GOOGL.US'],
      targets: [{ symbol: 'GOOGL.US', weight: 100, name: 'GOOGL', max_drawdown_pct: 50 }]
    }],
    market_data: {
      symbols: {
        'GOOGL.US': {
          dates: ['2025-09-01', '2025-09-02', '2025-09-03', '2025-09-04', '2025-09-05', '2025-09-08'],
          closes: [200, 100, 116, 130, 130, 100]
        }
      }
    },
    buy_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...buyFields],
    sell_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...sellFields],
    candidate_schema: ['candidate_id', 'buy_variant_id', 'sell_variant_id'],
    buy_variants: [[0, 'buy:pyramid_3', 'pyramid_3', null, null, null, null, null, null, null, null, null, null, null]],
    sell_variants: [[0, 'sell:cost', 'cost_deleverage', 0, null, null, null, null, null, null, 15, 25, 35, 25, 25, 25, 2, false, 0, 0]],
    candidate_rows: [[0, 0, 0]],
    include_trades: Boolean(flags.include_trades),
    include_series: Boolean(flags.include_series)
  };
}
async function run(flags) {
  messages.length = 0;
  const p = packet(flags);
  await context.initRun(p, 0, p.run_id, 1);
  await context.processBatch({ run_id: p.run_id, worker_index: 0, batch_id: 'b1', candidate_rows: p.candidate_rows }, 0, p.run_id);
  const done = messages.find((message) => message.type === 'batch_done');
  return done.rows[0].observations[0];
}
(async () => {
  const normal = await run({ run_id: 'normal' });
  const detailed = await run({ run_id: 'detail', include_trades: true, include_series: true });
  process.stdout.write(JSON.stringify({ normal, detailed }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
        completed = subprocess.run(
            ["node", "-e", script, str(WORKER_JS)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertNotIn("trade_log", result["normal"])
        self.assertNotIn("series", result["normal"])
        self.assertIn("trade_log", result["detailed"])
        self.assertIn("series", result["detailed"])
        self.assertGreater(len(result["detailed"]["trade_log"]), 0)
        self.assertEqual(result["detailed"]["series"]["dates"][0], "2025-09-01")

    def test_worker_googl_cost_detail_replay_skips_cooldown_day_sell(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript GOOGL replay check")

        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const messages = [];
const context = {
  console: { info() {}, warn() {}, error() {} },
  postMessage(message) { messages.push(message); },
  performance: { now: () => 0 },
  setTimeout
};
context.self = context;
vm.createContext(context);
vm.runInContext(source, context);
const buyFields = [
  'step_pct', 'equal_slice_allocation_pct', 'core_dip_initial_core_pct',
  'core_dip_weekly_core_pct', 'core_dip_cash_reserve_pct',
  'core_dip_start_drawdown_pct', 'core_dip_full_drawdown_pct',
  'core_dip_timing_enabled', 'core_dip_timing_max_delay_days',
  'core_dip_timing_rise_threshold_pct', 'core_dip_timing_near_low_pct'
];
const sellFields = [
  'sell_min_profit_pct', 'repair_sell_cooldown_days', 'repair_stage_sell_pct',
  'grid_rebound_step_pct', 'grid_first_sell_pct', 'grid_second_sell_pct',
  'grid_min_sell_amount', 'cost_first_profit_pct', 'cost_second_profit_pct',
  'cost_third_profit_pct', 'cost_first_sell_pct', 'cost_second_sell_pct',
  'cost_third_sell_pct', 'cost_deleverage_cooldown_days',
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct'
];
const packet = {
  run_id: 'googl-detail',
  inputs: {
    initial_cash: 1000,
    monthly_contribution: 0,
    max_drawdown_pct: 50,
    drawdown_basis: 'ath',
    trade_fee: 0,
    hkd_to_usd: 0.128,
    reserve_position_pct: 0,
    sell_min_profit_pct: 0,
    sell_allow_same_day_sell: false,
    dca_rearm_drawdown_pct: 0
  },
  tasks: [{
    key: 'googl_3y',
    portfolio_key: 'googl_100',
    portfolio_label: 'GOOGL',
    period_key: 'three_years',
    period_label: '近三年',
    start: '2025-09-01',
    end: '2025-09-08',
    symbols: ['GOOGL.US'],
    targets: [{ symbol: 'GOOGL.US', weight: 100, name: 'GOOGL', max_drawdown_pct: 50 }]
  }],
  market_data: {
    symbols: {
      'GOOGL.US': {
        dates: ['2025-09-01', '2025-09-02', '2025-09-03', '2025-09-04', '2025-09-05', '2025-09-08'],
        closes: [200, 100, 116, 130, 130, 100]
      }
    }
  },
  buy_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...buyFields],
  sell_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...sellFields],
  candidate_schema: ['candidate_id', 'buy_variant_id', 'sell_variant_id'],
  buy_variants: [[0, 'buy:pyramid_3', 'pyramid_3', null, null, null, null, null, null, null, null, null, null, null]],
  sell_variants: [[0, 'sell:cost', 'cost_deleverage', 0, null, null, null, null, null, null, 15, 25, 35, 25, 25, 25, 2, false, 0, 0]],
  candidate_rows: [[0, 0, 0]],
  include_trades: true,
  include_series: true
};
(async () => {
  await context.initRun(packet, 0, packet.run_id, 1);
  await context.processBatch({ run_id: packet.run_id, worker_index: 0, batch_id: 'b1', candidate_rows: packet.candidate_rows }, 0, packet.run_id);
  const done = messages.find((message) => message.type === 'batch_done');
  process.stdout.write(JSON.stringify(done.rows[0].observations[0].trade_log));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
        completed = subprocess.run(
            ["node", "-e", script, str(WORKER_JS)],
            check=True,
            capture_output=True,
            text=True,
        )
        trades = json.loads(completed.stdout)
        actions = [(trade["date"], trade["action"], trade.get("trigger_value")) for trade in trades]

        self.assertIn(("2025-09-02", "buy", None), actions)
        self.assertIn(("2025-09-03", "sell", 15), actions)
        self.assertIn(("2025-09-05", "sell", 25), actions)
        self.assertIn(("2025-09-08", "buy", None), actions)
        self.assertNotIn(("2025-09-04", "sell", 25), actions)

    def test_leaps_signal_helper_scores_low_cash_deep_drawdown_above_chasing_buy(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript LEAPS helper check")

        script = """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const context = {
  console,
  postMessage() {},
  performance: { now: () => 0 }
};
context.self = context;
vm.createContext(context);
vm.runInContext(source, context);
const inputs = {
  leaps_low_cash_threshold_pct: 12,
  leaps_min_drawdown_pct: 12,
  leaps_premium_budget_cap: 1000,
  leaps_target_dte_label: '18-24M'
};
const lowCash = context.scoreLeapsBuySignal({
  action: 'buy',
  date: '2024-01-10',
  symbol: 'TSLA.US',
  drawdown_pct: 24,
  cash_pct_after: 3,
  gross_amount: 900,
  day_change_pct: -1.2
}, context.leapsSignalSettings(inputs));
const highCash = context.scoreLeapsBuySignal({
  action: 'buy',
  date: '2024-01-11',
  symbol: 'TSLA.US',
  drawdown_pct: 24,
  cash_pct_after: 45,
  gross_amount: 900,
  day_change_pct: -1.2
}, context.leapsSignalSettings(inputs));
const chasing = context.scoreLeapsBuySignal({
  action: 'buy',
  date: '2024-01-12',
  symbol: 'TSLA.US',
  drawdown_pct: 24,
  cash_pct_after: 3,
  gross_amount: 900,
  day_change_pct: 5.4
}, context.leapsSignalSettings(inputs));
const shallow = context.scoreLeapsBuySignal({
  action: 'buy',
  date: '2024-01-13',
  symbol: 'TSLA.US',
  drawdown_pct: 2,
  cash_pct_after: 3,
  gross_amount: 900,
  day_change_pct: -0.5
}, context.leapsSignalSettings(inputs));
process.stdout.write(JSON.stringify({ lowCash, highCash, chasing, shallow }));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(WORKER_JS)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["lowCash"]["grade"], "高")
        self.assertGreater(result["lowCash"]["score"], result["highCash"]["score"])
        self.assertGreater(result["lowCash"]["score"], result["chasing"]["score"])
        self.assertGreater(result["lowCash"]["score"], result["shallow"]["score"])
        self.assertIn("低现金", result["lowCash"]["reasons"])
        self.assertIn("追高日降级", result["chasing"]["reasons"])

    def test_leaps_signal_summary_matches_next_same_symbol_stock_sell(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript LEAPS helper check")

        script = """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const context = {
  console,
  postMessage() {},
  performance: { now: () => 0 }
};
context.self = context;
vm.createContext(context);
vm.runInContext(source, context);
const inputs = {
  leaps_low_cash_threshold_pct: 12,
  leaps_min_drawdown_pct: 12,
  leaps_premium_budget_cap: 1000,
  leaps_target_dte_label: '18-24M'
};
const baseBuy = {
  action: 'buy',
  drawdown_pct: 24,
  cash_pct_after: 3,
  gross_amount: 900,
  day_change_pct: -1.2
};
const tradeLog = [
  { ...baseBuy, date: '2024-01-10', symbol: 'TSLA.US' },
  { action: 'sell', date: '2024-01-12', symbol: 'MSFT.US' },
  { action: 'sell', date: '2024-01-09', symbol: 'TSLA.US' },
  { action: 'sell', date: '2024-01-10', symbol: 'TSLA.US' },
  { action: 'sell', date: '2024-01-20', symbol: 'TSLA.US' },
  { action: 'sell', date: '2024-01-15', symbol: 'TSLA.US' },
  { ...baseBuy, date: '2024-02-01', symbol: 'GOOG.US' },
  { action: 'sell', date: '2024-02-02', symbol: 'AMZN.US' }
];
process.stdout.write(JSON.stringify(context.summarizeLeapsSignals(tradeLog, inputs)));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(WORKER_JS)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        signals = {item["symbol"]: item for item in result["top_signals"]}

        self.assertEqual(signals["TSLA.US"]["next_stock_sell_date"], "2024-01-15")
        self.assertEqual(signals["TSLA.US"]["stock_holding_days"], 5)
        self.assertIn("stock_buy_price", signals["TSLA.US"])
        self.assertIn("stock_sell_price", signals["TSLA.US"])
        self.assertEqual(signals["GOOG.US"]["next_stock_sell_date"], "")
        self.assertIsNone(signals["GOOG.US"]["stock_holding_days"])

    def test_leaps_signal_details_are_opt_in_and_match_trigger_count(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript LEAPS helper check")

        script = """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const context = {
  console,
  postMessage() {},
  performance: { now: () => 0 }
};
context.self = context;
vm.createContext(context);
vm.runInContext(source, context);
const inputs = {
  leaps_low_cash_threshold_pct: 12,
  leaps_min_drawdown_pct: 12,
  leaps_premium_budget_cap: 1000,
  leaps_target_dte_label: '18-24M'
};
const buy = {
  action: 'buy',
  symbol: 'TSLA.US',
  drawdown_pct: 24,
  cash_pct_after: 3,
  gross_amount: 900,
  day_change_pct: -1.2
};
const tradeLog = [
  { ...buy, date: '2024-01-10' },
  { ...buy, date: '2024-01-11' },
  { action: 'sell', date: '2024-01-20', symbol: 'TSLA.US' }
];
process.stdout.write(JSON.stringify({
  normal: context.summarizeLeapsSignals(tradeLog, inputs),
  detailed: context.summarizeLeapsSignals(tradeLog, inputs, true)
}));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(WORKER_JS)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertNotIn("all_signals", result["normal"])
        self.assertEqual(result["detailed"]["trigger_count"], 2)
        self.assertEqual(len(result["detailed"]["all_signals"]), result["detailed"]["trigger_count"])
        self.assertEqual(result["detailed"]["all_signals"][0]["next_stock_sell_date"], "2024-01-20")
        self.assertEqual(result["detailed"]["all_signals"][0]["stock_holding_days"], 10)

    def test_worker_candidate_keys_match_python_registry_for_representative_cases(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript worker parity check")

        samples = []
        cases = [
            (["pyramid_3"], ["none"], StrategyInputs(), 0),
            (["equal_slice"], ["grid_rebound"], StrategyInputs(grid_min_sell_amount=123.456789), lambda items: len(items) // 2),
            (["weekly_dca"], ["repair_step"], StrategyInputs(), -1),
            (["salary_flow_dca"], ["cost_deleverage"], StrategyInputs(cost_min_sell_amount=12.3456789), 0),
            (["core_dip_dca"], ["grid_rebound"], StrategyInputs(), lambda items: next((index for index, item in enumerate(items) if item.get("core_dip_timing_enabled")), 0)),
        ]
        for buy_strategies, sell_strategies, inputs, selector in cases:
            candidates = expand_strategy_candidate_payloads(buy_strategies, sell_strategies, inputs)
            index = selector(candidates) if callable(selector) else selector
            samples.append(candidates[index])

        script = """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const cases = JSON.parse(process.argv[2]);
const context = {
  console,
  postMessage() {},
  performance: { now: () => 0 }
};
context.self = context;
vm.createContext(context);
vm.runInContext(source, context);
const results = cases.map((candidate) => context.buildCandidateKey(candidate.buy_strategy, candidate.sell_strategy, candidate, candidate));
process.stdout.write(JSON.stringify(results));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(WORKER_JS), json.dumps(samples)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(completed.stdout), [candidate["key"] for candidate in samples])

    def test_page_scores_worker_rows_by_candidate_key_and_keeps_pause_cancel(self):
        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")

        self.assertIn("function buildCandidateIndex(packet)", html)
        self.assertIn("candidate_rows: next.candidate_rows", html)
        self.assertIn("const candidate = candidateById.get(String(row.candidate_id)) || candidateByKey.get(row.candidate_key)", html)
        self.assertIn("row.candidate_key", html)
        self.assertIn("type: 'pause', run_id: currentRun.run_id", html)
        self.assertIn("type: 'cancel', run_id: runId", html)
        self.assertIn("/api/strategy-lab/parameter-lab/estimate", html)
        self.assertIn("confirmLargeRunIfNeeded", html)

    def test_page_groups_parameter_rows_and_shows_dual_ranks(self):
        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")

        self.assertIn("function strategyGroupKey(candidate)", html)
        self.assertIn("return `${candidate?.buy_strategy || ''}__${candidate?.sell_strategy || ''}`;", html)
        self.assertIn("function strategyGroupLabel(candidate)", html)
        self.assertIn("function rankParameterRows(rows, rankMethod = 'normalized')", html)
        self.assertIn("row.global_rank = index + 1", html)
        self.assertIn("row.final_rank = row.global_rank", html)
        self.assertIn("row.group_rank = index + 1", html)
        self.assertIn("function groupVisibleParameterRows(visibleRows, allRows, rankMethod)", html)
        self.assertIn("class=\"group-row\"", html)
        self.assertIn("toggleParameterGroup", html)
        self.assertIn("全局 #${number(row.global_rank, 0)}", html)
        self.assertIn("组内 #${number(row.group_rank, 0)}", html)
        self.assertIn("<span>全局排名</span>", html)
        self.assertIn("<span>组内排名</span>", html)
        self.assertIn("parameters.group_key = row.group_key", html)
        self.assertIn("parameters.group_label = row.group_label", html)

    def test_parameter_lab_page_exposes_leaps_signal_layer_without_option_scan(self):
        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")

        self.assertIn("LEAPS 信号", html)
        self.assertIn("leaps_low_cash_threshold_pct", html)
        self.assertIn("leaps_min_drawdown_pct", html)
        self.assertIn("leaps_premium_budget_cap", html)
        self.assertIn("row.leaps_signal = aggregateLeapsSignals(row.cells)", html)
        self.assertIn("include_leaps_signal_details", html)
        self.assertIn("groupLeapsSignalsByDateSymbol", html)
        self.assertIn("共 ${number(summary.trigger_count, 0)} 次，按日期+标的聚合为", html)
        self.assertIn("LEAPS_DETAIL_PAGE_SIZE = 25", html)
        self.assertIn("renderLeapsBadge(group.best_leaps_signal || {}, false)", html)
        self.assertIn("renderDetailLeaps(row)", html)
        self.assertIn("正股卖出", html)
        self.assertIn("formatStockSell(signal)", html)
        self.assertIn("leaps-reason-chip", html)
        self.assertIn("/api/strategy-lab/parameter-lab/leaps-option-outcomes", html)
        self.assertIn("calculateLeapsOptionOutcomesForActiveRow", html)
        self.assertIn("calculateLeapsOptionOutcomesForActiveGroup", html)
        self.assertIn("calculateLeapsOptionOutcomeForActiveSignal", html)
        self.assertIn("function buildLeapsOptionQueue(signals)", html)
        self.assertIn("return normalizeLeapsOptionSignals(signals).map", html)
        self.assertIn("signals: [queueItem.signal]", html)
        self.assertIn("LEAPS_OPTION_QUEUE_CONCURRENCY = 2", html)
        self.assertIn("Promise.all(Array.from({ length: outcomeEntry.concurrency }, () => runQueueWorker()))", html)
        self.assertIn("stopLeapsOptionOutcomesForActiveRow", html)
        self.assertIn("function leapsOptionVisibleOutcomes(entry)", html)
        self.assertIn("'partial_done'", html)
        self.assertIn("leapsOptionVisibleOutcomes(outcomeEntry)", html)
        self.assertIn("renderLeapsOptionProgress", html)
        self.assertIn("浏览器逐条发起请求；Polygon 定价在服务端完成", html)
        self.assertIn("计算本行", html)
        self.assertIn("计算该信号", html)
        self.assertIn("row.leaps_option_summary = outcomeEntry.summary", html)
        self.assertIn("renderLeapsOptionSummary", html)

    def test_partial_done_leaps_option_outcome_is_rendered_for_group_rows(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript LEAPS render check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        visible_helper = html[
            html.index("        function leapsOptionVisibleOutcomes") : html.index("        function replaceLeapsOutcomesForSignals")
        ]
        match_helper = html[
            html.index("        function optionOutcomeMatchesSignal") : html.index("        function uniqueValues")
        ]
        render_helpers = html[
            html.index("        function renderLeapsGroupDetails") : html.index("        function renderDetailLeaps")
        ]
        helpers = "\n".join([visible_helper, match_helper, render_helpers])
        script = """
const vm = require('vm');
const helpers = process.argv[1];
const context = {
  Array,
  Map,
  Math,
  Number,
  String,
  LEAPS_DETAIL_PAGE_SIZE: 25,
  leapsOptionOutcomeCache: new Map(),
  leapsDetailCacheKey: (row) => row.key,
  escapeHtml: (value) => String(value ?? ''),
  renderLeapsBadge: () => '<span class="leaps-badge">LEAPS</span>',
  number: (value) => String(value ?? '--'),
  pct: (value) => `${value}%`,
  money: (value) => `$${value ?? 0}`,
  renderLeapsReasons: () => '',
  signalSourceLabel: () => 'source',
  formatStockSell: (signal) => signal.next_stock_sell_date || '未卖出',
  aggregateLeapsOptionOutcomes: (outcomes) => ({ total: outcomes.length, success_count: outcomes.filter((item) => item.status === 'success').length }),
  renderLeapsOptionSummary: (summary) => `<span class="leaps-option-summary">ROI summary ${summary.total}</span>`
};
vm.createContext(context);
vm.runInContext(helpers, context);
const signal = {
  signal_key: 'sig-1',
  date: '2024-01-10',
  symbol: 'TSLA.US',
  grade: '高',
  score: 90,
  drawdown_pct: 12,
  cash_pct_after: 4,
  buy_amount: 100,
  premium_budget_cap: 35,
  next_stock_sell_date: '2024-01-20',
  reasons: []
};
const row = {
  key: 'row-1',
  leaps_signal: { best_date: '2024-01-10', trigger_count: 1, score: 90, target_dte_label: '18-24M' }
};
const entry = {
  status: 'done',
  page: 1,
  all_signals: [signal],
  groups: [{
    key: '2024-01-10__TSLA.US',
    date: '2024-01-10',
    symbol: 'TSLA.US',
    signals: [signal],
    best_signal: signal,
    trigger_count: 1,
    source_topic_count: 1,
    drawdown_range: '12%',
    cash_range: '4%',
    buy_amount_total: 100,
    premium_budget_cap_total: 35,
    stock_sell_label: '2024-01-20 / 10天',
    reasons: []
  }]
};
context.leapsOptionOutcomeCache.set('row-1', {
  status: 'partial_done',
  outcomes: [{
    status: 'success',
    signal_key: 'sig-1',
    date: '2024-01-10',
    symbol: 'TSLA.US',
    stock_sell_date: '2024-01-20',
    roi_pct: 12.34,
    contract: 'TSLA250117C00100000'
  }],
  summary: { total: 1, success_count: 1 }
});
const rendered = context.renderFullLeapsGroups(row, entry);
if (rendered.includes('未计算')) throw new Error(rendered);
if (!rendered.includes('ROI summary 1')) throw new Error(rendered);
if (!rendered.includes('TSLA250117C00100000')) throw new Error(rendered);
"""
        subprocess.run(["node", "-e", script, helpers], check=True, capture_output=True, text=True)

    def test_page_groups_leaps_signals_by_date_symbol(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript frontend helper check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        match = re.search(
            r"function betterLeapsSignal\(a, b\) \{.*?\n        function readNumber",
            html,
            re.S,
        )
        self.assertIsNotNone(match)
        helpers = match.group(0).rsplit("\n        function readNumber", 1)[0]
        script = """
const vm = require('vm');
const helpers = process.argv[1];
const signals = JSON.parse(process.argv[2]);
const context = {
  leapsGradeRank: { '高': 3, '中': 2, '低': 1, '无': 0 },
  Number,
  String,
  Array,
  Set,
  Map,
  Math
};
context.number = (value, digits = 2) => Number(value).toFixed(digits).replace(/\\.00$/, '');
context.pct = (value) => `${context.number(value)}%`;
vm.createContext(context);
vm.runInContext(helpers, context);
process.stdout.write(JSON.stringify(context.groupLeapsSignalsByDateSymbol(signals)));
"""
        signals = [
            {
                "date": "2024-01-10",
                "symbol": "TSLA.US",
                "grade": "中",
                "score": 60,
                "topic_key": "topic_a",
                "portfolio_label": "组合A",
                "period_label": "2024",
                "drawdown_pct": 12,
                "cash_pct_after": 8,
                "buy_amount": 100,
                "premium_budget_cap": 35,
                "next_stock_sell_date": "2024-01-20",
                "stock_holding_days": 10,
                "reasons": ["低现金", "回撤达标"],
            },
            {
                "date": "2024-01-10",
                "symbol": "TSLA.US",
                "grade": "高",
                "score": 90,
                "topic_key": "topic_b",
                "portfolio_label": "组合B",
                "period_label": "2025",
                "drawdown_pct": 18,
                "cash_pct_after": 4,
                "buy_amount": 200,
                "premium_budget_cap": 70,
                "next_stock_sell_date": "2024-01-22",
                "stock_holding_days": 12,
                "reasons": ["低现金", "股票策略买入"],
            },
            {
                "date": "2024-01-11",
                "symbol": "MSFT.US",
                "grade": "低",
                "score": 30,
                "topic_key": "topic_a",
                "drawdown_pct": 9,
                "cash_pct_after": 11,
                "buy_amount": 50,
                "premium_budget_cap": 17.5,
                "next_stock_sell_date": "",
                "stock_holding_days": None,
                "reasons": ["股票策略买入"],
            },
            {
                "date": "2024-01-09",
                "symbol": "AAPL.US",
                "grade": "中",
                "score": 70,
                "topic_key": "topic_c",
                "drawdown_pct": 10,
                "cash_pct_after": 9,
                "buy_amount": 80,
                "premium_budget_cap": 28,
                "next_stock_sell_date": "",
                "stock_holding_days": None,
                "reasons": ["回撤达标"],
            },
            {
                "date": "2024-01-09",
                "symbol": "AMZN.US",
                "grade": "中",
                "score": 70,
                "topic_key": "topic_d",
                "drawdown_pct": 10,
                "cash_pct_after": 9,
                "buy_amount": 80,
                "premium_budget_cap": 28,
                "next_stock_sell_date": "",
                "stock_holding_days": None,
                "reasons": ["回撤达标"],
            },
            {
                "date": "2024-01-08",
                "symbol": "NVDA.US",
                "grade": "中",
                "score": 65,
                "topic_key": "topic_e",
                "drawdown_pct": 10,
                "cash_pct_after": 9,
                "buy_amount": 80,
                "premium_budget_cap": 28,
                "next_stock_sell_date": "",
                "stock_holding_days": None,
                "reasons": ["回撤达标"],
            },
        ]
        completed = subprocess.run(
            ["node", "-e", script, helpers, json.dumps(signals)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(len(result), 5)
        self.assertEqual(
            [(group["date"], group["symbol"], group["best_signal"]["grade"], group["best_signal"]["score"]) for group in result],
            [
                ("2024-01-10", "TSLA.US", "高", 90),
                ("2024-01-09", "AAPL.US", "中", 70),
                ("2024-01-09", "AMZN.US", "中", 70),
                ("2024-01-08", "NVDA.US", "中", 65),
                ("2024-01-11", "MSFT.US", "低", 30),
            ],
        )
        self.assertEqual(result[0]["date"], "2024-01-10")
        self.assertEqual(result[0]["symbol"], "TSLA.US")
        self.assertEqual(result[0]["trigger_count"], 2)
        self.assertEqual(result[0]["source_topic_count"], 2)
        self.assertEqual(result[0]["best_signal"]["grade"], "高")
        self.assertEqual(result[0]["buy_amount_total"], 300)
        self.assertEqual(result[0]["premium_budget_cap_total"], 105)
        self.assertCountEqual(result[0]["reasons"], ["低现金", "回撤达标", "股票策略买入"])
        self.assertEqual(result[0]["stock_sell_label"], "2024-01-22 / 10-12天")

    def test_parameter_lab_rank_helper_recomputes_global_and_group_ranks(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript rank helper check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        match = re.search(
            r"function strategyGroupKey\(candidate\) \{.*?\n        function scoreParameterResults",
            html,
            re.S,
        )
        self.assertIsNotNone(match)
        helpers = match.group(0).rsplit("\n        function scoreParameterResults", 1)[0]
        script = """
const vm = require('vm');
const helpers = process.argv[1];
const rows = JSON.parse(process.argv[2]);
const context = {
  buyStrategyLabels: { buy_a: '买A', buy_b: '买B' },
  sellStrategyLabels: { sell_x: '卖X', sell_y: '卖Y' }
};
vm.createContext(context);
vm.runInContext(helpers, context);
const normalized = context.rankParameterRows(rows, 'normalized').map((row) => ({
  key: row.key,
  global_rank: row.global_rank,
  group_rank: row.group_rank,
  group_key: row.group_key,
  group_label: row.group_label
}));
const raw = context.rankParameterRows(rows, 'raw').map((row) => ({
  key: row.key,
  global_rank: row.global_rank,
  group_rank: row.group_rank,
  group_key: row.group_key,
  group_label: row.group_label
}));
process.stdout.write(JSON.stringify({ normalized, raw }));
"""
        rows = [
            {"key": "a1", "final_score": 80, "raw_score": 1, "candidate": {"buy_strategy": "buy_a", "sell_strategy": "sell_x"}},
            {"key": "a2", "final_score": 70, "raw_score": 4, "candidate": {"buy_strategy": "buy_a", "sell_strategy": "sell_x"}},
            {"key": "b1", "final_score": 90, "raw_score": 2, "candidate": {"buy_strategy": "buy_b", "sell_strategy": "sell_y"}},
            {"key": "c1", "final_score": 60, "raw_score": 3, "candidate": {"buy_strategy": "buy_a", "sell_strategy": "sell_y"}},
        ]
        completed = subprocess.run(
            ["node", "-e", script, helpers, json.dumps(rows)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual([row["key"] for row in result["normalized"]], ["b1", "a1", "a2", "c1"])
        self.assertEqual([row["global_rank"] for row in result["normalized"]], [1, 2, 3, 4])
        self.assertEqual(
            [(row["key"], row["group_key"], row["group_rank"]) for row in result["normalized"]],
            [("b1", "buy_b__sell_y", 1), ("a1", "buy_a__sell_x", 1), ("a2", "buy_a__sell_x", 2), ("c1", "buy_a__sell_y", 1)],
        )
        self.assertEqual([row["key"] for row in result["raw"]], ["a2", "c1", "b1", "a1"])
        self.assertEqual([row["global_rank"] for row in result["raw"]], [1, 2, 3, 4])
        self.assertEqual(
            [(row["key"], row["group_key"], row["group_rank"]) for row in result["raw"]],
            [("a2", "buy_a__sell_x", 1), ("c1", "buy_a__sell_y", 1), ("b1", "buy_b__sell_y", 1), ("a1", "buy_a__sell_x", 2)],
        )
        self.assertEqual(result["normalized"][1]["group_label"], "买A / 卖X")

    def test_parameter_lab_page_no_longer_exposes_option_scan(self):
        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")

        self.assertNotIn("option" + "-packet", html)
        self.assertNotIn("option" + "_scan_worker", html)
        self.assertNotIn("option" + "_debug", html)
        self.assertIn("200-300D 月期" + "权收益", html)


if __name__ == "__main__":
    unittest.main()
