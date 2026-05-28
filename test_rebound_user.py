#!/usr/bin/env python3
import json
from datetime import datetime, date, time
from pathlib import Path
from drawdown.position_strategy import simulate_portfolio, StrategyInputs, PortfolioTarget
from drawdown.generate_drawdown_report import build_price_points_from_series

def run():
    path = Path("data/longbridge_daily_candles/GOOGL.US.json")
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    raw_candles = payload.get("candles", [])
    start_date = date(2025, 5, 28)
    end_date = date(2026, 5, 28)

    filtered_candles = [
        item for item in raw_candles
        if start_date <= date.fromisoformat(item["date"]) <= end_date
    ]

    series = [
        (datetime.combine(date.fromisoformat(item["date"]), time.min), float(item["close"]))
        for item in filtered_candles
    ]
    series.sort(key=lambda x: x[0])
    points = build_price_points_from_series(series)

    # User's exact parameters
    inputs = StrategyInputs(
        initial_cash=100000.0,
        max_drawdown_pct=40.0,       # reasonable default
        drawdown_basis="ath",
        step_pct=2.5,                # 步长 2.5%
        equal_slice_allocation_pct=10.0,  # 每步 10%
        trade_fee=0.35,
        hkd_to_usd=1.0,
        reserve_position_pct=25.0,   # default 25% 底仓
        sell_min_profit_pct=15.0,    # 15% 最小盈利
        grid_rebound_step_pct=10.0,  # 网格回弹 10% 步长
        grid_sell_pct=15.0,          # 每档 15% 卖出
        grid_second_sell_pct=15.0,
        grid_min_sell_amount=200.0,  # default $200
        grid_rebound_cycle_reset=1.0,  # 周期重启1
        sell_stage_rearm_drawdown_pct=15.0,  # 卖档重启 15%回撤
        sell_allow_same_day_sell=True,  # 买入日可卖
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
    buy_trades = [t for t in trades if t["action"] == "buy"]
    sell_trades = [t for t in trades if t["action"] == "sell"]
    print(f"Total trades: {len(trades)}")
    print(f"Buy trades: {len(buy_trades)}, Sell trades: {len(sell_trades)}")
    print(f"\nMetrics: {json.dumps(strategy_result['metrics'], indent=2, default=str)}")

    print("\n--- All Trades ---")
    for t in trades:
        extra = ""
        if t["action"] == "sell":
            extra = f", Stage: {t.get('sell_stage', 'N/A')}"
        elif t["action"] == "buy":
            extra = f", Threshold: {t.get('threshold_pct', 0):.2f}%"
            if t.get("sell_cycle_rearmed"):
                extra += " [SELL CYCLE REARMED]"
        print(f"  {t['date']}  {t['action']:4s}  ${t['price']:.2f}  shares={t['shares']:.2f}  DD={t.get('drawdown_pct', 0):.2f}%{extra}")

    # Also print buy drawdowns for analysis
    print("\n--- Buy Drawdown Distribution ---")
    buy_dds = [t.get("drawdown_pct", 0) for t in buy_trades]
    if buy_dds:
        print(f"  Min buy DD: {min(buy_dds):.2f}%, Max buy DD: {max(buy_dds):.2f}%, Avg: {sum(buy_dds)/len(buy_dds):.2f}%")
    
    # Print price range
    prices = [t["price"] for t in trades]
    print(f"\n--- Price Range ---")
    print(f"  Min: ${min(prices):.2f}, Max: ${max(prices):.2f}")

if __name__ == "__main__":
    run()
