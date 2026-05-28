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

    # Test with $10K (default)
    inputs = StrategyInputs(
        initial_cash=10000.0,        # DEFAULT $10K
        max_drawdown_pct=40.0,
        drawdown_basis="ath",
        step_pct=2.5,
        equal_slice_allocation_pct=10.0,
        trade_fee=0.35,
        hkd_to_usd=1.0,
        reserve_position_pct=25.0,   # DEFAULT 25%
        sell_min_profit_pct=15.0,
        grid_rebound_step_pct=10.0,
        grid_sell_pct=15.0,
        grid_second_sell_pct=15.0,
        grid_min_sell_amount=200.0,  # DEFAULT $200
        grid_rebound_cycle_reset=1.0,
        sell_stage_rearm_drawdown_pct=15.0,
        sell_allow_same_day_sell=True,
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
    print(f"=== $10K Initial Cash ===")
    print(f"Total trades: {len(trades)}")
    print(f"Buy trades: {len(buy_trades)}, Sell trades: {len(sell_trades)}")
    print(f"Max shares: {strategy_result['symbols'][0].get('max_shares', 'N/A')}")
    print(f"\nMetrics:")
    for k, v in strategy_result['metrics'].items():
        print(f"  {k}: {v}")

    print("\n--- All Trades ---")
    for t in trades:
        extra = ""
        if t["action"] == "sell":
            extra = f", Stage: {t.get('sell_stage', 'N/A')}"
        elif t["action"] == "buy":
            extra = f", Thresh: {t.get('threshold_pct', 0):.2f}%"
            if t.get("sell_cycle_rearmed"):
                extra += " [REARM]"
        gross = t.get("gross_amount", 0)
        print(f"  {t['date']}  {t['action']:4s}  ${t['price']:.2f}  shares={t['shares']:.4f}  DD={t.get('drawdown_pct', 0):.2f}%  gross=${gross:.2f}{extra}")

if __name__ == "__main__":
    run()
