"""Live strategy profile storage for real-account reminders."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drawdown.position_strategy import SELL_STRATEGY_LABELS, STRATEGY_LABELS, StrategyInputs
from drawdown.strategy_parameter_registry import (
    BUY_PARAMETER_FIELDS,
    SELL_PARAMETER_FIELDS,
    STRATEGY_DEFINITION_VERSION,
)
from trade_sync.normalize import canonical_symbol, infer_longbridge_symbol


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data" / "account_signal"
PROFILES_PATH = DATA_DIR / "profiles.json"

PROFILE_PARAMETER_FIELDS = tuple(field.name for field in fields(StrategyInputs))
LAB_PARAMETER_FIELDS = tuple(dict.fromkeys((*BUY_PARAMETER_FIELDS, *SELL_PARAMETER_FIELDS)))
ALLOWED_PARAMETER_FIELDS = set(PROFILE_PARAMETER_FIELDS)


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


def normalize_profile_symbol(symbol: str) -> str:
    return infer_longbridge_symbol(canonical_symbol(symbol), "US")


def strategy_inputs_for_profile(
    profile: AccountSignalProfile,
    *,
    fallback_same_day_sell: bool | None = None,
) -> StrategyInputs:
    kwargs: dict[str, Any] = {}
    for key, value in profile.parameters.items():
        if key in ALLOWED_PARAMETER_FIELDS and value is not None:
            kwargs[key] = value
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
        "enabled_targets": enabled_targets,
        "missing_profile_symbols": missing,
        "strategy_definition_version": STRATEGY_DEFINITION_VERSION,
    }


def promote_candidate_to_profile(
    *,
    symbol: str,
    candidate: dict[str, Any],
    note: str = "",
    dry_run: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ValueError("candidate 必须是对象")
    profiles = load_profiles(path)
    normalized_symbol = normalize_profile_symbol(symbol)
    previous = profiles.get(normalized_symbol) or built_in_default_profiles().get(normalized_symbol)
    new_profile = profile_from_candidate(normalized_symbol, candidate, note=note)
    diff = _profile_diff(previous, new_profile)
    if not dry_run:
        profiles[normalized_symbol] = new_profile
        save_profiles(profiles, path)
    return {
        "success": True,
        "dry_run": dry_run,
        "symbol": normalized_symbol,
        "previous": previous.to_dict() if previous else None,
        "new": new_profile.to_dict(),
        "diff": diff,
        "written": not dry_run,
    }


def profile_from_candidate(symbol: str, candidate: dict[str, Any], *, note: str = "") -> AccountSignalProfile:
    parameters: dict[str, Any] = {}
    for key in LAB_PARAMETER_FIELDS:
        if key in candidate and candidate[key] is not None:
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
                if nested_value is not None:
                    parameters[nested_key] = nested_value
    payload = {
        "symbol": symbol,
        "enabled": True,
        "buy_strategy": candidate.get("buy_strategy"),
        "sell_strategy": candidate.get("sell_strategy"),
        "parameters": parameters,
        "candidate_key": candidate.get("key") or candidate.get("candidate_key") or "",
        "strategy_definition_version": candidate.get("strategy_definition_version") or STRATEGY_DEFINITION_VERSION,
        "source": "parameter_lab",
        "promoted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "note": note,
    }
    return validate_profile_payload(payload)


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


def profile_summary(profile: AccountSignalProfile) -> dict[str, Any]:
    params = profile.parameters
    return {
        "buy": _buy_summary(profile.buy_strategy, params),
        "sell": _sell_summary(profile.sell_strategy, params),
        "parameters": dict(params),
    }


def _input_parameters(inputs: StrategyInputs, *, exclude_same_day: bool) -> dict[str, Any]:
    payload = {field.name: getattr(inputs, field.name) for field in fields(StrategyInputs)}
    if exclude_same_day:
        payload.pop("sell_allow_same_day_sell", None)
    return payload


def _persisted_profile_payload(profile: AccountSignalProfile) -> dict[str, Any]:
    payload = asdict(profile)
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
            f"卖出{params.get('grid_first_sell_pct', 40):g}%+{params.get('grid_second_sell_pct', 40):g}%"
        )
    return (
        f"成本去杠杆: 盈利{params.get('cost_first_profit_pct', 8):g}/"
        f"{params.get('cost_second_profit_pct', 15):g}/"
        f"{params.get('cost_third_profit_pct', 25):g}%, "
        f"卖出{params.get('cost_first_sell_pct', 30):g}/"
        f"{params.get('cost_second_sell_pct', 30):g}/"
        f"{params.get('cost_third_sell_pct', 30):g}%, "
        f"冷却{params.get('cost_deleverage_cooldown_days', 0):g}日, "
        f"卖后重启{params.get('dca_rearm_drawdown_pct', 5):g}%回撤, "
        f"卖档重启{params.get('sell_stage_rearm_drawdown_pct', params.get('dca_rearm_drawdown_pct', 5)):g}%回撤"
    )


def _bool(value: Any, default: bool = False) -> bool:
    if value in ("", None):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "是", "启用"}
