#!/usr/bin/env python3
"""诊断：预设回测参数映射问题

预期：预设「交易质量高2」的 equal_slice/price_rise_grid 搭配 50k/0 资金，
     应给出 ~400% 的 GA 优化结果
实际：只显示 ~54%

问题定位：StrategyLabConfig.from_runtime_payload 与预设存储的 key 命名不匹配。
"""

import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
from drawdown.position_strategy import (
    simulate_portfolio, parse_portfolio_targets,
    build_longbridge_quote_context, fetch_longbridge_daily_candles,
    candle_datetime, build_price_points_from_series,
)
from drawdown.strategy_lab_config import StrategyLabConfig

PRESET_ID = "20260531164843_9081d4e7"
preset_path = f"data/strategy_lab/presets/{PRESET_ID}.json"
preset = json.load(open(preset_path))
cp = dict(preset["config_payload"])

end_date = date.today()
start_date = end_date - timedelta(days=365 * 3)

default_targets = [
    {"symbol": "TSM.US", "weight": 40.0, "name": "TSM", "max_drawdown_pct": 40.0},
    {"symbol": "GOOGL.US", "weight": 30.0, "name": "GOOGL", "max_drawdown_pct": 40.0},
    {"symbol": "TSLA.US", "weight": 20.0, "name": "TSLA", "max_drawdown_pct": 50.0},
    {"symbol": "0700.HK", "weight": 10.0, "name": "Tencent", "max_drawdown_pct": 50.0},
]
targets = parse_portfolio_targets(default_targets)

print("=" * 70)
print("诊断：预设回测参数映射")
print(f"预设: {preset['name']} ({PRESET_ID})")
print(f"日期: {start_date} ~ {end_date}")
print(f"标的: {', '.join(t.symbol for t in targets)}")
print()

# ── 场景1: 当前 API 实际行为 ──
print("── 场景1: API 当前实际行为 ──")
print("  from_runtime_payload 读取 'initial_cash' 等不带 default_ 前缀的 key")
print("  但预设存储的是 'default_initial_cash' → 全部参数回退到系统默认值")
lab = StrategyLabConfig.from_runtime_payload(cp, None)
inputs = lab.to_strategy_inputs()
print(f"  使用的参数: cash={inputs.initial_cash}, monthly={inputs.monthly_contribution}")
print(f"    step={inputs.step_pct}, alloc={inputs.equal_slice_allocation_pct}")
print(f"    sell_min_profit={inputs.sell_min_profit_pct}, grid_rebound={inputs.grid_rebound_step_pct}")
print(f"    grid_sell_pct={inputs.grid_sell_pct}, same_day={inputs.sell_allow_same_day_sell}")

quote_ctx = build_longbridge_quote_context()
pp = {}
for t in targets:
    candles = fetch_longbridge_daily_candles(quote_ctx, t.symbol, start_date, end_date)
    series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
    pp[t.symbol] = build_price_points_from_series(series)

result = simulate_portfolio(pp, targets, inputs, strategies=["equal_slice"], sell_strategies=["price_rise_grid"])
s = result["strategies"][0]
m = s["metrics"]
print(f"  结果: ret={m['return_pct']:.2f}% profit=${m['profit']:,.0f} dd={m['max_drawdown_pct']:.2f}% trades={m['trade_count']}")
print()

# ── 场景2: 用户通过UI覆盖50k/0 ──
print("── 场景2: UI 覆盖 50k/0 (但其他参数仍是默认) ──")
cp2 = dict(cp)
cp2["initial_cash"] = 50000
cp2["monthly_contribution"] = 0
lab = StrategyLabConfig.from_runtime_payload(cp2, None)
inputs = lab.to_strategy_inputs()
print(f"  使用的参数: cash={inputs.initial_cash}, monthly={inputs.monthly_contribution}")
print(f"    step={inputs.step_pct}, alloc={inputs.equal_slice_allocation_pct}")
print(f"    sell_min_profit={inputs.sell_min_profit_pct}, grid_rebound={inputs.grid_rebound_step_pct}")
result = simulate_portfolio(pp, targets, inputs, strategies=["equal_slice"], sell_strategies=["price_rise_grid"])
s = result["strategies"][0]
m = s["metrics"]
print(f"  结果: ret={m['return_pct']:.2f}% profit=${m['profit']:,.0f} dd={m['max_drawdown_pct']:.2f}% trades={m['trade_count']}")
print()

# ── 场景3: 完整映射预设参数 ──
print("── 场景3: 完整映射预设参数 (手动修复 key 不匹配) ──")
cp3 = {}
for k, v in cp.items():
    if k.startswith("default_"):
        cp3[k[8:]] = v  # 去掉 default_ 前缀
    else:
        cp3[k] = v
cp3["step_pct"] = cp3.get("slice_step_pct", cp3.get("step_pct", 5))
cp3["initial_cash"] = 50000
cp3["monthly_contribution"] = 0

lab = StrategyLabConfig.from_runtime_payload(cp3, None)
inputs = lab.to_strategy_inputs()
print(f"  使用的参数: cash={inputs.initial_cash}, monthly={inputs.monthly_contribution}")
print(f"    step={inputs.step_pct}, alloc={inputs.equal_slice_allocation_pct}")
print(f"    sell_min_profit={inputs.sell_min_profit_pct}, grid_rebound={inputs.grid_rebound_step_pct}")
print(f"    grid_sell_pct={inputs.grid_sell_pct}, grid_min_sell={inputs.grid_min_sell_amount}")
print(f"    same_day_sell={inputs.sell_allow_same_day_sell}, dca_rearm={inputs.dca_rearm_drawdown_pct}")
print(f"    sell_stage_rearm={inputs.sell_stage_rearm_drawdown_pct}")
result = simulate_portfolio(pp, targets, inputs, strategies=["equal_slice"], sell_strategies=["price_rise_grid"])
s = result["strategies"][0]
m = s["metrics"]
print(f"  结果: ret={m['return_pct']:.2f}% profit=${m['profit']:,.0f} dd={m['max_drawdown_pct']:.2f}% trades={m['trade_count']}")
print()

# ── 对比总结 ──
print("=" * 70)
print("总结")
print("=" * 70)
print("""
问题1: 预设存储用 'default_xxx' key，但 from_runtime_payload 读取 'xxx'
      → 预设保存的所有参数值都被忽略，回退到系统默认值

问题2: UI 覆盖 initial_cash/monthly_contribution 时，API 代码设置的是
      config_payload["default_initial_cash"]，同样不会被读取
      → 用户自定义资金也无效

问题3: GA 优化的参数 (sell_min_profit=30, grid_sell_pct=10, alloc=17.96 等)
      完全没有被使用
      → 场景3 显示用正确参数可得 208%，接近 GA 的结果

问题4: GA 标的 vs 预设标的不同
      → GA 可能针对特定股票运行，但预设不存储 targets
      → 预设回退到默认组合 (TSM/GOOGL/TSLA/0700)
""")
