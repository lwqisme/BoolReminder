"""
JS vs Python LEAPS compute_sell_ladder alignment tests.

JS engine is ground truth. Python must produce identical sell_events.
"""

import json
import subprocess
import unittest
from datetime import date
from pathlib import Path

LEAPS_ENGINE_JS = Path(__file__).resolve().parent / "web/static/leaps_ga_engine.js"


class LeapsSellLadderAlignmentTest(unittest.TestCase):
    """Compare JS computeSellLadder vs Python compute_sell_ladder."""

    @classmethod
    def setUpClass(cls):
        golden_path = Path(__file__).resolve().parent / "test_data" / "leaps_signal_golden.json"
        with open(golden_path) as f:
            cls.golden = json.load(f)

    def _run_js_sell_ladder(self, window_key: str) -> list[dict]:
        """Run JS computeSellLadder and return sell_events."""
        ga = self.golden["expected_ga_results"][window_key]
        prices_raw = self.golden["price_slices"][window_key]
        stages = self.golden["stages"]

        # Let JS handle date parsing — pass date strings, not timestamps
        price_entries = [[d_str, p] for d_str, p in prices_raw]

        script = f"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const ctx = {{ console: {{ log() {{}}, info() {{}}, warn() {{}}, error() {{}} }} }};
const sandbox = vm.createContext(ctx);
vm.runInContext(source, sandbox);

const entry = {{ date: '{ga["entry_date"]}', price: {ga["entry_price"]} }};
const stages = {json.dumps(stages)};

// Build price array as [ts, price, dateStr] — let JS compute timestamps
const prices = {json.dumps(price_entries)}.map(([d, p]) => {{
  const ts = new Date(d + 'T00:00:00Z').getTime();
  return [ts, p, d];
}});

const result = sandbox.computeSellLadder(entry, prices, stages);
process.stdout.write(JSON.stringify(result.sell_events));
"""
        result = subprocess.run(
            ["node", "-e", script, str(LEAPS_ENGINE_JS)],
            check=True, capture_output=True, text=True,
        )
        return json.loads(result.stdout)

    def _run_py_sell_ladder(self, window_key: str) -> list[dict]:
        """Run Python compute_sell_ladder and return sell_events as dicts."""
        from drawdown.leaps_option_ga import compute_sell_ladder, LeapsEntrySignal

        ga = self.golden["expected_ga_results"][window_key]
        prices = [(date.fromisoformat(d), p) for d, p in self.golden["price_slices"][window_key]]
        stages = [(int(d), float(p), float(s)) for d, p, s in self.golden["stages"]]

        entry = LeapsEntrySignal(
            date=date.fromisoformat(ga["entry_date"]),
            price=ga["entry_price"],
            drawdown_pct=0.0, bollinger_score=0.0, composite_score=0.0,
        )

        trade = compute_sell_ladder(entry, prices, stages)
        return [
            {"date": str(se.date), "price": se.price, "pct_sold": se.pct_sold, "roi_pct": se.roi_pct}
            for se in trade.sell_events
        ]

    # ── Tests ─────────────────────────────────────────────────────

    def test_window_1_sell_events_match(self):
        """Window 1: force-sell at cutoff."""
        js = self._run_js_sell_ladder("window_1")
        py = self._run_py_sell_ladder("window_1")
        self._assert_same(js, py, "window_1")

    def test_window_2_sell_events_match(self):
        """Window 2: S1, S2, S3 trigger on different days."""
        js = self._run_js_sell_ladder("window_2")
        py = self._run_py_sell_ladder("window_2")
        self._assert_same(js, py, "window_2")

    def test_window_3_sell_events_match(self):
        """Window 3: S1+S2 same day, S3 later."""
        js = self._run_js_sell_ladder("window_3")
        py = self._run_py_sell_ladder("window_3")
        self._assert_same(js, py, "window_3")

    def _assert_same(self, js: list, py: list, label: str):
        diffs = []
        max_len = max(len(js), len(py))
        for i in range(max_len):
            j = js[i] if i < len(js) else None
            p = py[i] if i < len(py) else None
            if j and p:
                same = (
                    j["date"] == p["date"]
                    and abs(float(j["price"]) - float(p["price"])) < 0.01
                    and abs(float(j["pct_sold"]) - float(p["pct_sold"])) < 0.01
                    and abs(float(j["roi_pct"]) - float(p["roi_pct"])) < 0.05
                )
                if not same:
                    diffs.append(f"  [{i}] JS: {j['date']} {j['pct_sold']}% @{j['price']} ROI={j['roi_pct']}%")
                    diffs.append(f"  [{i}] PY: {p['date']} {p['pct_sold']}% @{p['price']} ROI={p['roi_pct']}%")
            elif j:
                diffs.append(f"  [{i}] JS only: {j['date']} {j['pct_sold']}% @{j['price']}")
            else:
                diffs.append(f"  [{i}] PY only: {p['date']} {p['pct_sold']}% @{p['price']}")
        if diffs:
            self.fail(f"{label}: {len(diffs)//2} mismatches\n" + "\n".join(diffs[:20]))
    def test_window_2_with_trade_overrides_s1_partial(self):
        """JS computeSellLadder with trade_overrides: S1 partial 15% → remaining 16%."""
        ga = self.golden["expected_ga_results"]["window_2"]
        s1_date = ga["sell_events"][0]["date"]
        prices_raw = self.golden["price_slices"]["window_2"]
        stages = self.golden["stages"]

        script = f"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const ctx = {{ console: {{ log() {{}}, info() {{}}, warn() {{}}, error() {{}} }} }};
const sandbox = vm.createContext(ctx);
vm.runInContext(source, sandbox);

const entry = {{ date: '{ga["entry_date"]}', price: {ga["entry_price"]} }};
const stages = {json.dumps(stages)};
const prices = {json.dumps([[d, p] for d, p in prices_raw])}.map(([d, p]) => {{
  const ts = new Date(d + 'T00:00:00Z').getTime();
  return [ts, p, d];
}});

// S1 partially sold 15% on S1 date
const overrides = {{ '{s1_date}': 15.0 }};

const result = sandbox.computeSellLadder(entry, prices, stages, 190, null, 0.05, 0.40, overrides);
const s1Events = result.sell_events.filter(e => e.date === '{s1_date}');
process.stdout.write(JSON.stringify({{ events: s1Events, total: result.sell_events.length }}));
"""
        result = subprocess.run(
            ["node", "-e", script, str(LEAPS_ENGINE_JS)],
            check=True, capture_output=True, text=True,
        )
        data = json.loads(result.stdout)
        s1_events = data["events"]
        self.assertEqual(len(s1_events), 1, f"S1 day should have 1 sell event, got {len(s1_events)}")
        self.assertAlmostEqual(float(s1_events[0]["pct_sold"]), 16.0, delta=0.1,
            msg=f"Expected ~16% remaining, got {s1_events[0]['pct_sold']}%")

    def test_window_2_with_trade_overrides_s1_full_then_s2(self):
        """JS computeSellLadder with trade_overrides: S1 full → S2 triggers."""
        ga = self.golden["expected_ga_results"]["window_2"]
        s2_date = ga["sell_events"][1]["date"]
        prices_raw = self.golden["price_slices"]["window_2"]
        stages = self.golden["stages"]

        script = f"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const ctx = {{ console: {{ log() {{}}, info() {{}}, warn() {{}}, error() {{}} }} }};
const sandbox = vm.createContext(ctx);
vm.runInContext(source, sandbox);

const entry = {{ date: '{ga["entry_date"]}', price: {ga["entry_price"]} }};
const stages = {json.dumps(stages)};
const prices = {json.dumps([[d, p] for d, p in prices_raw])}.map(([d, p]) => {{
  const ts = new Date(d + 'T00:00:00Z').getTime();
  return [ts, p, d];
}});

// S1 fully sold 31% on its date
const s1Date = '{ga["sell_events"][0]["date"]}';
const overrides = {{ [s1Date]: 31.0 }};

const result = sandbox.computeSellLadder(entry, prices, stages, 190, null, 0.05, 0.40, overrides);
const s2Events = result.sell_events.filter(e => e.date === '{s2_date}');
process.stdout.write(JSON.stringify({{ events: s2Events, all: result.sell_events.map(e => e.date) }}));
"""
        result = subprocess.run(
            ["node", "-e", script, str(LEAPS_ENGINE_JS)],
            check=True, capture_output=True, text=True,
        )
        data = json.loads(result.stdout)
        s2_events = data["events"]
        self.assertEqual(len(s2_events), 1, f"S2 day should have 1 event, got {len(s2_events)}: {data}")
        self.assertAlmostEqual(float(s2_events[0]["pct_sold"]), 30.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
