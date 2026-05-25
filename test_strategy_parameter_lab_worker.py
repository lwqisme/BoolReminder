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
        self.assertIn("simulate_elapsed_ms_sum: simulateElapsedMsSum", source)
        self.assertIn("simulate_elapsed_ms_max: simulateElapsedMsMax", source)
        self.assertIn("slow_simulation_count: slowSimulationCount", source)
        self.assertIn("batch_total_simulations: batchTotal", source)
        self.assertIn("leaps_signal: summarizeLeapsSignals(tradeLog, inputs, Boolean(workerState?.include_leaps_signal_details), task)", source)
        self.assertIn("cash_after: state.cash", source)
        self.assertIn("cash_pct_after: pct(safeRatio(state.cash, state.cash + state.last_value))", source)
        self.assertIn("grid_rebound_cycle_anchor_drawdown_pct: null", source)
        self.assertIn("cost_deleverage_cycle_anchor_price: null", source)

    def test_page_short_circuits_zero_candidate_packets(self):
        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")

        self.assertIn("0 候选：当前策略和参数筛选没有可运行组合", html)
        self.assertIn("当前策略和参数筛选没有可运行组合。请调整策略组合或参数筛选后重试。", html)
        run_function = re.search(
            r"async function runParameterLab\(\) \{(?P<body>.*?)\n        function runWorkerPool",
            html,
            re.S,
        )
        self.assertIsNotNone(run_function)
        body = run_function.group("body")
        zero_guard = body.find("if (candidateCount === 0)")
        worker_call = body.find("const data = await runWorkerPool")
        self.assertGreaterEqual(zero_guard, 0)
        self.assertGreater(worker_call, zero_guard)

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
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
  'sell_stage_rearm_drawdown_pct'
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
      dca_rearm_drawdown_pct: 0,
      sell_stage_rearm_drawdown_pct: null
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
    sell_variants: [[0, 'sell:cost', 'cost_deleverage', 0, null, null, null, null, null, null, 15, 25, 35, 25, 25, 25, 2, false, 0, 0, 15]],
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

    def test_worker_grid_rebound_cycles_until_ath_grid_two(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript worker grid rebound check")

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
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
  'sell_stage_rearm_drawdown_pct'
];
const packet = {
  run_id: 'grid-cycle',
  inputs: {
    initial_cash: 10000,
    monthly_contribution: 0,
    max_drawdown_pct: 60,
    drawdown_basis: 'ath',
    trade_fee: 0,
    hkd_to_usd: 0.128,
    reserve_position_pct: 0,
    sell_min_profit_pct: 0,
    sell_allow_same_day_sell: false,
    dca_rearm_drawdown_pct: 0,
    sell_stage_rearm_drawdown_pct: null
  },
  tasks: [{
    key: 'grid_cycle',
    portfolio_key: 'single',
    portfolio_label: 'Single',
    period_key: 'cycle',
    period_label: 'Cycle',
    start: '2025-01-01',
    end: '2025-01-07',
    symbols: ['TSLA.US'],
    targets: [{ symbol: 'TSLA.US', weight: 100, name: 'TSLA', max_drawdown_pct: 60 }]
  }],
  market_data: {
    symbols: {
      'TSLA.US': {
        dates: ['2025-01-01', '2025-01-02', '2025-01-03', '2025-01-04', '2025-01-05', '2025-01-06', '2025-01-07'],
        closes: [200, 100, 110, 120, 200, 210, 220]
      }
    }
  },
  buy_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...buyFields],
  sell_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...sellFields],
  candidate_schema: ['candidate_id', 'buy_variant_id', 'sell_variant_id'],
  buy_variants: [[0, 'buy:equal', 'equal_slice', 10, 100, null, null, null, null, null, null, null, null, null]],
  sell_variants: [[0, 'sell:grid', 'grid_rebound', 0, null, null, 5, 10, 10, 0, null, null, null, null, null, null, null, false, 0, 0, null]],
  candidate_rows: [[0, 0, 0]],
  include_trades: true
};
(async () => {
  await context.initRun(packet, 0, packet.run_id, 1);
  await context.processBatch({ run_id: packet.run_id, worker_index: 0, batch_id: 'b1', candidate_rows: packet.candidate_rows }, 0, packet.run_id);
  const done = messages.find((message) => message.type === 'batch_done');
  const trades = done.rows[0].observations[0].trade_log;
  const sells = trades.filter((trade) => trade.action === 'sell').map((trade) => [trade.date, trade.trigger_value]);
  process.stdout.write(JSON.stringify(sells));
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

        self.assertEqual(
            json.loads(completed.stdout),
            [["2025-01-03", 45], ["2025-01-04", 40], ["2025-01-05", 35], ["2025-01-06", 30]],
        )

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
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
  'sell_stage_rearm_drawdown_pct'
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
    dca_rearm_drawdown_pct: 0,
    sell_stage_rearm_drawdown_pct: null
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
  sell_variants: [[0, 'sell:cost', 'cost_deleverage', 0, null, null, null, null, null, null, 15, 25, 35, 25, 25, 25, 2, false, 0, 0, 15]],
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

    def test_cost_deleverage_restarts_three_stage_cycle_from_third_sell_price(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript cost deleverage cycle check")

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
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
  'sell_stage_rearm_drawdown_pct'
];
const packet = {
  run_id: 'cost-cycle',
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
    dca_rearm_drawdown_pct: 0,
    sell_stage_rearm_drawdown_pct: null
  },
  tasks: [{
    key: 'cost_cycle',
    portfolio_key: 'single',
    portfolio_label: 'Single',
    period_key: 'cycle',
    period_label: 'Cycle',
    start: '2025-01-01',
    end: '2025-01-10',
    symbols: ['GOOG.US'],
    targets: [{ symbol: 'GOOG.US', weight: 100, name: 'GOOG', max_drawdown_pct: 50 }]
  }],
  market_data: {
    symbols: {
      'GOOG.US': {
        dates: ['2025-01-01', '2025-01-02', '2025-01-03', '2025-01-04', '2025-01-05', '2025-01-06', '2025-01-07', '2025-01-08', '2025-01-09', '2025-01-10'],
        closes: [200, 100, 110, 120, 130, 140, 150, 156, 165, 170]
      }
    }
  },
  buy_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...buyFields],
  sell_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...sellFields],
  candidate_schema: ['candidate_id', 'buy_variant_id', 'sell_variant_id'],
  buy_variants: [[0, 'buy:pyramid_3', 'pyramid_3', null, null, null, null, null, null, null, null, null, null, null]],
  sell_variants: [[0, 'sell:cost', 'cost_deleverage', 0, null, null, null, null, null, null, 10, 20, 30, 10, 10, 10, 0, false, 0, 0, 15]],
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
        sells = [(trade["date"], trade.get("trigger_value")) for trade in trades if trade["action"] == "sell"]

        self.assertEqual(
            sells,
            [
                ("2025-01-03", 10),
                ("2025-01-04", 20),
                ("2025-01-05", 30),
                ("2025-01-07", 10),
                ("2025-01-08", 20),
                ("2025-01-10", 30),
            ],
        )
        self.assertNotIn(("2025-01-06", 10), sells)
        self.assertNotIn(("2025-01-09", 30), sells)

    def test_cost_deleverage_rearm_buy_resets_cycle_anchor_to_new_average_cost(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript cost deleverage rearm check")

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
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
  'sell_stage_rearm_drawdown_pct'
];
const packet = {
  run_id: 'cost-rearm',
  inputs: {
    initial_cash: 1000,
    monthly_contribution: 0,
    max_drawdown_pct: 70,
    drawdown_basis: 'ath',
    trade_fee: 0,
    hkd_to_usd: 0.128,
    reserve_position_pct: 0,
    sell_min_profit_pct: 0,
    sell_allow_same_day_sell: false,
    dca_rearm_drawdown_pct: 0,
    sell_stage_rearm_drawdown_pct: 50
  },
  tasks: [{
    key: 'cost_rearm',
    portfolio_key: 'single',
    portfolio_label: 'Single',
    period_key: 'cycle',
    period_label: 'Cycle',
    start: '2025-01-01',
    end: '2025-01-05',
    symbols: ['GOOG.US'],
    targets: [{ symbol: 'GOOG.US', weight: 100, name: 'GOOG', max_drawdown_pct: 70 }]
  }],
  market_data: {
    symbols: {
      'GOOG.US': {
        dates: ['2025-01-01', '2025-01-02', '2025-01-03', '2025-01-04', '2025-01-05'],
        closes: [200, 100, 110, 80, 106]
      }
    }
  },
  buy_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...buyFields],
  sell_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...sellFields],
  candidate_schema: ['candidate_id', 'buy_variant_id', 'sell_variant_id'],
  buy_variants: [[0, 'buy:pyramid_3', 'pyramid_3', null, null, null, null, null, null, null, null, null, null, null]],
  sell_variants: [[0, 'sell:cost', 'cost_deleverage', 0, null, null, null, null, null, null, 10, 20, 30, 10, 10, 10, 0, false, 0, 0, 50]],
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
        sells = [(trade["date"], trade.get("trigger_value")) for trade in trades if trade["action"] == "sell"]
        rearm_buys = [trade for trade in trades if trade["action"] == "buy" and trade.get("sell_cycle_rearmed")]

        self.assertIn(("2025-01-03", 10), sells)
        self.assertIn(("2025-01-05", 10), sells)
        self.assertTrue(rearm_buys)

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
        self.assertEqual(signals["TSLA.US"]["stock_sell_risk"], "")
        self.assertEqual(signals["GOOG.US"]["next_stock_sell_date"], "")
        self.assertIsNone(signals["GOOG.US"]["stock_holding_days"])
        self.assertEqual(signals["GOOG.US"]["stock_sell_risk"], "no_stock_sell")
        self.assertEqual(signals["GOOG.US"]["stock_sell_risk_label"], "无正股卖点")

    def test_leaps_signal_stock_mark_return_and_volatility_fields(self):
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
const mkDate = (index) => {
  const d = new Date(Date.UTC(2024, 0, 1 + index));
  return d.toISOString().slice(0, 10);
};
const tslaPoints = Array.from({ length: 75 }, (_, index) => ({
  date: mkDate(index),
  close: 80 + index
}));
const task = {
  price_points: {
    'TSLA.US': tslaPoints,
    'GOOG.US': [
      { date: '2024-02-01', close: 100 },
      { date: '2024-02-02', close: 112 },
      { date: '2024-02-03', close: 118 }
    ]
  }
};
const baseBuy = {
  action: 'buy',
  drawdown_pct: 24,
  cash_pct_after: 3,
  gross_amount: 900,
  day_change_pct: -1.2
};
const tradeLog = [
  { ...baseBuy, date: '2024-03-05', symbol: 'TSLA.US', price: 120 },
  { action: 'sell', date: '2024-03-10', symbol: 'TSLA.US', price: 150 },
  { ...baseBuy, date: '2024-02-02', symbol: 'GOOG.US', price: 112 }
];
process.stdout.write(JSON.stringify(context.summarizeLeapsSignals(tradeLog, inputs, true, task)));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(WORKER_JS)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        signals = {item["symbol"]: item for item in result["all_signals"]}

        self.assertEqual(signals["TSLA.US"]["stock_mark_date"], "2024-03-10")
        self.assertEqual(signals["TSLA.US"]["stock_mark_price"], 150)
        self.assertEqual(signals["TSLA.US"]["stock_sell_date"], "2024-03-10")
        self.assertAlmostEqual(signals["TSLA.US"]["stock_return_pct"], 25)
        self.assertGreaterEqual(signals["TSLA.US"]["realized_volatility_pct"], 15)
        self.assertLessEqual(signals["TSLA.US"]["realized_volatility_pct"], 120)
        self.assertEqual(signals["GOOG.US"]["stock_mark_date"], "2024-02-03")
        self.assertEqual(signals["GOOG.US"]["stock_mark_price"], 118)
        self.assertAlmostEqual(signals["GOOG.US"]["stock_return_pct"], (118 / 112 - 1) * 100)
        self.assertEqual(signals["GOOG.US"]["realized_volatility_pct"], 60)

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
        self.assertIn("function findCandidatePacketRow(packet, row)", html)
        self.assertIn("function parameterRowCandidate(row, packet = null)", html)
        self.assertIn("Object.defineProperty(row, '_candidate_cache'", html)
        self.assertIn("findCandidateRowForDetail(row)", html)
        self.assertIn("return findCandidatePacketRow(lastParameterPacket, row);", html)
        self.assertIn("type: 'pause', run_id: currentRun.run_id", html)
        self.assertIn("type: 'cancel', run_id: runId", html)
        self.assertIn("sell_stage_rearm_drawdown_pct", html)
        self.assertIn("卖档重启", html)
        self.assertIn("/api/strategy-lab/parameter-lab/estimate", html)
        self.assertIn("confirmLargeRunIfNeeded", html)

    def test_page_groups_parameter_rows_and_shows_dual_ranks(self):
        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")

        self.assertIn("function strategyGroupKey(candidate)", html)
        self.assertIn("const buyKey = candidate?.buy_strategy || candidate?.candidate?.buy_strategy || ''", html)
        self.assertIn("function strategyGroupLabel(candidate)", html)
        self.assertIn('<option value="option_estimate_roi_mean">预估期权ROI</option>', html)
        self.assertIn("function rankParameterRows(rows, rankMethod = 'normalized')", html)
        self.assertIn("function rankedRowsFor(data, rankMethod = 'normalized')", html)
        self.assertIn("function parameterRankCacheKey(rankMethod, scope = null)", html)
        self.assertIn("${method}:${parameterRankScope(scope)}", html)
        self.assertIn("function optionEstimateRoiMeanForRank(row, scope = null)", html)
        self.assertIn("Object.defineProperty(data, '_ranked_rows_cache'", html)
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

    def test_score_parameter_results_keeps_rows_light_and_compacts_observations(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript score helper check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        setup_match = re.search(r"const parameterFields = \[.*?\n        async function runParameterLab", html, re.S)
        score_match = re.search(r"function normalize\(value, values\) \{.*?\n        function heatStyle", html, re.S)
        self.assertIsNotNone(setup_match)
        self.assertIsNotNone(score_match)
        helpers = (
            setup_match.group(0).rsplit("\n        async function runParameterLab", 1)[0]
            + "\n"
            + score_match.group(0).rsplit("\n        function heatStyle", 1)[0]
        )
        script = """
const vm = require('vm');
const helpers = process.argv[1];
const packet = JSON.parse(process.argv[2]);
const partialRows = JSON.parse(process.argv[3]);
const context = {
  performance: { now: () => 10 },
  navigator: {},
  buyStrategyLabels: { buy_a: '买A', buy_b: '买B' },
  sellStrategyLabels: { sell_x: '卖X' },
  strategyRegistry: { version: 'test', definitions: {} },
  console
};
vm.createContext(context);
vm.runInContext(helpers, context);
const result = context.scoreParameterResults(packet, partialRows, [{
  worker_index: 0,
  chunk_size: 2,
  batch_count: 1,
  completed_simulations: 4,
  elapsed_ms: 8,
  simulate_elapsed_ms_sum: 4,
  simulate_elapsed_ms_max: 2,
  slow_simulation_count: 0,
  batch_total_simulations: 4
}], 9);
process.stdout.write(JSON.stringify({
  row_count: result.rows.length,
  observation_count: result.observation_count,
  compacted: result.observations_compacted,
  observations_length: result.observations.length,
  diagnostics_observation_count: result.diagnostics.scale.observation_count,
  row_keys: result.rows.map((row) => row.key),
  first_row_keys: Object.keys(result.rows[0]),
  first_cell_count: result.rows[0].cells.length,
  first_group_key: result.rows[0].group_key,
  first_global_rank: result.rows[0].global_rank,
  topic_ranks: result.rows[0].cells.map((cell) => cell.topic_rank)
}));
"""
        packet = {
            "candidate_schema": ["candidate_id", "buy_variant_id", "sell_variant_id"],
            "buy_variant_schema": ["variant_id", "variant_key", "strategy", "step_pct", "equal_slice_allocation_pct"],
            "sell_variant_schema": ["variant_id", "variant_key", "strategy", "sell_min_profit_pct"],
            "buy_variants": [
                [0, "buy_a_1", "buy_a", 1, 10],
                [1, "buy_b_1", "buy_b", 2, 20],
            ],
            "sell_variants": [[0, "sell_x_1", "sell_x", 5]],
            "candidate_rows": [[101, 0, 0], [102, 1, 0]],
            "tasks": [{"key": "topic_a"}, {"key": "topic_b"}],
            "market_data": {"symbols": {}},
        }
        partial_rows = [
            {
                "candidate_id": 101,
                "candidate_key": "buy_a__step1__alloc10__sell_x",
                "observations": [
                    {"topic_key": "topic_a", "return_pct": 10, "max_drawdown_pct": -5},
                    {"topic_key": "topic_b", "return_pct": 20, "max_drawdown_pct": -8},
                ],
            },
            {
                "candidate_id": 102,
                "candidate_key": "buy_b__step2__alloc20__sell_x",
                "observations": [
                    {"topic_key": "topic_a", "return_pct": 5, "max_drawdown_pct": -10},
                    {"topic_key": "topic_b", "return_pct": 12, "max_drawdown_pct": -12},
                ],
            },
        ]
        completed = subprocess.run(
            ["node", "-e", script, helpers, json.dumps(packet), json.dumps(partial_rows)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["observation_count"], 4)
        self.assertEqual(result["diagnostics_observation_count"], 4)
        self.assertTrue(result["compacted"])
        self.assertEqual(result["observations_length"], 0)
        self.assertNotIn("candidate", result["first_row_keys"])
        self.assertIn("candidate_id", result["first_row_keys"])
        self.assertEqual(result["first_cell_count"], 2)
        self.assertEqual(result["first_group_key"], "buy_a__sell_x")
        self.assertGreaterEqual(result["first_global_rank"], 1)
        self.assertEqual(sorted(result["topic_ranks"]), [1, 1])

    def test_parameter_lab_page_exposes_leaps_signal_layer_without_option_scan(self):
        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")

        self.assertIn("LEAPS 信号", html)
        self.assertIn("leaps_low_cash_threshold_pct", html)
        self.assertIn("leaps_min_drawdown_pct", html)
        self.assertIn("leaps_premium_budget_cap", html)
        self.assertIn("row.leaps_signal = aggregateLeapsSignals(row.cells)", html)
        self.assertIn("include_leaps_signal_details", html)
        self.assertIn('id="leapsEstimateScope"', html)
        self.assertIn('<option value="high" selected>仅高等级</option>', html)
        self.assertIn('<option value="option_estimate_roi_mean">预估期权ROI</option>', html)
        self.assertIn("parameterRankLabel(rankMethod)", html)
        self.assertIn("formatParameterRankScore(scoreForRankMethod(row, rankMethod, rankScope), rankMethod)", html)
        self.assertIn("groupLeapsSignalsByDateSymbol", html)
        self.assertIn("共 ${number(summary.trigger_count, 0)} 次，按日期+标的聚合为", html)
        self.assertIn("LEAPS_DETAIL_PAGE_SIZE = 25", html)
        self.assertIn("function renderAggregateLeapsBadge(summary)", html)
        self.assertIn("renderAggregateLeapsBadge(group.leaps_signal || {})", html)
        self.assertIn("renderLeapsOptionOrEstimate({ estimate_signals: group.leaps_estimate_signals || [] }, group.leaps_option_summary)", html)
        self.assertIn("function estimatedLeapsOptionSummary(summary, scope = currentLeapsEstimateScope())", html)
        self.assertIn("signal?.grade === '高'", html)
        self.assertIn("renderEstimatedLeapsSummary(cell.leaps_signal || {})", html)
        self.assertIn("renderAggregateLeapsBadge(row.leaps_signal || {})", html)
        self.assertIn("renderLeapsOptionOrEstimate(row.leaps_signal || {}, row.leaps_option_summary)", html)
        self.assertIn("estimate_signals: sortedLeapsSignals(estimateSignals)", html)
        self.assertIn("grade_counts: gradeCounts", WORKER_JS.read_text(encoding="utf-8"))
        self.assertIn("renderDetailLeaps(row)", html)
        self.assertIn("正股卖出", html)
        self.assertIn("formatStockSell(signal)", html)
        self.assertIn("function renderLeapsStockSell(signal)", html)
        self.assertIn("function renderLeapsGroupedStockSell(group)", html)
        self.assertIn("stock_sell_risk: !group.signals.some", html)
        self.assertIn(".leaps-stock-sell-risk", html)
        self.assertIn("! 无正股卖点", html)
        self.assertIn("期权到期风险", html)
        self.assertIn("leaps-reason-chip", html)
        self.assertIn("/api/strategy-lab/parameter-lab/leaps-option-outcomes", html)
        self.assertIn("calculateLeapsOptionOutcomesForActiveRow", html)
        self.assertIn("calculateLeapsOptionOutcomesForActiveGroup", html)
        self.assertIn("calculateLeapsOptionOutcomeForActiveSignal", html)
        self.assertIn("calculateLeapsOptionOutcomesForActiveHighGradeSignals", html)
        self.assertIn("function highGradeLeapsOptionSignals(signals)", html)
        self.assertIn("只计算高等级信号", html)
        self.assertIn("signal.grade === '高'", html)
        self.assertIn("计算 Top3 参数行高等级期权收益", html)
        self.assertIn("计算这些行里的全部高等级 LEAPS 信号", html)
        self.assertIn("后端每批 ${number(LEAPS_OPTION_BATCH_SIZE, 0)} 个", html)
        self.assertIn("TOP_GROUP_LEAPS_OPTION_ROW_LIMIT = 3", html)
        self.assertIn("TOP_GROUP_LEAPS_OPTION_SIGNAL_BUDGET = 300", html)
        self.assertIn("function topGroupLeapsOptionRows(data, rankMethod = currentParameterRankMethod())", html)
        self.assertIn("function buildTopGroupLeapsOptionQueue(rowSignalPairs)", html)
        self.assertIn("function splitTopGroupLeapsOptionQueueByBudget", html)
        self.assertIn("await ensureFullLeapsDetailsForRow(row)", html)
        self.assertIn("runLeapsDetailWorker(row)", html)
        self.assertIn("function leapsDetailTasksForActiveScope(sourcePacket)", html)
        self.assertIn("return tasks.filter((task) => String(task.key) === String(activeDetailTopicKey));", html)
        self.assertIn("topic_key: activeDetailTopicKey || '__row__'", html)
        self.assertNotIn("const tasks = sourcePacket.tasks || [];\n            const packet = {\n                ...sourcePacket,\n                run_id: runId,", html)
        self.assertIn("当前题目 LEAPS 明细：", html)
        self.assertIn("参数行全部题目 LEAPS 明细", html)
        self.assertIn("期权卖出日", html)
        self.assertIn("highGradeLeapsOptionSignals(detailEntry?.all_signals || [])", html)
        self.assertIn("row.leaps_option_summary = entry.summary", html)
        self.assertIn("/api/strategy-lab/parameter-lab/leaps-option-outcomes/batch", html)
        self.assertIn("function buildLeapsOptionQueue(signals)", html)
        self.assertIn("const queueByKey = new Map();", html)
        self.assertIn("signals: batch.map((queueItem) => queueItem.signal)", html)
        self.assertIn("LEAPS_OPTION_BATCH_SIZE = 5", html)
        self.assertIn("LEAPS_OPTION_OUTCOME_RETRY_DELAYS_MS = [5000, 15000]", html)
        self.assertIn("function requestLeapsOptionOutcomeBatch(payload, queueItems, options = {})", html)
        self.assertNotIn("LEAPS_OPTION_QUEUE_CONCURRENCY = 2", html)
        self.assertNotIn("Promise.all(Array.from({ length: outcomeEntry.concurrency }, () => runQueueWorker()))", html)
        self.assertIn("stopLeapsOptionOutcomesForActiveRow", html)
        self.assertIn("function leapsOptionVisibleOutcomes(entry)", html)
        self.assertIn("'partial_done'", html)
        self.assertIn("leapsOptionVisibleOutcomes(outcomeEntry)", html)
        self.assertIn("renderLeapsOptionProgress", html)
        self.assertIn("浏览器批量发起请求；期权定价在服务端完成", html)
        self.assertIn("计算本行", html)
        self.assertIn("计算该信号", html)
        self.assertIn("row.leaps_option_summary = outcomeEntry.summary", html)
        self.assertIn("renderLeapsOptionSummary", html)
        self.assertIn("function formatLeapsOptionExitStatus(outcome)", html)
        self.assertIn("holding: '持有中'", html)
        self.assertIn("expired_without_stock_sell: '已到期'", html)
        self.assertIn("! 正股替代", html)
        self.assertIn("正股收益", html)
        self.assertIn("期权实际", html)
        self.assertNotIn("option_take_profit", html)
        self.assertNotIn("期权独立止盈", html)
        self.assertNotIn("期权止盈", html)
        self.assertNotIn("option_exit_policy", html)
        self.assertIn("currentRun = null;\n                    renderTopGroupLeapsOptionControls();", html)

    def test_aggregate_leaps_badge_and_estimate_pool_keep_period_coverage(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript LEAPS aggregate helper check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        match = re.search(
            r"function betterLeapsSignal\(a, b\) \{.*?\n        function signalOptionKey",
            html,
            re.S,
        )
        self.assertIsNotNone(match)
        helpers = match.group(0).rsplit("\n        function signalOptionKey", 1)[0]
        script = r"""
const vm = require('vm');
const helpers = process.argv[1];
const context = {
  leapsGradeRank: { '高': 3, '中': 2, '低': 1, '无': 0 },
  leapsBadgeClass: (grade) => grade === '高' ? 'high' : grade === '中' ? 'medium' : grade === '低' ? 'low' : '',
  number: (value) => String(Math.round(Number(value || 0))),
  pct: (value) => `${Number(value || 0).toFixed(1)}%`,
  escapeHtml: (value) => String(value).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch])),
  estimatedLeapsOutcomeFromSignal: (signal) => signal,
  estimatedLeapsOptionRoiPct: (signal) => Number(signal.roi_pct),
  document: { getElementById: () => ({ value: 'high' }) }
};
vm.createContext(context);
vm.runInContext(helpers, context);

const fiveYearSignals = Array.from({ length: 5 }, (_, index) => ({
  grade: '高',
  score: 100,
  date: `2020-01-0${index + 1}`,
  symbol: 'GOOGL.US',
  stock_buy_price: 100,
  stock_sell_price: 140,
  roi_pct: 40
}));
const cells = [
  {
    topic_key: 'googl_1y',
    portfolio_label: 'GOOGL',
    period_label: '1Y',
    leaps_signal: {
      grade: '高',
      score: 92,
      best_date: '2024-01-10',
      trigger_count: 2,
      grade_counts: { '高': 1, '中': 1, '低': 0 },
      top_signals: [
        { grade: '高', score: 92, date: '2024-01-10', symbol: 'GOOGL.US', stock_buy_price: 100, stock_sell_price: 130, roi_pct: 30 },
        { grade: '中', score: 70, date: '2024-02-10', symbol: 'GOOGL.US', stock_buy_price: 100, stock_sell_price: 115, roi_pct: 15 }
      ]
    }
  },
  {
    topic_key: 'googl_3y',
    portfolio_label: 'GOOGL',
    period_label: '3Y',
    leaps_signal: {
      grade: '高',
      score: 91,
      best_date: '2022-01-10',
      trigger_count: 1,
      grade_counts: { '高': 1, '中': 0, '低': 0 },
      top_signals: [
        { grade: '高', score: 91, date: '2022-01-10', symbol: 'GOOGL.US', stock_buy_price: 100, stock_sell_price: 150, roi_pct: 50 }
      ]
    }
  },
  {
    topic_key: 'googl_5y',
    portfolio_label: 'GOOGL',
    period_label: '5Y',
    leaps_signal: {
      grade: '高',
      score: 100,
      best_date: '2020-01-01',
      trigger_count: 5,
      grade_counts: { '高': 5, '中': 0, '低': 0 },
      top_signals: fiveYearSignals
    }
  }
];
const aggregate = context.aggregateLeapsSignals(cells);
const badge = context.renderAggregateLeapsBadge(aggregate);
const highSummary = context.estimatedLeapsOptionSummary(aggregate, 'high');
const allSummary = context.estimatedLeapsOptionSummary(aggregate, 'all');
process.stdout.write(JSON.stringify({
  badge,
  topPeriods: aggregate.top_signals.map((signal) => signal.period_label),
  estimatePeriods: aggregate.estimate_signals.map((signal) => signal.period_label),
  highTotal: highSummary.total,
  allTotal: allSummary.total,
  highSuccess: highSummary.success_count,
  allSuccess: allSummary.success_count
}));
"""
        completed = subprocess.run(
            ["node", "-e", script, helpers],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertIn("LEAPS 高 · 3周期 · 7条", result["badge"])
        self.assertNotIn("2020-01-01", result["badge"])
        self.assertEqual(result["topPeriods"], ["5Y", "5Y", "5Y", "5Y", "5Y"])
        self.assertIn("3Y", result["estimatePeriods"])
        self.assertEqual(result["highTotal"], 7)
        self.assertEqual(result["allTotal"], 8)
        self.assertEqual(result["highSuccess"], 7)
        self.assertEqual(result["allSuccess"], 8)

    def test_page_scopes_leaps_detail_worker_tasks_to_active_cell(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript LEAPS scope check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        helpers = html[
            html.index("        function leapsDetailCacheKey") : html.index("        function renderTopLeapsTable")
        ]
        script = r"""
const vm = require('vm');
const helpers = process.argv[1];
const startedPackets = [];
function Worker() {
  this.terminate = () => {};
  this.postMessage = (message) => {
    if (message.type === 'start') startedPackets.push(message);
  };
}
const sourcePacket = {
  run_id: 'run-1',
  payload_schema: 'schema-1',
  inputs: { leaps_low_cash_threshold_pct: 10 },
  tasks: [
    { key: 'googl_100__1y' },
    { key: 'googl_100__3y' }
  ],
  market_data: {},
  registry: {},
  buy_variant_schema: [],
  sell_variant_schema: [],
  candidate_schema: [],
  buy_variants: [],
  sell_variants: []
};
const context = {
  Worker,
  Promise,
  Date: { now: () => 123456 },
  Math,
  String,
  JSON,
  Array,
  startedPackets,
  parameterLabWorkerUrl: '/worker.js',
  activeDetailTopicKey: 'googl_100__1y',
  lastParameterPacket: sourcePacket,
  lastParameterResult: null,
  findCandidatePacketRow: () => ['candidate-row'],
  createParameterLabRunId: () => 'generated-run',
  scoreParameterResults: () => ({ rows: [] }),
  buildCandidateIndex: () => ({})
};
vm.createContext(context);
vm.runInContext(helpers, context);
const row = { key: 'row-1' };
const cellKey = context.leapsDetailCacheKey(row);
context.runLeapsDetailWorker(row);
const cellTasks = startedPackets.pop().packet.tasks.map((task) => task.key);
if (cellTasks.length !== 1 || cellTasks[0] !== 'googl_100__1y') {
  throw new Error(`cell scope tasks were ${JSON.stringify(cellTasks)}`);
}
context.activeDetailTopicKey = '';
const rowKey = context.leapsDetailCacheKey(row);
context.runLeapsDetailWorker(row);
const rowTasks = startedPackets.pop().packet.tasks.map((task) => task.key);
if (rowTasks.length !== 2 || rowTasks[0] !== 'googl_100__1y' || rowTasks[1] !== 'googl_100__3y') {
  throw new Error(`row scope tasks were ${JSON.stringify(rowTasks)}`);
}
if (!cellKey.includes('"topic_key":"googl_100__1y"')) throw new Error(cellKey);
if (!rowKey.includes('"topic_key":"__row__"')) throw new Error(rowKey);
"""
        subprocess.run(["node", "-e", script, helpers], check=True, capture_output=True, text=True)

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
  activeDetailTopicKey: '',
  leapsOptionOutcomeCache: new Map(),
  leapsDetailCacheKey: (row) => row.key,
  findSourceCell: () => null,
  escapeHtml: (value) => String(value ?? ''),
  renderLeapsBadge: () => '<span class="leaps-badge">LEAPS</span>',
  number: (value) => String(value ?? '--'),
  pct: (value) => `${value}%`,
  money: (value) => `$${value ?? 0}`,
  renderLeapsReasons: () => '',
  signalSourceLabel: () => 'source',
  formatStockSell: (signal) => signal.next_stock_sell_date || '未卖出',
  renderLeapsStockSell: (signal) => signal.next_stock_sell_date || '! 无正股卖点',
  renderLeapsGroupedStockSell: (group) => group.stock_sell_label || '! 无正股卖点',
  highGradeLeapsOptionSignals: (signals) => (signals || []).filter((signal) => signal.grade === '高'),
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

    def test_holding_leaps_option_outcome_status_is_rendered(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript LEAPS render check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        render_helpers = html[
            html.index("        function renderLeapsOptionOutcome") : html.index("        function outcomesForSignals")
        ]
        script = """
const vm = require('vm');
const helpers = process.argv[1];
const context = {
  Number,
  String,
  escapeHtml: (value) => String(value ?? ''),
  pct: (value) => `${value}%`,
  number: (value) => String(value ?? '--')
};
vm.createContext(context);
vm.runInContext(helpers, context);
const holding = {
  status: 'success',
  roi_pct: 12.34,
  contract: 'TSLA250117C00100000',
  expiration: '2026-01-17',
  strike: 100,
  stock_buy_price: 100,
  stock_mark_price: 120,
  stock_mark_date: '2025-06-10',
  stock_return_pct: 20,
  realized_volatility_pct: 60,
  entry_date: '2025-01-10',
  exit_status: 'holding',
  exit_date: '2025-01-10',
  exit_price: 16
};
const missingInputs = {
  ...holding,
  strike: null,
  contract: 'TSLA250117C00110000'
};
const signalOnly = {
  date: '2025-01-10',
  stock_buy_price: 100,
  stock_mark_date: '2025-06-10',
  stock_mark_price: 120,
  stock_return_pct: 20,
  realized_volatility_pct: 60
};
const rendered = context.renderLeapsOptionOutcome(holding);
const estimatedBeforeCalculation = context.renderLeapsOptionOutcome(null, signalOnly);
const expiredSignalOnly = {
  date: '2022-01-07',
  stock_buy_price: 100,
  stock_mark_date: '2024-01-20',
  stock_mark_price: 150,
  stock_return_pct: 50,
  realized_volatility_pct: 60
};
const expiredEstimate = context.renderLeapsOptionOutcome(null, expiredSignalOnly);
const expiredEstimateCell = context.renderLeapsStockEstimateCell(context.estimatedLeapsOutcomeFromSignal(expiredSignalOnly));
const table = context.renderLeapsOptionOutcomeTable([holding, missingInputs]);
if (!rendered.includes('持有中')) throw new Error(rendered);
if (!estimatedBeforeCalculation.includes('预估 +')) throw new Error(estimatedBeforeCalculation);
if (!expiredEstimate.includes('预估 +50%')) throw new Error(expiredEstimate);
if (!expiredEstimate.includes('! 正股替代')) throw new Error(expiredEstimate);
if (!expiredEstimate.includes('已到期')) throw new Error(expiredEstimate);
if (!expiredEstimateCell.includes('预估 +50%')) throw new Error(expiredEstimateCell);
if (!expiredEstimateCell.includes('! 正股替代')) throw new Error(expiredEstimateCell);
if (context.estimatedOptionVolatilityPct(22) !== 32) throw new Error('vol floor failed');
if (context.estimatedOptionVolatilityPct(80) !== 65) throw new Error('vol cap failed');
if (!table.includes('持有中')) throw new Error(table);
if (!table.includes('正股/预估')) throw new Error(table);
if (!table.includes('正股 +20%')) throw new Error(table);
if (!table.includes('预估 +')) throw new Error(table);
if (!table.includes('TSLA250117C00110000')) throw new Error(table);
if (context.formatLeapsOptionExitStatus({ status: 'success', exit_status: 'expired_without_stock_sell' }) !== '已到期') {
  throw new Error('expired status not translated');
}
const substitute = context.renderLeapsOptionOutcome({ ...holding, roi_source: 'stock_return_substitute', roi_pct: 20, option_roi_pct: -30 });
if (!substitute.includes('! 正股替代')) throw new Error(substitute);
const substituteTable = context.renderLeapsOptionOutcomeTable([{ ...holding, roi_source: 'stock_return_substitute', roi_pct: 20, stock_return_pct: 20, option_roi_pct: -30 }]);
if (!substituteTable.includes('正股收益')) throw new Error(substituteTable);
if (!substituteTable.includes('期权实际')) throw new Error(substituteTable);
if (!substituteTable.includes('! 正股替代')) throw new Error(substituteTable);
"""
        subprocess.run(["node", "-e", script, render_helpers], check=True, capture_output=True, text=True)

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
        nvda = next(group for group in result if group["symbol"] == "NVDA.US")
        self.assertEqual(nvda["stock_sell_label"], "! 无正股卖点")
        self.assertTrue(nvda["stock_sell_risk"])

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
  sellStrategyLabels: { sell_x: '卖X', sell_y: '卖Y' },
  currentLeapsEstimateScope: () => context.__scope || 'high',
  estimatedLeapsOptionSummary: (summary, scope) => {
    const value = summary?.roi_by_scope?.[scope];
    return Number.isFinite(Number(value)) ? { roi_mean_pct: Number(value) } : { roi_mean_pct: null };
  }
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
const optionHigh = context.rankParameterRows(rows, 'option_estimate_roi_mean', 'high').map((row) => ({
  key: row.key,
  global_rank: row.global_rank,
  group_rank: row.group_rank,
  score: context.scoreForRankMethod(row, 'option_estimate_roi_mean', 'high')
}));
const optionAll = context.rankParameterRows(rows, 'option_estimate_roi_mean', 'all').map((row) => ({
  key: row.key,
  global_rank: row.global_rank,
  group_rank: row.group_rank,
  score: context.scoreForRankMethod(row, 'option_estimate_roi_mean', 'all')
}));
const cacheData = { rows: JSON.parse(process.argv[2]) };
context.__scope = 'high';
const cachedHigh = context.rankedRowsFor(cacheData, 'option_estimate_roi_mean').map((row) => row.key);
context.__scope = 'all';
const cachedAll = context.rankedRowsFor(cacheData, 'option_estimate_roi_mean').map((row) => row.key);
process.stdout.write(JSON.stringify({
  normalized,
  raw,
  optionHigh,
  optionAll,
  cachedHigh,
  cachedAll,
  cacheKeys: Array.from(cacheData._ranked_rows_cache.keys())
}));
"""
        rows = [
            {"key": "a1", "final_score": 80, "raw_score": 1, "candidate": {"buy_strategy": "buy_a", "sell_strategy": "sell_x"}, "leaps_signal": {"roi_by_scope": {"high": 10, "all": 10}}},
            {"key": "a2", "final_score": 70, "raw_score": 4, "candidate": {"buy_strategy": "buy_a", "sell_strategy": "sell_x"}, "leaps_signal": {"roi_by_scope": {"all": 20}}},
            {"key": "b1", "final_score": 90, "raw_score": 2, "candidate": {"buy_strategy": "buy_b", "sell_strategy": "sell_y"}, "leaps_signal": {"roi_by_scope": {"high": 12, "all": 15}}},
            {"key": "c1", "final_score": 60, "raw_score": 3, "candidate": {"buy_strategy": "buy_a", "sell_strategy": "sell_y"}, "leaps_signal": {}},
            {"key": "d1", "final_score": 80, "raw_score": 5, "candidate": {"buy_strategy": "buy_b", "sell_strategy": "sell_y"}, "leaps_signal": {"roi_by_scope": {"high": 10, "all": 5}}},
        ]
        completed = subprocess.run(
            ["node", "-e", script, helpers, json.dumps(rows)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual([row["key"] for row in result["normalized"]], ["b1", "a1", "d1", "a2", "c1"])
        self.assertEqual([row["global_rank"] for row in result["normalized"]], [1, 2, 3, 4, 5])
        self.assertEqual(
            [(row["key"], row["group_key"], row["group_rank"]) for row in result["normalized"]],
            [("b1", "buy_b__sell_y", 1), ("a1", "buy_a__sell_x", 1), ("d1", "buy_b__sell_y", 2), ("a2", "buy_a__sell_x", 2), ("c1", "buy_a__sell_y", 1)],
        )
        self.assertEqual([row["key"] for row in result["raw"]], ["d1", "a2", "c1", "b1", "a1"])
        self.assertEqual([row["global_rank"] for row in result["raw"]], [1, 2, 3, 4, 5])
        self.assertEqual(
            [(row["key"], row["group_key"], row["group_rank"]) for row in result["raw"]],
            [("d1", "buy_b__sell_y", 1), ("a2", "buy_a__sell_x", 1), ("c1", "buy_a__sell_y", 1), ("b1", "buy_b__sell_y", 2), ("a1", "buy_a__sell_x", 2)],
        )
        self.assertEqual([row["key"] for row in result["optionHigh"]], ["b1", "a1", "d1", "a2", "c1"])
        self.assertEqual([row["score"] for row in result["optionHigh"]], [12, 10, 10, None, None])
        self.assertEqual([row["key"] for row in result["optionAll"]], ["a2", "b1", "a1", "d1", "c1"])
        self.assertEqual(result["cachedHigh"], ["b1", "a1", "d1", "a2", "c1"])
        self.assertEqual(result["cachedAll"], ["a2", "b1", "a1", "d1", "c1"])
        self.assertEqual(result["cacheKeys"], ["option_estimate_roi_mean:high", "option_estimate_roi_mean:all"])
        self.assertEqual(result["normalized"][1]["group_label"], "买A / 卖X")

    def test_top_group_leaps_option_helpers_select_dedupe_and_cap_signals(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript top group LEAPS helper check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        rank_match = re.search(
            r"function strategyGroupKey\(candidate\) \{.*?\n        function scoreParameterResults",
            html,
            re.S,
        )
        self.assertIsNotNone(rank_match)
        rank_helpers = rank_match.group(0).rsplit("\n        function scoreParameterResults", 1)[0]
        match = re.search(
            r"function median\(values\) \{.*?\n        function hasRunningLeapsOptionQueue",
            html,
            re.S,
        )
        self.assertIsNotNone(match)
        helpers = "\n".join([rank_helpers, match.group(0).rsplit("\n        function hasRunningLeapsOptionQueue", 1)[0]])
        script = """
const vm = require('vm');
const helpers = process.argv[1];
const rows = JSON.parse(process.argv[2]);
const context = {
  buyStrategyLabels: { buy_a: '买A', buy_b: '买B' },
  sellStrategyLabels: { sell_x: '卖X', sell_y: '卖Y' },
  Number,
  String,
  Array,
  Map,
  Set,
  Math,
  TOP_GROUP_LEAPS_OPTION_ROW_LIMIT: 3,
  TOP_GROUP_LEAPS_OPTION_SIGNAL_BUDGET: 300,
  document: { getElementById: () => ({ value: 'normalized' }) }
};
vm.createContext(context);
vm.runInContext(helpers, context);
const selected = context.topGroupLeapsOptionRows({ rows }, 'normalized');
const rowSignalPairs = [];
selected.forEach((row) => {
  context.highGradeLeapsOptionSignals(row.detailSignals || []).forEach((signal) => {
    rowSignalPairs.push({ row, signal });
  });
});
const uniqueQueue = context.buildTopGroupLeapsOptionQueue(rowSignalPairs);
const split = context.splitTopGroupLeapsOptionQueueByBudget(uniqueQueue, 3);
process.stdout.write(JSON.stringify({
  selected: selected.map((row) => [row.key, row.group_key, row.group_rank]),
  highSignalCount: rowSignalPairs.length,
  uniqueCount: uniqueQueue.length,
  firstSourceCount: uniqueQueue[0].sources.length,
  requestCount: split.requestQueue.length,
  overflowCount: split.overflowQueue.length,
  budgetTruncated: split.budgetTruncated
}));
"""
        rows = [
            {
                "key": "a1",
                "final_score": 100,
                "raw_score": 1,
                "buy_strategy": "buy_a",
                "sell_strategy": "sell_x",
                "leaps_signal": {"grade": "高"},
                "detailSignals": [
                    {"signal_key": "a1-s1", "grade": "高", "date": "2024-01-10", "symbol": "TSLA.US", "next_stock_sell_date": "2024-01-20", "stock_buy_price": 10},
                    {"signal_key": "a1-s2", "grade": "中", "date": "2024-01-11", "symbol": "TSLA.US", "next_stock_sell_date": "2024-01-21", "stock_buy_price": 11},
                ],
            },
            {
                "key": "a2",
                "final_score": 90,
                "raw_score": 2,
                "buy_strategy": "buy_a",
                "sell_strategy": "sell_x",
                "leaps_signal": {"grade": "中"},
                "detailSignals": [
                    {"signal_key": "a2-s1", "grade": "高", "date": "2024-01-12", "symbol": "MSFT.US", "next_stock_sell_date": "2024-01-22", "stock_buy_price": 20}
                ],
            },
            {
                "key": "a3",
                "final_score": 80,
                "raw_score": 3,
                "buy_strategy": "buy_a",
                "sell_strategy": "sell_x",
                "leaps_signal": {"grade": "高"},
                "detailSignals": [
                    {"signal_key": "a3-s1", "grade": "高", "date": "2024-01-10", "symbol": "TSLA.US", "next_stock_sell_date": "2024-01-20", "stock_buy_price": 10},
                    {"signal_key": "a3-s2", "grade": "高", "date": "2024-01-13", "symbol": "AAPL.US", "next_stock_sell_date": "2024-01-23", "stock_buy_price": 30},
                ],
            },
            {
                "key": "a4",
                "final_score": 70,
                "raw_score": 4,
                "buy_strategy": "buy_a",
                "sell_strategy": "sell_x",
                "leaps_signal": {"grade": "高"},
                "detailSignals": [
                    {"signal_key": "a4-s1", "grade": "高", "date": "2024-01-14", "symbol": "NVDA.US", "next_stock_sell_date": "2024-01-24", "stock_buy_price": 40}
                ],
            },
            {
                "key": "b1",
                "final_score": 60,
                "raw_score": 5,
                "buy_strategy": "buy_b",
                "sell_strategy": "sell_y",
                "leaps_signal": {"grade": "高"},
                "detailSignals": [
                    {"signal_key": "b1-s1", "grade": "高", "date": "2024-01-15", "symbol": "AMZN.US", "next_stock_sell_date": "2024-01-25", "stock_buy_price": 50},
                    {"signal_key": "b1-s2", "grade": "高", "date": "2024-01-16", "symbol": "GOOG.US", "next_stock_sell_date": "2024-01-26", "stock_buy_price": 60},
                ],
            },
        ]
        completed = subprocess.run(
            ["node", "-e", script, helpers, json.dumps(rows)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(
            result["selected"],
            [
                ["a1", "buy_a__sell_x", 1],
                ["a3", "buy_a__sell_x", 3],
                ["b1", "buy_b__sell_y", 1],
            ],
        )
        self.assertEqual(result["highSignalCount"], 5)
        self.assertEqual(result["uniqueCount"], 4)
        self.assertEqual(result["firstSourceCount"], 2)
        self.assertEqual(result["requestCount"], 3)
        self.assertEqual(result["overflowCount"], 1)
        self.assertTrue(result["budgetTruncated"])

    def test_leaps_option_request_helper_uses_batch_endpoint(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript LEAPS request helper check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        helper_body = html[
            html.index("        function skippedLeapsOptionOutcome") : html.index("        function formatElapsedTime")
        ]
        helpers = "\n".join(
            [
                "const LEAPS_OPTION_BATCH_SIZE = 5;",
                "const LEAPS_OPTION_OUTCOME_RETRY_DELAYS_MS = [5000, 15000];",
                "const LEAPS_OPTION_STOPPED_REASON = '已停止，未计算';",
                helper_body,
            ]
        )
        script = """
const vm = require('vm');
const helpers = process.argv[1];
let now = 100000;
const urls = [];
const bodies = [];
const delays = [];
const context = {
  Date: { now: () => now },
  Math, Number, String, Array, Map, Set, Promise, Error, RegExp,
  setTimeout: (callback, ms) => { delays.push(ms); now += ms; callback(); return 1; },
  fetch: async (url, options) => {
    urls.push(url);
    bodies.push(JSON.parse(options.body));
    return {
      ok: true,
      status: 200,
      json: async () => ({ outcomes: [
        { status: 'success', symbol: 'AAPL.US', roi_pct: 1 },
        { status: 'success', symbol: 'MSFT.US', roi_pct: 2 }
      ], cache_stats: { polygon_requests: 0, outcome: { memory_hit: 2 } } })
    };
  }
};
vm.createContext(context);
vm.runInContext(helpers, context);
(async () => {
  const signalA = { signal_key: 'sig-a', date: '2024-01-10', symbol: 'AAPL.US' };
  const signalB = { signal_key: 'sig-b', date: '2024-01-11', symbol: 'MSFT.US' };
  const result = await context.requestLeapsOptionOutcomeBatch(
    { signals: [signalA, signalB] },
    [{ signal: signalA }, { signal: signalB }]
  );
  process.stdout.write(JSON.stringify({ urls, bodies, delays, statuses: result.outcomes.map((item) => item.status), cacheStats: result.cache_stats }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
        completed = subprocess.run(["node", "-e", script, helpers], check=True, capture_output=True, text=True)
        result = json.loads(completed.stdout)

        self.assertEqual(result["statuses"], ["success", "success"])
        self.assertEqual(result["urls"], ["/api/strategy-lab/parameter-lab/leaps-option-outcomes/batch"])
        self.assertNotIn("option_exit_policy", result["bodies"][0])
        self.assertEqual(result["cacheStats"]["outcome"]["memory_hit"], 2)

    def test_leaps_option_request_helper_does_not_retry_polygon_403(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript LEAPS request helper check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        helper_body = html[
            html.index("        function skippedLeapsOptionOutcome") : html.index("        function formatElapsedTime")
        ]
        helpers = "\n".join(
            [
                "const LEAPS_OPTION_BATCH_SIZE = 5;",
                "const LEAPS_OPTION_OUTCOME_RETRY_DELAYS_MS = [5000, 15000];",
                "const LEAPS_OPTION_STOPPED_REASON = '已停止，未计算';",
                helper_body,
            ]
        )
        script = """
const vm = require('vm');
const helpers = process.argv[1];
const urls = [];
const delays = [];
const context = {
  Date, Math, Number, String, Array, Map, Set, Promise, Error, RegExp,
  setTimeout: (callback, ms) => { delays.push(ms); callback(); return 1; },
  fetch: async (url) => {
    urls.push(url);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        outcomes: [{ status: 'skipped', skipped_reason: 'Polygon API 无权限/套餐不支持期权历史K线' }],
        summary: { top_failure_reason: 'Polygon API 无权限/套餐不支持期权历史K线' },
        cache_stats: { polygon_requests: 1 }
      })
    };
  }
};
vm.createContext(context);
vm.runInContext(helpers, context);
(async () => {
  const signal = { signal_key: 'sig-a', date: '2024-01-10', symbol: 'AAPL.US' };
  const result = await context.requestLeapsOptionOutcomeBatch({ signals: [signal] }, [{ signal }]);
  process.stdout.write(JSON.stringify({
    urls,
    delays,
    reason: result.outcomes[0].skipped_reason,
    permissionDenied: context.leapsOptionPermissionDeniedReason(result)
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
        completed = subprocess.run(["node", "-e", script, helpers], check=True, capture_output=True, text=True)
        result = json.loads(completed.stdout)

        self.assertEqual(result["urls"], ["/api/strategy-lab/parameter-lab/leaps-option-outcomes/batch"])
        self.assertEqual(result["delays"], [])
        self.assertIn("无权限", result["reason"])
        self.assertIn("无权限", result["permissionDenied"])

    def test_top_group_leaps_option_request_retries_and_clones_dedupe_sources(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript top group LEAPS request helper check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        request_helpers = html[
            html.index("        function skippedLeapsOptionOutcome") : html.index("        function updateLeapsOptionEntrySummary")
        ]
        top_group_helpers = html[
            html.index("        function appendTopGroupLeapsOptionOutcome") : html.index("        async function calculateTopGroupLeapsOptionOutcomes")
        ]
        prefix = "\n".join(
            [
                "const LEAPS_OPTION_BATCH_SIZE = 5;",
                "const LEAPS_OPTION_OUTCOME_RETRY_DELAYS_MS = [5000, 15000];",
                "const LEAPS_OPTION_STOPPED_REASON = '已停止，未计算';",
                "const leapsOptionOutcomeCache = new Map();",
                "let activeDetailRowKey = '';",
                "let lastParameterPacket = { run_id: 'run-1' };",
            ]
        )
        script = """
const vm = require('vm');
const helpers = process.argv[1];
let now = 100000;
let fetchCount = 0;
const delays = [];
const context = {
  Date: { now: () => now },
  Math, Number, String, Array, Map, Set, Promise, Error, RegExp, Boolean,
  setTimeout: (callback, ms) => { delays.push(ms); now += ms; callback(); return 1; },
  fetch: async () => {
    fetchCount += 1;
    if (fetchCount === 1) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ outcomes: [{ status: 'skipped', skipped_reason: 'API 限流/超时: retry later' }] })
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ outcomes: [{ status: 'success', symbol: 'TSLA.US', roi_pct: 12.3, contract: 'TSLA250117C00100000' }] })
    };
  },
  number: (value) => String(value ?? 0),
  escapeHtml: (value) => String(value ?? ''),
  aggregateLeapsOptionOutcomes: (outcomes) => ({ total: outcomes.length, success_count: outcomes.filter((item) => item.status === 'success').length }),
  leapsDetailCacheKey: (row) => row.key,
  renderDetailLeapsIntoDrawer: () => {},
  renderParameterMatrixPreservingScroll: () => {},
  document: { getElementById: () => null },
  currentParameterRankMethod: () => 'normalized',
  topGroupLeapsOptionRows: () => [],
  lastParameterResult: null,
  currentRun: null,
  topGroupLeapsOptionRun: null
};
vm.createContext(context);
vm.runInContext(helpers, context);
context.renderTopGroupLeapsOptionControls = () => {};
(async () => {
  const rowA = { key: 'row-a' };
  const rowB = { key: 'row-b' };
  const baseSignal = { signal_key: 'base', date: '2024-01-10', symbol: 'TSLA.US', next_stock_sell_date: '2024-01-20' };
  const queueItem = {
    key: 'dedupe',
    signal: baseSignal,
    sources: [
      { row: rowA, signal: { ...baseSignal, signal_key: 'row-a-sig' } },
      { row: rowB, signal: { ...baseSignal, signal_key: 'row-b-sig' } }
    ]
  };
  const run = { controller: { cancelled: false }, activeLabels: [], startedAt: now, requestCompleted: 0, batchCompleted: 0, batchTotal: 1, successCount: 0, skippedCount: 0, lastFailure: '', cacheStats: {} };
  await context.processTopGroupLeapsOptionBatch([queueItem], run, 0, 1);
  process.stdout.write(JSON.stringify({
    fetchCount,
    delays,
    rowA: rowA.leaps_option_outcomes,
    rowB: rowB.leaps_option_outcomes,
    successCount: run.successCount,
    skippedCount: run.skippedCount
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
        completed = subprocess.run(
            ["node", "-e", script, "\n".join([prefix, request_helpers, top_group_helpers])],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["fetchCount"], 2)
        self.assertIn(5000, result["delays"])
        self.assertEqual(result["successCount"], 1)
        self.assertEqual(result["skippedCount"], 0)
        self.assertEqual(result["rowA"][0]["status"], "success")
        self.assertEqual(result["rowA"][0]["signal_key"], "row-a-sig")
        self.assertEqual(result["rowB"][0]["status"], "success")
        self.assertEqual(result["rowB"][0]["signal_key"], "row-b-sig")

    def test_top_group_leaps_option_stop_marks_unstarted_items(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript top group LEAPS stop helper check")

        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")
        request_helpers = html[
            html.index("        function skippedLeapsOptionOutcome") : html.index("        function updateLeapsOptionEntrySummary")
        ]
        top_group_helpers = html[
            html.index("        function appendTopGroupLeapsOptionOutcome") : html.index("        async function calculateTopGroupLeapsOptionOutcomes")
        ]
        prefix = "\n".join(
            [
                "const LEAPS_OPTION_BATCH_SIZE = 5;",
                "const LEAPS_OPTION_OUTCOME_RETRY_DELAYS_MS = [5000, 15000];",
                "const LEAPS_OPTION_STOPPED_REASON = '已停止，未计算';",
                "const leapsOptionOutcomeCache = new Map();",
                "let activeDetailRowKey = '';",
                "let lastParameterPacket = { run_id: 'run-1' };",
            ]
        )
        script = """
const vm = require('vm');
const helpers = process.argv[1];
let now = 100000;
const context = {
  Date: { now: () => now },
  Math, Number, String, Array, Map, Set, Promise, Error, RegExp, Boolean,
  setTimeout: (callback, ms) => { now += ms; callback(); return 1; },
  fetch: async () => {
    return {
      ok: true,
      status: 200,
      json: async () => ({ outcomes: [{ status: 'success', symbol: 'TSLA.US', roi_pct: 12.3 }] })
    };
  },
  number: (value) => String(value ?? 0),
  escapeHtml: (value) => String(value ?? ''),
  aggregateLeapsOptionOutcomes: (outcomes) => ({ total: outcomes.length, success_count: outcomes.filter((item) => item.status === 'success').length }),
  leapsDetailCacheKey: (row) => row.key,
  renderDetailLeapsIntoDrawer: () => {},
  renderParameterMatrixPreservingScroll: () => {},
  document: { getElementById: () => null },
  currentParameterRankMethod: () => 'normalized',
  topGroupLeapsOptionRows: () => [],
  lastParameterResult: null,
  currentRun: null,
  topGroupLeapsOptionRun: null
};
vm.createContext(context);
vm.runInContext(helpers, context);
context.renderTopGroupLeapsOptionControls = () => {};
(async () => {
  const firstRow = { key: 'row-first' };
  const secondRow = { key: 'row-second' };
  const requestQueue = [
    {
      key: 'first',
      signal: { signal_key: 'first', date: '2024-01-10', symbol: 'TSLA.US' },
      sources: [{ row: firstRow, signal: { signal_key: 'first', date: '2024-01-10', symbol: 'TSLA.US' } }]
    },
    {
      key: 'second',
      signal: { signal_key: 'second', date: '2024-01-11', symbol: 'MSFT.US' },
      sources: [{ row: secondRow, signal: { signal_key: 'second', date: '2024-01-11', symbol: 'MSFT.US' } }]
    }
  ];
  const run = { controller: { cancelled: false }, activeLabels: [], startedAt: now, requestCompleted: 0, batchCompleted: 0, batchTotal: 2, successCount: 0, skippedCount: 0, lastFailure: '', cacheStats: {} };
  for (const queueItem of requestQueue) {
    if (run.controller.cancelled) break;
    await context.processTopGroupLeapsOptionBatch([queueItem], run, run.batchCompleted, 2);
    run.controller.cancelled = true;
  }
  if (run.controller.cancelled) {
    requestQueue.forEach((queueItem) => {
      if (!queueItem.started && !queueItem.done) context.appendTopGroupSkippedOutcomes(queueItem, context.LEAPS_OPTION_STOPPED_REASON || '已停止，未计算', run);
    });
  }
  process.stdout.write(JSON.stringify({
    first: firstRow.leaps_option_outcomes,
    second: secondRow.leaps_option_outcomes,
    requestCompleted: run.requestCompleted,
    skippedCount: run.skippedCount,
    lastFailure: run.lastFailure
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
        completed = subprocess.run(
            ["node", "-e", script, "\n".join([prefix, request_helpers, top_group_helpers])],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["requestCompleted"], 1)
        self.assertEqual(result["first"][0]["status"], "success")
        self.assertEqual(result["second"][0]["status"], "skipped")
        self.assertEqual(result["second"][0]["skipped_reason"], "已停止，未计算")
        self.assertEqual(result["lastFailure"], "已停止，未计算")

    def test_parameter_lab_page_no_longer_exposes_option_scan(self):
        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")

        self.assertNotIn("option" + "-packet", html)
        self.assertNotIn("option" + "_scan_worker", html)
        self.assertNotIn("option" + "_debug", html)
        self.assertIn("200-300D 月期" + "权收益", html)


if __name__ == "__main__":
    unittest.main()
