"""Shared score helpers for Strategy Lab ranking payloads."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping


SCORE_FORMULA_VERSION = "return_drawdown_sellquality_v2"
DEFAULT_RETURN_WEIGHT = 0.9
DEFAULT_DRAWDOWN_WEIGHT = 0.1
DEFAULT_SELL_QUALITY_WEIGHT = 0.0


def normalize_bigger_better(value: float, values: Iterable[float]) -> float:
    values = [float(item) for item in values]
    if not values:
        return 1.0
    low = min(values)
    high = max(values)
    if abs(high - low) < 1e-9:
        return 1.0
    return (float(value) - low) / (high - low)


def _resolve_weights(
    return_weight: float,
    drawdown_weight: float,
    sell_quality_weight: float,
) -> tuple[float, float, float]:
    """Resolve effective weights so return+drawdown+sell_quality sum to 1.0.

    Accepts the raw sell_quality_weight (0.0–1.0). When it is > 0, the
    original return/drawdown weights are scaled down proportionally.
    """
    if sell_quality_weight <= 0.0:
        return (return_weight, drawdown_weight, 0.0)
    sq = float(sell_quality_weight)
    return (return_weight * (1.0 - sq), drawdown_weight * (1.0 - sq), sq)


def score_topic_observations(
    observations: Iterable[Mapping[str, object]],
    *,
    return_weight: float = DEFAULT_RETURN_WEIGHT,
    drawdown_weight: float = DEFAULT_DRAWDOWN_WEIGHT,
    sell_quality_weight: float = DEFAULT_SELL_QUALITY_WEIGHT,
) -> list[dict[str, object]]:
    """Score observations inside one topic and attach rank/explainability fields."""
    rows = [dict(item) for item in observations]
    returns = [float(item.get("return_pct", 0.0)) for item in rows]
    drawdown_controls = [float(item.get("max_drawdown_pct", 0.0)) for item in rows]
    sell_qualities = [float(item.get("sell_quality_score", 0.0)) for item in rows]
    eff_return, eff_drawdown, eff_sq = _resolve_weights(return_weight, drawdown_weight, sell_quality_weight)
    for row in rows:
        return_score = normalize_bigger_better(float(row.get("return_pct", 0.0)), returns) * 100.0
        drawdown_score = normalize_bigger_better(float(row.get("max_drawdown_pct", 0.0)), drawdown_controls) * 100.0
        sell_quality_norm = normalize_bigger_better(float(row.get("sell_quality_score", 0.0)), sell_qualities) * 100.0
        topic_score = return_score * eff_return + drawdown_score * eff_drawdown + sell_quality_norm * eff_sq
        row["return_score"] = return_score
        row["drawdown_score"] = drawdown_score
        row["sell_quality_score"] = sell_quality_norm
        row["topic_score"] = topic_score
    for rank, row in enumerate(
        sorted(rows, key=lambda item: float(item.get("topic_score", 0.0)), reverse=True),
        start=1,
    ):
        row["topic_rank"] = rank
    return rows


def score_parameter_matrix(
    candidates: Iterable[Mapping[str, object]],
    observations: Iterable[Mapping[str, object]],
    *,
    return_weight: float = DEFAULT_RETURN_WEIGHT,
    drawdown_weight: float = DEFAULT_DRAWDOWN_WEIGHT,
    sell_quality_weight: float = DEFAULT_SELL_QUALITY_WEIGHT,
) -> dict[str, object]:
    """Score candidate/topic observations into ranked Parameter Lab rows."""
    candidate_by_key = {
        str(candidate.get("key") or candidate.get("combination_key")): dict(candidate)
        for candidate in candidates
    }
    observations_by_topic: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for observation in observations:
        observations_by_topic[str(observation.get("topic_key") or observation.get("task_key"))].append(observation)

    scored_cells: list[dict[str, object]] = []
    for topic_key in sorted(observations_by_topic):
        scored_cells.extend(
            score_topic_observations(
                observations_by_topic[topic_key],
                return_weight=return_weight,
                drawdown_weight=drawdown_weight,
                sell_quality_weight=sell_quality_weight,
            )
        )

    cells_by_candidate: dict[str, list[dict[str, object]]] = defaultdict(list)
    for cell in scored_cells:
        candidate_key = str(cell.get("candidate_key") or cell.get("key") or cell.get("combination_key"))
        cells_by_candidate[candidate_key].append(cell)

    rows: list[dict[str, object]] = []
    for candidate_key, candidate in candidate_by_key.items():
        cells = cells_by_candidate.get(candidate_key, [])
        rows.append(
            {
                "key": candidate_key,
                "combination_key": candidate.get("combination_key", candidate_key),
                "candidate": candidate,
                "avg_return_pct": _avg(float(cell.get("return_pct", 0.0)) for cell in cells),
                "avg_drawdown_pct": _avg(float(cell.get("max_drawdown_pct", 0.0)) for cell in cells),
                "avg_topic_score": _avg(float(cell.get("topic_score", 0.0)) for cell in cells),
                "final_score": _avg(float(cell.get("topic_score", 0.0)) for cell in cells),
                "topic_count": len(cells),
                "cells": sorted(cells, key=lambda cell: str(cell.get("topic_key") or cell.get("task_key"))),
            }
        )

    rows.sort(key=lambda item: float(item.get("final_score", 0.0)), reverse=True)
    for rank, row in enumerate(rows, start=1):
        row["final_rank"] = rank

    eff_return, eff_drawdown, eff_sq = _resolve_weights(return_weight, drawdown_weight, sell_quality_weight)
    return {
        "formula_version": SCORE_FORMULA_VERSION,
        "weights": {
            "return": eff_return,
            "drawdown": eff_drawdown,
            "sell_quality": eff_sq,
        },
        "rows": rows,
        "cells": scored_cells,
    }


def _avg(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0
