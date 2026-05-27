"""Profile-driven real-account reminder strategy execution."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from drawdown.generate_drawdown_report import PricePoint
from drawdown.position_strategy import StrategyInputs, build_strategy_tranches
from drawdown.strategy_rules import (
    core_dip_boost_ratio,
    core_dip_cash_reserve_ratio,
    core_dip_timing_allows_buy,
    cost_deleverage_date_cooldown_elapsed,
    grid_rebound_stages,
    point_drawdown_pct,
    select_cost_deleverage_stage,
    sell_stage_rearm_drawdown_pct,
)

from account_signal.config import AccountSnapshot, SignalTarget
from account_signal.profiles import AccountSignalProfile, strategy_inputs_for_profile
from account_signal.state import AccountLot, AccountPosition, derive_profile_state, parse_iso_date


def generate_profile_signals(
    *,
    profile: AccountSignalProfile,
    position: AccountPosition,
    target: SignalTarget,
    account: AccountSnapshot,
    points: list[PricePoint],
    debug: list[dict[str, Any]],
    fallback_same_day_sell: bool = False,
) -> list[dict[str, Any]]:
    inputs = strategy_inputs_for_profile(profile, fallback_same_day_sell=fallback_same_day_sell)
    position = derive_profile_state(position, profile, inputs)
    _attach_buy_drawdowns(position, points, inputs)
    point = points[-1]
    trade_date = point.date.date().isoformat()
    signals: list[dict[str, Any]] = []

    allow_same_day_sell = bool(inputs.sell_allow_same_day_sell)
    pre_buy_sell = None if allow_same_day_sell else _sell_signal_for_profile(profile, position, points, point, inputs, trade_date, debug)
    buy_signal = _buy_signal_for_profile(profile, position, target, account, points, point, inputs, trade_date, debug)

    if allow_same_day_sell and buy_signal:
        signals.append(buy_signal)
        estimated = _position_after_estimated_buy(position, point, float(buy_signal["amount_usd"]), trade_date, inputs)
        sell_signal = _sell_signal_for_profile(
            profile,
            estimated,
            points,
            point,
            inputs,
            trade_date,
            debug,
            estimated_same_day_buy=True,
        )
        if sell_signal:
            signals.append(sell_signal)
    else:
        sell_signal = (
            _sell_signal_for_profile(profile, position, points, point, inputs, trade_date, debug)
            if allow_same_day_sell and buy_signal is None
            else pre_buy_sell
        )
        if sell_signal:
            signals.append(sell_signal)
        if buy_signal:
            signals.append(buy_signal)
    return [_with_profile_metadata(signal, profile) for signal in signals]


def _buy_signal_for_profile(
    profile: AccountSignalProfile,
    position: AccountPosition,
    target: SignalTarget,
    account: AccountSnapshot,
    points: list[PricePoint],
    point: PricePoint,
    inputs: StrategyInputs,
    trade_date: str,
    debug: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if profile.buy_strategy == "core_dip_dca":
        return _core_dip_buy_signal(profile, position, target, account, points, point, inputs, trade_date, debug)
    if profile.buy_strategy in {"weekly_dca", "salary_flow_dca"}:
        return _dca_buy_signal(profile, position, target, account, point, inputs, trade_date, debug)
    return _tranche_buy_signal(profile, position, target, account, points, point, inputs, trade_date, debug)


def _core_dip_buy_signal(
    profile: AccountSignalProfile,
    position: AccountPosition,
    target: SignalTarget,
    account: AccountSnapshot,
    points: list[PricePoint],
    point: PricePoint,
    inputs: StrategyInputs,
    trade_date: str,
    debug: list[dict[str, Any]],
) -> dict[str, Any] | None:
    drawdown_pct = point_drawdown_pct(point, inputs)
    available_cash = max(0.0, min(account.cash, account.buying_power))
    reserve_cash = target.target_budget_usd * core_dip_cash_reserve_ratio(drawdown_pct, inputs)
    spendable_cash = max(0.0, available_cash - reserve_cash)

    if position.shares <= 1e-9:
        amount = min(target.target_budget_usd * inputs.core_dip_initial_core_pct / 100.0, spendable_cash)
        if amount >= target.min_buy_amount_usd > 0:
            return _buy_signal(
                symbol=profile.symbol,
                strategy=profile.buy_strategy,
                stage="initial_core",
                trade_date=trade_date,
                point=point,
                inputs=inputs,
                amount=amount,
                rationale=[
                    f"真实账户当前没有 {profile.symbol} 持仓",
                    f"初始核心仓按初始投入 {inputs.core_dip_initial_core_pct:g}% 计算",
                    f"保留现金垫约 ${reserve_cash:,.2f}",
                ],
                confidence="high",
            )
        return None

    weekly_core = max(0.0, target.monthly_contribution_usd / 4.0 * inputs.core_dip_weekly_core_pct / 100.0)
    boost = core_dip_boost_ratio(drawdown_pct, inputs)
    idle_sweep = max(0.0, spendable_cash - weekly_core) * (0.25 + 0.65 * boost) if drawdown_pct >= inputs.core_dip_start_drawdown_pct else 0.0
    amount = min(spendable_cash, weekly_core + idle_sweep)
    timing_allowed, timing_reason = core_dip_timing_allows_buy(point, points, drawdown_pct, pending_days=1, is_initial_buy=(position.shares <= 1e-9), inputs=inputs)
    debug.append({"event": "core_dip_buy_check", "symbol": profile.symbol, "amount": amount, "timing_allowed": timing_allowed, "timing_reason": timing_reason})
    if amount < target.min_buy_amount_usd or target.min_buy_amount_usd <= 0 or not timing_allowed or _bought_this_week(position, point.date.date()):
        return None
    return _buy_signal(
        symbol=profile.symbol,
        strategy=profile.buy_strategy,
        stage="weekly_core" if drawdown_pct < inputs.core_dip_start_drawdown_pct else "weekly_core_dip",
        trade_date=trade_date,
        point=point,
        inputs=inputs,
        amount=amount,
        rationale=[
            f"周投核心金额 ${weekly_core:,.2f}",
            f"{inputs.drawdown_basis} 回撤 {drawdown_pct:.2f}%",
            f"买点优化: {timing_reason}",
        ],
        confidence="high" if drawdown_pct >= inputs.core_dip_start_drawdown_pct or timing_reason in {"down_day", "near_recent_low"} else "medium",
    )


def _dca_buy_signal(
    profile: AccountSignalProfile,
    position: AccountPosition,
    target: SignalTarget,
    account: AccountSnapshot,
    point: PricePoint,
    inputs: StrategyInputs,
    trade_date: str,
    debug: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if _bought_this_week(position, point.date.date()):
        return None
    available_cash = max(0.0, min(account.cash, account.buying_power))
    base_amount = target.monthly_contribution_usd / 4.0 if target.monthly_contribution_usd > 0 else target.target_budget_usd * 0.025
    if position.shares <= 1e-9:
        base_amount = max(base_amount, target.target_budget_usd * 0.1)
    amount = min(available_cash, base_amount)
    debug.append({"event": "dca_buy_check", "symbol": profile.symbol, "amount": amount})
    if amount < target.min_buy_amount_usd or target.min_buy_amount_usd <= 0:
        return None
    return _buy_signal(
        symbol=profile.symbol,
        strategy=profile.buy_strategy,
        stage="weekly_dca",
        trade_date=trade_date,
        point=point,
        inputs=inputs,
        amount=amount,
        rationale=["本周尚无真实买入记录", f"按月投入折算本周提醒 ${amount:,.2f}"],
        confidence="medium",
    )


def _tranche_buy_signal(
    profile: AccountSignalProfile,
    position: AccountPosition,
    target: SignalTarget,
    account: AccountSnapshot,
    points: list[PricePoint],
    point: PricePoint,
    inputs: StrategyInputs,
    trade_date: str,
    debug: list[dict[str, Any]],
) -> dict[str, Any] | None:
    drawdown_pct = point_drawdown_pct(point, inputs)
    completed = _completed_buy_thresholds(position, points, inputs, profile.buy_strategy)
    available_cash = max(0.0, min(account.cash, account.buying_power))
    crossed_thresholds: list[float] = []
    crossed_allocations: list[float] = []
    trigger_events: list[dict[str, Any]] = []
    total_amount = 0.0
    for tranche in build_strategy_tranches(inputs, profile.buy_strategy):
        threshold = round(tranche.threshold_pct, 8)
        if threshold in completed or drawdown_pct + 1e-9 < tranche.threshold_pct:
            continue
        amount = min(available_cash, target.target_budget_usd * tranche.allocation_pct / 100.0)
        debug.append({"event": "tsla_buy_cross" if profile.symbol == "TSLA.US" else "tranche_buy_cross", "symbol": profile.symbol, "threshold": tranche.threshold_pct, "amount": amount})
        if amount <= 0:
            continue
        crossed_thresholds.append(tranche.threshold_pct)
        crossed_allocations.append(tranche.allocation_pct)
        trigger_events.append(
            {
                "stage": f"dd_{tranche.threshold_pct:g}",
                "threshold_pct": tranche.threshold_pct,
                "allocation_pct": tranche.allocation_pct,
                "amount_usd": round(float(amount), 2),
                "trade_date": trade_date,
                "price": float(point.close),
                "drawdown_pct": drawdown_pct,
            }
        )
        total_amount += amount
        available_cash = max(0.0, available_cash - amount)

    if total_amount > 0 and total_amount < target.min_buy_amount_usd:
        debug.append({"event": "tsla_buy_filtered_min_amount" if profile.symbol == "TSLA.US" else "tranche_buy_filtered_min_amount", "symbol": profile.symbol, "amount": total_amount, "min_buy_amount_usd": target.min_buy_amount_usd, "suppressed_trigger_events": trigger_events})
        return None
    if total_amount < target.min_buy_amount_usd or not crossed_thresholds:
        return None
    stage = f"dd_{crossed_thresholds[0]:g}" if len(crossed_thresholds) == 1 else f"dd_{crossed_thresholds[0]:g}_{crossed_thresholds[-1]:g}"
    threshold_text = ", ".join(f"{item:g}%" for item in crossed_thresholds)
    return _buy_signal(
        symbol=profile.symbol,
        strategy=profile.buy_strategy,
        stage=stage,
        trade_date=trade_date,
        point=point,
        inputs=inputs,
        amount=total_amount,
        rationale=[
            f"当前 {inputs.drawdown_basis} 回撤 {drawdown_pct:.2f}% 穿越 {threshold_text} 档",
            f"同日未完成档位已聚合，合计权重 {sum(crossed_allocations):.2f}%",
            f"聚合金额 ${total_amount:,.2f} 已达到最小提醒额 ${target.min_buy_amount_usd:,.2f}",
        ],
        confidence="high" if drawdown_pct >= 15.0 else "medium",
        trigger_events=trigger_events,
    )


def _sell_signal_for_profile(
    profile: AccountSignalProfile,
    position: AccountPosition,
    points: list[PricePoint],
    point: PricePoint,
    inputs: StrategyInputs,
    trade_date: str,
    debug: list[dict[str, Any]],
    *,
    estimated_same_day_buy: bool = False,
) -> dict[str, Any] | None:
    if profile.sell_strategy == "none":
        return None
    if position.shares <= 0 or position.avg_cost <= 0:
        return None
    if profile.sell_strategy == "grid_rebound":
        return _grid_rebound_sell_signal(profile, position, point, inputs, trade_date, estimated_same_day_buy=estimated_same_day_buy)
    if profile.sell_strategy == "repair_step":
        return _repair_step_sell_signal(profile, position, point, inputs, trade_date, estimated_same_day_buy=estimated_same_day_buy)
    return _cost_deleverage_sell_signal(profile, position, points, point, inputs, trade_date, debug, estimated_same_day_buy=estimated_same_day_buy)


def _grid_rebound_sell_signal(
    profile: AccountSignalProfile,
    position: AccountPosition,
    point: PricePoint,
    inputs: StrategyInputs,
    trade_date: str,
    *,
    estimated_same_day_buy: bool = False,
) -> dict[str, Any] | None:
    current_price = float(point.close)
    profit_pct = current_price / position.avg_cost * 100.0 - 100.0
    if profit_pct + 1e-9 < inputs.sell_min_profit_pct:
        return None
    avg_buy_drawdown = _avg_lot_buy_drawdown(position)
    if avg_buy_drawdown <= 0:
        return None
    current_drawdown = point_drawdown_pct(point, inputs)
    for stage, threshold, sell_pct in grid_rebound_stages(avg_buy_drawdown, inputs):
        if stage in position.grid_rebound_marks or current_drawdown > threshold + 1e-9:
            continue
        shares = position.shares * sell_pct / 100.0
        if shares * current_price + 1e-9 < inputs.grid_min_sell_amount:
            return None
        return _sell_signal(
            symbol=profile.symbol,
            strategy=profile.sell_strategy,
            stage=stage,
            trade_date=trade_date,
            point=point,
            inputs=inputs,
            shares=shares,
            rationale=[
                (
                    f"基于同日买入后估算，lot 加权买入回撤 {avg_buy_drawdown:.2f}%"
                    if estimated_same_day_buy
                    else f"真实 lot 加权买入回撤 {avg_buy_drawdown:.2f}%"
                ),
                f"当前回撤修复到 {current_drawdown:.2f}%，触发阈值 {threshold:.2f}%",
                f"相对真实剩余均价盈利 {profit_pct:.2f}%，满足 {inputs.sell_min_profit_pct:g}% 门槛",
            ],
        )
    return None


def _repair_step_sell_signal(
    profile: AccountSignalProfile,
    position: AccountPosition,
    point: PricePoint,
    inputs: StrategyInputs,
    trade_date: str,
    *,
    estimated_same_day_buy: bool = False,
) -> dict[str, Any] | None:
    if not _cooldown_elapsed(position.last_repair_sell_date, points_date=point.date.date(), cooldown_days=inputs.repair_sell_cooldown_days):
        return None
    current_price = float(point.close)
    profit_pct = current_price / position.avg_cost * 100.0 - 100.0
    if profit_pct + 1e-9 < inputs.sell_min_profit_pct:
        return None
    avg_buy_drawdown = _avg_lot_buy_drawdown(position)
    current_drawdown = point_drawdown_pct(point, inputs)
    stages = [
        ("repair_50", avg_buy_drawdown * 0.50),
        ("repair_20", avg_buy_drawdown * 0.20),
        ("repair_ath", 0.50),
    ]
    for stage, threshold in stages:
        if stage in position.repair_step_marks or current_drawdown > threshold + 1e-9:
            continue
        return _sell_signal(
            symbol=profile.symbol,
            strategy=profile.sell_strategy,
            stage=stage,
            trade_date=trade_date,
            point=point,
            inputs=inputs,
            shares=position.shares * inputs.repair_stage_sell_pct / 100.0,
            rationale=[
                "基于同日买入后估算" if estimated_same_day_buy else "基于真实剩余持仓",
                f"当前回撤修复到 {current_drawdown:.2f}%，触发阈值 {threshold:.2f}%",
                f"相对真实剩余均价盈利 {profit_pct:.2f}%，满足 {inputs.sell_min_profit_pct:g}% 门槛",
            ],
        )
    return None


def _cost_deleverage_sell_signal(
    profile: AccountSignalProfile,
    position: AccountPosition,
    points: list[PricePoint],
    point: PricePoint,
    inputs: StrategyInputs,
    trade_date: str,
    debug: list[dict[str, Any]],
    *,
    estimated_same_day_buy: bool = False,
) -> dict[str, Any] | None:
    if not cost_deleverage_date_cooldown_elapsed(
        parse_iso_date(position.last_cost_deleverage_sell_date),
        current_date=point.date.date(),
        cooldown_days=inputs.cost_deleverage_cooldown_days,
    ):
        debug.append({"event": "cost_deleverage_cooldown", "symbol": profile.symbol, "last_sell": position.last_cost_deleverage_sell_date})
        return None
    current_price = float(point.close)
    profit_pct = current_price / position.avg_cost * 100.0 - 100.0
    marks = _active_cost_marks(position, points, point, inputs, debug, profile.symbol)
    stage = select_cost_deleverage_stage(inputs=inputs, active_marks=marks, profit_pct=profit_pct)
    if stage is None:
        return None
    shares = position.shares * stage.sell_pct / 100.0
    if shares * current_price + 1e-9 < inputs.cost_min_sell_amount:
        return None
    basis_text = "基于同日买入后估算" if estimated_same_day_buy else "相对真实剩余均价盈利"
    return _sell_signal(
        symbol=profile.symbol,
        strategy=profile.sell_strategy,
        stage=stage.mark,
        trade_date=trade_date,
        point=point,
        inputs=inputs,
        shares=shares,
        rationale=[
            f"{basis_text} {profit_pct:.2f}%",
            f"触发 {stage.profit_pct:g}% 盈利档，建议卖出当前剩余持仓 {stage.sell_pct:g}%",
            "成本去杠杆冷却已满足",
        ],
    )


def _position_after_estimated_buy(
    position: AccountPosition,
    point: PricePoint,
    amount: float,
    trade_date: str,
    inputs: StrategyInputs,
) -> AccountPosition:
    price = float(point.close)
    if amount <= 0 or price <= 0:
        return position
    shares = amount / price
    drawdown_pct = point_drawdown_pct(point, inputs)
    lots = [
        AccountLot(
            buy_date=lot.buy_date,
            buy_price=lot.buy_price,
            initial_shares=lot.initial_shares,
            remaining_shares=lot.remaining_shares,
            amount=lot.amount,
            buy_drawdown_pct=lot.buy_drawdown_pct,
        )
        for lot in position.lots
    ]
    lots.append(AccountLot(buy_date=trade_date, buy_price=price, initial_shares=shares, remaining_shares=shares, amount=amount, buy_drawdown_pct=drawdown_pct))
    marks = set(position.cost_deleverage_marks)
    if marks and drawdown_pct + 1e-9 >= sell_stage_rearm_drawdown_pct(inputs):
        marks.clear()
    return AccountPosition(
        symbol=position.symbol,
        shares=position.shares + shares,
        lots=lots,
        buy_events=[*position.buy_events, {"trade_date": trade_date, "side": "buy", "shares": shares, "price": price, "amount": amount}],
        sell_events=list(position.sell_events),
        cost_deleverage_marks=marks,
        grid_rebound_marks=set(position.grid_rebound_marks),
        repair_step_marks=set(position.repair_step_marks),
        last_cost_deleverage_sell_date=position.last_cost_deleverage_sell_date,
        last_repair_sell_date=position.last_repair_sell_date,
        last_sell_date=position.last_sell_date,
    )


def _buy_signal(
    *,
    symbol: str,
    strategy: str,
    stage: str,
    trade_date: str,
    point: PricePoint,
    inputs: StrategyInputs,
    amount: float,
    rationale: list[str],
    confidence: str,
    trigger_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    signal = {
        "symbol": symbol,
        "action": "buy",
        "strategy": strategy,
        "stage": stage,
        "trade_date": trade_date,
        "price": float(point.close),
        "drawdown_pct": point_drawdown_pct(point, inputs),
        "amount_usd": round(float(amount), 2),
        "confidence": confidence,
        "rationale": rationale,
    }
    if trigger_events:
        signal["trigger_events"] = trigger_events
    if confidence == "high":
        leaps_triggers = trigger_events or [{"stage": stage, "threshold_pct": signal["drawdown_pct"], "allocation_pct": None, "amount_usd": signal["amount_usd"], "trade_date": trade_date, "price": float(point.close), "drawdown_pct": signal["drawdown_pct"]}]
        signal["leaps"] = {
            "enabled": True,
            "target_dte": "180-540",
            "stock_entry": f"${float(point.close):.2f}",
            "drawdown_pct": signal["drawdown_pct"],
            "trigger_count": len(leaps_triggers),
            "triggers": leaps_triggers,
        }
    return signal


def _sell_signal(
    *,
    symbol: str,
    strategy: str,
    stage: str,
    trade_date: str,
    point: PricePoint,
    inputs: StrategyInputs,
    shares: float,
    rationale: list[str],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "action": "sell",
        "strategy": strategy,
        "stage": stage,
        "trade_date": trade_date,
        "price": float(point.close),
        "drawdown_pct": point_drawdown_pct(point, inputs),
        "shares": round(float(shares), 6),
        "confidence": "high",
        "rationale": rationale,
    }


def _with_profile_metadata(signal: dict[str, Any], profile: AccountSignalProfile) -> dict[str, Any]:
    signal["profile_id"] = profile.profile_id
    signal["profile_source"] = profile.source
    signal["candidate_key"] = profile.candidate_key
    signal["strategy_definition_version"] = profile.strategy_definition_version
    return signal


def _active_cost_marks(
    position: AccountPosition,
    points: list[PricePoint],
    point: PricePoint,
    inputs: StrategyInputs,
    debug: list[dict[str, Any]],
    symbol: str,
) -> set[str]:
    retained_event = "googl_cost_marks_retained" if symbol == "GOOGL.US" else "cost_marks_retained"
    rearmed_event = "googl_cost_marks_rearmed" if symbol == "GOOGL.US" else "cost_marks_rearmed"
    marks = set(position.cost_deleverage_marks)
    if not marks:
        return marks
    current_drawdown = point_drawdown_pct(point, inputs)
    sell_context = _latest_cost_sell_context(position, points, inputs)
    if sell_context is not None:
        rearm_drawdown = min(inputs.max_drawdown_pct, sell_context["drawdown_pct"] + sell_stage_rearm_drawdown_pct(inputs))
        if current_drawdown + 1e-9 >= rearm_drawdown:
            debug.append({"event": rearmed_event, "symbol": symbol, "basis": "last_real_sell_drawdown", "last_sell_date": sell_context["trade_date"], "last_sell_drawdown_pct": sell_context["drawdown_pct"], "rearm_drawdown_pct": rearm_drawdown, "current_drawdown_pct": current_drawdown})
            return set()
        debug.append({"event": retained_event, "symbol": symbol, "basis": "last_real_sell_drawdown", "last_sell_date": sell_context["trade_date"], "last_sell_drawdown_pct": sell_context["drawdown_pct"], "rearm_drawdown_pct": rearm_drawdown, "current_drawdown_pct": current_drawdown})
        return marks
    if current_drawdown + 1e-9 >= sell_stage_rearm_drawdown_pct(inputs):
        debug.append({"event": rearmed_event, "symbol": symbol, "basis": "current_drawdown_fallback", "current_drawdown_pct": current_drawdown})
        return set()
    return marks


def _latest_cost_sell_context(position: AccountPosition, points: list[PricePoint], inputs: StrategyInputs) -> dict[str, Any] | None:
    drawdown_by_day = {item.date.date().isoformat(): point_drawdown_pct(item, inputs) for item in points}
    for event in reversed(position.sell_events):
        if float(event.get("profit_pct") or 0.0) + 1e-9 < inputs.cost_first_profit_pct:
            continue
        trade_date = str(event.get("trade_date", "") or "")
        if trade_date and trade_date in drawdown_by_day:
            return {"trade_date": trade_date, "drawdown_pct": drawdown_by_day[trade_date]}
    return None


def _completed_buy_thresholds(position: AccountPosition, points: list[PricePoint], inputs: StrategyInputs, strategy: str) -> set[float]:
    drawdown_by_day = {point.date.date().isoformat(): point_drawdown_pct(point, inputs) for point in points}
    thresholds = [round(item.threshold_pct, 8) for item in build_strategy_tranches(inputs, strategy)]
    completed: set[float] = set()
    for event in position.buy_events:
        buy_day = str(event.get("trade_date", "") or "")
        buy_drawdown = event.get("buy_drawdown_pct")
        if buy_drawdown is None:
            buy_drawdown = drawdown_by_day.get(buy_day)
        if buy_drawdown is None:
            continue
        for threshold in thresholds:
            if float(buy_drawdown) + 1e-9 >= threshold:
                completed.add(threshold)
    return completed


def _attach_buy_drawdowns(position: AccountPosition, points: list[PricePoint], inputs: StrategyInputs) -> None:
    drawdown_by_day = {point.date.date().isoformat(): point_drawdown_pct(point, inputs) for point in points}
    for lot in position.lots:
        if lot.buy_drawdown_pct is None and lot.buy_date in drawdown_by_day:
            lot.buy_drawdown_pct = drawdown_by_day[lot.buy_date]
    for event in position.buy_events:
        buy_day = str(event.get("trade_date", "") or "")
        if "buy_drawdown_pct" not in event and buy_day in drawdown_by_day:
            event["buy_drawdown_pct"] = drawdown_by_day[buy_day]


def _avg_lot_buy_drawdown(position: AccountPosition) -> float:
    total_shares = 0.0
    weighted = 0.0
    for lot in position.lots:
        if lot.remaining_shares <= 0 or lot.buy_drawdown_pct is None:
            continue
        total_shares += lot.remaining_shares
        weighted += lot.remaining_shares * lot.buy_drawdown_pct
    if total_shares <= 0:
        buy_drawdowns = [float(event.get("buy_drawdown_pct", 0.0) or 0.0) for event in position.buy_events if event.get("buy_drawdown_pct") is not None]
        return sum(buy_drawdowns) / len(buy_drawdowns) if buy_drawdowns else 0.0
    return weighted / total_shares


def _bought_this_week(position: AccountPosition, current_day: date) -> bool:
    current_year_week = current_day.isocalendar()[:2]
    for event in position.buy_events:
        event_day = parse_iso_date(str(event.get("trade_date", "") or ""))
        if event_day and event_day.isocalendar()[:2] == current_year_week:
            return True
    return False


def _cooldown_elapsed(last_sell_date: str | None, *, points_date: date, cooldown_days: int) -> bool:
    last = parse_iso_date(last_sell_date)
    if not last:
        return True
    return (points_date - last).days >= cooldown_days
