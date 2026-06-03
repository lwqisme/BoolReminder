"""LEAPS option GA — evaluation, fitness, and evolution loop.

Separated from leaps_option_ga.py to keep each file under 1000 lines.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from drawdown.leaps_option_ga import (
    LeapsEntrySignal,
    LeapsEvolutionConfig,
    LeapsIndividual,
    LeapsParamRanges,
    LeapsSellEvent,
    LeapsTrade,
    _bs_call_price,
    _dd_options,
    _enforce_day_order,
    _enforce_profit_order,
    _ENTRY_MODE_OPTIONS,
    _random_individual,
    _tournament_select,
    bollinger_lower_band,
    compute_sell_ladder,
    detect_leaps_entries,
    leaps_crossover,
    leaps_mutate,
    proxy_option_roi,
)


def _eval_trades(
    individual: LeapsIndividual,
    price_series_by_symbol: dict[str, list[tuple[date, float]]],
    *,
    min_entry_date: date | None = None,
) -> list[LeapsTrade]:
    """Simulate all trades for an individual, return trade objects chronologically."""
    all_trades: list[LeapsTrade] = []

    for symbol, prices in price_series_by_symbol.items():
        entries = detect_leaps_entries(
            prices, individual.drawdown_threshold_pct, individual.entry_mode,
            min_entry_date=min_entry_date,
        )
        stages = individual.to_stages()
        for entry in entries:
            trade = compute_sell_ladder(entry, prices, stages, expiration_days=190,
                                         strike_price=entry.price * 1.10)
            all_trades.append(trade)
    all_trades.sort(key=lambda t: t.entry.date)
    return all_trades


def _eval_fixed_capital(
    individual: LeapsIndividual,
    price_series_by_symbol: dict[str, list[tuple[date, float]]],
    total_capital: float = 10000.0,
    *,
    min_entry_date: date | None = None,
) -> dict[str, object]:
    """Simulate trades with fixed capital, cooldown, and fund-limited entries."""
    all_trades = _eval_trades(individual, price_series_by_symbol, min_entry_date=min_entry_date)
    if not all_trades:
        return {
            "final_equity": total_capital, "cagr": 0.0, "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0, "trade_count": 0, "executed_trades": [],
        }

    invest_per_trade = total_capital * individual.position_pct / 100.0
    equity = total_capital
    cooldown_days = individual.cooldown_days

    peak_equity = total_capital
    max_dd_pct = 0.0
    equity_curve: list[tuple[date, float]] = []

    # Collect all date events across all trades
    all_dates_set: set[date] = set()
    entries_by_date: dict[date, list[LeapsTrade]] = {}
    for t in all_trades:
        all_dates_set.add(t.entry.date)
        entries_by_date.setdefault(t.entry.date, []).append(t)
        for se in t.sell_events:
            all_dates_set.add(se.date)
    all_dates_sorted = sorted(all_dates_set)

    open_positions: list[dict[str, object]] = []
    sell_events_by_date: dict[date, list[dict[str, object]]] = {}
    executed_trades: list[LeapsTrade] = []

    equity_curve.append((all_dates_sorted[0] - timedelta(days=1), equity))
    global_cooldown_until: date | None = None

    for current_date in all_dates_sorted:
        # Process sells first
        if current_date in sell_events_by_date:
            for se in sell_events_by_date[current_date]:
                invested = se["invested"]
                pct = se["pct_sold"]
                roi = se["roi_pct"]
                released = invested * (pct / 100.0) * (1.0 + roi / 100.0)
                equity += released
                pos = se["position"]
                pos["cumulative_sold"] = pos.get("cumulative_sold", 0.0) + pct

        # Remove completed positions
        open_positions = [p for p in open_positions if p.get("cumulative_sold", 0.0) < 99.9]

        # Track equity curve
        equity_curve.append((current_date, equity))
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100.0
        if dd > max_dd_pct:
            max_dd_pct = dd

        # Process new entries
        if current_date in entries_by_date:
            for trade in entries_by_date[current_date]:
                if global_cooldown_until is not None and current_date <= global_cooldown_until:
                    continue
                if equity < invest_per_trade:
                    continue
                equity -= invest_per_trade
                pos_data = {"invested": invest_per_trade, "cumulative_sold": 0.0, "trade": trade}
                open_positions.append(pos_data)
                executed_trades.append(trade)
                for se in trade.sell_events:
                    sell_events_by_date.setdefault(se.date, []).append({
                        "invested": invest_per_trade,
                        "pct_sold": se.pct_sold,
                        "roi_pct": se.roi_pct,
                        "position": pos_data,
                    })
                global_cooldown_until = current_date + timedelta(days=cooldown_days)

    # Force-sell remaining open positions using actual option ROI at last date
    if open_positions:
        last_date = all_dates_sorted[-1]
        for pos in open_positions:
            remaining = 100.0 - pos.get("cumulative_sold", 0.0)
            if remaining <= 0.1:
                continue
            trade = pos.get("trade")
            if trade is not None:
                entry_signal = trade.entry
                expiration = entry_signal.date + timedelta(days=190)
                last_roi = proxy_option_roi(
                    entry_signal.price, pos.get("_last_price", entry_signal.price),
                    entry_signal.date, last_date, expiration,
                    entry_signal.price * 1.10,
                )
                recovery = 1.0 + last_roi / 100.0
            else:
                recovery = 0.10  # conservative fallback
            equity += pos["invested"] * (remaining / 100.0) * recovery

    equity_curve.append((all_dates_sorted[-1] + timedelta(days=1), equity))

    total_return = (equity / total_capital - 1.0) * 100.0
    years = max((all_dates_sorted[-1] - all_dates_sorted[0]).days / 365.0, 0.5)
    cagr = (equity / total_capital) ** (1.0 / years) - 1.0

    return {
        "final_equity": round(equity, 2),
        "cagr": round(cagr * 100.0, 2),
        "total_return_pct": round(total_return, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "trade_count": len(executed_trades),
        "executed_trades": executed_trades,
    }


def _eval_unlimited_capital(
    individual: LeapsIndividual,
    price_series_by_symbol: dict[str, list[tuple[date, float]]],
    *,
    min_entry_date: date | None = None,
) -> dict[str, object]:
    """Simulate trades with unlimited capital (all signals, geometric compounding)."""
    all_trades = _eval_trades(individual, price_series_by_symbol, min_entry_date=min_entry_date)
    if not all_trades:
        return {
            "geo_product": 1.0, "annualized_geo": 0.0, "total_return_pct": 0.0,
            "trade_count": 0,
        }

    geo_product = 1.0
    total_opt_cost = 0.0
    total_opt_revenue = 0.0
    for t in all_trades:
        geo_product *= (1.0 + t.total_roi_pct / 100.0)
        opt_entry = _bs_call_price(
            t.entry.price, t.entry.price * 1.10, 190.0 / 365.0, 0.05, 0.40,
        )
        total_opt_cost += opt_entry
        total_opt_revenue += opt_entry * (1.0 + t.total_roi_pct / 100.0)

    years = max((all_trades[-1].entry.date - all_trades[0].entry.date).days / 365.0, 0.5)
    annualized = geo_product ** (1.0 / years) - 1.0
    total_return = (geo_product - 1.0) * 100.0

    return {
        "geo_product": round(geo_product, 6),
        "annualized_geo": round(annualized * 100.0, 2),
        "total_return_pct": round(total_return, 2),
        "trade_count": len(all_trades),
        "total_opt_cost": round(total_opt_cost, 4),
        "total_opt_revenue": round(total_opt_revenue, 4),
    }


def leaps_fitness_fn(
    individual: LeapsIndividual,
    price_series_by_symbol: dict[str, list[tuple[date, float]]],
    capital_mode: str = "fixed",
    total_capital: float = 10000.0,
    *,
    min_entry_date: date | None = None,
) -> float:
    """Evaluate fitness based on capital mode.

    fixed: Fitness = final_equity / total_capital.
    unlimited: Fitness = geometric product.
    """
    if capital_mode == "unlimited":
        result = _eval_unlimited_capital(individual, price_series_by_symbol, min_entry_date=min_entry_date)
        return float(result["geo_product"])
    else:
        result = _eval_fixed_capital(individual, price_series_by_symbol, total_capital, min_entry_date=min_entry_date)
        return result["final_equity"] / total_capital


def leaps_total_roi(
    individual: LeapsIndividual,
    price_series_by_symbol: dict[str, list[tuple[date, float]]],
    capital_mode: str = "fixed",
    total_capital: float = 10000.0,
    *,
    min_entry_date: date | None = None,
) -> float:
    """Total return percentage for display."""
    if capital_mode == "unlimited":
        return float(_eval_unlimited_capital(individual, price_series_by_symbol, min_entry_date=min_entry_date)["total_return_pct"])
    return float(_eval_fixed_capital(individual, price_series_by_symbol, total_capital, min_entry_date=min_entry_date)["total_return_pct"])


def _precompute_bollinger(
    price_series_by_symbol: dict[str, list[tuple[date, float]]],
) -> dict[str, dict[str, float | None]]:
    """Precompute bollinger lower band by date for all symbols."""
    result: dict[str, dict[str, float | None]] = {}
    for symbol, prices in price_series_by_symbol.items():
        bb_full = bollinger_lower_band(prices, period=22, std_mult=2.0)
        bb_by_date: dict[str, float | None] = {}
        for d, band in bb_full:
            bb_by_date[d.isoformat()] = band
        result[symbol] = bb_by_date
    return result


_EVAL_RESULT_KEY = "_eval_result"


def _collect_trade_details(
    individual: LeapsIndividual,
    price_series_by_symbol: dict[str, list[tuple[date, float]]],
    bollinger_cache: dict[str, dict[str, float | None]],
    capital_mode: str = "fixed",
    total_capital: float = 10000.0,
    eval_cache: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Collect all trades for an individual across all symbols."""
    if capital_mode == "fixed" and eval_cache is not None and _EVAL_RESULT_KEY in eval_cache:
        trades_list = eval_cache[_EVAL_RESULT_KEY].get("executed_trades", [])
    elif capital_mode == "fixed":
        result = _eval_fixed_capital(individual, price_series_by_symbol, total_capital)
        trades_list = result.get("executed_trades", [])
    else:
        trades_list = _eval_trades(individual, price_series_by_symbol)

    output: list[dict[str, object]] = []
    for symbol, prices in price_series_by_symbol.items():
        bb_by_date = bollinger_cache.get(symbol, {})

        for trade in trades_list:
            if not hasattr(trade, 'sell_events'):
                continue
            entry_signal = trade.entry
            entry_date_str = entry_signal.date.isoformat()

            all_dates = [entry_signal.date]
            for se in trade.sell_events:
                all_dates.append(se.date)
            if all_dates:
                price_slice_start = min(all_dates) - timedelta(days=60)
                price_slice_end = max(all_dates) + timedelta(days=30)
                price_series = []
                for d, p in prices:
                    if price_slice_start <= d <= price_slice_end:
                        bb = bb_by_date.get(d.isoformat())
                        pt: dict[str, object] = {"date": d.isoformat(), "price": p}
                        if bb is not None:
                            pt["bollinger_lower"] = bb
                        price_series.append(pt)
            else:
                price_series = []
            output.append({
                "symbol": symbol,
                "entry_date": entry_date_str,
                "entry_price": entry_signal.price,
                "drawdown_pct": entry_signal.drawdown_pct,
                "bollinger_score": entry_signal.bollinger_score,
                "composite_score": entry_signal.composite_score,
                "sell_events": [{
                    "date": se.date.isoformat(),
                    "price": se.price,
                    "pct_sold": se.pct_sold,
                    "roi_pct": se.roi_pct,
                } for se in trade.sell_events],
                "expired": trade.expired,
                "total_roi_pct": trade.total_roi_pct,
                "price_series": price_series,
            })
    return output


def evolve_leaps_parameters(
    price_series_by_symbol: dict[str, list[tuple[date, float]]],
    config: LeapsEvolutionConfig | None = None,
    param_ranges: LeapsParamRanges | None = None,
    *,
    min_entry_date: date | None = None,
) -> dict[str, object]:
    """Run genetic algorithm to optimize LEAPS call parameters."""
    config = config or LeapsEvolutionConfig()
    ranges = param_ranges or LeapsParamRanges()
    capital_mode = config.capital_mode
    total_capital = config.total_capital
    if config.seed is not None:
        random.seed(config.seed)

    # Precompute bollinger cache for trade detail collection
    bollinger_cache = _precompute_bollinger(price_series_by_symbol)

    # Initialize population with dedup
    seen_keys: set[str] = set()
    population: list[LeapsIndividual] = []
    while len(population) < config.population_size:
        ind = _random_individual(ranges)
        if ind.key not in seen_keys:
            seen_keys.add(ind.key)
            population.append(ind)

    fitnesses = [
        leaps_fitness_fn(ind, price_series_by_symbol, capital_mode, total_capital, min_entry_date=min_entry_date)
        for ind in population
    ]

    snapshots: list[dict[str, object]] = []
    best_individual = population[0]
    best_fitness = fitnesses[0]
    # all_evaluated: key -> (individual, fitness, eval_result_dict)
    all_evaluated: dict[str, tuple[LeapsIndividual, float, dict[str, object]]] = {}

    for gen in range(config.generations):
        ranked = sorted(zip(population, fitnesses), key=lambda x: x[1], reverse=True)
        population = [x[0] for x in ranked]
        fitnesses = [x[1] for x in ranked]

        for ind, fit in ranked:
            if ind.key not in all_evaluated or fit > all_evaluated[ind.key][1]:
                # We don't have the eval result here (fitness computed earlier).
                # Store it lazily: will be populated in the final ranking pass.
                all_evaluated[ind.key] = (ind, fit, {})

        gen_best = fitnesses[0]
        gen_avg = sum(fitnesses) / len(fitnesses)
        gen_worst = fitnesses[-1]

        if gen_best > best_fitness:
            best_fitness = gen_best
            best_individual = population[0]

        snapshots.append({
            "generation": gen + 1,
            "best_fitness": gen_best,
            "avg_fitness": gen_avg,
            "worst_fitness": gen_worst,
            "best_key": best_individual.key,
            "best_params": best_individual.to_params_dict(),
        })

        elites = population[:config.elitism_count]
        next_population: list[LeapsIndividual] = list(elites)

        while len(next_population) < config.population_size:
            p1 = _tournament_select(population, fitnesses, config.tournament_size)
            p2 = _tournament_select(population, fitnesses, config.tournament_size)

            if random.random() < config.crossover_rate:
                child = leaps_crossover(p1, p2)
            else:
                child = p1

            child = leaps_mutate(child, config, ranges)
            next_population.append(child)

        population = next_population[:config.population_size]
        fitnesses = [
            leaps_fitness_fn(ind, price_series_by_symbol, capital_mode, total_capital, min_entry_date=min_entry_date)
            for ind in population
        ]

    ranked_final = sorted(all_evaluated.items(), key=lambda x: x[1][1], reverse=True)
    final_rows: list[dict[str, object]] = []
    for rank, (key, (ind, fit, cached)) in enumerate(ranked_final, start=1):
        # Compute or fetch cached eval result
        if not cached:
            if capital_mode == "fixed":
                cached = _eval_fixed_capital(ind, price_series_by_symbol, total_capital, min_entry_date=min_entry_date)
            else:
                eval_result = _eval_unlimited_capital(ind, price_series_by_symbol, min_entry_date=min_entry_date)
                # Normalize to same key structure
                cached = {
                    "total_return_pct": eval_result["total_return_pct"],
                    "trade_count": eval_result["trade_count"],
                    "annualized_geo": eval_result.get("annualized_geo", 0.0),
                    "total_opt_cost": eval_result.get("total_opt_cost", 0.0),
                    "total_opt_revenue": eval_result.get("total_opt_revenue", 0.0),
                }

        total_roi = leaps_total_roi(ind, price_series_by_symbol, capital_mode, total_capital, min_entry_date=min_entry_date)
        row: dict[str, object] = {
            "rank": rank,
            "key": ind.key,
            "fitness": fit,
            "total_roi": total_roi,
            **ind.to_params_dict(),
        }
        if capital_mode == "fixed":
            row["final_equity"] = cached["final_equity"]
            row["cagr"] = cached["cagr"]
            row["max_drawdown_pct"] = cached["max_drawdown_pct"]
            row["trade_count"] = cached["trade_count"]
        else:
            row["annualized_geo"] = cached.get("annualized_geo", 0.0)
            row["trade_count"] = cached.get("trade_count", 0)
            total_cost = cached.get("total_opt_cost", 0.0)
            total_rev = cached.get("total_opt_revenue", 0.0)
            row["input_output_ratio"] = round(total_rev / total_cost, 4) if total_cost > 0 else 0.0
        if rank <= 10:
            # Pass cached result to avoid recomputation
            cache_for_collect = {_EVAL_RESULT_KEY: cached} if cached else None
            row["trade_details"] = _collect_trade_details(
                ind, price_series_by_symbol, bollinger_cache,
                capital_mode, total_capital, cache_for_collect,
            )
        final_rows.append(row)

    return {
        "config": {
            "population_size": config.population_size,
            "generations": config.generations,
            "mutation_rate": config.mutation_rate,
            "crossover_rate": config.crossover_rate,
            "elitism_count": config.elitism_count,
            "tournament_size": config.tournament_size,
            "seed": config.seed,
            "capital_mode": capital_mode,
            "total_capital": total_capital,
        },
        "snapshots": snapshots,
        "best": final_rows[0] if final_rows else None,
        "final_population": final_rows,
        "total_evaluated": len(all_evaluated),
    }
