"""End-to-end tests for GA worker lifecycle and evolution flow.

Runs the actual Web Worker JS in a Node.js VM with a realistic GA packet
to verify the complete start→ready→batch→batch_done lifecycle produces
valid (non-NaN) observations.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

WORKER_JS = Path(__file__).resolve().parent / "web/static/strategy_parameter_lab_worker.js"


class GeneticAlgorithmE2ETest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if shutil.which("node") is None:
            raise unittest.SkipTest("node is required for GA E2E tests")

    def _run_node_script(self, script: str, *args: str) -> dict:
        completed = subprocess.run(
            ["node", "-e", script, str(WORKER_JS), *args],
            check=True, capture_output=True, text=True,
        )
        return json.loads(completed.stdout)

    def test_worker_init_run_and_batch_produces_valid_observations(self):
        """S1 (happy): initRun + processBatch → batch_done with valid return_pct, max_drawdown_pct."""
        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const messages = [];
const context = {
  console: { info() {}, warn() {}, error() {} },
  postMessage(message) { messages.push(message); },
  performance: { now: () => 0 },
  setTimeout,
  importScripts() {},
};
context.self = context;
vm.createContext(context);
vm.runInContext(source, context);

const buyFields = [
  'step_pct', 'equal_slice_allocation_pct', 'core_dip_initial_core_pct', 'core_dip_weekly_core_pct',
  'core_dip_cash_reserve_pct', 'core_dip_start_drawdown_pct', 'core_dip_full_drawdown_pct',
  'core_dip_timing_enabled', 'core_dip_timing_max_delay_days', 'core_dip_timing_rise_threshold_pct',
  'core_dip_timing_near_low_pct'
];
const sellFields = [
  'sell_min_profit_pct', 'repair_sell_cooldown_days', 'repair_stage_sell_pct',
  'grid_rebound_step_pct', 'grid_sell_pct', 'grid_first_sell_pct', 'grid_second_sell_pct',
  'grid_min_sell_amount', 'cost_first_profit_pct', 'cost_second_profit_pct', 'cost_third_profit_pct',
  'cost_first_sell_pct', 'cost_second_sell_pct', 'cost_third_sell_pct', 'cost_deleverage_cooldown_days',
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
  'buy_rearm_mode', 'sell_stage_rearm_drawdown_pct'
];

function gaPacket(opts) {
  opts = opts || {};
  return {
    run_id: 'e2e-test',
    inputs: {
      initial_cash: 20000, monthly_contribution: 1000, max_drawdown_pct: 50,
      drawdown_basis: 'ath', trade_fee: 0.35, hkd_to_usd: 0.128,
      reserve_position_pct: 40, sell_min_profit_pct: 10,
      sell_allow_same_day_sell: false, dca_rearm_drawdown_pct: 5,
      sell_stage_rearm_drawdown_pct: null, step_pct: 5,
      equal_slice_allocation_pct: 10, grid_rebound_step_pct: 5,
      grid_sell_pct: 40, grid_min_sell_amount: 200
    },
    tasks: [{
      key: 'tsla_1y', portfolio_key: 'tsla_100', portfolio_label: 'TSLA',
      period_key: 'one_year', period_label: '近一年',
      start: '2025-01-01', end: '2025-03-01',
      symbols: ['TSLA.US'],
      targets: [{ symbol: 'TSLA.US', weight: 100, name: 'TSLA', max_drawdown_pct: 50 }]
    }],
    market_data: {
      symbols: {
        'TSLA.US': {
          dates: ['2025-01-01','2025-01-02','2025-01-03','2025-01-04','2025-01-05',
                  '2025-01-06','2025-01-07','2025-01-08','2025-01-09','2025-01-10',
                  '2025-01-11','2025-01-12','2025-01-13','2025-01-14','2025-01-15'],
          closes: [250, 240, 230, 220, 210, 200, 195, 200, 210, 220, 230, 240, 250, 260, 270]
        }
      }
    },
    buy_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...buyFields],
    sell_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...sellFields],
    candidate_schema: ['candidate_id', 'buy_variant_id', 'sell_variant_id'],
    buy_variants: opts.buy_variants || [
      [0, 'buy:pyramid_3', 'pyramid_3', null, null, null, null, null, null, null, null, null, null, null],
      [1, 'buy:equal_slice', 'equal_slice', 5, 10, null, null, null, null, null, null, null, null, null]
    ],
    sell_variants: opts.sell_variants || [
      [0, 'sell:none', 'none', null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, false, null, null, null],
      [1, 'sell:grid', 'grid_rebound', 0, null, null, 5, 40, null, null, 200, null, null, null, null, null, null, null, false, null, null, null]
    ],
    candidate_rows: opts.candidate_rows || [[0, 0, 0], [1, 1, 1]]
  };
}
(async () => {
  messages.length = 0;
  const p = gaPacket({});
  await context.initRun(p, 0, p.run_id, p.candidate_rows.length * p.tasks.length);
  await context.processBatch({ run_id: p.run_id, worker_index: 0, batch_id: 'b1', candidate_rows: p.candidate_rows }, 0, p.run_id);
  const done = messages.find(msg => msg.type === 'batch_done');
  if (!done) { process.stdout.write(JSON.stringify({error: 'no batch_done', messages: messages.map(m => m.type)})); return; }
  const summary = done.rows.map(row => ({
    candidate_id: row.candidate_id,
    observation_count: row.observations.length,
    first_return: Number(row.observations[0]?.return_pct),
    first_drawdown: Number(row.observations[0]?.max_drawdown_pct),
    all_finite: row.observations.every(o => isFinite(Number(o.return_pct)) && isFinite(Number(o.max_drawdown_pct)))
  }));
  process.stdout.write(JSON.stringify({ ok: true, rows: summary }));
})().catch(err => { process.stdout.write(JSON.stringify({ error: err.message, stack: err.stack })); });
"""
        result = self._run_node_script(script)
        self.assertIn("ok", result, f"Worker failed: {result.get('error', 'unknown')}")
        self.assertTrue(result["ok"])
        rows = result["rows"]
        self.assertEqual(len(rows), 2, "Should have 2 candidate rows")
        for i, row in enumerate(rows):
            self.assertIsNotNone(row["candidate_id"], f"Row {i} missing candidate_id")
            self.assertGreater(row["observation_count"], 0, f"Row {i} has no observations")
            self.assertTrue(row["all_finite"], f"Row {i} has NaN: r={row['first_return']} dd={row['first_drawdown']}")
            self.assertIsInstance(row["first_return"], (int, float), f"Row {i} return_pct not numeric")
            self.assertIsInstance(row["first_drawdown"], (int, float), f"Row {i} max_drawdown_pct not numeric")

    def test_worker_returns_candidate_id_matching_input(self):
        """S2 (edge): candidate_id in batch_done must match input candidate_rows."""
        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const messages = [];
const context = {
  console: { info() {}, warn() {}, error() {} },
  postMessage(msg) { messages.push(msg); },
  performance: { now: () => 0 },
  setTimeout,
  importScripts() {},
};
context.self = context;
vm.createContext(context);
vm.runInContext(source, context);

const buyF = [
  'step_pct', 'equal_slice_allocation_pct', 'core_dip_initial_core_pct', 'core_dip_weekly_core_pct',
  'core_dip_cash_reserve_pct', 'core_dip_start_drawdown_pct', 'core_dip_full_drawdown_pct',
  'core_dip_timing_enabled', 'core_dip_timing_max_delay_days', 'core_dip_timing_rise_threshold_pct',
  'core_dip_timing_near_low_pct'
];
const sellF = [
  'sell_min_profit_pct', 'repair_sell_cooldown_days', 'repair_stage_sell_pct',
  'grid_rebound_step_pct', 'grid_sell_pct', 'grid_first_sell_pct', 'grid_second_sell_pct',
  'grid_min_sell_amount', 'cost_first_profit_pct', 'cost_second_profit_pct', 'cost_third_profit_pct',
  'cost_first_sell_pct', 'cost_second_sell_pct', 'cost_third_sell_pct', 'cost_deleverage_cooldown_days',
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
  'buy_rearm_mode', 'sell_stage_rearm_drawdown_pct'
];
const p = {
  run_id: 'e2e-id-match',
  inputs: { initial_cash: 1000, monthly_contribution: 0, max_drawdown_pct: 50, drawdown_basis: 'ath',
    trade_fee: 0, hkd_to_usd: 0.128, reserve_position_pct: 0, sell_min_profit_pct: 0,
    sell_allow_same_day_sell: false, dca_rearm_drawdown_pct: 0, sell_stage_rearm_drawdown_pct: null },
  tasks: [{
    key: 'test', portfolio_key: 't', portfolio_label: 'T', period_key: 'p', period_label: 'P',
    start: '2025-01-01', end: '2025-01-05', symbols: ['X.US'],
    targets: [{ symbol: 'X.US', weight: 100, name: 'X', max_drawdown_pct: 50 }]
  }],
  market_data: { symbols: { 'X.US': { dates: ['2025-01-01','2025-01-02','2025-01-03','2025-01-04','2025-01-05'],
    closes: [100, 90, 95, 110, 120] } } },
  buy_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...buyF],
  sell_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...sellF],
  candidate_schema: ['candidate_id', 'buy_variant_id', 'sell_variant_id'],
  buy_variants: [
    [0, 'b0', 'pyramid_3', null,null,null,null,null,null,null,null,null,null,null],
    [1, 'b1', 'equal_slice', 5, 10, null,null,null,null,null,null,null,null,null],
    [2, 'b2', 'equal_slice', 10, 20, null,null,null,null,null,null,null,null,null]
  ],
  sell_variants: [
    [0, 's0', 'none', null,null,null,null,null,null,null,null,null,null,null,null,null,null,false,null,null,null],
    [1, 's1', 'none', null,null,null,null,null,null,null,null,null,null,null,null,null,null,false,null,null,null],
    [2, 's2', 'none', null,null,null,null,null,null,null,null,null,null,null,null,null,null,false,null,null,null]
  ],
  candidate_rows: [[10, 1, 1], [20, 2, 2], [30, 0, 0]]
};
(async () => {
  messages.length = 0;
  await context.initRun(p, 0, p.run_id, 3);
  await context.processBatch({ run_id: p.run_id, worker_index: 0, batch_id: 'b1', candidate_rows: p.candidate_rows }, 0, p.run_id);
  const done = messages.find(msg => msg.type === 'batch_done');
  if (!done) { process.stdout.write(JSON.stringify({error: 'no batch_done'})); return; }
  const ids = done.rows.map(r => r.candidate_id);
  process.stdout.write(JSON.stringify({ ok: true, candidate_ids: ids }));
})().catch(err => { process.stdout.write(JSON.stringify({ error: err.message })); });
"""
        result = self._run_node_script(script)
        self.assertIn("ok", result)
        ids = result["candidate_ids"]
        self.assertEqual(sorted(ids), [10, 20, 30], f"candidate_ids mismatch: {ids}")

    def test_worker_handles_continuous_mutation_parameters(self):
        """S3 (edge): continuous/野蛮生长 parameter values must be handled correctly by worker."""
        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const messages = [];
const context = {
  console: { info() {}, warn() {}, error() {} },
  postMessage(msg) { messages.push(msg); },
  performance: { now: () => 0 },
  setTimeout,
  importScripts() {},
};
context.self = context;
vm.createContext(context);
vm.runInContext(source, context);

const buyF = [
  'step_pct', 'equal_slice_allocation_pct', 'core_dip_initial_core_pct', 'core_dip_weekly_core_pct',
  'core_dip_cash_reserve_pct', 'core_dip_start_drawdown_pct', 'core_dip_full_drawdown_pct',
  'core_dip_timing_enabled', 'core_dip_timing_max_delay_days', 'core_dip_timing_rise_threshold_pct',
  'core_dip_timing_near_low_pct'
];
const sellF = [
  'sell_min_profit_pct', 'repair_sell_cooldown_days', 'repair_stage_sell_pct',
  'grid_rebound_step_pct', 'grid_sell_pct', 'grid_first_sell_pct', 'grid_second_sell_pct',
  'grid_min_sell_amount', 'cost_first_profit_pct', 'cost_second_profit_pct', 'cost_third_profit_pct',
  'cost_first_sell_pct', 'cost_second_sell_pct', 'cost_third_sell_pct', 'cost_deleverage_cooldown_days',
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
  'buy_rearm_mode', 'sell_stage_rearm_drawdown_pct'
];

// Simulate continuous-mutation values: non-standard numbers like 3.7, 11.2
const p = {
  run_id: 'e2e-continuous',
  inputs: { initial_cash: 1000, monthly_contribution: 0, max_drawdown_pct: 50, drawdown_basis: 'ath',
    trade_fee: 0, hkd_to_usd: 0.128, reserve_position_pct: 0, sell_min_profit_pct: 0,
    sell_allow_same_day_sell: false, dca_rearm_drawdown_pct: 0, sell_stage_rearm_drawdown_pct: null },
  tasks: [{
    key: 't', portfolio_key: 't', portfolio_label: 'T', period_key: 'p', period_label: 'P',
    start: '2025-01-01', end: '2025-01-05', symbols: ['X.US'],
    targets: [{ symbol: 'X.US', weight: 100, name: 'X', max_drawdown_pct: 50 }]
  }],
  market_data: { symbols: { 'X.US': { dates: ['2025-01-01','2025-01-02','2025-01-03','2025-01-04','2025-01-05'],
    closes: [100, 90, 95, 110, 120] } } },
  buy_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...buyF],
  sell_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...sellF],
  candidate_schema: ['candidate_id', 'buy_variant_id', 'sell_variant_id'],
  // Continuous values: 3.7 step, 11.2 alloc (not in the discrete set [2.5,5,10])
  buy_variants: [[0, 'b0', 'equal_slice', 3.7, 11.2, null,null,null,null,null,null,null,null,null]],
  // Grid with non-standard grid_sell_pct=33.5
  sell_variants: [[0, 's0', 'grid_rebound', 0, null, null, 6.3, 33.5, null, null, 300, null,null,null,null,null,null,null,false,null,null,null]],
  candidate_rows: [[0, 0, 0]]
};
(async () => {
  messages.length = 0;
  await context.initRun(p, 0, p.run_id, 1);
  await context.processBatch({ run_id: p.run_id, worker_index: 0, batch_id: 'b1', candidate_rows: p.candidate_rows }, 0, p.run_id);
  const done = messages.find(msg => msg.type === 'batch_done');
  if (!done) { process.stdout.write(JSON.stringify({error: 'no batch_done'})); return; }
  const obs = done.rows[0].observations[0];
  process.stdout.write(JSON.stringify({
    ok: true,
    return_pct: Number(obs.return_pct),
    max_drawdown_pct: Number(obs.max_drawdown_pct),
    finite: isFinite(Number(obs.return_pct)) && isFinite(Number(obs.max_drawdown_pct))
  }));
})().catch(err => { process.stdout.write(JSON.stringify({ error: err.message, stack: err.stack })); });
"""
        result = self._run_node_script(script)
        self.assertIn("ok", result)
        self.assertTrue(result["finite"], f"Continuous params produced NaN: {result}")
        self.assertIsInstance(result["return_pct"], (int, float))
        self.assertIsInstance(result["max_drawdown_pct"], (int, float))

    def test_worker_handles_large_population_batch(self):
        """S4 (load): 50-candidate batch must complete without errors and all results valid."""
        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const messages = [];
const context = {
  console: { info() {}, warn() {}, error() {} },
  postMessage(msg) { messages.push(msg); },
  performance: { now: () => 0 },
  setTimeout,
  importScripts() {},
};
context.self = context;
vm.createContext(context);
vm.runInContext(source, context);

const buyF = [
  'step_pct', 'equal_slice_allocation_pct', 'core_dip_initial_core_pct', 'core_dip_weekly_core_pct',
  'core_dip_cash_reserve_pct', 'core_dip_start_drawdown_pct', 'core_dip_full_drawdown_pct',
  'core_dip_timing_enabled', 'core_dip_timing_max_delay_days', 'core_dip_timing_rise_threshold_pct',
  'core_dip_timing_near_low_pct'
];
const sellF = [
  'sell_min_profit_pct', 'repair_sell_cooldown_days', 'repair_stage_sell_pct',
  'grid_rebound_step_pct', 'grid_sell_pct', 'grid_first_sell_pct', 'grid_second_sell_pct',
  'grid_min_sell_amount', 'cost_first_profit_pct', 'cost_second_profit_pct', 'cost_third_profit_pct',
  'cost_first_sell_pct', 'cost_second_sell_pct', 'cost_third_sell_pct', 'cost_deleverage_cooldown_days',
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
  'buy_rearm_mode', 'sell_stage_rearm_drawdown_pct'
];

const p = {
  run_id: 'e2e-load',
  inputs: { initial_cash: 1000, monthly_contribution: 0, max_drawdown_pct: 50, drawdown_basis: 'ath',
    trade_fee: 0, hkd_to_usd: 0.128, reserve_position_pct: 0, sell_min_profit_pct: 0,
    sell_allow_same_day_sell: false, dca_rearm_drawdown_pct: 0, sell_stage_rearm_drawdown_pct: null },
  tasks: [{
    key: 't', portfolio_key: 't', portfolio_label: 'T', period_key: 'p', period_label: 'P',
    start: '2025-01-01', end: '2025-01-05', symbols: ['X.US'],
    targets: [{ symbol: 'X.US', weight: 100, name: 'X', max_drawdown_pct: 50 }]
  }],
  market_data: { symbols: { 'X.US': { dates: ['2025-01-01','2025-01-02','2025-01-03','2025-01-04','2025-01-05'],
    closes: [100, 90, 95, 110, 120] } } },
  buy_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...buyF],
  sell_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...sellF],
  candidate_schema: ['candidate_id', 'buy_variant_id', 'sell_variant_id'],
  buy_variants: [],
  sell_variants: [],
  candidate_rows: []
};

// Generate 50 candidates (GA default population size)
for (let i = 0; i < 50; i++) {
  p.buy_variants.push([i, 'buy' + i, 'pyramid_3', null,null,null,null,null,null,null,null,null,null,null]);
  p.sell_variants.push([i, 'sell' + i, 'none', null,null,null,null,null,null,null,null,null,null,null,null,null,null,false,null,null,null]);
  p.candidate_rows.push([i, i, i]);
}
(async () => {
  messages.length = 0;
  await context.initRun(p, 0, p.run_id, p.candidate_rows.length * p.tasks.length);
  await context.processBatch({ run_id: p.run_id, worker_index: 0, batch_id: 'b1', candidate_rows: p.candidate_rows }, 0, p.run_id);
  const done = messages.find(msg => msg.type === 'batch_done');
  if (!done) { process.stdout.write(JSON.stringify({error: 'no batch_done'})); return; }
  const allFinite = done.rows.every(row =>
    row.observations.every(o => isFinite(Number(o.return_pct)) && isFinite(Number(o.max_drawdown_pct)))
  );
  process.stdout.write(JSON.stringify({
    ok: true, row_count: done.rows.length,
    all_finite: allFinite,
    first_candidate_id: done.rows[0].candidate_id,
    last_candidate_id: done.rows[done.rows.length - 1].candidate_id
  }));
})().catch(err => { process.stdout.write(JSON.stringify({ error: err.message })); });
"""
        result = self._run_node_script(script)
        self.assertIn("ok", result)
        self.assertEqual(result["row_count"], 50, "Should process all 50 candidates")
        self.assertTrue(result["all_finite"], "All 50 candidates must have finite observations")
        self.assertEqual(result["first_candidate_id"], 0)
        self.assertEqual(result["last_candidate_id"], 49)

    def test_ga_packet_endpoint_returns_valid_structure(self):
        """S5 (surface): ga-packet endpoint returns all required fields for client-side GA."""
        import urllib.request, urllib.error
        req = urllib.request.Request(
            "http://127.0.0.1:5000/api/strategy-lab/parameter-lab/ga-packet",
            data=json.dumps({
                "ga_buy_strategy": "pyramid_3", "ga_sell_strategy": "none",
                "start": "2026-01-01", "end": "2026-05-01",
                "ga_population_size": 3, "ga_generations": 1
            }).encode(),
            headers={"Content-Type": "application/json"}
        )
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
        except Exception as e:
            self.skipTest(f"Server not reachable: {e}")
            return

        self.assertTrue(data.get("success"), f"ga-packet failed: {data.get('message')}")
        p = data["packet"]
        required = ["inputs", "tasks", "market_data", "registry",
                     "buy_variant_schema", "sell_variant_schema", "candidate_schema",
                     "buy_variants", "sell_variants", "candidate_rows",
                     "ga_config", "ga_parameter_ranges"]
        for field in required:
            self.assertIn(field, p, f"Missing field: {field}")

        self.assertEqual(len(p["candidate_rows"]), 3, "Should have 3 candidates")
        self.assertEqual(len(p["buy_variants"]), 3)
        self.assertEqual(len(p["sell_variants"]), 3)
        for row in p["buy_variants"]:
            self.assertEqual(len(row), len(p["buy_variant_schema"]))
        for row in p["sell_variants"]:
            self.assertEqual(len(row), len(p["sell_variant_schema"]))
        gc = p["ga_config"]
        for key in ["population_size", "generations", "mutation_rate", "crossover_rate", "continuous_mutation"]:
            self.assertIn(key, gc, f"ga_config missing: {key}")
        pr = p["ga_parameter_ranges"]
        self.assertIn("bounds", pr)
        self.assertIn("int_fields", pr)

    def test_equal_slice_grid_rebound_full_worker_lifecycle(self):
        """S6: equal_slice + grid_rebound (user's failing combo) through worker message-passing lifecycle."""
        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');

// Simulate the evaluatePopulationWithWorkers lifecycle with a real worker VM
const buyF = [
  'step_pct', 'equal_slice_allocation_pct', 'core_dip_initial_core_pct', 'core_dip_weekly_core_pct',
  'core_dip_cash_reserve_pct', 'core_dip_start_drawdown_pct', 'core_dip_full_drawdown_pct',
  'core_dip_timing_enabled', 'core_dip_timing_max_delay_days', 'core_dip_timing_rise_threshold_pct',
  'core_dip_timing_near_low_pct'
];
const sellF = [
  'sell_min_profit_pct', 'repair_sell_cooldown_days', 'repair_stage_sell_pct',
  'grid_rebound_step_pct', 'grid_sell_pct', 'grid_first_sell_pct', 'grid_second_sell_pct',
  'grid_min_sell_amount', 'cost_first_profit_pct', 'cost_second_profit_pct', 'cost_third_profit_pct',
  'cost_first_sell_pct', 'cost_second_sell_pct', 'cost_third_sell_pct', 'cost_deleverage_cooldown_days',
  'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
  'buy_rearm_mode', 'sell_stage_rearm_drawdown_pct'
];

const packet = {
  run_id: 'e2e-s6',
  inputs: { initial_cash: 20000, monthly_contribution: 1000, max_drawdown_pct: 50, drawdown_basis: 'ath',
    trade_fee: 0.35, hkd_to_usd: 0.128, reserve_position_pct: 40, sell_min_profit_pct: 10,
    sell_allow_same_day_sell: false, dca_rearm_drawdown_pct: 5, sell_stage_rearm_drawdown_pct: null },
  tasks: [{
    key: 'tsla_1y', portfolio_key: 'tsla_100', portfolio_label: 'TSLA', period_key: '1y', period_label: '1Y',
    start: '2025-06-01', end: '2025-06-15', symbols: ['TSLA.US'],
    targets: [{ symbol: 'TSLA.US', weight: 100, name: 'TSLA', max_drawdown_pct: 50 }]
  }],
  market_data: { symbols: { 'TSLA.US': {
    dates: ['2025-06-01','2025-06-02','2025-06-03','2025-06-04','2025-06-05',
            '2025-06-06','2025-06-07','2025-06-08','2025-06-09','2025-06-10',
            '2025-06-11','2025-06-12','2025-06-13','2025-06-14','2025-06-15'],
    closes: [200,195,190,185,180,175,180,190,200,210,220,215,210,205,200]
  } } },
  buy_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...buyF],
  sell_variant_schema: ['variant_id', 'variant_key', 'strategy_key', ...sellF],
  candidate_schema: ['candidate_id', 'buy_variant_id', 'sell_variant_id'],
  buy_variants: [],
  sell_variants: [],
  candidate_rows: [],
  diagnostics: { verbose_simulation_logs: false, progress_every: 1, progress_min_ms: 500, slow_simulation_ms: 3000 }
};

// Generate 10 equal_slice + grid_rebound candidates (matching user scenario)
for (let i = 0; i < 10; i++) {
  packet.buy_variants.push([i, 'buy' + i, 'equal_slice', 5 + i * 2, 5 + i * 3, null,null,null,null,null,null,null,null,null]);
  packet.sell_variants.push([i, 'sell' + i, 'grid_rebound', 10, null, null, 5 + i, 40, null, null, 200, null,null,null,null,null,null,null,false,null,null,null]);
  packet.candidate_rows.push([i, i, i]);
}

// Create worker VM and simulate the message-passing lifecycle
const runId = packet.run_id;
let settled = false, allRows = [], batchDoneCount = 0;
const totalCandidates = packet.candidate_rows.length;

// Simulate worker creation + start message
const messages = [];
const workerContext = {
  console: { info() {}, warn() {}, error() {} },
  postMessage(msg) { messages.push(msg); },
  performance: { now: () => 0 },
  setTimeout,
  importScripts() {},
};
workerContext.self = workerContext;
vm.createContext(workerContext);
vm.runInContext(source, workerContext);

async function runLifecycle() {
  // Phase 1: start (equivalent to worker.postMessage({type:'start', packet}))
  await workerContext.initRun(packet, 0, runId, totalCandidates * packet.tasks.length);
  
  const readyMsgs = messages.filter(m => m.type === 'ready');
  if (!readyMsgs.length) return { error: 'No ready message from worker' };
  
  // Phase 2: batch (only after ready!)
  await workerContext.processBatch(
    { run_id: runId, worker_index: 0, batch_id: 'b1', candidate_rows: packet.candidate_rows },
    0, runId
  );
  
  const done = messages.find(m => m.type === 'batch_done');
  if (!done) return { error: 'No batch_done', msgTypes: messages.map(m => m.type) };
  
  const summary = done.rows.map(r => ({
    cid: r.candidate_id, obs_count: r.observations.length,
    ret: Number(r.observations[0]?.return_pct),
    dd: Number(r.observations[0]?.max_drawdown_pct),
    all_finite: r.observations.every(o => isFinite(Number(o.return_pct)) && isFinite(Number(o.max_drawdown_pct)))
  }));
  
  return {
    ok: true, row_count: done.rows.length,
    all_finite: summary.every(s => s.all_finite),
    any_nan: summary.some(s => !s.all_finite),
    candidates: summary
  };
}

runLifecycle().then(r => process.stdout.write(JSON.stringify(r)))
  .catch(e => process.stdout.write(JSON.stringify({error: e.message, stack: e.stack?.split('\n').slice(0,3)})));
"""
        result = self._run_node_script(script)
        self.assertIn("ok", result, f"Lifecycle failed: {result}")
        self.assertTrue(result["ok"])
        self.assertEqual(result["row_count"], 10, "Should process all 10 candidates")
        self.assertTrue(result["all_finite"], "All candidates must have finite observations")
        self.assertFalse(result.get("any_nan"), "No NaN values allowed")
        for c in result["candidates"]:
            self.assertTrue(c["all_finite"], f"candidate {c['cid']} has NaN")


if __name__ == "__main__":
    unittest.main()
