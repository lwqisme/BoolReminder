"""LEAPS option genetic algorithm optimization.

Independent from stock strategy GA. Evolves LEAPS call entry/exit parameters
using delta-proxy P&L estimation during evolution, with Polygon-verified
results for final ranking.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class LeapsEntrySignal:
    """A detected LEAPS call entry opportunity."""
    date: date
    price: float
    drawdown_pct: float
    bollinger_score: float
    composite_score: float


@dataclass(frozen=True)
class LeapsSellEvent:
    """A single sell execution within a LEAPS trade."""
    date: date
    price: float
    pct_sold: float
    roi_pct: float


@dataclass(frozen=True)
class LeapsTrade:
    """A complete LEAPS call trade from entry to final exit."""
    entry: LeapsEntrySignal
    sell_events: tuple[LeapsSellEvent, ...]
    expired: bool
    total_roi_pct: float


@dataclass(frozen=True)
class LeapsIndividual:
    """A single LEAPS parameter candidate for GA evolution.

    8 evolvable dimensions plus fixed stage3 sells remaining 100%.
    """
    drawdown_threshold_pct: float
    entry_mode: str
    stage1_days: int
    stage1_profit: float
    stage1_sell: float
    stage2_days: int
    stage2_profit: float
    stage2_sell: float
    key: str = ""

    def __post_init__(self):
        if not self.key:
            object.__setattr__(self, "key", leaps_individual_key(
                self.drawdown_threshold_pct, self.entry_mode,
                self.stage1_days, self.stage1_profit, self.stage1_sell,
                self.stage2_days, self.stage2_profit, self.stage2_sell,
            ))

    def to_stages(self) -> list[tuple[int, float, float]]:
        """Convert to sell ladder stage tuples."""
        return [
            (self.stage1_days, self.stage1_profit, self.stage1_sell),
            (self.stage2_days, self.stage2_profit, self.stage2_sell),
        ]

    def to_params_dict(self) -> dict[str, object]:
        return {
            "drawdown_threshold_pct": self.drawdown_threshold_pct,
            "entry_mode": self.entry_mode,
            "stage1_days": self.stage1_days,
            "stage1_profit": self.stage1_profit,
            "stage1_sell": self.stage1_sell,
            "stage2_days": self.stage2_days,
            "stage2_profit": self.stage2_profit,
            "stage2_sell": self.stage2_sell,
        }


def leaps_individual_key(
    drawdown_threshold_pct: float,
    entry_mode: str,
    stage1_days: int,
    stage1_profit: float,
    stage1_sell: float,
    stage2_days: int,
    stage2_profit: float,
    stage2_sell: float,
) -> str:
    """Generate deterministic key for a LEAPS individual."""
    return (
        f"dd{drawdown_threshold_pct:g}__{entry_mode}"
        f"__s1d{stage1_days}_p{stage1_profit:g}_s{stage1_sell:g}"
        f"__s2d{stage2_days}_p{stage2_profit:g}_s{stage2_sell:g}"
    )


def estimate_option_delta(
    stock_price: float,
    strike: float,
    dte: int,
    risk_free_rate: float = 0.05,
    sigma: float = 0.40,
) -> float:
    """Black-Scholes call delta for LEAPS proxy estimation.

    Args:
        stock_price: Current underlying stock price.
        strike: Option strike price.
        dte: Days to expiration.
        risk_free_rate: Annual risk-free rate (default 5%).
        sigma: Annualized implied volatility (default 40%).

    Returns:
        Call delta in range (0, 1).
    """
    if dte <= 0:
        return 1.0 if stock_price > strike else 0.0

    t = dte / 365.0
    if stock_price <= 0 or strike <= 0 or t <= 0:
        return 0.0

    d1 = (math.log(stock_price / strike) + (risk_free_rate + sigma**2 / 2) * t) / (
        sigma * math.sqrt(t)
    )

    # Normal CDF approximation (Abramowitz & Stegun 26.2.17)
    delta = _norm_cdf(d1)
    return max(0.0, min(1.0, delta))


def bollinger_lower_band(
    prices: list[tuple[date, float]],
    period: int = 22,
    std_mult: float = 2.0,
) -> list[tuple[date, float | None]]:
    """Compute Bollinger lower band for each date.

    Args:
        prices: List of (date, price) sorted ascending.
        period: Moving average period.
        std_mult: Standard deviation multiplier.

    Returns:
        List of (date, lower_band_or_none) with same length as input.
    """
    result: list[tuple[date, float | None]] = []
    window: list[float] = []

    for i, (d, p) in enumerate(prices):
        window.append(p)
        if i >= period:
            window.pop(0)
        if i >= period - 1:
            mean = sum(window) / len(window)
            variance = sum((v - mean) ** 2 for v in window) / len(window)
            std = math.sqrt(variance)
            band = mean - std_mult * std
            result.append((d, band))
        else:
            result.append((d, None))

    return result


def _bollinger_with_ma(
    prices: list[tuple[date, float]],
    period: int = 22,
    std_mult: float = 2.0,
) -> list[dict[str, float | None]]:
    """Compute Bollinger band with MA for internal use."""
    result: list[dict[str, float | None]] = []
    window: list[float] = []

    for i, (d, p) in enumerate(prices):
        window.append(p)
        if i >= period:
            window.pop(0)
        if i >= period - 1:
            mean = sum(window) / len(window)
            variance = sum((v - mean) ** 2 for v in window) / len(window)
            std = math.sqrt(variance)
            result.append({"date": d, "ma": mean, "band": mean - std_mult * std})
        else:
            result.append({"date": d, "ma": None, "band": None})

    return result


def rolling_120d_high(
    prices: list[tuple[date, float]],
) -> list[tuple[date, float | None]]:
    """Compute rolling 120-day high for each date in the price series.

    For the first 119 data points, the high is None (not enough history).

    Args:
        prices: List of (date, price) tuples sorted by date ascending.

    Returns:
        List of (date, high_or_none) with same length as input.
    """
    if len(prices) < 120:
        return [(d, None) for d, _ in prices]

    result: list[tuple[date, float | None]] = []
    window: list[float] = []

    for i, (d, p) in enumerate(prices):
        window.append(p)
        if i >= 120:
            window.pop(0)
        if i >= 119:
            result.append((d, max(window)))
        else:
            result.append((d, None))

    return result


def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    # Abramowitz & Stegun approximation
    if x < -8.0:
        return 0.0
    if x > 8.0:
        return 1.0
    # Use math.erf for accuracy
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def detect_leaps_entries(
    prices: list[tuple[date, float]],
    drawdown_threshold_pct: float = 20.0,
    entry_mode: str = "both",
) -> list[LeapsEntrySignal]:
    """Detect LEAPS call entry signals from price data.

    Computes rolling 120-day high drawdown and Bollinger band (22, 2σ)
    lower band position for each date. Entries are triggered when:

    - touch: price <= lower band AND drawdown >= threshold
    - bounce: price was <= lower band yesterday AND price > lower band today
      AND drawdown >= threshold
    - both: either touch or bounce

    Composite score combines drawdown severity (0-1) and Bollinger position (0-1).

    Args:
        prices: List of (date, price) sorted ascending.
        drawdown_threshold_pct: Minimum drawdown from 120-day high to trigger.
        entry_mode: "touch", "bounce", or "both".

    Returns:
        List of LeapsEntrySignal in chronological order.
    """
    from collections import defaultdict

    if len(prices) < 122:
        return []

    highs = rolling_120d_high(prices)
    bb_data = _bollinger_with_ma(prices, period=22, std_mult=2.0)

    entries: list[LeapsEntrySignal] = []

    for i in range(121, len(prices)):
        d, p = prices[i]
        high = highs[i][1]
        bb = bb_data[i]
        ma = bb.get("ma")
        band = bb.get("band")
        if high is None or ma is None or band is None or high <= 0 or ma <= 0:
            continue

        drawdown_pct = (high - p) / high * 100.0
        if drawdown_pct < drawdown_threshold_pct:
            continue

        # Bollinger score: 0 = at MA, 1 = at lower band, >1 = below band
        ma_minus_band = ma - band
        if ma_minus_band <= 0:
            bollinger_score = 1.0 if p <= band else 0.0
        else:
            bollinger_score = (ma - p) / ma_minus_band

        is_touch = bollinger_score >= 1.0
        is_bounce = False
        if i > 0:
            prev_bb = bb_data[i - 1]
            prev_ma = prev_bb.get("ma")
            prev_band = prev_bb.get("band")
            if prev_ma is not None and prev_band is not None:
                prev_ma_minus_band = prev_ma - prev_band
                if prev_ma_minus_band > 0:
                    prev_score = (prev_ma - prices[i - 1][1]) / prev_ma_minus_band
                    is_bounce = prev_score >= 1.0 and bollinger_score < 1.0

        if entry_mode == "touch" and not is_touch:
            continue
        if entry_mode == "bounce" and not is_bounce:
            continue
        if entry_mode == "both" and not (is_touch or is_bounce):
            continue

        dd_score = min(drawdown_pct / 40.0, 1.0)
        bb_score = min(bollinger_score / 2.0, 1.0)
        composite = (dd_score + bb_score) / 2.0

        entries.append(LeapsEntrySignal(
            date=d,
            price=p,
            drawdown_pct=round(drawdown_pct, 2),
            bollinger_score=round(bollinger_score, 4),
            composite_score=round(composite, 4),
        ))

    return entries


def compute_sell_ladder(
    entry: LeapsEntrySignal,
    prices: list[tuple[date, float]],
    stages: list[tuple[int, float, float]],
    expiration_days: int = 190,
    strike_price: float | None = None,
    risk_free_rate: float = 0.05,
    sigma: float = 0.40,
) -> LeapsTrade:
    """Simulate staged sell ladder from entry to expiration.

    Walks through prices day by day after entry. At each date, checks
    each stage's conditions:
    - hold days >= stage min_hold_days
    - proxy option ROI >= stage profit_pct
    - If met: sell stage's sell_pct of remaining position

    Stage 3 (last) always sells 100% of remaining at expiration cutoff.

    Args:
        entry: The entry signal.
        prices: Full price history (must include dates >= entry.date).
        stages: List of (min_hold_days, profit_pct_threshold, sell_fraction_pct).
        expiration_days: Days from entry to option expiration.
        strike_price: Option strike (default: entry.price * 1.10).
        risk_free_rate: For BS proxy.
        sigma: Implied volatility for BS proxy.

    Returns:
        LeapsTrade with sell events and total ROI.
    """
    if strike_price is None:
        strike_price = entry.price * 1.10

    expiration = entry.date + timedelta(days=expiration_days)
    hard_cutoff = expiration - timedelta(days=60)

    # Build price lookup by date
    price_by_date = {d: p for d, p in prices}

    # Ensure at least 3 stages, last one always sells remaining
    effective_stages = list(stages)
    while len(effective_stages) < 3:
        effective_stages.append((9999, 0.0, 100.0))

    sell_events: list[LeapsSellEvent] = []
    remaining_pct = 100.0  # portion of initial position not yet sold
    stage_triggered: list[bool] = [False] * len(effective_stages)

    # Start from entry date + 1 day
    current_date = entry.date + timedelta(days=1)
    end_date = max(hard_cutoff, max(d for d, _ in prices))

    while current_date <= end_date:
        if current_date > hard_cutoff:
            # Force-sell remaining at cutoff date
            exit_price = price_by_date.get(current_date, entry.price)
            roi = proxy_option_roi(
                entry.price, exit_price, entry.date, current_date,
                expiration, strike_price, risk_free_rate, sigma,
            )
            sell_events.append(LeapsSellEvent(
                date=current_date, price=exit_price,
                pct_sold=remaining_pct, roi_pct=round(roi, 2),
            ))
            break

        exit_price = price_by_date.get(current_date)
        if exit_price is None:
            current_date += timedelta(days=1)
            continue

        hold_days = (current_date - entry.date).days
        roi = proxy_option_roi(
            entry.price, exit_price, entry.date, current_date,
            expiration, strike_price, risk_free_rate, sigma,
        )

        # Check each stage in order
        for stage_idx, (min_hold, profit_threshold, sell_fraction) in enumerate(effective_stages):
            if stage_triggered[stage_idx]:
                continue
            if hold_days < min_hold:
                continue
            if roi < profit_threshold:
                continue
            if remaining_pct <= 0:
                break

            # Trigger this stage
            sell_amount = min(sell_fraction, remaining_pct)
            remaining_pct -= sell_amount
            stage_triggered[stage_idx] = True
            sell_events.append(LeapsSellEvent(
                date=current_date, price=exit_price,
                pct_sold=round(sell_amount, 2),
                roi_pct=round(roi, 2),
            ))

            if remaining_pct <= 0:
                break

        if remaining_pct <= 0:
            break

        current_date += timedelta(days=1)

    # If we exhausted prices without selling, mark as expired at hard_cutoff
    expired = remaining_pct > 0
    if expired:
        cutoff_price = price_by_date.get(hard_cutoff, entry.price)
        roi = proxy_option_roi(
            entry.price, cutoff_price, entry.date, hard_cutoff,
            expiration, strike_price, risk_free_rate, sigma,
        )
        sell_events.append(LeapsSellEvent(
            date=hard_cutoff, price=cutoff_price,
            pct_sold=remaining_pct, roi_pct=round(roi, 2),
        ))

    # Compute total weighted ROI
    if sell_events:
        total_roi = sum(e.roi_pct * (e.pct_sold / 100.0) for e in sell_events)
        total_roi /= sum(e.pct_sold / 100.0 for e in sell_events)
    else:
        total_roi = 0.0

    return LeapsTrade(
        entry=entry,
        sell_events=tuple(sell_events),
        expired=expired,
        total_roi_pct=round(total_roi, 2),
    )


def _bs_call_price(
    stock_price: float,
    strike: float,
    t: float,
    risk_free_rate: float = 0.05,
    sigma: float = 0.40,
) -> float:
    """Black-Scholes call option price."""
    if t <= 0:
        return max(0.0, stock_price - strike)
    d1 = (math.log(stock_price / strike) + (risk_free_rate + sigma**2 / 2) * t) / (
        sigma * math.sqrt(t)
    )
    d2 = d1 - sigma * math.sqrt(t)
    return stock_price * _norm_cdf(d1) - strike * math.exp(-risk_free_rate * t) * _norm_cdf(d2)


def proxy_option_roi(
    entry_price: float,
    exit_price: float,
    entry_date: date,
    exit_date: date,
    expiration: date,
    strike_price: float,
    risk_free_rate: float = 0.05,
    sigma: float = 0.40,
) -> float:
    """Estimate LEAPS call ROI using Black-Scholes at entry and exit.

    Computes option price at entry and exit via BS, then returns
    percentage return. This captures delta, gamma, and theta effects.

    Args:
        entry_price: Stock price at option entry.
        exit_price: Stock price at option exit.
        entry_date: Date of option entry.
        exit_date: Date of option exit.
        expiration: Option expiration date.
        strike_price: Option strike price.
        risk_free_rate: Annual risk-free rate.
        sigma: Annualized implied volatility.

    Returns:
        Option ROI percentage (e.g. 80.0 = +80%).
    """
    dte_entry = max(1, (expiration - entry_date).days)
    dte_exit = max(1, (expiration - exit_date).days)

    t_entry = dte_entry / 365.0
    t_exit = dte_exit / 365.0

    opt_entry = _bs_call_price(entry_price, strike_price, t_entry, risk_free_rate, sigma)
    opt_exit = _bs_call_price(exit_price, strike_price, t_exit, risk_free_rate, sigma)

    if opt_entry <= 0:
        return 0.0
    return (opt_exit / opt_entry - 1.0) * 100.0


# ── GA Engine ──────────────────────────────────────────────────────────────

_DRAWDOWN_THRESHOLD_OPTIONS = [10.0, 15.0, 20.0, 25.0, 30.0]
_ENTRY_MODE_OPTIONS = ["touch", "bounce", "both"]
_STAGE1_DAYS_RANGE = (10, 30)
_STAGE2_DAYS_RANGE = (30, 90)
_STAGE1_PROFIT_RANGE = (60.0, 120.0)
_STAGE2_PROFIT_RANGE = (40.0, 100.0)
_SELL_PCT_RANGE = (30.0, 70.0)


@dataclass
class LeapsEvolutionConfig:
    """GA hyperparameters for LEAPS evolution."""
    population_size: int = 50
    generations: int = 20
    mutation_rate: float = 0.15
    crossover_rate: float = 0.80
    elitism_count: int = 3
    tournament_size: int = 4
    seed: int | None = None

    def __post_init__(self):
        if self.elitism_count >= self.population_size:
            self.elitism_count = max(1, self.population_size // 5)
        if self.tournament_size >= self.population_size:
            self.tournament_size = max(2, self.population_size // 10)


@dataclass
class LeapsParamRanges:
    """Custom parameter ranges for LEAPS GA evolution."""
    drawdown_threshold_pct: tuple[float, float] = (10.0, 30.0)
    stage1_days: tuple[int, int] = (10, 30)
    stage2_days: tuple[int, int] = (30, 90)
    stage1_profit: tuple[float, float] = (60.0, 120.0)
    stage2_profit: tuple[float, float] = (40.0, 100.0)
    stage1_sell: tuple[float, float] = (30.0, 70.0)
    stage2_sell: tuple[float, float] = (30.0, 70.0)

    @classmethod
    def from_dict(cls, d: dict[str, list] | None) -> "LeapsParamRanges":
        """Create from {param_name: [min, max]} dict, using defaults for missing."""
        if not d:
            return cls()
        kwargs = {}
        for field_name in ["drawdown_threshold_pct", "stage1_days", "stage2_days",
                           "stage1_profit", "stage2_profit", "stage1_sell", "stage2_sell"]:
            val = d.get(field_name)
            if isinstance(val, list) and len(val) == 2:
                lo = val[0] if val[0] is not None else getattr(cls(), field_name)[0]
                hi = val[1] if val[1] is not None else getattr(cls(), field_name)[1]
                kwargs[field_name] = (float(lo), float(hi))
        return cls(**{k: kwargs.get(k, getattr(cls(), k)) for k in [
            "drawdown_threshold_pct", "stage1_days", "stage2_days",
            "stage1_profit", "stage2_profit", "stage1_sell", "stage2_sell",
        ]})


def _dd_options(ranges: LeapsParamRanges) -> list[float]:
    lo, hi = ranges.drawdown_threshold_pct
    vals = []
    v = lo
    while v <= hi + 0.01:
        vals.append(round(v, 1))
        v += 5.0
    return vals or [lo]


def leaps_crossover(parent1: LeapsIndividual, parent2: LeapsIndividual) -> LeapsIndividual:
    """Uniform crossover: each gene from parent1 or parent2 randomly."""
    def pick(f1, f2):
        return f1 if random.random() < 0.5 else f2

    dd = pick(parent1.drawdown_threshold_pct, parent2.drawdown_threshold_pct)
    mode = pick(parent1.entry_mode, parent2.entry_mode)
    s1d = pick(parent1.stage1_days, parent2.stage1_days)
    s1p = pick(parent1.stage1_profit, parent2.stage1_profit)
    s1s = pick(parent1.stage1_sell, parent2.stage1_sell)
    s2d = pick(parent1.stage2_days, parent2.stage2_days)
    s2p = pick(parent2.stage2_profit, parent2.stage2_profit)
    s2s = pick(parent1.stage2_sell, parent2.stage2_sell)

    # Enforce constraints
    s1d, s2d = _enforce_day_order(s1d, s2d)
    s1p, s2p = _enforce_profit_order(s1p, s2p)

    return LeapsIndividual(
        drawdown_threshold_pct=dd, entry_mode=mode,
        stage1_days=s1d, stage1_profit=s1p, stage1_sell=s1s,
        stage2_days=s2d, stage2_profit=s2p, stage2_sell=s2s,
    )


def leaps_mutate(
    individual: LeapsIndividual,
    config: LeapsEvolutionConfig,
    ranges: LeapsParamRanges | None = None,
) -> LeapsIndividual:
    """Mutate random genes, preserving stage constraints."""
    r = ranges or LeapsParamRanges()
    dd_opts = _dd_options(r)
    dd = individual.drawdown_threshold_pct
    mode = individual.entry_mode
    s1d = individual.stage1_days
    s1p = individual.stage1_profit
    s1s = individual.stage1_sell
    s2d = individual.stage2_days
    s2p = individual.stage2_profit
    s2s = individual.stage2_sell

    if random.random() < config.mutation_rate:
        dd = random.choice(dd_opts)
    if random.random() < config.mutation_rate:
        mode = random.choice(_ENTRY_MODE_OPTIONS)
    if random.random() < config.mutation_rate:
        s1d = random.randint(int(r.stage1_days[0]), int(r.stage1_days[1]))
    if random.random() < config.mutation_rate:
        s1p = round(random.uniform(*r.stage1_profit), 0)
    if random.random() < config.mutation_rate:
        s1s = round(random.uniform(*r.stage1_sell), 0)
    if random.random() < config.mutation_rate:
        s2d = random.randint(int(r.stage2_days[0]), int(r.stage2_days[1]))
    if random.random() < config.mutation_rate:
        s2p = round(random.uniform(*r.stage2_profit), 0)
    if random.random() < config.mutation_rate:
        s2s = round(random.uniform(*r.stage2_sell), 0)

    s1d, s2d = _enforce_day_order(s1d, s2d)
    s1p, s2p = _enforce_profit_order(s1p, s2p)

    return LeapsIndividual(
        drawdown_threshold_pct=dd, entry_mode=mode,
        stage1_days=s1d, stage1_profit=s1p, stage1_sell=s1s,
        stage2_days=s2d, stage2_profit=s2p, stage2_sell=s2s,
    )


def leaps_fitness_fn(
    individual: LeapsIndividual,
    price_series_by_symbol: dict[str, list[tuple[date, float]]],
) -> float:
    """Evaluate fitness: D-weighted ROI percentiles.

    Fitness = 0.5 * median_roi + 0.3 * p25_roi + 0.2 * p75_roi

    For each symbol's price series: detect entries, simulate trades,
    collect all ROIs, compute weighted percentile score.
    """
    all_rois: list[float] = []

    for symbol, prices in price_series_by_symbol.items():
        entries = detect_leaps_entries(
            prices, individual.drawdown_threshold_pct, individual.entry_mode,
        )
        stages = individual.to_stages()
        for entry in entries:
            trade = compute_sell_ladder(entry, prices, stages, expiration_days=190,
                                         strike_price=entry.price * 1.10)
            all_rois.append(trade.total_roi_pct)

    if not all_rois:
        return 0.0

    sorted_rois = sorted(all_rois)
    n = len(sorted_rois)

    def percentile(pct: float) -> float:
        k = (n - 1) * pct / 100.0
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_rois[int(k)]
        return sorted_rois[int(f)] * (c - k) + sorted_rois[int(c)] * (k - f)

    median = percentile(50.0)
    p25 = percentile(25.0)
    p75 = percentile(75.0)

    return 0.5 * median + 0.3 * p25 + 0.2 * p75


def _enforce_day_order(d1: int, d2: int, ranges: LeapsParamRanges | None = None) -> tuple[int, int]:
    """Ensure stage1_days < stage2_days within ranges."""
    r = ranges or LeapsParamRanges()
    s1_hi = int(r.stage1_days[1])
    s2_hi = int(r.stage2_days[1])
    if d1 >= d2:
        d2 = d1 + random.randint(10, 30)
        if d2 > s2_hi:
            d2 = s2_hi
            d1 = d2 - random.randint(10, 20)
    return d1, d2


def _enforce_profit_order(p1: float, p2: float, ranges: LeapsParamRanges | None = None) -> tuple[float, float]:
    """Ensure stage1_profit > stage2_profit within ranges."""
    r = ranges or LeapsParamRanges()
    s1_hi = r.stage1_profit[1]
    if p1 <= p2:
        p1 = p2 + random.uniform(10.0, 30.0)
        if p1 > s1_hi:
            p1 = s1_hi
            p2 = p1 - random.uniform(10.0, 30.0)
    return p1, p2


def _random_individual(ranges: LeapsParamRanges | None = None) -> LeapsIndividual:
    """Create a random valid LEAPS individual."""
    r = ranges or LeapsParamRanges()
    dd = random.choice(_dd_options(r))
    mode = random.choice(_ENTRY_MODE_OPTIONS)
    s1d = random.randint(int(r.stage1_days[0]), int(r.stage1_days[1]))
    s2d = random.randint(int(r.stage2_days[0]), int(r.stage2_days[1]))
    s1p = round(random.uniform(*r.stage1_profit), 0)
    s2p = round(random.uniform(*r.stage2_profit), 0)
    s1s = round(random.uniform(*r.stage1_sell), 0)
    s2s = round(random.uniform(*r.stage2_sell), 0)
    s1d, s2d = _enforce_day_order(s1d, s2d, r)
    s1p, s2p = _enforce_profit_order(s1p, s2p, r)
    return LeapsIndividual(
        drawdown_threshold_pct=dd, entry_mode=mode,
        stage1_days=s1d, stage1_profit=s1p, stage1_sell=s1s,
        stage2_days=s2d, stage2_profit=s2p, stage2_sell=s2s,
    )


def _tournament_select(
    population: list[LeapsIndividual],
    fitnesses: list[float],
    tournament_size: int,
) -> LeapsIndividual:
    indices = random.sample(range(len(population)), min(tournament_size, len(population)))
    best_idx = max(indices, key=lambda i: fitnesses[i])
    return population[best_idx]


def evolve_leaps_parameters(
    price_series_by_symbol: dict[str, list[tuple[date, float]]],
    config: LeapsEvolutionConfig | None = None,
    param_ranges: LeapsParamRanges | None = None,
) -> dict[str, object]:
    """Run genetic algorithm to optimize LEAPS call parameters.

    Args:
        price_series_by_symbol: Symbol → list of (date, price) tuples.
        config: GA hyperparameters.
        param_ranges: Custom parameter ranges for evolution.

    Returns:
        Dict with "best", "final_population", "snapshots", "config".
    """
    config = config or LeapsEvolutionConfig()
    ranges = param_ranges or LeapsParamRanges()
    if config.seed is not None:
        random.seed(config.seed)

    # Initialize population with dedup
    seen_keys: set[str] = set()
    population: list[LeapsIndividual] = []
    while len(population) < config.population_size:
        ind = _random_individual(ranges)
        if ind.key not in seen_keys:
            seen_keys.add(ind.key)
            population.append(ind)

    fitnesses = [leaps_fitness_fn(ind, price_series_by_symbol) for ind in population]

    snapshots: list[dict[str, object]] = []
    best_individual = population[0]
    best_fitness = fitnesses[0]
    all_evaluated: dict[str, tuple[LeapsIndividual, float]] = {}

    for gen in range(config.generations):
        ranked = sorted(zip(population, fitnesses), key=lambda x: x[1], reverse=True)
        population = [x[0] for x in ranked]
        fitnesses = [x[1] for x in ranked]

        for ind, fit in ranked:
            if ind.key not in all_evaluated or fit > all_evaluated[ind.key][1]:
                all_evaluated[ind.key] = (ind, fit)

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
        fitnesses = [leaps_fitness_fn(ind, price_series_by_symbol) for ind in population]

    ranked_final = sorted(all_evaluated.items(), key=lambda x: x[1][1], reverse=True)
    final_rows: list[dict[str, object]] = []
    for rank, (key, (ind, fit)) in enumerate(ranked_final, start=1):
        final_rows.append({
            "rank": rank,
            "key": ind.key,
            "fitness": fit,
            **ind.to_params_dict(),
        })

    return {
        "config": {
            "population_size": config.population_size,
            "generations": config.generations,
            "mutation_rate": config.mutation_rate,
            "crossover_rate": config.crossover_rate,
            "elitism_count": config.elitism_count,
            "tournament_size": config.tournament_size,
            "seed": config.seed,
        },
        "snapshots": snapshots,
        "best": final_rows[0] if final_rows else None,
        "final_population": final_rows,
        "total_evaluated": len(all_evaluated),
    }
