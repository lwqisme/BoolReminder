import json
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

    def test_option_cell_scan_sends_dte_window_and_stock_inputs_to_worker(self):
        html = PARAMETER_LAB_HTML.read_text(encoding="utf-8")

        self.assertIn("payload.option_dte_min = optVal('optDteMin')", html)
        self.assertIn("payload.option_dte_target = optVal('optDteTarget')", html)
        self.assertIn("payload.option_dte_max = optVal('optDteMax')", html)
        self.assertIn("option_dte_min: optVal('optDteMin')", html)
        self.assertIn("option_dte_target: optVal('optDteTarget')", html)
        self.assertIn("option_dte_max: optVal('optDteMax')", html)
        self.assertIn("stock_inputs: optionPacket.stock_inputs", html)


if __name__ == "__main__":
    unittest.main()
