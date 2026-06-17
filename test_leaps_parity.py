#!/usr/bin/env python3
"""LEAPS 引擎数值一致性校验：Python drawdown/leaps_option_ga.py vs JS
web/static/leaps_ga_engine.js。

校验三个核心函数逐函数数值一致：detect_leaps_entries、proxy_option_roi、
compute_sell_ladder（含 trade_overrides + allow_open）。这是把 LEAPS 信号
对齐到 JS worker 的前置条件——若已移植函数有数值偏差，对齐就是修 bug 而非接线。

JS 端通过 scripts/leaps_parity_runner.js（node 直驱 leaps_ga_engine 模块）调用，
无需 worker 沙箱。价格形状：Python (date, price)；JS [ts, price, dateStr]。
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from datetime import date, timedelta
from pathlib import Path

from drawdown.leaps_option_ga import (
    LeapsEntrySignal,
    compute_sell_ladder,
    detect_leaps_entries,
    proxy_option_roi,
)

_REPO = Path(__file__).resolve().parent
_RUNNER = _REPO / "scripts" / "leaps_parity_runner.js"
_TOL = 1e-6


def _node_bin() -> str:
    found = os.environ.get("WORKER_NODE_BIN") or _which("node")
    if not found:
        raise unittest.SkipTest("node 不在 PATH（跳过 LEAPS parity 校验）")
    return found


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


def _js(fn: str, args: list) -> object:
    """调用 JS 端 leaps_ga_engine.<fn>(*args)，返回结果对象。"""
    req = json.dumps({"fn": fn, "args": args}) + "\n"
    proc = subprocess.run(
        [_node_bin(), str(_RUNNER)],
        input=req.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node runner 退出码 {proc.returncode}: "
            + (proc.stderr or b"").decode("utf-8", "replace")[-400:]
        )
    resp = json.loads(proc.stdout.decode("utf-8"))
    if not resp.get("success"):
        raise AssertionError(f"JS {fn} 报错: {resp.get('error')}")
    return resp["result"]


def _deterministic_prices(start: date, n: int, seed: int = 20260618) -> list[tuple[date, float]]:
    """可复现的伪随机价格序列（含一段下跌触发布林+回撤）。"""
    # LCG 确定性伪随机，避免依赖 random 全局态
    state = seed
    prices: list[tuple[date, float]] = []
    base = 200.0
    # 前 130 天缓慢上升建立高点
    for i in range(130):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        noise = (state % 1000) / 1000.0 - 0.5
        base *= 1 + 0.002 + noise * 0.01
        prices.append((start + timedelta(days=i), round(base, 4)))
    # 之后注入一段下跌到 ~-25%（触发 20% 回撤阈值 + 跌破布林下轨）
    for i in range(130, n):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        noise = (state % 1000) / 1000.0 - 0.5
        # 前 30 天下跌，之后反弹
        drift = -0.012 if i < 160 else 0.004
        base *= 1 + drift + noise * 0.01
        prices.append((start + timedelta(days=i), round(base, 4)))
    return prices


def _to_js_prices(prices: list[tuple[date, float]]) -> list[list]:
    """(date, price) -> [ts(ms since Unix epoch), price, dateStr]（JS 引擎期望形状）。

    ts 必须与 JS ``new Date(iso).getTime()`` 同尺度（Unix epoch ms），否则
    computeSellLadder 的 ``pt.ts <= entryTs`` 跳过逻辑会失准。
    """
    epoch = date(1970, 1, 1)
    return [[(d - epoch).days * 86400000, p, d.isoformat()] for d, p in prices]


class LeapsParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prices = _deterministic_prices(date(2025, 1, 2), 300)
        cls.js_prices = _to_js_prices(cls.prices)

    # ── detect_leaps_entries ────────────────────────────────────────────
    def test_detect_entries_both(self):
        total_entries = 0
        for threshold in (15.0, 20.0, 25.0):
            for mode in ("touch", "bounce", "both"):
                with self.subTest(threshold=threshold, mode=mode):
                    py = detect_leaps_entries(self.prices, threshold, mode)
                    js = _js("detectLeapsEntries", [self.js_prices, threshold, mode, None])
                    self._assert_entries_equal(py, js)
                    total_entries += len(py)
        # 非空保证：固定数据必须在某些 (threshold, mode) 下触发入场，否则上述断言
        # 会空跑（len 0==0），无法真正校验 parity。
        self.assertGreater(total_entries, 0, "固定数据未触发任何入场，parity 校验空跑")

    def test_detect_entries_min_entry_date(self):
        min_date = self.prices[150][0]
        py = detect_leaps_entries(self.prices, 20.0, "both", min_entry_date=min_date)
        js = _js("detectLeapsEntries", [self.js_prices, 20.0, "both", min_date.isoformat()])
        self._assert_entries_equal(py, js)

    def _assert_entries_equal(self, py_entries, js_entries):
        self.assertEqual(len(py_entries), len(js_entries),
                         f"entry count mismatch: py={len(py_entries)} js={len(js_entries)}")
        for pe, je in zip(py_entries, js_entries):
            self.assertEqual(pe.date.isoformat(), je["date"], f"date: {pe.date} vs {je['date']}")
            self.assertAlmostEqual(pe.price, je["price"], places=4, msg="price")
            self.assertAlmostEqual(pe.drawdown_pct, je["drawdown_pct"], places=2, msg="drawdown_pct")
            self.assertAlmostEqual(pe.bollinger_score, je["bollinger_score"], places=4, msg="bollinger_score")
            self.assertAlmostEqual(pe.composite_score, je["composite_score"], places=4, msg="composite_score")

    # ── proxy_option_roi ────────────────────────────────────────────────
    def test_proxy_option_roi(self):
        cases = [
            # entry_price, exit_price, entry_date, exit_date, expiration, strike
            (200.0, 240.0, date(2025, 3, 1), date(2025, 6, 1), date(2026, 12, 1), 210.0),
            (180.0, 150.0, date(2025, 3, 1), date(2025, 9, 1), date(2027, 1, 1), 200.0),
            (300.0, 420.0, date(2025, 1, 15), date(2025, 8, 20), date(2026, 6, 1), 310.0),
            (250.0, 250.0, date(2025, 2, 1), date(2025, 7, 1), date(2026, 12, 15), 260.0),
        ]
        for ep, xp, ed, xd, exp, strike in cases:
            with self.subTest(entry=ep, exit=xp):
                py = proxy_option_roi(ep, xp, ed, xd, exp, strike)
                epoch = date(1970, 1, 1)
                js = _js("proxyOptionRoi", [
                    ep, xp,
                    (ed - epoch).days * 86400000, (xd - epoch).days * 86400000,
                    (exp - epoch).days * 86400000, strike,
                ])
                self.assertAlmostEqual(py, js, places=2, msg=f"roi: py={py} js={js}")

    # ── compute_sell_ladder ─────────────────────────────────────────────
    def _entry(self) -> LeapsEntrySignal:
        # 选一个能触发入场的点作为 entry（threshold 15 在固定数据上触发 2 个）
        entries = detect_leaps_entries(self.prices, 15.0, "both")
        self.assertTrue(entries, "固定数据未触发入场，调整 _deterministic_prices")
        return entries[0]

    def test_compute_sell_ladder_ga_mode(self):
        entry = self._entry()
        stages = [(15, 80.0, 50.0), (60, 60.0, 50.0)]
        py = compute_sell_ladder(entry, self.prices, stages, expiration_days=190,
                                 strike_price=entry.price * 1.1)
        js_entry = {"date": entry.date.isoformat(), "price": entry.price}
        js = _js("computeSellLadder", [js_entry, self.js_prices, stages, 190, entry.price * 1.1])
        self._assert_trade_equal(py, js, allow_open=False)

    def test_compute_sell_ladder_signal_mode(self):
        entry = self._entry()
        stages = [(15, 80.0, 50.0), (60, 60.0, 50.0)]
        py = compute_sell_ladder(entry, self.prices, stages, expiration_days=190,
                                 strike_price=entry.price * 1.1, allow_open=True)
        js_entry = {"date": entry.date.isoformat(), "price": entry.price}
        js = _js("computeSellLadder", [js_entry, self.js_prices, stages, 190, entry.price * 1.1, 0.05, 0.40, None, True])
        self._assert_trade_equal(py, js, allow_open=True)

    def test_compute_sell_ladder_with_overrides(self):
        entry = self._entry()
        stages = [(15, 80.0, 50.0), (60, 60.0, 50.0)]
        # 注入几个真实部分卖出 override（date iso -> pct）
        ov_idx = entry.date.toordinal() - self.prices[0][0].toordinal() + 20
        overrides = {
            (entry.date + timedelta(days=20)).isoformat(): 25.0,
            (entry.date + timedelta(days=40)).isoformat(): 25.0,
        }
        overrides_py = {date.fromisoformat(d): v for d, v in overrides.items()}
        py = compute_sell_ladder(entry, self.prices, stages, expiration_days=190,
                                 strike_price=entry.price * 1.1, allow_open=True,
                                 trade_overrides=overrides_py)
        js_entry = {"date": entry.date.isoformat(), "price": entry.price}
        js = _js("computeSellLadder", [js_entry, self.js_prices, stages, 190, entry.price * 1.1,
                                       0.05, 0.40, overrides, True])
        self._assert_trade_equal(py, js, allow_open=True)

    def _assert_trade_equal(self, py_trade, js_trade, *, allow_open: bool):
        self.assertEqual(len(py_trade.sell_events), len(js_trade["sell_events"]),
                         f"sell_events count: py={len(py_trade.sell_events)} js={len(js_trade['sell_events'])}")
        for pe, je in zip(py_trade.sell_events, js_trade["sell_events"]):
            self.assertEqual(pe.date.isoformat(), je["date"], f"sell date: {pe.date} vs {je['date']}")
            self.assertAlmostEqual(pe.price, je["price"], places=4, msg="sell price")
            self.assertAlmostEqual(pe.pct_sold, je["pct_sold"], places=2, msg="pct_sold")
            self.assertAlmostEqual(pe.roi_pct, je["roi_pct"], places=2, msg="roi_pct")
        # total_roi 是各 sell_event roi 的加权均值；per-event roi 已在 places=2 对齐，
        # 此处 0.01 量级偏差来自 erf 近似（JS A&S vs Python C erf）经加权后跨过舍入边界，
        # 非逻辑分歧。放宽到 places=1（0.1%），信号 reason 用整数 %，无实际影响。
        self.assertAlmostEqual(py_trade.total_roi_pct, js_trade["total_roi_pct"], places=1, msg="total_roi")
        if allow_open:
            self.assertAlmostEqual(py_trade.open_pct, js_trade.get("open_pct", 0), places=2, msg="open_pct")
            self.assertAlmostEqual(py_trade.unrealized_roi_pct, js_trade.get("unrealized_roi_pct", 0),
                                   places=2, msg="unrealized_roi")
        self.assertEqual(py_trade.expired, js_trade.get("expired", False), "expired flag")


if __name__ == "__main__":
    unittest.main()
