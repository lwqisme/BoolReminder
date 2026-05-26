"""File-backed Strategy Lab run history."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


ROOT_DIR = Path(__file__).resolve().parent.parent
RUN_ID_PATTERN = re.compile(r"^[0-9]{14}_[a-f0-9]{8}$")
PRESET_ID_PATTERN = re.compile(r"^[0-9]{14}_[a-f0-9]{8}$")


def strategy_lab_data_dir() -> Path:
    configured = os.environ.get("STRATEGY_LAB_DATA_DIR")
    if configured:
        return Path(configured)
    return ROOT_DIR / "data" / "strategy_lab"


def runs_dir() -> Path:
    return strategy_lab_data_dir() / "runs"


def presets_dir() -> Path:
    return strategy_lab_data_dir() / "presets"


def save_run_snapshot(
    kind: str,
    payload: Mapping[str, object],
    result: Mapping[str, object],
    *,
    job_id: str | None = None,
) -> dict[str, object]:
    directory = runs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    snapshot = {
        "schema_version": 1,
        "id": run_id,
        "kind": kind,
        "kind_label": _kind_label(kind),
        "job_id": job_id,
        "created_at": now.isoformat(),
        "config_payload": _json_safe(dict(payload)),
        "config_summary": _config_summary(payload),
        "result_summary": _result_summary(kind, result),
    }
    path = directory / f"{run_id}.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return _public_snapshot(snapshot, include_config=False)


def list_run_snapshots(limit: int = 50, kind: str | None = None) -> list[dict[str, object]]:
    directory = runs_dir()
    if not directory.exists():
        return []
    limit = max(1, min(int(limit or 50), 200))
    snapshots: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        if len(snapshots) >= limit:
            break
        snapshot = _read_snapshot(path)
        if not snapshot:
            continue
        if kind and snapshot.get("kind") != kind:
            continue
        snapshots.append(_public_snapshot(snapshot, include_config=False))
    return snapshots


def load_run_snapshot(run_id: str) -> dict[str, object] | None:
    if not RUN_ID_PATTERN.match(run_id):
        return None
    snapshot = _read_snapshot(runs_dir() / f"{run_id}.json")
    if not snapshot:
        return None
    return _public_snapshot(snapshot, include_config=True)


def delete_run_snapshot(run_id: str) -> bool:
    if not RUN_ID_PATTERN.match(run_id):
        return False
    path = runs_dir() / f"{run_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def save_experiment_preset(name: str, payload: Mapping[str, object]) -> dict[str, object]:
    cleaned_name = str(name or "").strip()
    if not cleaned_name:
        raise ValueError("预设名称不能为空")
    if len(cleaned_name) > 80:
        raise ValueError("预设名称不能超过 80 个字符")
    directory = presets_dir()
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    preset_id = f"{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    preset = {
        "schema_version": 1,
        "id": preset_id,
        "name": cleaned_name,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "config_payload": _json_safe(dict(payload)),
        "config_summary": _config_summary(payload),
    }
    path = directory / f"{preset_id}.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return _public_preset(preset, include_config=False)


def list_experiment_presets(limit: int = 50) -> list[dict[str, object]]:
    directory = presets_dir()
    if not directory.exists():
        return []
    limit = max(1, min(int(limit or 50), 200))
    presets: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        if len(presets) >= limit:
            break
        preset = _read_snapshot(path)
        if not preset:
            continue
        presets.append(_public_preset(preset, include_config=False))
    return presets


def load_experiment_preset(preset_id: str) -> dict[str, object] | None:
    if not PRESET_ID_PATTERN.match(preset_id):
        return None
    preset = _read_snapshot(presets_dir() / f"{preset_id}.json")
    if not preset:
        return None
    return _public_preset(preset, include_config=True)


def delete_experiment_preset(preset_id: str) -> bool:
    if not PRESET_ID_PATTERN.match(preset_id):
        return False
    path = presets_dir() / f"{preset_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def _read_snapshot(path: Path) -> dict[str, object] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    return raw


def _public_snapshot(snapshot: Mapping[str, object], *, include_config: bool) -> dict[str, object]:
    keys = [
        "id",
        "kind",
        "kind_label",
        "job_id",
        "created_at",
        "config_summary",
        "result_summary",
    ]
    public = {key: snapshot.get(key) for key in keys if key in snapshot}
    if include_config:
        public["config_payload"] = snapshot.get("config_payload") or {}
    return public


def _public_preset(preset: Mapping[str, object], *, include_config: bool) -> dict[str, object]:
    keys = [
        "id",
        "name",
        "created_at",
        "updated_at",
        "config_summary",
    ]
    public = {key: preset.get(key) for key in keys if key in preset}
    if include_config:
        public["config_payload"] = preset.get("config_payload") or {}
    return public


def _json_safe(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _kind_label(kind: str) -> str:
    return {
        "run": "组合演算",
        "score": "策略评分",
        "scan": "参数扫描",
        "robust": "收益 Top10",
        "evolve": "遗传优化",
    }.get(kind, kind)


def _config_summary(payload: Mapping[str, object]) -> dict[str, object]:
    targets = payload.get("targets")
    target_count = len(targets) if isinstance(targets, list) else 0
    weight_sum = 0.0
    if isinstance(targets, list):
        for item in targets:
            if not isinstance(item, Mapping):
                continue
            try:
                weight_sum += float(item.get("weight") or 0)
            except (TypeError, ValueError):
                continue
    return {
        "start": payload.get("start"),
        "end": payload.get("end"),
        "target_count": target_count,
        "target_weight": round(weight_sum, 4),
        "buy_strategy": _strategy_selector(payload.get("buy_strategies")) or payload.get("buy_strategy"),
        "sell_strategy": _strategy_selector(payload.get("sell_strategies")),
        "score_topics": len(payload.get("scorecard_portfolio_keys") or []),
        "score_periods": len(payload.get("scorecard_periods") or []),
        "scan_trading_days": payload.get("trading_days"),
    }


def _strategy_selector(raw: object) -> str | None:
    if not isinstance(raw, list):
        return None
    if len(raw) == 1:
        return str(raw[0])
    if len(raw) > 1:
        return "all"
    return None


def _result_summary(kind: str, result: Mapping[str, object]) -> dict[str, object]:
    if kind == "run":
        return _run_result_summary(result)
    if kind == "score":
        return _score_result_summary(result)
    if kind == "scan":
        return _scan_result_summary(result)
    if kind == "robust":
        return _robust_result_summary(result)
    if kind == "evolve":
        return _evolve_result_summary(result)
    return {
        "warnings": _warnings(result),
    }


def _run_result_summary(result: Mapping[str, object]) -> dict[str, object]:
    strategies = result.get("strategies")
    strategies = strategies if isinstance(strategies, list) else []
    best = _best_by_metric(strategies, "return_pct", reverse=True)
    worst_drawdown = _best_by_metric(strategies, "max_drawdown_pct", reverse=True)
    return {
        "strategy_count": len(strategies),
        "range": result.get("range") or {},
        "best_label": best.get("label") if best else None,
        "best_return_pct": _metric(best, "return_pct") if best else None,
        "worst_drawdown_label": worst_drawdown.get("label") if worst_drawdown else None,
        "worst_drawdown_pct": _metric(worst_drawdown, "max_drawdown_pct") if worst_drawdown else None,
        "warnings": _warnings(result),
    }


def _score_result_summary(result: Mapping[str, object]) -> dict[str, object]:
    summary = result.get("summary")
    summary = summary if isinstance(summary, list) else []
    questions = result.get("questions")
    questions = questions if isinstance(questions, list) else []
    top = summary[0] if summary and isinstance(summary[0], Mapping) else None
    return {
        "strategy_count": len(summary),
        "question_count": len(questions),
        "range": result.get("range") or {},
        "top_label": top.get("label") if top else None,
        "top_score": _number(top.get("score")) if top else None,
        "top_return_pct": _number(top.get("avg_return_pct")) if top else None,
        "top_drawdown_pct": _number(top.get("avg_drawdown_pct")) if top else None,
        "warnings": _warnings(result),
    }


def _scan_result_summary(result: Mapping[str, object]) -> dict[str, object]:
    cells = result.get("cells")
    cells = cells if isinstance(cells, list) else []
    best = result.get("best")
    best = best if isinstance(best, Mapping) else None
    return {
        "cell_count": len(cells),
        "range": result.get("range") or {},
        "buy_strategy_label": result.get("buy_strategy_label"),
        "best_sell_min_profit_pct": _number(best.get("sell_min_profit_pct")) if best else None,
        "best_repair_sell_cooldown_days": _number(best.get("repair_sell_cooldown_days")) if best else None,
        "best_repair_stage_sell_pct": _number(best.get("repair_stage_sell_pct")) if best else None,
        "best_return_pct": _number(best.get("return_pct")) if best else None,
        "best_drawdown_pct": _number(best.get("max_drawdown_pct")) if best else None,
        "warnings": _warnings(result),
    }


def _robust_result_summary(result: Mapping[str, object]) -> dict[str, object]:
    leaderboard = result.get("leaderboard")
    leaderboard = leaderboard if isinstance(leaderboard, list) else []
    tasks = result.get("tasks")
    tasks = tasks if isinstance(tasks, list) else []
    top = leaderboard[0] if leaderboard and isinstance(leaderboard[0], Mapping) else None
    candidate = top.get("candidate") if isinstance(top, Mapping) else None
    candidate = candidate if isinstance(candidate, Mapping) else {}
    return {
        "leaderboard_count": len(leaderboard),
        "task_count": len(tasks),
        "range": result.get("range") or {},
        "top_label": candidate.get("label"),
        "top_score": _number(top.get("score", top.get("robust_score"))) if top else None,
        "top_return_pct": _number(top.get("avg_return_pct")) if top else None,
        "top_drawdown_pct": _number(top.get("avg_drawdown_pct")) if top else None,
        "warnings": _warnings(result),
    }


def _evolve_result_summary(result: Mapping[str, object]) -> dict[str, object]:
    snaps = result.get("snapshots")
    snaps = snaps if isinstance(snaps, list) else []
    population = result.get("final_population")
    population = population if isinstance(population, list) else []
    best = result.get("best")
    best = best if isinstance(best, Mapping) else None
    last_snap = snaps[-1] if snaps and isinstance(snaps[-1], Mapping) else None
    return {
        "generations": len(snaps),
        "total_evaluated": result.get("total_evaluated") or 0,
        "population_size": len(population),
        "range": result.get("range") or {},
        "buy_strategy_label": result.get("buy_strategy_label"),
        "sell_strategy_label": result.get("sell_strategy_label"),
        "best_fitness": _number(last_snap.get("best_fitness")) if last_snap else None,
        "best_label": best.get("label") if best else None,
        "best_rank": best.get("rank") if best else None,
        "warnings": _warnings(result),
    }


def _best_by_metric(items: list[object], metric: str, *, reverse: bool) -> Mapping[str, object] | None:
    candidates = [item for item in items if isinstance(item, Mapping) and _metric(item, metric) is not None]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: _metric(item, metric) or 0, reverse=reverse)[0]


def _metric(item: Mapping[str, object] | None, key: str) -> float | None:
    if not item:
        return None
    metrics = item.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    return _number(metrics.get(key))


def _number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _warnings(result: Mapping[str, object]) -> list[str]:
    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [str(item) for item in warnings]
