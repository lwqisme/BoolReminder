#!/usr/bin/env python3
import json
from datetime import datetime, date, time
from pathlib import Path
from drawdown.position_strategy import (
    simulate_portfolio,
    StrategyInputs,
    PortfolioTarget,
)
from drawdown.generate_drawdown_report import (
    build_price_points_from_series,
)

def run():
    path = Path("data/longbridge_daily_candles/GOOGL.US.json")
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    raw_candles = payload.get("candles", [])
    start_date = date(2025, 5, 28)
    end_date = date(2026, 5, 28)

    filtered_candles = []
    for item in raw_candles:
        candle_day = date.fromisoformat(item["date"])
        if start_date <= candle_day <= end_date:
            filtered_candles.append(item)

    series = [
        (datetime.combine(date.fromisoformat(item["date"]), time.min), float(item["close"]))
        for item in filtered_candles
    ]
    series.sort(key=lambda x: x[0])
    points = build_price_points_from_series(series)

    inputs = StrategyInputs(
        initial_cash=100000.0,
        max_drawdown_pct=40.0,
        drawdown_basis="ath",
        step_pct=2.0,
        equal_slice_allocation_pct=12.0,
        trade_fee=0.35,
        hkd_to_usd=1.0,
        reserve_position_pct=0.0,
        sell_min_profit_pct=15.0, # 15% 最小盈利
        grid_rebound_step_pct=10.0, # 网格回弹 10% 步长
        grid_sell_pct=15.0, # 每档 15% 卖出
        grid_second_sell_pct=15.0,
        grid_min_sell_amount=0.0,
        grid_rebound_cycle_reset=1.0,
        sell_allow_same_day_sell=True, # 买入日可卖
    )

    targets = [
        PortfolioTarget(symbol="GOOGL.US", weight=100.0, name="GOOGL", max_drawdown_pct=40.0)
    ]

    result = simulate_portfolio(
        price_points_by_symbol={"GOOGL.US": points},
        targets=targets,
        inputs=inputs,
        strategies=("equal_slice",),
        sell_strategies=("grid_rebound",),
    )

    strategy_result = result["strategies"][0]
    trades = strategy_result.get("trades", [])
    print(f"\n--- Strategy simulation finished ---")
    print(f"Total trades: {len(trades)}")
    buy_trades = [t for t in trades if t["action"] == "buy"]
    sell_trades = [t for t in trades if t["action"] == "sell"]
    print(f"Buy trades: {len(buy_trades)}, Sell trades: {len(sell_trades)}")

    # Print first few trades
    print("\nTrades detail:")
    for t in trades:
        print(f"Date: {t['date']}, Action: {t['action']}, Price: {t['price']:.2f}, Shares: {t['shares']:.2f}, Drawdown: {t.get('drawdown_pct', 0.0):.2f}%")

if __name__ == "__main__":
    run()