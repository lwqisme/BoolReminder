"""Live strategy profile storage for real-account reminders."""

from __future__ import annotations

import json
import hashlib
import os
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drawdown.position_strategy import SELL_STRATEGY_LABELS, STRATEGY_LABELS, StrategyInputs
from drawdown.strategy_parameter_registry import (
    BASELINE_PARAMETER_FIELDS,
    BUY_PARAMETER_FIELDS,
    SELL_PARAMETER_FIELDS,
    STRATEGY_DEFINITION_VERSION,
)
from trade_sync.normalize import canonical_symbol, infer_longbridge_symbol


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data" / "account_signal"
PROFILES_PATH = DATA_DIR / "profiles.json"
PROFILE_CANDIDATES_PATH = DATA_DIR / "profile_candidates.json"

PROFILE_PARAMETER_FIELDS = tuple(field.name for field in fields(StrategyInputs))
LAB_PARAMETER_FIELDS = tuple(dict.fromkeys((*BASELINE_PARAMETER_FIELDS, *BUY_PARAMETER_FIELDS, *SELL_PARAMETER_FIELDS)))
ALLOWED_PARAMETER_FIELDS = set(PROFILE_PARAMETER_FIELDS)
NULLABLE_PARAMETER_FIELDS = {"sell_stage_rearm_drawdown_pct"}


@dataclass(frozen=True)
class AccountSignalProfile:
    symbol: str
    enabled: bool
    buy_strategy: str
    sell_strategy: str
    parameters: dict[str, Any]
    candidate_key: str = ""
    strategy_definition_version: str = STRATEGY_DEFINITION_VERSION
    source: str = "live"
    promoted_at: str = ""
    note: str = ""

    @property
    def profile_id(self) -> str:
        source_key = self.candidate_key or self.source or "manual"
        return f"{self.symbol}:{self.buy_strategy}:{self.sell_strategy}:{source_key}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["profile_id"] = self.profile_id
        payload["summary"] = profile_summary(self)
        return payload


@dataclass(frozen=True)
class AccountSignalProfileCandidate:
    candidate_key: str
    buy_strategy: str
    sell_strategy: str
    parameters: dict[str, Any]
    parameter_hash: str
    strategy_definition_version: str = STRATEGY_DEFINITION_VERSION
    source: str = "parameter_lab"
    saved_at: str = ""
    note: str = ""
    candidate_snapshot: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["summary"] = candidate_summary(self)
        return payload


def normalize_profile_symbol(symbol: str) -> str:
    return infer_longbridge_symbol(canonical_symbol(symbol), "US")


def strategy_inputs_for_profile(
    profile: AccountSignalProfile,
    *,
    fallback_same_day_sell: bool | None = None,
) -> StrategyInputs:
    kwargs: dict[str, Any] = {}
    for key, value in profile.parameters.items():
        if key in ALLOWED_PARAMETER_FIELDS and (value is not None or key in NULLABLE_PARAMETER_FIELDS):
            kwargs[key] = value
    if "grid_sell_pct" not in kwargs and "grid_second_sell_pct" in kwargs:
        kwargs["grid_sell_pct"] = kwargs["grid_second_sell_pct"]
    if "sell_allow_same_day_sell" not in profile.parameters and fallback_same_day_sell is not None:
        kwargs["sell_allow_same_day_sell"] = bool(fallback_same_day_sell)
    return replace(StrategyInputs(), **kwargs)


def load_profiles(path: Path | None = None) -> dict[str, AccountSignalProfile]:
    profile_path = path or PROFILES_PATH
    if not profile_path.exists():
        return {}
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    items = raw.get("profiles", raw) if isinstance(raw, dict) else {}
    if not isinstance(items, dict):
        return {}
    profiles: dict[str, AccountSignalProfile] = {}
    for symbol, payload in items.items():
        if not isinstance(payload, dict):
            continue
        profile = validate_profile_payload({**payload, "symbol": payload.get("symbol") or symbol})
        profiles[profile.symbol] = profile
    return profiles


def save_profiles(profiles: dict[str, AccountSignalProfile], path: Path | None = None) -> None:
    profile_path = path or PROFILES_PATH
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "strategy_definition_version": STRATEGY_DEFINITION_VERSION,
        "profiles": {
            symbol: _persisted_profile_payload(profile)
            for symbol, profile in sorted(profiles.items())
        },
    }
    tmp_path = profile_path.with_suffix(profile_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, profile_path)


def load_profile_candidates(path: Path | None = None) -> dict[str, AccountSignalProfileCandidate]:
    candidate_path = path or PROFILE_CANDIDATES_PATH
    if not candidate_path.exists():
        return {}
    raw = json.loads(candidate_path.read_text(encoding="utf-8"))
    items = raw.get("candidates", raw) if isinstance(raw, dict) else {}
    if not isinstance(items, dict):
        return {}
    candidates: dict[str, AccountSignalProfileCandidate] = {}
    for key, payload in items.items():
        if not isinstance(payload, dict):
            continue
        candidate = validate_candidate_payload({**payload, "candidate_key": payload.get("candidate_key") or key})
        candidates[candidate.candidate_key] = candidate
    return candidates


def save_profile_candidates(
    candidates: dict[str, AccountSignalProfileCandidate],
    path: Path | None = None,
) -> None:
    candidate_path = path or PROFILE_CANDIDATES_PATH
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "strategy_definition_version": STRATEGY_DEFINITION_VERSION,
        "candidates": {
            key: _persisted_candidate_payload(candidate)
            for key, candidate in sorted(candidates.items())
        },
    }
    tmp_path = candidate_path.with_suffix(candidate_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, candidate_path)


def active_profiles_for_symbols(symbols: list[str] | tuple[str, ...]) -> dict[str, AccountSignalProfile]:
    saved = load_profiles()
    defaults = built_in_default_profiles()
    result: dict[str, AccountSignalProfile] = {}
    for raw_symbol in symbols:
        symbol = normalize_profile_symbol(raw_symbol)
        profile = saved.get(symbol) or defaults.get(symbol)
        if profile and profile.enabled:
            result[symbol] = profile
    return result


def built_in_default_profiles() -> dict[str, AccountSignalProfile]:
    from account_signal.config import googl_inputs, tsla_inputs

    googl = _input_parameters(googl_inputs(), exclude_same_day=True)
    tsla = _input_parameters(tsla_inputs(), exclude_same_day=True)
    return {
        "GOOGL.US": AccountSignalProfile(
            symbol="GOOGL.US",
            enabled=True,
            buy_strategy="core_dip_dca",
            sell_strategy="cost_deleverage",
            parameters=googl,
            source="built_in_default",
            strategy_definition_version=STRATEGY_DEFINITION_VERSION,
            note="Compatibility default for existing GOOGL real-account reminders.",
        ),
        "TSLA.US": AccountSignalProfile(
            symbol="TSLA.US",
            enabled=True,
            buy_strategy="linear_weighted_slice",
            sell_strategy="grid_rebound",
            parameters=tsla,
            source="built_in_default",
            strategy_definition_version=STRATEGY_DEFINITION_VERSION,
            note="Compatibility default for existing TSLA real-account reminders.",
        ),
    }


def profiles_status_payload(targets: dict[str, Any] | None = None) -> dict[str, Any]:
    saved = load_profiles()
    defaults = built_in_default_profiles()
    candidate_library = load_profile_candidates()
    enabled_targets = sorted(
        symbol
        for symbol, target in (targets or {}).items()
        if getattr(target, "enabled", False)
    )
    active_symbols = sorted(set(enabled_targets) | set(saved.keys()) | set(defaults.keys()))
    active: dict[str, Any] = {}
    missing: list[str] = []
    for symbol in active_symbols:
        profile = saved.get(symbol) or defaults.get(symbol)
        if profile and profile.enabled:
            active[symbol] = profile.to_dict()
        elif symbol in enabled_targets:
            missing.append(symbol)
    return {
        "active": active,
        "saved": {symbol: profile.to_dict() for symbol, profile in saved.items()},
        "built_in_defaults": {symbol: profile.to_dict() for symbol, profile in defaults.items()},
        "candidate_library": {key: candidate.to_dict() for key, candidate in candidate_library.items()},
        "enabled_targets": enabled_targets,
        "missing_profile_symbols": missing,
        "strategy_definition_version": STRATEGY_DEFINITION_VERSION,
    }


def save_candidate_to_library(
    *,
    candidate: dict[str, Any],
    note: str = "",
    dry_run: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ValueError("candidate 必须是对象")
    library = load_profile_candidates(path)
    new_candidate = candidate_from_lab_payload(candidate, note=note)
    previous_key = new_candidate.candidate_key if new_candidate.candidate_key in library else ""
    if not previous_key:
        for key, existing in library.items():
            if existing.parameter_hash == new_candidate.parameter_hash:
                previous_key = key
                break
    if previous_key:
        previous = library[previous_key]
        new_candidate = replace(
            new_candidate,
            candidate_key=previous.candidate_key,
            note=note,
            saved_at=new_candidate.saved_at,
        )
    else:
        previous = None
    diff = _candidate_diff(previous, new_candidate)
    if not dry_run:
        library[new_candidate.candidate_key] = new_candidate
        save_profile_candidates(library, path)
    return {
        "success": True,
        "dry_run": dry_run,
        "candidate_key": new_candidate.candidate_key,
        "previous": previous.to_dict() if previous else None,
        "candidate": new_candidate.to_dict(),
        "new": new_candidate.to_dict(),
        "diff": diff,
        "written": not dry_run,
        "created": previous is None,
    }


def assign_candidate_to_profile(
    *,
    symbol: str,
    candidate_key: str,
    note: str = "",
    dry_run: bool = False,
    profiles_path: Path | None = None,
    candidates_path: Path | None = None,
) -> dict[str, Any]:
    normalized_symbol = normalize_profile_symbol(symbol)
    key = str(candidate_key or "").strip()
    if not key:
        raise ValueError("缺少 candidate_key")
    library = load_profile_candidates(candidates_path)
    candidate = library.get(key)
    if candidate is None:
        raise ValueError(f"候选不存在: {key}")
    profiles = load_profiles(profiles_path)
    previous = profiles.get(normalized_symbol) or built_in_default_profiles().get(normalized_symbol)
    new_profile = profile_from_library_candidate(normalized_symbol, candidate, note=note)
    diff = _profile_diff(previous, new_profile)
    if not dry_run:
        profiles[normalized_symbol] = new_profile
        save_profiles(profiles, profiles_path)
    return {
        "success": True,
        "dry_run": dry_run,
        "symbol": normalized_symbol,
        "candidate_key": key,
        "candidate": candidate.to_dict(),
        "previous": previous.to_dict() if previous else None,
        "new": new_profile.to_dict(),
        "diff": diff,
        "written": not dry_run,
    }


def promote_candidate_to_profile(
    *,
    symbol: str = "",
    candidate: dict[str, Any],
    note: str = "",
    dry_run: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    """Compatibility alias: old promote calls now only save a reusable candidate."""
    return save_candidate_to_library(candidate=candidate, note=note, dry_run=dry_run, path=path)


def profile_from_candidate(symbol: str, candidate: dict[str, Any], *, note: str = "") -> AccountSignalProfile:
    parameters = _candidate_parameters_from_payload(candidate)
    payload = {
        "symbol": symbol,
        "enabled": True,
        "buy_strategy": candidate.get("buy_strategy"),
        "sell_strategy": candidate.get("sell_strategy"),
        "parameters": parameters,
        "candidate_key": candidate.get("key") or candidate.get("candidate_key") or _candidate_parameter_hash(
            candidate.get("buy_strategy"),
            candidate.get("sell_strategy"),
            parameters,
            candidate.get("strategy_definition_version") or STRATEGY_DEFINITION_VERSION,
        )[:16],
        "strategy_definition_version": candidate.get("strategy_definition_version") or STRATEGY_DEFINITION_VERSION,
        "source": "parameter_lab",
        "promoted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "note": note,
    }
    return validate_profile_payload(payload)


def profile_from_library_candidate(
    symbol: str,
    candidate: AccountSignalProfileCandidate,
    *,
    note: str = "",
) -> AccountSignalProfile:
    payload = {
        "symbol": symbol,
        "enabled": True,
        "buy_strategy": candidate.buy_strategy,
        "sell_strategy": candidate.sell_strategy,
        "parameters": dict(candidate.parameters),
        "candidate_key": candidate.candidate_key,
        "strategy_definition_version": candidate.strategy_definition_version,
        "source": candidate.source,
        "promoted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "note": note or candidate.note,
    }
    return validate_profile_payload(payload)


def candidate_from_lab_payload(candidate: dict[str, Any], *, note: str = "") -> AccountSignalProfileCandidate:
    if not isinstance(candidate, dict):
        raise ValueError("candidate 必须是对象")
    parameters = _candidate_parameters_from_payload(candidate)
    buy_strategy = str(candidate.get("buy_strategy", "") or "")
    sell_strategy = str(candidate.get("sell_strategy", "") or "")
    if buy_strategy not in STRATEGY_LABELS:
        raise ValueError(f"不支持的买入策略: {buy_strategy}")
    if sell_strategy not in SELL_STRATEGY_LABELS:
        raise ValueError(f"不支持的卖出策略: {sell_strategy}")
    validated_parameters = _validate_parameters(parameters)
    version = str(candidate.get("strategy_definition_version") or STRATEGY_DEFINITION_VERSION)
    parameter_hash = _candidate_parameter_hash(buy_strategy, sell_strategy, validated_parameters, version)
    candidate_key = str(candidate.get("key") or candidate.get("candidate_key") or f"candidate-{parameter_hash[:16]}")
    return validate_candidate_payload(
        {
            "candidate_key": candidate_key,
            "buy_strategy": buy_strategy,
            "sell_strategy": sell_strategy,
            "parameters": validated_parameters,
            "parameter_hash": parameter_hash,
            "strategy_definition_version": version,
            "source": str(candidate.get("source") or "parameter_lab"),
            "saved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "note": note,
            "candidate_snapshot": _jsonable(candidate),
        }
    )


def _candidate_parameters_from_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for key in LAB_PARAMETER_FIELDS:
        if key in candidate and (candidate[key] is not None or key in NULLABLE_PARAMETER_FIELDS):
            parameters[key] = candidate[key]
    snapshot_items = (
        (candidate.get("parameter_snapshot") or {}).items()
        if isinstance(candidate.get("parameter_snapshot"), dict)
        else []
    )
    for _group_key, value in snapshot_items:
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if nested_key not in LAB_PARAMETER_FIELDS:
                    raise ValueError(f"不支持的候选参数: {nested_key}")
                if nested_value is not None or nested_key in NULLABLE_PARAMETER_FIELDS:
                    parameters[nested_key] = nested_value
    return parameters


def validate_profile_payload(payload: dict[str, Any]) -> AccountSignalProfile:
    symbol = normalize_profile_symbol(str(payload.get("symbol", "") or ""))
    buy_strategy = str(payload.get("buy_strategy", "") or "")
    sell_strategy = str(payload.get("sell_strategy", "") or "")
    if buy_strategy not in STRATEGY_LABELS:
        raise ValueError(f"不支持的买入策略: {buy_strategy}")
    if sell_strategy not in SELL_STRATEGY_LABELS:
        raise ValueError(f"不支持的卖出策略: {sell_strategy}")
    parameters = _validate_parameters(payload.get("parameters") or {})
    return AccountSignalProfile(
        symbol=symbol,
        enabled=_bool(payload.get("enabled"), True),
        buy_strategy=buy_strategy,
        sell_strategy=sell_strategy,
        parameters=parameters,
        candidate_key=str(payload.get("candidate_key", "") or ""),
        strategy_definition_version=str(payload.get("strategy_definition_version") or STRATEGY_DEFINITION_VERSION),
        source=str(payload.get("source") or "live"),
        promoted_at=str(payload.get("promoted_at", "") or ""),
        note=str(payload.get("note", "") or ""),
    )


def validate_candidate_payload(payload: dict[str, Any]) -> AccountSignalProfileCandidate:
    candidate_key = str(payload.get("candidate_key", "") or "").strip()
    if not candidate_key:
        raise ValueError("缺少 candidate_key")
    buy_strategy = str(payload.get("buy_strategy", "") or "")
    sell_strategy = str(payload.get("sell_strategy", "") or "")
    if buy_strategy not in STRATEGY_LABELS:
        raise ValueError(f"不支持的买入策略: {buy_strategy}")
    if sell_strategy not in SELL_STRATEGY_LABELS:
        raise ValueError(f"不支持的卖出策略: {sell_strategy}")
    parameters = _validate_parameters(payload.get("parameters") or {})
    version = str(payload.get("strategy_definition_version") or STRATEGY_DEFINITION_VERSION)
    parameter_hash = str(payload.get("parameter_hash") or _candidate_parameter_hash(buy_strategy, sell_strategy, parameters, version))
    snapshot = payload.get("candidate_snapshot")
    return AccountSignalProfileCandidate(
        candidate_key=candidate_key,
        buy_strategy=buy_strategy,
        sell_strategy=sell_strategy,
        parameters=parameters,
        parameter_hash=parameter_hash,
        strategy_definition_version=version,
        source=str(payload.get("source") or "parameter_lab"),
        saved_at=str(payload.get("saved_at", "") or ""),
        note=str(payload.get("note", "") or ""),
        candidate_snapshot=snapshot if isinstance(snapshot, dict) else None,
    )


def profile_summary(profile: AccountSignalProfile) -> dict[str, Any]:
    params = profile.parameters
    return {
        "buy": _buy_summary(profile.buy_strategy, params),
        "sell": _sell_summary(profile.sell_strategy, params),
        "parameters": dict(params),
    }


def candidate_summary(candidate: AccountSignalProfileCandidate) -> dict[str, Any]:
    return {
        "buy": _buy_summary(candidate.buy_strategy, candidate.parameters),
        "sell": _sell_summary(candidate.sell_strategy, candidate.parameters),
        "parameters": dict(candidate.parameters),
    }


def _input_parameters(inputs: StrategyInputs, *, exclude_same_day: bool) -> dict[str, Any]:
    payload = {field.name: getattr(inputs, field.name) for field in fields(StrategyInputs)}
    if exclude_same_day:
        payload.pop("sell_allow_same_day_sell", None)
    return payload


def _persisted_profile_payload(profile: AccountSignalProfile) -> dict[str, Any]:
    payload = asdict(profile)
    return payload


def _persisted_candidate_payload(candidate: AccountSignalProfileCandidate) -> dict[str, Any]:
    payload = asdict(candidate)
    payload["summary"] = candidate_summary(candidate)
    return payload


def _validate_parameters(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("parameters 必须是对象")
    defaults = StrategyInputs()
    result: dict[str, Any] = {}
    field_names = {field.name for field in fields(StrategyInputs)}
    for key, value in raw.items():
        if key not in field_names:
            raise ValueError(f"不支持的 profile 参数: {key}")
        result[key] = _coerce_parameter(key, value, getattr(defaults, key))
    if "grid_sell_pct" not in result and "grid_second_sell_pct" in result:
        result["grid_sell_pct"] = result["grid_second_sell_pct"]
    return result


def _coerce_parameter(key: str, value: Any, default: Any) -> Any:
    if value is None:
        return None
    if isinstance(default, bool):
        return _bool(value, False)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(float(value))
    if isinstance(default, float) or key == "sell_stage_rearm_drawdown_pct":
        return float(value)
    return value


def _profile_diff(previous: AccountSignalProfile | None, new_profile: AccountSignalProfile) -> dict[str, Any]:
    if previous is None:
        return {"created": True}
    prev = _persisted_profile_payload(previous)
    new = _persisted_profile_payload(new_profile)
    diff: dict[str, Any] = {}
    for key in sorted(set(prev) | set(new)):
        if prev.get(key) != new.get(key):
            diff[key] = {"previous": prev.get(key), "new": new.get(key)}
    return diff


def _candidate_diff(
    previous: AccountSignalProfileCandidate | None,
    new_candidate: AccountSignalProfileCandidate,
) -> dict[str, Any]:
    if previous is None:
        return {"created": True}
    prev = _persisted_candidate_payload(previous)
    new = _persisted_candidate_payload(new_candidate)
    diff: dict[str, Any] = {}
    for key in sorted(set(prev) | set(new)):
        if prev.get(key) != new.get(key):
            diff[key] = {"previous": prev.get(key), "new": new.get(key)}
    return diff


def _candidate_parameter_hash(
    buy_strategy: Any,
    sell_strategy: Any,
    parameters: dict[str, Any],
    strategy_definition_version: Any,
) -> str:
    payload = {
        "buy_strategy": str(buy_strategy or ""),
        "sell_strategy": str(sell_strategy or ""),
        "parameters": parameters,
        "strategy_definition_version": str(strategy_definition_version or STRATEGY_DEFINITION_VERSION),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(key): _jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(item) for item in value]
        return str(value)


def _buy_summary(strategy: str, params: dict[str, Any]) -> str:
    if strategy == "core_dip_dca":
        timing = ""
        if params.get("core_dip_timing_enabled"):
            timing = (
                f" / 买点优化 延迟{params.get('core_dip_timing_max_delay_days', 3):g}日 "
                f"大涨{params.get('core_dip_timing_rise_threshold_pct', 1.5):g}% "
                f"近低{params.get('core_dip_timing_near_low_pct', 2):g}%"
            )
        return (
            f"核心定投+回撤加仓: 初始{params.get('core_dip_initial_core_pct', 80):g}% / "
            f"周投{params.get('core_dip_weekly_core_pct', 90):g}% / "
            f"现金垫{params.get('core_dip_cash_reserve_pct', 8):g}% / "
            f"加仓{params.get('core_dip_start_drawdown_pct', 5):g}%-{params.get('core_dip_full_drawdown_pct', 25):g}%"
            f"{timing}"
        )
    if strategy == "weekly_dca":
        return "每周定投"
    if strategy == "salary_flow_dca":
        return "工资流定投"
    if strategy == "equal_slice":
        return f"等距细切: 步长{params.get('step_pct', 5):g}%, 每档{params.get('equal_slice_allocation_pct', 5):g}%"
    if strategy == "linear_weighted_slice":
        return f"线性递增加权细切: 步长{params.get('step_pct', 5):g}%"
    return "三档金字塔"


def _sell_summary(strategy: str, params: dict[str, Any]) -> str:
    if strategy == "none":
        return "不卖出"
    if strategy == "repair_step":
        return (
            f"阶梯修复卖出: 盈利{params.get('sell_min_profit_pct', 10):g}% / "
            f"冷却{params.get('repair_sell_cooldown_days', 30):g}日 / "
            f"单档{params.get('repair_stage_sell_pct', 12):g}%"
        )
    if strategy == "grid_rebound":
        return (
            f"网格回弹卖出: 步长{params.get('grid_rebound_step_pct', 5):g}% / "
            f"每档{params.get('grid_sell_pct', params.get('grid_second_sell_pct', 40)):g}%卖出"
        )
    sell_stage_rearm = params.get("sell_stage_rearm_drawdown_pct")
    if sell_stage_rearm is None:
        sell_stage_rearm = params.get("dca_rearm_drawdown_pct", 5)
    return (
        f"成本去杠杆: 盈利{params.get('cost_first_profit_pct', 8):g}/"
        f"{params.get('cost_second_profit_pct', 15):g}/"
        f"{params.get('cost_third_profit_pct', 25):g}%, "
        f"卖出{params.get('cost_first_sell_pct', 30):g}/"
        f"{params.get('cost_second_sell_pct', 30):g}/"
        f"{params.get('cost_third_sell_pct', 30):g}%, "
        f"冷却{params.get('cost_deleverage_cooldown_days', 0):g}日, "
        f"卖后重启{params.get('dca_rearm_drawdown_pct', 5):g}%回撤, "
        f"卖档重启{sell_stage_rearm:g}%回撤"
    )


def _bool(value: Any, default: bool = False) -> bool:
    if value in ("", None):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "是", "启用"}
