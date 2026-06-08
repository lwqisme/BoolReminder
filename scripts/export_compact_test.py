#!/usr/bin/env python3
"""Export GA-style compact test data for JS engine comparison.

Uses the SAME format as _compact_parameter_lab_task + market_data
so the JS inflateTask -> rebuildPricePoints path is exercised.
"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta, datetime
from drawdown.position_strategy import (
    build_longbridge_quote_context, fetch_longbridge_daily_candles,
    candle_datetime, build_price_points_from_series,
)
from drawdown.strategy_lab_config import StrategyLabConfig

preset_path = "data/strategy_lab/presets/20260608141716_130f73dd.json"
preset = json.load(open(preset_path))
cp = dict(preset["config_payload"])

# Fix key mapping
cp2 = {}
for k, v in cp.items():
    if k.startswith("default_"):
        cp2[k[8:]] = v
    else:
        cp2[k] = v
cp2["step_pct"] = cp2.get("slice_step_pct", cp2.get("step_pct", 5))
cp2["initial_cash"] = 50000
cp2["monthly_contribution"] = 0

lab = StrategyLabConfig.from_runtime_payload(cp2, None)
inputs = lab.to_strategy_inputs()

today = date.today()
symbol = "NVDA.US"
quote_ctx = build_longbridge_quote_context()

for years, label in [(1, "1y"), (3, "3y"), (5, "5y")]:
    start = today - timedelta(days=365 * years)
    candles = fetch_longbridge_daily_candles(quote_ctx, symbol, start, today)
    series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
    points = build_price_points_from_series(series)

    # Export in COMPACT format: dates + closes arrays (not pre-built price_points)
    # This forces JS inflateTask to call rebuildPricePoints which computes
    # drawdown_ath, drawdown_120, etc.
    dates = [p.date.date().isoformat() for p in points]
    closes = [p.close for p in points]

    # Task in compact format (no price_points - they're in market_data)
    task = {
        "key": f"nvda_{label}",
        "symbols": [symbol],
        "targets": [
            {"symbol": symbol, "weight": 100, "name": "NVDA", "max_drawdown_pct": 50}
        ],
        "start": start.isoformat(),
        "end": today.isoformat(),
        "portfolio_key": "nvda_100",
        "portfolio_label": "NVDA",
        "period_key": label,
        "period_label": f"{years}年",
    }

    # Market data in compact format: {symbols: {SYM: {dates: [...], closes: [...]}}}
    market_data = {
        "symbols": {
            symbol: {
                "dates": dates,
                "closes": closes,
            }
        }
    }

    base_inputs = {
        "initial_cash": inputs.initial_cash,
        "monthly_contribution": inputs.monthly_contribution,
        "max_drawdown_pct": inputs.max_drawdown_pct,
        "drawdown_basis": inputs.drawdown_basis,
        "step_pct": inputs.step_pct,
        "equal_slice_allocation_pct": inputs.equal_slice_allocation_pct,
        "trade_fee": inputs.trade_fee,
        "hkd_to_usd": inputs.hkd_to_usd,
        "reserve_position_pct": inputs.reserve_position_pct,
        "sell_min_profit_pct": inputs.sell_min_profit_pct,
        "repair_sell_cooldown_days": inputs.repair_sell_cooldown_days,
        "repair_stage_sell_pct": inputs.repair_stage_sell_pct,
        "dca_rearm_drawdown_pct": inputs.dca_rearm_drawdown_pct,
        "sell_stage_rearm_drawdown_pct": inputs.sell_stage_rearm_drawdown_pct,
        "grid_rebound_step_pct": inputs.grid_rebound_step_pct,
        "grid_sell_pct": inputs.grid_sell_pct,
        "grid_first_sell_pct": inputs.grid_first_sell_pct,
        "grid_second_sell_pct": inputs.grid_second_sell_pct,
        "grid_min_sell_amount": inputs.grid_min_sell_amount,
        "grid_rebound_cycle_reset": inputs.grid_rebound_cycle_reset,
        "cost_first_profit_pct": inputs.cost_first_profit_pct,
        "cost_second_profit_pct": inputs.cost_second_profit_pct,
        "cost_third_profit_pct": inputs.cost_third_profit_pct,
        "cost_first_sell_pct": inputs.cost_first_sell_pct,
        "cost_second_sell_pct": inputs.cost_second_sell_pct,
        "cost_third_sell_pct": inputs.cost_third_sell_pct,
        "cost_deleverage_cooldown_days": inputs.cost_deleverage_cooldown_days,
        "sell_allow_same_day_sell": inputs.sell_allow_same_day_sell,
        "cost_min_sell_amount": inputs.cost_min_sell_amount,
        "buy_rearm_mode": inputs.buy_rearm_mode,
        "core_dip_initial_core_pct": inputs.core_dip_initial_core_pct,
        "core_dip_weekly_core_pct": inputs.core_dip_weekly_core_pct,
        "core_dip_cash_reserve_pct": inputs.core_dip_cash_reserve_pct,
        "core_dip_start_drawdown_pct": inputs.core_dip_start_drawdown_pct,
        "core_dip_full_drawdown_pct": inputs.core_dip_full_drawdown_pct,
        "core_dip_timing_enabled": inputs.core_dip_timing_enabled,
        "core_dip_timing_max_delay_days": inputs.core_dip_timing_max_delay_days,
        "core_dip_timing_rise_threshold_pct": inputs.core_dip_timing_rise_threshold_pct,
        "core_dip_timing_near_low_pct": inputs.core_dip_timing_near_low_pct,
    }

    candidate = {
        "buy_strategy": "equal_slice",
        "sell_strategy": "price_rise_grid",
        "step_pct": inputs.step_pct,
        "equal_slice_allocation_pct": inputs.equal_slice_allocation_pct,
        "sell_min_profit_pct": inputs.sell_min_profit_pct,
        "grid_rebound_step_pct": inputs.grid_rebound_step_pct,
        "grid_sell_pct": inputs.grid_sell_pct,
        "grid_min_sell_amount": inputs.grid_min_sell_amount,
        "sell_allow_same_day_sell": inputs.sell_allow_same_day_sell,
        "candidate_id": "test",
        "key": "equal_slice__price_rise_grid",
    }

    test_data = {
        "label": f"NVDA {years}年",
        "start": start.isoformat(),
        "end": today.isoformat(),
        "task": task,
        "market_data": market_data,
        "baseInputs": base_inputs,
        "candidate": candidate,
    }

    out_path = f"data/js_test_compact_{label}.json"
    with open(out_path, "w") as f:
        json.dump(test_data, f, indent=2) if years == 3 else json.dump(test_data, f)
    print(f"Wrote {out_path} ({len(dates)} price points)")

# Also run Python for comparison
from drawdown.position_strategy import simulate_portfolio, parse_portfolio_targets

print()
print("=" * 60)
print("Python engine (for comparison):")
for years in [1, 3, 5]:
    start = today - timedelta(days=365 * years)
    targets = parse_portfolio_targets([{"symbol": symbol, "weight": 100, "name": "NVDA", "max_drawdown_pct": 50}])
    candles = fetch_longbridge_daily_candles(quote_ctx, symbol, start, today)
    series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
    pp = {symbol: build_price_points_from_series(series)}
    result = simulate_portfolio(pp, targets, inputs, strategies=["equal_slice"], sell_strategies=["price_rise_grid"])
    s = result["strategies"][0]
    m = s["metrics"]
    print(f"  {years}年: ret={m['return_pct']:.2f}% profit=${m['profit']:,.0f} dd={m['max_drawdown_pct']:.2f}% trades={m['trade_count']}")
