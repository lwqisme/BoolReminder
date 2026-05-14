"""
Flask Web应用
提供Web界面查看结果、更新token、手动触发分析
"""

import os
import sys
import math
import threading
import time
import uuid
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request, session, redirect, url_for, send_from_directory
from typing import Callable, Optional
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.config_manager import ConfigManager
from drawdown.generate_drawdown_report import TradeOverlay, normalize_longbridge_symbol, render_longbridge_drawdown_from_overlays
from drawdown.option_overlay import apply_option_overlay
from drawdown.position_strategy import (
    SCORECARD_PERIODS,
    SCORECARD_PORTFOLIOS,
    SELL_STRATEGY_LABELS,
    STRATEGY_LABELS,
    parse_date_range,
    run_longbridge_strategy_lab,
    run_longbridge_robust_leaderboard,
    run_longbridge_strategy_scorecard,
    run_longbridge_sell_parameter_scan,
)
from drawdown.strategy_lab_config import StrategyLabConfig
from drawdown.strategy_lab_history import (
    delete_experiment_preset,
    delete_run_snapshot,
    list_experiment_presets,
    list_run_snapshots,
    load_experiment_preset,
    load_run_snapshot,
    save_experiment_preset,
    save_run_snapshot,
)
from trade_sync.cleanup import run_trade_sync_cleanup
from trade_sync.normalize import canonical_symbol, normalize_trade_rows
from trade_sync.store import (
    drawdown_html_path,
    is_drawdown_stale,
    list_synced_symbols,
    load_symbol_snapshot,
    save_drawdown_meta,
    save_sync_payload,
)
import watchlist_boll_filter
from watchlist_boll_filter import main, run_analysis_and_notify, WatchlistBollFilterResult
from report.html_generator import generate_html_report
from option_quote import OptionQuoteService

# 全局变量
app = Flask(__name__)
config_manager: Optional[ConfigManager] = None
latest_result: Optional[WatchlistBollFilterResult] = None
scheduler_instance = None  # 全局调度器实例，用于动态更新
watchlist_cache: dict[str, object] = {
    "expires_at": None,
    "symbols": [],
    "symbol_to_name": {},
    "error": None,
    "last_success_at": None,
}
strategy_lab_jobs: dict[str, dict[str, object]] = {}
strategy_lab_jobs_lock = threading.Lock()
STRATEGY_LAB_JOB_TTL_SECONDS = 60 * 60

# 初始化配置管理器（模块级别）
try:
    config_manager = ConfigManager()
except Exception as e:
    print(f"警告: 配置管理器初始化失败: {e}")

# 启动时加载最新的分析结果
try:
    from watchlist_boll_filter import load_latest_result
    loaded_result = load_latest_result()
    if loaded_result:
        latest_result = loaded_result
        print(f"已加载最新分析结果: {latest_result.update_time}")
    else:
        print("未找到历史分析结果，将在首次分析后显示")
except Exception as e:
    print(f"加载最新结果失败: {e}")
    import traceback
    traceback.print_exc()


def init_app():
    """初始化Flask应用"""
    global config_manager
    
    if config_manager is None:
        config_manager = ConfigManager()
    
    web_config = config_manager.get_web_config()
    
    # 设置Flask secret_key
    secret_key = web_config.get("secret_key")
    if not secret_key:
        # 如果没有配置，生成一个随机密钥（仅用于开发）
        secret_key = os.urandom(24).hex()
        print("警告: 未配置secret_key，使用临时密钥。生产环境请设置固定密钥。")
    app.secret_key = secret_key


def _json_error(message: str, status_code: int):
    return jsonify({"success": False, "message": message}), status_code


def _get_trade_sync_config() -> dict:
    global config_manager
    if config_manager is None:
        config_manager = ConfigManager()
    return config_manager.get_trade_sync_config()


def _get_trade_sync_cleanup_config() -> dict:
    global config_manager
    if config_manager is None:
        config_manager = ConfigManager()
    return config_manager.get_trade_sync_cleanup_config()


def _get_position_strategy_config() -> dict:
    global config_manager
    if config_manager is None:
        config_manager = ConfigManager()
    return config_manager.get_position_strategy_config()


def _check_trade_sync_auth() -> tuple[bool, str]:
    trade_sync_config = _get_trade_sync_config()
    if not trade_sync_config.get("enabled", True):
        return False, "Trade sync is disabled"

    expected_token = trade_sync_config.get("bearer_token", "").strip()
    if not expected_token:
        return False, "Trade sync bearer token is not configured"

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False, "Missing bearer token"

    actual_token = auth_header.split(" ", 1)[1].strip()
    if actual_token != expected_token:
        return False, "Invalid bearer token"

    return True, ""


def _watchlist_cache_valid(now: datetime) -> bool:
    expires_at = watchlist_cache.get("expires_at")
    return isinstance(expires_at, datetime) and expires_at > now


def _watchlist_cache_has_snapshot() -> bool:
    symbols = watchlist_cache.get("symbols", [])
    symbol_to_name = watchlist_cache.get("symbol_to_name", {})
    return bool(symbols) and isinstance(symbol_to_name, dict)


def _watchlist_last_success_label() -> str | None:
    last_success_at = watchlist_cache.get("last_success_at")
    if not isinstance(last_success_at, datetime):
        return None
    return last_success_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _fetch_watchlist_from_longbridge() -> tuple[list[str], dict[str, str]]:
    global config_manager
    if config_manager is None:
        config_manager = ConfigManager()

    lb_config = config_manager.get_longbridge_config()
    oauth_client_id = lb_config.get("oauth_client_id", "")
    oauth = watchlist_boll_filter.OAuthBuilder(oauth_client_id).build(
        lambda url: (_ for _ in ()).throw(RuntimeError(f"Token expired, needs re-auth: {url}"))
    )
    lb_config_obj = watchlist_boll_filter.Config.from_oauth(oauth)
    quote_ctx = watchlist_boll_filter.QuoteContext(lb_config_obj)
    return watchlist_boll_filter.get_watchlist_symbols(
        quote_ctx,
        exclude_options=True,
    )


def _format_watchlist_error_message(exc: Exception, using_cache: bool) -> str:
    raw_message = str(exc)
    lowered = raw_message.lower()
    if "timeout" in lowered:
        prefix = "Longbridge 网络请求超时"
        if using_cache:
            success_label = _watchlist_last_success_label()
            message = f"{prefix}，已回退到上一次成功缓存"
            if success_label:
                message += f"（{success_label}）"
            return f"{message}: {exc}"
        return f"{prefix}，当前没有可用缓存: {exc}"
    if using_cache:
        success_label = _watchlist_last_success_label()
        message = "加载 Longbridge 自选列表失败，已回退到上一次成功缓存"
        if success_label:
            message += f"（{success_label}）"
        return f"{message}: {exc}"
    return f"加载 Longbridge 自选列表失败: {exc}"


def _load_watchlist_snapshot() -> tuple[list[str], dict[str, str], str | None]:
    now = datetime.now(timezone.utc)
    if _watchlist_cache_valid(now):
        return (
            list(watchlist_cache.get("symbols", [])),
            dict(watchlist_cache.get("symbol_to_name", {})),
            watchlist_cache.get("error"),
        )

    if not watchlist_boll_filter.LONGBRIDGE_AVAILABLE:
        error = "Longbridge SDK 不可用，无法加载自选列表。"
        watchlist_cache.update(
            {
                "expires_at": now + timedelta(minutes=5),
                "symbols": [],
                "symbol_to_name": {},
                "error": error,
            }
        )
        return [], {}, error

    try:
        for attempt in range(1, 4):
            try:
                symbols, symbol_to_name = _fetch_watchlist_from_longbridge()
                break
            except Exception as exc:
                if attempt >= 3:
                    raise
                watchlist_boll_filter.time.sleep(1.2 * attempt)

        watchlist_cache.update(
            {
                "expires_at": now + timedelta(minutes=5),
                "symbols": list(symbols),
                "symbol_to_name": dict(symbol_to_name),
                "error": None,
                "last_success_at": now,
            }
        )
        return symbols, symbol_to_name, None
    except Exception as exc:
        error = _format_watchlist_error_message(exc, using_cache=False)
        if _watchlist_cache_has_snapshot():
            fallback_message = _format_watchlist_error_message(exc, using_cache=True)
            watchlist_cache.update(
                {
                    "expires_at": now + timedelta(seconds=45),
                    "error": fallback_message,
                }
            )
            return (
                list(watchlist_cache.get("symbols", [])),
                dict(watchlist_cache.get("symbol_to_name", {})),
                fallback_message,
            )
        watchlist_cache.update(
            {
                "expires_at": now + timedelta(minutes=2),
                "symbols": [],
                "symbol_to_name": {},
                "error": error,
            }
        )
        return [], {}, error


def _base_symbol(symbol: str) -> str:
    return canonical_symbol(symbol).split(".", 1)[0]


def _drawdown_watchlist_label(symbol: str, name: str) -> str:
    normalized_symbol = canonical_symbol(symbol)
    normalized_name = (name or "").strip()
    if "." not in normalized_symbol:
        return normalized_symbol

    market_suffix = normalized_symbol.rsplit(".", 1)[-1]
    if market_suffix in {"HK", "SH", "SZ"} and normalized_name and normalized_name != normalized_symbol:
        return f"{normalized_name} · {normalized_symbol}"

    return normalized_symbol


def _build_watchlist_overview(
    synced_symbols: list[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]], str | None]:
    watchlist_symbols, symbol_to_name, watchlist_error = _load_watchlist_snapshot()
    synced_bases = {_base_symbol(symbol) for symbol in synced_symbols}

    synced_watchlist_items: list[dict[str, str]] = []
    remaining_watchlist_items: list[dict[str, str]] = []
    for symbol in watchlist_symbols:
        item = {
            "symbol": symbol,
            "name": symbol_to_name.get(symbol, symbol),
            "base_symbol": _base_symbol(symbol),
            "display_label": _drawdown_watchlist_label(symbol, symbol_to_name.get(symbol, symbol)),
        }
        if item["base_symbol"] in synced_bases:
            synced_watchlist_items.append(item)
        else:
            remaining_watchlist_items.append(item)

    return synced_watchlist_items, remaining_watchlist_items, watchlist_error


def _build_trade_overlays(snapshot: dict) -> list[TradeOverlay]:
    overlays: list[TradeOverlay] = []
    for row in snapshot.get("rows", []):
        overlays.append(
            TradeOverlay(
                date=datetime.strptime(row["trade_date"], "%Y-%m-%d"),
                amount=row.get("amount"),
                shares=row.get("shares"),
                price=row.get("price"),
                event_type=row["side"],
            )
        )
    return overlays


def _parse_drawdown_date(raw_value: str | None, field_name: str) -> date | None:
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是 YYYY-MM-DD 格式。") from exc


def _parse_drawdown_range(
    start_raw: str | None,
    end_raw: str | None,
) -> tuple[date | None, date | None]:
    start_date = _parse_drawdown_date(start_raw, "start")
    end_date = _parse_drawdown_date(end_raw, "end")
    if start_date and end_date and start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期。")
    return start_date, end_date


def _build_drawdown_url(
    symbol: str,
    start_date: date | None = None,
    end_date: date | None = None,
    force: bool = False,
) -> str:
    params: dict[str, str] = {}
    if start_date:
        params["start"] = start_date.isoformat()
    if end_date:
        params["end"] = end_date.isoformat()
    if force:
        params["force"] = "1"
    query = urlencode(params)
    return f"/drawdown/{symbol}" + (f"?{query}" if query else "")


def _ensure_drawdown_report(
    symbol: str,
    start_date: date | None = None,
    end_date: date | None = None,
    force: bool = False,
) -> tuple[Path, dict]:
    requested_symbol = canonical_symbol(symbol)
    snapshot = load_symbol_snapshot(requested_symbol)
    if not snapshot:
        snapshot = load_symbol_snapshot(_base_symbol(requested_symbol))

    trade_sync_config = _get_trade_sync_config()
    source_symbol = snapshot["symbol"] if snapshot else requested_symbol
    longbridge_symbol = snapshot.get("longbridge_symbol") if snapshot else requested_symbol
    source_version = snapshot.get("sync_version", "") if snapshot else f"longbridge-only:{requested_symbol}"
    start_token = start_date.isoformat() if start_date else None
    end_token = end_date.isoformat() if end_date else None
    output_path = drawdown_html_path(source_symbol, start_token, end_token)
    if is_drawdown_stale(
        source_symbol,
        source_version=source_version,
        cache_ttl_minutes=int(trade_sync_config.get("cache_ttl_minutes", 30)),
        force=force,
        start_date=start_token,
        end_date=end_token,
    ):
        overlays = _build_trade_overlays(snapshot) if snapshot else []
        html, warnings, resolved_symbol = render_longbridge_drawdown_from_overlays(
            source_symbol,
            overlays,
            longbridge_symbol,
            start_date=start_date,
            end_date=end_date,
        )
        output_path.write_text(html, encoding="utf-8")
        save_drawdown_meta(
            source_symbol,
            {
                "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "source_version": source_version,
                "symbol": source_symbol,
                "longbridge_symbol": resolved_symbol,
                "requested_start_date": start_token,
                "requested_end_date": end_token,
                "warning_count": len(warnings),
                "trade_count": snapshot.get("trade_count", len(snapshot.get("rows", []))) if snapshot else 0,
                "has_synced_trades": bool(snapshot),
            },
            start_token,
            end_token,
        )

    if snapshot:
        return output_path, snapshot

    return output_path, {
        "symbol": source_symbol,
        "longbridge_symbol": longbridge_symbol,
        "trade_count": 0,
        "rows": [],
    }


# HTML模板
INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BOLL指标筛选系统</title>
    <style>
        :root {
            --paper: #fafafa;
            --sky: #a2d5f2;
            --blue: #07689f;
            --coral: #ff7e67;
            --bg: #e9f6fc;
            --panel: #fafafa;
            --border: #c7e4f5;
            --border-strong: #87c6e8;
            --ink: #06324c;
            --muted: #58778d;
            --accent: #ff7e67;
            --accent-strong: #e8644f;
            --blue-dark: #054d76;
            --shadow: 0 20px 48px rgba(7, 104, 159, 0.18);
            --paper-shadow: 0 12px 30px rgba(7, 104, 159, 0.12);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            min-height: 100vh;
            background:
                radial-gradient(circle at 10% 0%, rgba(162, 213, 242, 0.78), transparent 34%),
                radial-gradient(circle at 92% 8%, rgba(255, 126, 103, 0.24), transparent 30%),
                linear-gradient(180deg, #fafafa 0%, #e9f6fc 48%, #d9effa 100%);
            color: var(--ink);
            font-family: "Aptos", "IBM Plex Sans", "Noto Sans SC", "Microsoft YaHei", sans-serif;
            font-size: 14px;
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 26px; }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 18px;
            margin-bottom: 20px;
            padding: 24px;
            border: 1px solid rgba(162, 213, 242, 0.48);
            border-radius: 8px;
            background:
                linear-gradient(135deg, rgba(255, 126, 103, 0.22), transparent 34%),
                linear-gradient(115deg, #07689f 0%, #0b75ae 58%, #a2d5f2 100%);
            color: #fafafa;
            box-shadow: var(--shadow);
        }
        .header-title { margin: 0; font-family: "Avenir Next", "Noto Sans SC", sans-serif; font-size: 28px; line-height: 1.15; }
        .header-sub { margin: 8px 0 0; color: #eaf6fc; font-size: 14px; line-height: 1.5; }
        .header-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; align-items: flex-start; }
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 36px;
            padding: 8px 14px;
            background: linear-gradient(180deg, #ff8c77, var(--accent-strong));
            color: #fafafa;
            border: 1px solid var(--accent);
            border-radius: 7px;
            cursor: pointer;
            text-decoration: none;
            font-weight: 700;
            font-size: 13px;
            white-space: nowrap;
            box-shadow: 0 8px 16px rgba(255, 126, 103, 0.24);
        }
        .btn:hover { background: linear-gradient(180deg, #ff9b89, #df5f4a); border-color: #ff8c77; }
        .btn-secondary { background: rgba(250, 250, 250, 0.12); color: #fafafa; border-color: rgba(250, 250, 250, 0.36); box-shadow: none; }
        .btn-secondary:hover { background: rgba(250, 250, 250, 0.22); border-color: rgba(250, 250, 250, 0.58); }
        .btn-green { background: linear-gradient(180deg, #4ade80, #16a34a); border-color: #22c55e; box-shadow: 0 8px 16px rgba(34, 197, 94, 0.24); }
        .btn-green:hover { background: linear-gradient(180deg, #6ee7a0, #15803d); }
        .hero-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 20px; }
        .hero-card {
            display: block;
            padding: 20px;
            border-radius: 8px;
            text-decoration: none;
            color: #fafafa;
            border: 1px solid rgba(255, 255, 255, 0.18);
            box-shadow: var(--paper-shadow);
            transition: transform 150ms ease, box-shadow 150ms ease;
        }
        .hero-card:hover { transform: translateY(-2px); box-shadow: var(--shadow); }
        .hero-card strong { display: block; font-size: 17px; margin-bottom: 7px; }
        .hero-card span { font-size: 13px; opacity: 0.9; line-height: 1.5; }
        .hero-card.drawdown { background: linear-gradient(135deg, #07689f 0%, #0b75ae 58%, #2563eb 100%); }
        .hero-card.strategy { background: linear-gradient(135deg, #0f766e 0%, #059669 100%); }
        .hero-card.portal { background: linear-gradient(135deg, #334155 0%, #0f172a 100%); }
        .panel {
            background:
                linear-gradient(180deg, rgba(162, 213, 242, 0.18), transparent 32%),
                var(--panel);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 16px;
            box-shadow: var(--paper-shadow);
        }
        .status { padding: 11px 13px; margin-bottom: 16px; border-radius: 8px; display: none; border: 1px solid transparent; box-shadow: 0 10px 24px rgba(7, 104, 159, 0.14); }
        .status.success { display: block; background: #eaf6fc; color: #07547f; border-color: #a2d5f2; }
        .status.error { display: block; background: #ffe5df; color: #b94d3b; border-color: #ffb2a3; }
        .status.info { display: block; background: #eaf6fc; color: var(--blue); border-color: #a2d5f2; }
        @media (max-width: 720px) {
            .container { padding: 16px; }
            .header { flex-direction: column; align-items: flex-start; }
            .header-title { font-size: 22px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1 class="header-title">BOLL 指标筛选系统</h1>
                <p class="header-sub">布林带指标自动筛选 · 回撤分析 · 策略实验室</p>
            </div>
            <div class="header-actions">
                <a href="/history" class="btn btn-secondary">历史报告</a>
                <a href="/drawdown" class="btn btn-secondary">Drawdown</a>
                <a href="/schedule" class="btn btn-secondary">定时任务</a>
                <a href="/update-token" class="btn btn-secondary">更新 Token</a>
                <button onclick="triggerAnalysis(false)" class="btn">快速分析（无期权延迟）</button>
                <button onclick="triggerAnalysis(true)" class="btn btn-green">完整分析（含期权延迟）</button>
            </div>
        </div>

        <div id="status"></div>

        <div class="hero-grid">
            <a href="/drawdown" class="hero-card drawdown">
                <strong>Drawdown 图表</strong>
                <span>查看单股票回撤、加仓与卖出图层</span>
            </a>
            <a href="/strategy-lab" class="hero-card strategy">
                <strong>仓位策略实验室</strong>
                <span>六套买入策略的组合实时演算</span>
            </a>
            <a href="http://aqcloud.ltd" class="hero-card portal" target="_blank">
                <strong>AQCloud 首页</strong>
                <span>返回总站首页查看其他服务入口</span>
            </a>
        </div>

        <div class="panel">
            <div id="content">
                {% if result %}
                    {{ result_html|safe }}
                {% else %}
                    <p style="color: var(--muted); margin: 0;">暂无分析结果。点击"快速分析"或"完整分析"按钮开始分析。</p>
                {% endif %}
            </div>
        </div>
    </div>

    <script>
        function triggerAnalysis(optionDelay) {
            const message = optionDelay ? '正在完整分析（含期权延迟），请耐心等待...' : '正在快速分析，请稍候...';
            document.getElementById('status').innerHTML = '<div class="status info">' + message + '</div>';

            fetch('/api/trigger', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ option_delay: optionDelay })
            })
                .then(response => {
                    if (!response.ok) {
                        throw new Error('HTTP ' + response.status);
                    }
                    return response.json();
                })
                .then(data => {
                    if (data.success) {
                        location.reload();
                    } else {
                        document.getElementById('status').innerHTML = '<div class="status error">' + data.message + '</div>';
                    }
                })
                .catch(error => {
                    document.getElementById('status').innerHTML = '<div class="status error">请求失败: ' + error + '</div>';
                });
        }
    </script>
</body>
</html>
"""

DRAWDOWN_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Drawdown - BoolReminder</title>
    <style>
        :root {
            --paper: #fafafa;
            --blue: #07689f;
            --coral: #ff7e67;
            --panel: #fafafa;
            --border: #c7e4f5;
            --border-strong: #87c6e8;
            --ink: #06324c;
            --muted: #58778d;
            --accent: #ff7e67;
            --accent-strong: #e8644f;
            --blue-dark: #054d76;
            --shadow: 0 20px 48px rgba(7, 104, 159, 0.18);
            --paper-shadow: 0 12px 30px rgba(7, 104, 159, 0.12);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            min-height: 100vh;
            background:
                radial-gradient(circle at 10% 0%, rgba(162, 213, 242, 0.78), transparent 34%),
                radial-gradient(circle at 92% 8%, rgba(255, 126, 103, 0.24), transparent 30%),
                linear-gradient(180deg, #fafafa 0%, #e9f6fc 48%, #d9effa 100%);
            color: var(--ink);
            font-family: "Aptos", "IBM Plex Sans", "Noto Sans SC", "Microsoft YaHei", sans-serif;
            font-size: 14px;
        }
        .container { max-width: 960px; margin: 0 auto; padding: 26px; }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 18px;
            margin-bottom: 20px;
            padding: 24px;
            border: 1px solid rgba(162, 213, 242, 0.48);
            border-radius: 8px;
            background:
                linear-gradient(135deg, rgba(255, 126, 103, 0.22), transparent 34%),
                linear-gradient(115deg, #07689f 0%, #0b75ae 58%, #a2d5f2 100%);
            color: #fafafa;
            box-shadow: var(--shadow);
        }
        .header-title { margin: 0; font-family: "Avenir Next", "Noto Sans SC", sans-serif; font-size: 28px; line-height: 1.15; }
        .header-sub { margin: 8px 0 0; color: #eaf6fc; font-size: 13px; line-height: 1.5; }
        .header-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; align-items: flex-start; }
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 36px;
            padding: 8px 14px;
            background: linear-gradient(180deg, #ff8c77, var(--accent-strong));
            color: #fafafa;
            border: 1px solid var(--accent);
            border-radius: 7px;
            cursor: pointer;
            text-decoration: none;
            font-weight: 700;
            font-size: 13px;
            white-space: nowrap;
            box-shadow: 0 8px 16px rgba(255, 126, 103, 0.24);
        }
        .btn:hover { background: linear-gradient(180deg, #ff9b89, #df5f4a); }
        .btn-secondary { background: rgba(250, 250, 250, 0.12); color: #fafafa; border-color: rgba(250, 250, 250, 0.36); box-shadow: none; }
        .btn-secondary:hover { background: rgba(250, 250, 250, 0.22); border-color: rgba(250, 250, 250, 0.58); }
        .btn-blue { background: linear-gradient(180deg, #07689f, var(--blue-dark)); border-color: var(--blue); box-shadow: 0 8px 16px rgba(7, 104, 159, 0.24); }
        .btn-blue:hover { background: linear-gradient(180deg, #0b75ae, #054d76); }
        .panel {
            background:
                linear-gradient(180deg, rgba(162, 213, 242, 0.18), transparent 32%),
                var(--panel);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 19px;
            margin-bottom: 16px;
            box-shadow: var(--paper-shadow);
        }
        .field-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .field-row input[type="text"] {
            flex: 1;
            min-width: 180px;
            min-height: 38px;
            padding: 8px 10px;
            border: 1px solid var(--border-strong);
            border-radius: 7px;
            background: #fafafa;
            color: var(--ink);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.72);
        }
        .field-row input[type="text"]:focus { outline: 3px solid rgba(162, 213, 242, 0.58); border-color: var(--blue); }
        .field-row input[type="date"] {
            min-height: 38px;
            padding: 8px 10px;
            border: 1px solid var(--border-strong);
            border-radius: 7px;
            min-width: 160px;
            background: #fafafa;
            color: var(--ink);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.72);
        }
        .field-row input[type="date"]:focus { outline: 3px solid rgba(162, 213, 242, 0.58); border-color: var(--blue); }
        .field-row label { color: var(--muted); font-size: 13px; white-space: nowrap; cursor: pointer; }
        .field-row input[type="checkbox"] { accent-color: var(--accent); margin-right: 4px; }
        .preset-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
        .preset-btn {
            min-height: 30px;
            padding: 5px 12px;
            border: 1px solid rgba(7, 104, 159, 0.18);
            border-radius: 999px;
            background: rgba(250, 250, 250, 0.82);
            color: var(--blue);
            font-weight: 700;
            font-size: 13px;
            cursor: pointer;
            transition: background 140ms ease, border-color 140ms ease;
        }
        .preset-btn:hover { border-color: var(--coral); background: #fafafa; }
        .symbols { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
        .symbol-chip {
            display: inline-flex;
            align-items: center;
            padding: 6px 12px;
            background: rgba(7, 104, 159, 0.08);
            color: var(--blue);
            border: 1px solid rgba(7, 104, 159, 0.22);
            border-radius: 999px;
            text-decoration: none;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            transition: background 140ms ease, border-color 140ms ease, transform 140ms ease;
        }
        .symbol-chip:hover { background: rgba(7, 104, 159, 0.15); border-color: var(--blue); transform: translateY(-1px); }
        .symbol-chip.secondary { background: rgba(5, 182, 163, 0.08); color: #0f766e; border-color: rgba(5, 182, 163, 0.28); }
        .symbol-chip.secondary:hover { background: rgba(5, 182, 163, 0.16); border-color: #0f766e; }
        .symbol-chip.muted { background: rgba(88, 119, 141, 0.1); color: var(--muted); border-color: rgba(88, 119, 141, 0.22); }
        .symbol-chip.muted:hover { background: rgba(88, 119, 141, 0.18); border-color: var(--muted); }
        .panel-title { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 4px; }
        .panel-title strong { font-size: 15px; color: var(--blue); font-weight: 800; }
        .panel-count { color: var(--muted); font-size: 13px; }
        .hint {
            color: var(--muted);
            font-size: 13px;
            line-height: 1.55;
            margin-top: 10px;
            padding: 10px 12px;
            border-left: 4px solid var(--accent);
            background: rgba(162, 213, 242, 0.22);
            border-radius: 0 8px 8px 0;
        }
        .empty { color: var(--muted); padding: 10px 0; font-size: 13px; }
        .error { color: #b94d3b; font-size: 13px; padding: 10px 0; }
        @media (max-width: 720px) {
            .container { padding: 16px; }
            .header { flex-direction: column; align-items: flex-start; }
            .header-title { font-size: 22px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1 class="header-title">Drawdown</h1>
                <p class="header-sub">回撤分析 · 加仓与卖出图层</p>
            </div>
            <div class="header-actions">
                <a href="/" class="btn btn-secondary">BOLL 首页</a>
                <a href="http://aqcloud.ltd" class="btn btn-secondary" target="_blank">AQCloud 首页</a>
            </div>
        </div>

        <div class="panel">
            <form onsubmit="openDrawdown(event)">
                <div class="field-row">
                    <input id="symbol" type="text" placeholder="输入股票代码，例如 MSFT / TSLA" value="{{ default_symbol }}">
                    <input id="start" type="date" value="{{ selected_start }}">
                    <input id="end" type="date" value="{{ selected_end }}">
                    <label><input id="force" type="checkbox"> 强制刷新</label>
                    <button type="submit" class="btn btn-blue">打开图表</button>
                </div>
                <div class="preset-row">
                    <button class="preset-btn" type="button" data-preset="3m">3M</button>
                    <button class="preset-btn" type="button" data-preset="6m">6M</button>
                    <button class="preset-btn" type="button" data-preset="1y">1Y</button>
                    <button class="preset-btn" type="button" data-preset="ytd">YTD</button>
                    <button class="preset-btn" type="button" data-preset="all">All</button>
                </div>
                <div class="hint">时间范围会参与回撤重算。也就是说，起止日期变了，ATH 和 Rolling 120d 的回撤统计都会按新范围重新生成并缓存。</div>
            </form>
        </div>

        <div class="panel">
            <div class="panel-title">
                <strong>已同步股票</strong>
                <span class="panel-count">{{ symbols|length }} 只</span>
            </div>
            {% if symbols %}
                <div class="symbols">
                    {% for symbol in symbols %}
                        <a class="symbol-chip" href="/drawdown/{{ symbol }}" data-symbol="{{ symbol }}" target="_blank">{{ symbol }}</a>
                    {% endfor %}
                </div>
            {% else %}
                <div class="empty">还没有收到任何 Google Sheets 交易同步。</div>
            {% endif %}
        </div>

        <div class="panel">
            <div class="panel-title">
                <strong>自选但未同步</strong>
                <span class="panel-count">{{ remaining_watchlist_symbols|length }} 只</span>
            </div>
            {% if watchlist_error %}
                <div class="error">{{ watchlist_error }}</div>
            {% elif remaining_watchlist_symbols %}
                <div class="symbols">
                    {% for item in remaining_watchlist_symbols %}
                        <a class="symbol-chip secondary" href="/drawdown/{{ item.symbol }}" data-symbol="{{ item.symbol }}" title="{{ item.name }}" target="_blank">{{ item.display_label }}</a>
                    {% endfor %}
                </div>
                <div class="hint">这些股票已经在 Longbridge 自选里，但还没有出现在当前 Google Sheets 交易同步数据中。</div>
            {% else %}
                <div class="empty">当前自选列表里的股票都已经同步到交易列表，或者暂时没有可展示的剩余股票。</div>
            {% endif %}
        </div>

        <div class="panel">
            <div class="panel-title">
                <strong>自选且已同步</strong>
                <span class="panel-count">{{ synced_watchlist_symbols|length }} 只</span>
            </div>
            {% if watchlist_error %}
                <div class="empty">未能加载 Longbridge 自选列表，因此这里暂时无法按自选交集展示。</div>
            {% elif synced_watchlist_symbols %}
                <div class="symbols">
                    {% for item in synced_watchlist_symbols %}
                        <a class="symbol-chip muted" href="/drawdown/{{ item.base_symbol }}" data-symbol="{{ item.base_symbol }}" title="{{ item.name }}" target="_blank">{{ item.display_label }}</a>
                    {% endfor %}
                </div>
                <div class="hint">这里展示"Longbridge 自选列表"和"已同步交易股票"的交集，方便区分你关注且已经实际交易过的标的。</div>
            {% else %}
                <div class="empty">当前还没有 Longbridge 自选与交易同步的交集。</div>
            {% endif %}
        </div>
    </div>

    <script>
        function formatDateInput(date) {
            const year = date.getFullYear();
            const month = String(date.getMonth() + 1).padStart(2, '0');
            const day = String(date.getDate()).padStart(2, '0');
            return `${year}-${month}-${day}`;
        }

        function applyPresetRange(preset) {
            const startInput = document.getElementById('start');
            const endInput = document.getElementById('end');
            const today = new Date();
            if (preset === 'all') {
                startInput.value = '';
                endInput.value = '';
                return;
            }

            const end = new Date(today);
            const start = new Date(today);
            if (preset === '3m') {
                start.setMonth(start.getMonth() - 3);
            } else if (preset === '6m') {
                start.setMonth(start.getMonth() - 6);
            } else if (preset === '1y') {
                start.setFullYear(start.getFullYear() - 1);
            } else if (preset === 'ytd') {
                start.setMonth(0, 1);
            }

            startInput.value = formatDateInput(start);
            endInput.value = formatDateInput(end);
        }

        function buildDrawdownUrl(symbol) {
            const start = document.getElementById('start').value;
            const end = document.getElementById('end').value;
            const force = document.getElementById('force').checked;
            const params = new URLSearchParams();
            if (start) {
                params.set('start', start);
            }
            if (end) {
                params.set('end', end);
            }
            if (force) {
                params.set('force', '1');
            }
            const query = params.toString();
            return '/drawdown/' + encodeURIComponent(symbol) + (query ? '?' + query : '');
        }

        function openDrawdown(event) {
            event.preventDefault();
            const symbol = document.getElementById('symbol').value.trim().toUpperCase();
            if (!symbol) {
                return;
            }
            window.open(buildDrawdownUrl(symbol), '_blank');
        }

        document.querySelectorAll('[data-preset]').forEach((button) => {
            button.addEventListener('click', () => applyPresetRange(button.dataset.preset || 'all'));
        });

        document.querySelectorAll('.symbol-chip[data-symbol]').forEach((link) => {
            link.addEventListener('click', (event) => {
                event.preventDefault();
                window.open(buildDrawdownUrl(link.dataset.symbol || ''), '_blank');
            });
        });
    </script>
</body>
</html>
"""

STRATEGY_LAB_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>仓位策略实验室 - BoolReminder</title>
    <script src="/static/vendor/plotly-2.35.2.min.js"></script>
    <style>
        :root {
            --canvas: #f6f8fb;
            --surface: #ffffff;
            --surface-2: #f2f5f9;
            --surface-3: #e9eef5;
            --line: #d9e0ea;
            --line-strong: #aeb9c8;
            --ink: #111827;
            --muted: #657184;
            --faint: #8d99aa;
            --green: #00856f;
            --green-soft: #e2f5ef;
            --red: #d04437;
            --red-soft: #fae7e4;
            --amber: #d77b00;
            --amber-soft: #fff1d8;
            --blue: #1167d8;
            --blue-soft: #e3efff;
            --violet: #6b5cff;
            --charcoal: #0b1220;
            --shadow: 0 8px 22px rgba(15, 23, 42, 0.052);
            --radius: 18px;
            --radius-sm: 12px;
            --sans: "Aptos", "Noto Sans SC", "Microsoft YaHei", sans-serif;
            --mono: "Cascadia Mono", "SFMono-Regular", "Menlo", monospace;
            --page-backdrop:
                radial-gradient(circle at 78% -12%, rgba(17, 103, 216, 0.16), transparent 34%),
                radial-gradient(circle at 7% 16%, rgba(107, 92, 255, 0.10), transparent 30%),
                radial-gradient(circle at 94% 88%, rgba(0, 133, 111, 0.055), transparent 28%),
                linear-gradient(180deg, #fcfdff 0%, #f0f6ff 42%, #e8eef6 100%);
            --liquid-glass-bg:
                radial-gradient(circle at var(--glass-glint-x, 26%) var(--glass-glint-y, 4%), rgba(255, 255, 255, 0.72), transparent 30%),
                linear-gradient(155deg, rgba(255, 255, 255, 0.58), rgba(231, 241, 255, 0.26) 46%, rgba(255, 255, 255, 0.38)),
                rgba(255, 255, 255, 0.24);
            --liquid-glass-border: 1px solid rgba(255, 255, 255, 0.62);
            --liquid-glass-shadow:
                0 16px 34px rgba(15, 23, 42, 0.055),
                0 2px 8px rgba(17, 103, 216, 0.035),
                inset 0 1px 0 rgba(255, 255, 255, 0.78);
            --liquid-glass-filter: blur(28px) saturate(1.28) contrast(1.04);
            --liquid-glass-lens:
                linear-gradient(92deg, rgba(255, 255, 255, 0.56), transparent 18%, transparent 78%, rgba(255, 255, 255, 0.30)),
                linear-gradient(180deg, rgba(255, 255, 255, 0.42), transparent 36%, rgba(226, 235, 246, 0.16));
            --liquid-glass-lens-shadow:
                inset 10px 0 22px rgba(255, 255, 255, 0.22),
                inset -12px 0 20px rgba(116, 139, 170, 0.055);
            --liquid-glass-glow:
                radial-gradient(ellipse at 42% 24%, rgba(255, 255, 255, 0.58), transparent 42%),
                linear-gradient(180deg, rgba(17, 103, 216, 0.10), rgba(107, 92, 255, 0.045));
        }
        * { box-sizing: border-box; }
        html { min-height: 100%; background: #e8eef6; }
        body {
            margin: 0;
            min-height: 100vh;
            background: var(--page-backdrop);
            background-attachment: fixed;
            background-repeat: no-repeat;
            background-size: 100vw 100vh;
            background-color: #e8eef6;
            color: var(--ink);
            font-family: var(--sans);
            font-size: 14px;
        }
        button, input, select { font: inherit; }
        button { transition: transform 160ms ease, border-color 160ms ease, background 160ms ease, box-shadow 160ms ease; }
        .shell { display: grid; grid-template-columns: 96px minmax(0, 1fr); min-height: 100vh; }
        .rail,
        .panel,
        .quick-stat,
        .metric-card,
        .description-card,
        .holding,
        .kpi,
        .table-wrap,
        .status {
            position: relative;
            overflow: hidden;
            isolation: isolate;
            background: var(--liquid-glass-bg);
            border: var(--liquid-glass-border);
            box-shadow: var(--liquid-glass-shadow);
        }
        .rail::before,
        .rail::after,
        .panel::before,
        .panel::after,
        .quick-stat::before,
        .quick-stat::after,
        .metric-card::before,
        .metric-card::after,
        .description-card::before,
        .description-card::after,
        .holding::before,
        .holding::after,
        .kpi::before,
        .kpi::after,
        .table-wrap::before,
        .table-wrap::after {
            content: "";
            position: absolute;
            pointer-events: none;
            z-index: 0;
        }
        .rail::before,
        .panel::before,
        .quick-stat::before,
        .metric-card::before,
        .description-card::before,
        .holding::before,
        .kpi::before,
        .table-wrap::before {
            inset: 1px;
            border-radius: calc(var(--glass-radius, var(--radius)) - 1px);
            background: var(--liquid-glass-lens);
            box-shadow: var(--liquid-glass-lens-shadow);
            opacity: var(--glass-lens-opacity, 1);
        }
        .rail::after,
        .panel::after,
        .quick-stat::after,
        .metric-card::after,
        .description-card::after,
        .holding::after,
        .kpi::after,
        .table-wrap::after {
            width: var(--glass-glow-width, 92px);
            height: var(--glass-glow-height, 260px);
            left: var(--glass-glow-left, -28px);
            top: var(--glass-glow-top, 80px);
            border-radius: 999px;
            background: var(--liquid-glass-glow);
            filter: blur(var(--glass-glow-blur, 12px));
            opacity: var(--glass-glow-opacity, 0.82);
            display: none;
        }
        body.full-glass-mode .rail::after,
        body.full-glass-mode .panel::after,
        body.full-glass-mode .quick-stat::after,
        body.full-glass-mode .metric-card::after,
        body.full-glass-mode .description-card::after,
        body.full-glass-mode .holding::after,
        body.full-glass-mode .kpi::after,
        body.full-glass-mode .table-wrap::after {
            display: block;
        }
        .rail > *,
        .panel > *,
        .quick-stat > *,
        .metric-card > *,
        .description-card > *,
        .holding > *,
        .kpi > *,
        .table-wrap > * {
            position: relative;
            z-index: 1;
        }
        .rail {
            --glass-radius: 24px;
            position: sticky;
            top: 18px;
            height: calc(100vh - 36px);
            margin: 18px 10px 18px 16px;
            padding: 12px 10px;
            border-radius: var(--glass-radius);
            color: var(--charcoal);
        }
        .mark {
            position: relative;
            z-index: 1;
            display: grid;
            place-items: center;
            width: 48px;
            height: 48px;
            border: 1px solid rgba(255, 255, 255, 0.62);
            border-radius: 16px;
            font-family: var(--mono);
            font-weight: 800;
            color: var(--blue);
            background:
                radial-gradient(circle at 28% 10%, rgba(255, 255, 255, 0.84), transparent 42%),
                linear-gradient(145deg, rgba(255, 255, 255, 0.48), rgba(235, 243, 255, 0.24)),
                rgba(255, 255, 255, 0.24);
            box-shadow: 0 8px 18px rgba(15, 23, 42, 0.045), inset 0 1px 0 rgba(255, 255, 255, 0.76);
        }
        .container { min-width: 0; max-width: none; margin: 0; padding: 24px 28px 34px; }
        .header {
            display: grid;
            grid-template-columns: minmax(280px, 1fr) auto;
            gap: 18px;
            align-items: end;
            margin-bottom: 16px;
            padding: 0 0 20px;
            border-bottom: 1px solid var(--line);
        }
        .title-kicker {
            margin: 0 0 8px;
            color: var(--blue);
            font-family: var(--mono);
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0;
            text-transform: uppercase;
        }
        .title-block h1 {
            margin: 0;
            font-size: clamp(28px, 4vw, 48px);
            line-height: 1;
            letter-spacing: 0;
        }
        .title-block p { margin: 10px 0 0; color: var(--muted); line-height: 1.55; max-width: 760px; }
        .header-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
        .quick-stats {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 16px;
        }
        .quick-stat, .metric-card, .description-card {
            border-radius: var(--radius);
        }
        .quick-stat { min-height: 78px; padding: 15px 16px; }
        .quick-stat strong { display: block; font-family: var(--mono); font-size: 24px; line-height: 1.05; color: var(--blue); }
        .quick-stat span { display: block; margin-top: 7px; color: var(--muted); font-size: 13px; line-height: 1.4; }
        .command-bar {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            gap: 14px;
            align-items: center;
            margin-bottom: 16px;
            padding: 13px 14px;
            border: var(--liquid-glass-border);
            border-radius: var(--radius);
            background: rgba(255, 255, 255, 0.62);
            box-shadow:
                0 12px 28px rgba(15, 23, 42, 0.055),
                inset 0 1px 0 rgba(255, 255, 255, 0.82);
        }
        .command-main { min-width: 0; display: grid; gap: 8px; }
        .command-title {
            display: flex;
            align-items: baseline;
            gap: 8px;
            min-width: 0;
        }
        .command-title span {
            color: var(--muted);
            font-size: 12px;
            font-weight: 900;
        }
        .command-title strong {
            color: var(--charcoal);
            font-size: 15px;
            line-height: 1.2;
        }
        .command-summary {
            display: flex;
            align-items: center;
            gap: 7px;
            flex-wrap: wrap;
        }
        .command-chip {
            display: inline-flex;
            align-items: center;
            min-height: 28px;
            max-width: 100%;
            padding: 5px 8px;
            border: 1px solid rgba(17, 103, 216, 0.12);
            border-radius: 10px;
            background: rgba(255, 255, 255, 0.68);
            color: var(--charcoal);
            font-size: 12px;
            font-weight: 800;
            line-height: 1.25;
        }
        .command-chip span {
            margin-right: 5px;
            color: var(--muted);
            font-weight: 900;
        }
        .freshness-state {
            display: inline-flex;
            align-items: center;
            width: fit-content;
            max-width: 100%;
            min-height: 26px;
            padding: 4px 8px;
            border-radius: 999px;
            background: rgba(248, 250, 252, 0.70);
            color: var(--muted);
            font-size: 12px;
            font-weight: 800;
            line-height: 1.35;
        }
        .freshness-state::before {
            content: "";
            width: 7px;
            height: 7px;
            margin-right: 7px;
            border-radius: 999px;
            background: currentColor;
            opacity: 0.82;
        }
        .freshness-state.synced { background: rgba(225, 246, 238, 0.72); color: var(--green); }
        .freshness-state.stale { background: rgba(255, 241, 208, 0.72); color: #9a6400; }
        .freshness-state.idle { color: var(--muted); }
        .job-panel {
            display: none;
            margin: 0 0 16px;
            padding: 12px 14px;
            border: var(--liquid-glass-border);
            border-radius: var(--radius);
            background: rgba(255, 255, 255, 0.62);
            box-shadow:
                0 12px 28px rgba(15, 23, 42, 0.052),
                inset 0 1px 0 rgba(255, 255, 255, 0.82);
        }
        .job-panel.show { display: grid; gap: 9px; }
        .job-head {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 10px;
            color: var(--charcoal);
            font-weight: 900;
        }
        .job-head span {
            color: var(--muted);
            font-family: var(--mono);
            font-size: 11px;
            font-weight: 800;
            white-space: nowrap;
        }
        .job-progress {
            height: 7px;
            border-radius: 999px;
            background: rgba(226, 235, 246, 0.82);
            overflow: hidden;
        }
        .job-progress span {
            display: block;
            width: 0%;
            height: 100%;
            border-radius: inherit;
            background: linear-gradient(90deg, var(--blue), var(--green));
            transition: width 180ms ease;
        }
        .job-message {
            color: var(--muted);
            font-size: 12px;
            line-height: 1.45;
        }
        .command-actions {
            display: flex;
            align-items: center;
            justify-content: flex-end;
            gap: 8px;
            flex-wrap: wrap;
        }
        /* fieldset groups */
        .fieldsets { display: grid; gap: 12px; }
        .fieldset {
            display: grid;
            grid-template-columns: 132px minmax(0, 1fr);
            gap: 12px;
            align-items: start;
            padding: 14px;
            border: 0;
            border-radius: var(--radius-sm);
            background: rgba(248, 250, 252, 0.48);
        }
        .fieldset-title {
            display: inline-flex;
            align-items: center;
            width: fit-content;
            min-height: 26px;
            padding: 0 9px;
            border-radius: 999px;
            background: rgba(17, 103, 216, 0.08);
            color: var(--charcoal);
            font-family: var(--mono);
            font-size: 11px;
            font-weight: 900;
            text-transform: uppercase;
        }
        .fieldset .grid { grid-template-columns: repeat(4, minmax(118px, 1fr)); }
        /* switch rows for option overlay */
        .switch-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            padding: 12px;
            border-radius: var(--radius-sm);
            background: rgba(248, 250, 252, 0.42);
        }
        .switch-row + .switch-row { margin-top: 8px; }
        .switch-row strong { display: block; margin-bottom: 3px; }
        .switch-row span { color: var(--muted); font-size: 12px; }
        .toggle {
            position: relative;
            width: 46px;
            height: 26px;
            border-radius: 999px;
            border: 1px solid var(--line-strong);
            background: #eef2f7;
            flex: 0 0 auto;
            cursor: pointer;
            box-shadow: inset 0 1px 2px rgba(17, 24, 39, 0.08);
        }
        .toggle::after {
            content: "";
            position: absolute;
            top: 3px;
            left: 3px;
            width: 18px;
            height: 18px;
            border-radius: 50%;
            background: #7b8798;
            box-shadow: 0 1px 2px rgba(17, 24, 39, 0.18);
            transition: left 160ms ease, background 160ms ease;
        }
        .toggle.on { border-color: #9fb4d1; background: #edf4ff; }
        .toggle.on::after { left: 23px; background: var(--blue); }
        /* reference list */
        .reference-list { display: grid; gap: 8px; }
        .reference-item {
            display: grid;
            grid-template-columns: 56px minmax(0, 1fr);
            gap: 12px;
            padding: 12px;
            border-radius: var(--radius-sm);
            background: rgba(248, 250, 252, 0.38);
            align-items: start;
        }
        .reference-item strong { color: var(--charcoal); display: block; margin-bottom: 3px; }
        .reference-item p { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.45; }
        .tag {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            height: 24px;
            padding: 0 8px;
            border: 1px solid var(--line);
            border-radius: 999px;
            background: var(--surface-2);
            color: var(--muted);
            font-family: var(--mono);
            font-size: 11px;
            font-weight: 800;
            white-space: nowrap;
        }
        /* holding cards for portfolio */
        .holding-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
        .holding {
            border-radius: var(--radius);
            padding: 12px;
        }
        .holding-head { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 10px; align-items: center; }
        .weight-editor { display: inline-flex; align-items: center; gap: 4px; }
        .weight-step {
            width: 24px;
            height: 24px;
            min-height: 24px;
            padding: 0;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.54);
            color: var(--blue);
            border: 1px solid rgba(17, 103, 216, 0.16);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.62);
            font-family: var(--mono);
            font-size: 12px;
            font-weight: 900;
        }
        .weight-step:hover { transform: translateY(-1px); box-shadow: 0 6px 14px rgba(17, 103, 216, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.72); }
        .weight-value {
            min-width: 34px;
            text-align: right;
            font-family: var(--mono);
            font-size: 13px;
            color: var(--muted);
        }
        .holding-inputs { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 10px; }
        .holding-inputs input { min-height: 30px; padding: 4px 7px; font-size: 12px; }
        .holding-field {
            display: grid;
            gap: 4px;
            color: var(--muted);
            font-size: 11px;
            font-weight: 800;
        }
        .holding-field span[contenteditable="true"] {
            min-height: 30px;
            padding: 6px 7px;
            border: 1px solid var(--line);
            border-radius: 9px;
            background: rgba(255, 255, 255, 0.64);
            color: var(--charcoal);
            font-size: 12px;
            font-weight: 700;
        }
        .ticker { font-family: var(--mono); font-weight: 900; font-size: 14px; }
        .weight-bar { height: 5px; border-radius: 999px; background: var(--surface-3); overflow: hidden; }
        .weight-bar span { display: block; height: 100%; background: var(--blue); box-shadow: 0 0 10px rgba(17, 103, 216, 0.22); transition: width 300ms ease; }
        /* kpi grid for results */
        .kpi-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px; }
        .kpi {
            min-height: 92px;
            padding: 13px;
            border-radius: var(--radius);
        }
        .kpi span { display: block; color: var(--muted); font-size: 12px; font-weight: 800; }
        .kpi strong { display: block; margin-top: 8px; font-family: var(--mono); font-size: 22px; letter-spacing: 0; }
        .kpi.positive strong { color: var(--green); }
        .kpi.negative strong { color: var(--red); }
        .kpi.warning strong { color: var(--amber); }
        .workspace-nav { position: relative; z-index: 1; display: grid; gap: 10px; margin-top: 28px; }
        .workspace-nav button {
            width: 48px;
            height: 42px;
            border: 1px solid rgba(255, 255, 255, 0.54);
            border-radius: 14px;
            background:
                radial-gradient(circle at 24% 0%, rgba(255, 255, 255, 0.62), transparent 34%),
                linear-gradient(145deg, rgba(255, 255, 255, 0.38), rgba(235, 243, 255, 0.18)),
                rgba(255, 255, 255, 0.20);
            color: #5d6a7d;
            cursor: pointer;
            font-family: var(--sans);
            font-size: 12px;
            font-weight: 900;
            overflow: hidden;
            text-indent: -999px;
            position: relative;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.58);
        }
        .workspace-nav button::after {
            content: attr(data-short);
            position: absolute;
            inset: 0;
            display: grid;
            place-items: center;
            text-indent: 0;
        }
        .workspace-nav button:hover { border-color: rgba(17, 103, 216, 0.18); color: var(--blue); transform: translateY(-1px); box-shadow: 0 7px 16px rgba(15, 23, 42, 0.046), inset 0 1px 0 rgba(255, 255, 255, 0.7); }
        .workspace-nav button.active {
            background:
                radial-gradient(circle at 28% 0%, rgba(255, 255, 255, 0.84), transparent 40%),
                linear-gradient(145deg, rgba(227, 239, 255, 0.74), rgba(255, 255, 255, 0.34)),
                rgba(227, 239, 255, 0.36);
            border-color: rgba(17, 103, 216, 0.20);
            color: var(--blue);
            box-shadow: 0 9px 18px rgba(17, 103, 216, 0.085), inset 0 1px 0 rgba(255, 255, 255, 0.82);
        }
        .tab-hidden { display: none !important; }
        .setup-grid {
            display: grid;
            grid-template-columns: minmax(0, 1.25fr) minmax(340px, 0.75fr);
            gap: 18px;
            align-items: start;
        }
        .setup-side { position: sticky; top: 18px; }
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 38px;
            padding: 8px 13px;
            background: var(--blue);
            color: #ffffff;
            border: 1px solid var(--blue);
            border-radius: var(--radius-sm);
            cursor: pointer;
            text-decoration: none;
            font-weight: 800;
            white-space: nowrap;
            box-shadow: 0 10px 22px rgba(17, 103, 216, 0.18);
        }
        .btn:hover { transform: translateY(-1px); box-shadow: 0 14px 28px rgba(17, 103, 216, 0.22); }
        .btn-secondary { background: var(--surface); color: var(--charcoal); border-color: var(--line-strong); box-shadow: none; }
        .btn-secondary:hover { background: #f8fafc; border-color: var(--blue); }
        .btn-small { min-height: 30px; padding: 5px 10px; font-size: 13px; }
        .panel {
            border-radius: var(--radius);
            padding: 0;
            margin-bottom: 16px;
            transition: border-color 180ms ease, box-shadow 180ms ease;
        }
        .panel:hover {
            border-color: rgba(255, 255, 255, 0.86);
            box-shadow:
                0 18px 38px rgba(15, 23, 42, 0.064),
                0 3px 10px rgba(17, 103, 216, 0.045),
                inset 0 1px 0 rgba(255, 255, 255, 0.82);
        }
        .tool-head {
            display: flex; align-items: center; justify-content: space-between;
            gap: 12px; padding: 16px 18px 8px;
        }
        .tool-head h2 { margin: 0; font-size: 16px; }
        .code { color: var(--faint); font-family: var(--mono); font-size: 12px; font-weight: 800; }
        .tool-body { padding: 14px 18px 18px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 10px; }
        .panel > .grid {
            padding: 14px;
            border-radius: var(--radius-sm);
            background: rgba(248, 250, 252, 0.48);
        }
        .description-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 8px; }
        .setup-side .description-grid { grid-template-columns: 1fr; }
        .description-card { --glass-radius: var(--radius-sm); padding: 12px; border-radius: var(--radius-sm); }
        .description-card strong { display: block; margin-bottom: 6px; color: var(--charcoal); }
        .description-card p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.55; }
        label { display: grid; gap: 6px; font-weight: 800; font-size: 12px; color: var(--muted); }
        input, select {
            width: 100%;
            min-height: 36px;
            padding: 7px 9px;
            border: 1px solid var(--line);
            border-radius: var(--radius-sm);
            box-sizing: border-box;
            background: #ffffff;
            color: var(--ink);
        }
        input:hover, select:hover { border-color: var(--line-strong); }
        input:focus, select:focus { outline: 3px solid rgba(17, 103, 216, 0.16); border-color: var(--blue); }
        input[type="checkbox"] { width: auto; min-height: auto; margin-right: 6px; accent-color: var(--blue); }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 11px 10px; border-bottom: 1px solid rgba(217, 224, 234, 0.48); text-align: right; font-size: 13px; vertical-align: middle; }
        th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
        th { color: var(--muted); background: var(--surface-2); font-family: var(--mono); font-size: 11px; font-weight: 900; text-transform: uppercase; position: sticky; top: 0; z-index: 1; }
        tr:hover td { background: rgba(17, 103, 216, 0.045); }
        .score-matrix tr:hover td { background: inherit; }
        .score-matrix tr:hover .score-cell {
            background:
                linear-gradient(145deg, var(--score-fill, rgba(255, 255, 255, 0.92)), rgba(255, 255, 255, 0.66)),
                #ffffff;
        }
        .score-matrix tr:hover td:first-child { background: #ffffff; }
        .portfolio-table input { min-width: 88px; }
        .portfolio-table td { vertical-align: middle; }
        .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 14px; }
        .status {
            display: none;
            align-items: center;
            gap: 10px;
            min-height: 42px;
            margin-bottom: 16px;
            padding: 10px 14px;
            border-radius: var(--radius);
            color: var(--muted);
        }
        .status::before {
            content: "";
            width: 9px;
            height: 9px;
            border-radius: 999px;
            background: var(--muted);
            flex: 0 0 auto;
        }
        .status.info { display: flex; color: var(--blue); }
        .status.info::before { background: var(--blue); box-shadow: 0 0 0 4px rgba(17, 103, 216, 0.12); }
        .status.error { display: flex; color: var(--red); }
        .status.error::before { background: var(--red); box-shadow: 0 0 0 4px rgba(208, 68, 55, 0.12); }
        .status.success { display: flex; color: var(--green); }
        .status.success::before { background: var(--green); box-shadow: 0 0 0 4px rgba(0, 133, 111, 0.12); }
        .charts { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; }
        .chart { height: 430px; border-radius: var(--radius-sm); overflow: hidden; }
        .chart-combined { height: 720px; }
        .chart-panel-hidden { display: none; }
        .chart-tabs { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }
        .chart-tab {
            min-height: 30px;
            padding: 5px 10px;
            border: 1px solid rgba(17, 103, 216, 0.16);
            border-radius: 10px;
            background: #ffffff;
            color: var(--muted);
            box-shadow: none;
            font-size: 12px;
            font-weight: 900;
        }
        .chart-tab.active {
            border-color: rgba(17, 103, 216, 0.32);
            background: #edf4ff;
            color: var(--blue);
        }
        .hint {
            color: var(--muted);
            font-size: 13px;
            line-height: 1.55;
            margin-top: 10px;
            padding: 12px;
            background: rgba(248, 250, 252, 0.46);
            border-radius: var(--radius-sm);
        }
        .context-banner {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 12px;
            padding: 10px 12px;
            border-radius: var(--radius-sm);
            background: rgba(237, 244, 255, 0.74);
            color: var(--charcoal);
        }
        .context-banner.hidden { display: none; }
        .context-banner strong {
            display: block;
            margin-bottom: 2px;
            font-size: 13px;
        }
        .context-banner span {
            display: block;
            color: var(--muted);
            font-size: 12px;
            line-height: 1.45;
        }
        .score-weight-panel {
            display: flex;
            align-items: end;
            justify-content: space-between;
            gap: 12px;
            margin-top: 12px;
            padding: 10px 12px;
            border-radius: 18px;
            background: rgba(255, 255, 255, 0.48);
            box-shadow:
                inset 0 1px 0 rgba(255, 255, 255, 0.74),
                0 10px 26px rgba(15, 23, 42, 0.045);
        }
        .score-weight-fields {
            display: grid;
            grid-template-columns: repeat(2, minmax(120px, 160px));
            gap: 10px;
        }
        .score-weight-fields label { margin: 0; }
        .score-weight-fields input {
            min-height: 32px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.74);
            font-family: var(--mono);
            font-weight: 900;
        }
        .score-weight-note {
            max-width: 520px;
            color: var(--muted);
            font-size: 12px;
            line-height: 1.5;
            text-align: right;
        }
        .score-topic-panel {
            display: grid;
            grid-template-columns: minmax(120px, auto) minmax(0, 1fr);
            gap: 10px;
            align-items: center;
            margin-top: 12px;
            padding: 10px 12px;
            border-radius: 18px;
            background: rgba(255, 255, 255, 0.48);
            box-shadow:
                inset 0 1px 0 rgba(255, 255, 255, 0.74),
                0 10px 26px rgba(15, 23, 42, 0.035);
        }
        .score-topic-title {
            color: var(--muted);
            font-size: 12px;
            font-weight: 900;
        }
        .score-topic-options {
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
        }
        .score-topic-option {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            min-height: 30px;
            padding: 5px 9px;
            border: 1px solid rgba(17, 103, 216, 0.14);
            border-radius: 11px;
            background: rgba(255, 255, 255, 0.64);
            color: var(--ink);
            font-size: 12px;
            font-weight: 900;
            cursor: pointer;
        }
        .score-topic-option input { width: auto; min-height: 0; margin: 0; accent-color: var(--blue); }
        .score-period-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(180px, 1fr));
            gap: 10px;
            margin-top: 10px;
        }
        .score-period-card {
            padding: 10px;
            border: 1px solid rgba(17, 103, 216, 0.12);
            border-radius: 14px;
            background: rgba(255, 255, 255, 0.58);
        }
        .score-period-name {
            width: 100%;
            min-height: 30px;
            margin-bottom: 7px;
            border-radius: 10px;
            background: rgba(255, 255, 255, 0.78);
            color: var(--ink);
            font-size: 12px;
            font-weight: 900;
        }
        .score-period-fields {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 7px;
        }
        .score-period-fields label {
            margin: 0;
            color: var(--muted);
            font-size: 11px;
            font-weight: 800;
        }
        .score-period-fields input {
            min-height: 30px;
            border-radius: 10px;
            background: rgba(255, 255, 255, 0.74);
            font-size: 12px;
        }
        .scan-panel {
            margin-top: 12px;
            padding: 14px;
            border-radius: 18px;
            background: rgba(255, 255, 255, 0.50);
            box-shadow:
                inset 0 1px 0 rgba(255, 255, 255, 0.74),
                0 10px 26px rgba(15, 23, 42, 0.04);
        }
        .scan-controls {
            display: grid;
            grid-template-columns: repeat(4, minmax(138px, 1fr));
            gap: 10px;
            align-items: end;
        }
        .scan-controls label { margin: 0; }
        .scan-controls input,
        .scan-controls select {
            min-height: 34px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.76);
        }
        .scan-actions {
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
            margin-top: 10px;
        }
        .scan-mode {
            display: inline-flex;
            align-items: center;
            gap: 7px;
            min-height: 30px;
            padding: 4px 8px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.62);
            color: var(--muted);
            font-size: 12px;
            font-weight: 800;
        }
        .scan-mode select {
            min-height: 28px;
            padding: 3px 26px 3px 8px;
            border-radius: 9px;
            background: #ffffff;
            font-size: 12px;
            font-weight: 900;
        }
        .scan-note {
            color: var(--muted);
            font-size: 12px;
            line-height: 1.5;
        }
        .scan-result {
            display: none;
            margin-top: 12px;
        }
        .scan-result.show { display: block; }
        .scan-strip {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 10px;
        }
        .scan-stat {
            min-height: 82px;
            padding: 12px;
            border-radius: 16px;
            background: rgba(255, 255, 255, 0.72);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.76);
        }
        .scan-stat span {
            display: block;
            color: var(--muted);
            font-size: 12px;
            font-weight: 800;
        }
        .scan-stat strong {
            display: block;
            margin-top: 6px;
            color: var(--blue);
            font-family: var(--mono);
            font-size: 22px;
            line-height: 1.1;
        }
        .scan-stat small {
            display: block;
            margin-top: 6px;
            color: var(--muted);
            line-height: 1.45;
        }
        .scan-stage-tabs {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            margin: 6px 0 10px;
        }
        .scan-stage-tab {
            min-height: 30px;
            padding: 5px 10px;
            border: 1px solid rgba(17, 103, 216, 0.16);
            border-radius: 10px;
            background: #ffffff;
            color: var(--muted);
            box-shadow: none;
            font-size: 12px;
            font-weight: 900;
        }
        .scan-stage-tab.active {
            border-color: rgba(17, 103, 216, 0.38);
            background: #edf4ff;
            color: var(--blue);
        }
        .scan-view-tabs {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            margin: 0 0 10px;
        }
        .scan-view-tab {
            min-height: 32px;
            padding: 6px 12px;
            border: 1px solid rgba(17, 103, 216, 0.16);
            border-radius: 11px;
            background: #ffffff;
            color: var(--muted);
            box-shadow: none;
            font-size: 12px;
            font-weight: 900;
        }
        .scan-view-tab.active {
            border-color: rgba(17, 103, 216, 0.38);
            background: #edf4ff;
            color: var(--blue);
        }
        .scan-view-hidden { display: none; }
        .scan-3d-chart {
            height: 560px;
            border-radius: 16px;
            background: #ffffff;
            overflow: hidden;
        }
        .scan-table { table-layout: fixed; min-width: 820px; }
        .scan-table th,
        .scan-table td {
            width: 124px;
            min-width: 124px;
            padding: 8px;
            font-size: 11px;
            line-height: 1.2;
            vertical-align: top;
        }
        .scan-table th:first-child,
        .scan-table td:first-child {
            width: 112px;
            min-width: 112px;
            color: var(--muted);
            font-weight: 900;
            background: var(--surface-2);
        }
        .scan-cell {
            cursor: pointer;
            background: var(--scan-fill, #ffffff);
        }
        .scan-cell:hover { outline: 2px solid rgba(17, 103, 216, 0.24); outline-offset: -2px; }
        .scan-cell.baseline { box-shadow: inset 0 0 0 2px rgba(17, 103, 216, 0.46); }
        .scan-cell.best { box-shadow: inset 0 0 0 2px rgba(0, 133, 111, 0.58); }
        .scan-cell.best.baseline {
            box-shadow:
                inset 0 0 0 2px rgba(0, 133, 111, 0.64),
                inset 0 0 0 4px rgba(17, 103, 216, 0.25);
        }
        .scan-cell strong {
            display: block;
            color: var(--ink);
            font-family: var(--mono);
            font-size: 13px;
            font-weight: 900;
        }
        .scan-cell span {
            display: block;
            margin-top: 3px;
            color: var(--muted);
            font-family: var(--mono);
            font-size: 10px;
            font-weight: 800;
            white-space: nowrap;
        }
        .scan-cell em {
            display: block;
            margin-top: 4px;
            color: var(--green);
            font-style: normal;
            font-size: 10px;
            font-weight: 900;
        }
        .scan-legend {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
            margin-top: 8px;
            color: var(--muted);
            font-size: 12px;
        }
        .scan-legend i {
            display: inline-block;
            width: 22px;
            height: 10px;
            border-radius: 999px;
            background: linear-gradient(90deg, rgba(255, 184, 176, 0.95), rgba(255, 231, 184, 0.92), rgba(170, 230, 205, 0.98));
        }
        .robust-board {
            display: none;
            margin-top: 16px;
        }
        .robust-board.show {
            display: block;
        }
        .robust-strip {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
            gap: 10px;
            margin-bottom: 12px;
        }
        .robust-stat {
            padding: 12px;
            border-radius: var(--radius-sm);
            background: rgba(255, 255, 255, 0.62);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.72);
        }
        .robust-stat span {
            display: block;
            color: var(--muted);
            font-size: 11px;
            font-weight: 800;
            margin-bottom: 5px;
        }
        .robust-stat strong {
            color: var(--ink);
            font-family: var(--mono);
            font-size: 18px;
        }
        .robust-table td:first-child {
            min-width: 230px;
        }
        .metric-help.score-info-btn {
            position: static;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 16px;
            height: 16px;
            min-height: 16px;
            margin-left: 4px;
            padding: 0;
            border: 1px solid rgba(17, 103, 216, 0.28);
            border-radius: 50%;
            color: var(--blue);
            background: rgba(17, 103, 216, 0.08);
            box-shadow: none;
            font-family: var(--mono);
            font-size: 11px;
            font-weight: 900;
            line-height: 1;
            cursor: help;
            vertical-align: middle;
        }
        .robust-task {
            color: var(--muted);
            font-size: 11px;
            line-height: 1.35;
        }
        .history-list {
            display: grid;
            gap: 10px;
        }
        .history-section {
            margin-top: 14px;
        }
        .history-section:first-child {
            margin-top: 0;
        }
        .history-section-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin: 0 0 10px;
            flex-wrap: wrap;
        }
        .history-section-head h3 {
            margin: 0;
            color: var(--ink);
            font-size: 14px;
            letter-spacing: 0;
        }
        .preset-actions {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }
        .preset-actions input {
            width: 220px;
            min-height: 34px;
            padding: 8px 10px;
        }
        .history-empty {
            padding: 18px;
            border-radius: var(--radius-sm);
            background: rgba(248, 250, 252, 0.58);
            color: var(--muted);
            font-size: 13px;
            line-height: 1.55;
        }
        .history-item {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            gap: 12px;
            align-items: center;
            padding: 12px;
            border-radius: var(--radius-sm);
            background: rgba(255, 255, 255, 0.58);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.72);
        }
        .history-item strong {
            display: block;
            margin-bottom: 4px;
            color: var(--charcoal);
            font-size: 13px;
        }
        .history-item span {
            display: block;
            color: var(--muted);
            font-size: 12px;
            line-height: 1.45;
        }
        .history-item small {
            color: var(--faint);
            font-family: var(--mono);
            font-size: 11px;
            white-space: nowrap;
        }
        .history-actions {
            display: flex;
            align-items: center;
            justify-content: flex-end;
            gap: 8px;
            margin-top: 8px;
            flex-wrap: wrap;
        }
        .history-meta {
            text-align: right;
        }
        .table-wrap {
            overflow: auto;
            border-radius: var(--radius);
        }
        .table-wrap table { min-width: 860px; }
        .score-matrix { table-layout: fixed; min-width: 1740px; }
        .score-matrix th, .score-matrix td { padding: 7px 8px; font-size: 11px; line-height: 1.2; }
        .score-matrix th:first-child, .score-matrix td:first-child {
            width: 190px;
            min-width: 190px;
            max-width: 190px;
            white-space: normal;
            word-break: keep-all;
            overflow-wrap: normal;
            position: sticky;
            left: 0;
            z-index: 2;
            background: #ffffff;
        }
        .score-matrix th:first-child { z-index: 3; background: var(--surface-2); }
        .score-cell {
            position: relative;
            width: 128px;
            border-left: 1px solid rgba(217, 224, 234, 0.42);
            white-space: normal;
            color: var(--ink);
            text-align: left;
            vertical-align: top;
            background:
                linear-gradient(145deg, var(--score-fill, rgba(255, 255, 255, 0.92)), rgba(255, 255, 255, 0.66)),
                #ffffff;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.66);
        }
        .score-cell-rank {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 6px;
            margin-bottom: 5px;
            padding-right: 18px;
            color: var(--ink);
            font-family: var(--mono);
            font-size: 11px;
            font-weight: 900;
            line-height: 1.15;
        }
        .score-cell-rank span { color: var(--muted); font-size: 10px; font-weight: 800; }
        .score-metrics {
            display: grid;
            gap: 3px;
            margin-bottom: 5px;
        }
        .score-metric {
            display: block;
            min-height: 18px;
            padding: 3px 6px;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.42);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.54);
            line-height: 1.1;
            text-align: right;
        }
        .score-metric strong {
            color: var(--ink);
            font-family: var(--mono);
            font-size: 10.5px;
            font-weight: 900;
            text-align: right;
            white-space: nowrap;
            word-break: keep-all;
            overflow-wrap: normal;
            font-variant-numeric: tabular-nums;
        }
        .score-metric.return strong { color: var(--blue); }
        .score-metric.drawdown strong { color: var(--red); }
        .score-info-btn {
            position: absolute;
            top: 6px;
            right: 6px;
            display: inline-grid;
            place-items: center;
            width: 16px;
            height: 16px;
            min-height: 16px;
            padding: 0;
            border: 1px solid rgba(17, 103, 216, 0.20);
            border-radius: 999px;
            background: #ffffff;
            color: var(--blue);
            box-shadow: none;
            cursor: help;
            font-family: var(--mono);
            font-size: 10px;
            font-weight: 900;
            line-height: 1;
        }
        .score-info-btn:hover,
        .score-info-btn:focus {
            outline: none;
            border-color: rgba(17, 103, 216, 0.42);
            background: #f8fbff;
        }
        .score-tooltip {
            position: fixed;
            z-index: 50;
            width: min(280px, calc(100vw - 28px));
            padding: 11px 12px;
            border: 1px solid rgba(255, 255, 255, 0.78);
            border-radius: 14px;
            background:
                radial-gradient(circle at 18% 0%, rgba(255, 255, 255, 0.92), transparent 38%),
                linear-gradient(135deg, rgba(255, 255, 255, 0.98), rgba(244, 249, 255, 0.96) 52%, rgba(255, 255, 255, 0.98)),
                #ffffff;
            box-shadow:
                0 12px 26px rgba(15, 23, 42, 0.10),
                inset 0 1px 0 rgba(255, 255, 255, 0.92),
                inset 0 -1px 0 rgba(201, 214, 232, 0.36);
            color: var(--ink);
            pointer-events: none;
            opacity: 0;
            transition: opacity 80ms ease;
        }
        .score-tooltip.show { opacity: 1; }
        .score-tooltip strong {
            display: block;
            margin-bottom: 5px;
            font-size: 12px;
            line-height: 1.25;
        }
        .score-tooltip span {
            display: block;
            color: var(--muted);
            font-size: 11px;
            line-height: 1.45;
            white-space: pre-wrap;
        }
        .perf-panel {
            display: none;
            position: fixed;
            right: 16px;
            bottom: 16px;
            z-index: 80;
            width: min(360px, calc(100vw - 32px));
            padding: 12px;
            border: var(--liquid-glass-border);
            border-radius: 18px;
            background: var(--liquid-glass-bg);
            box-shadow:
                0 18px 38px rgba(15, 23, 42, 0.10),
                inset 0 1px 0 rgba(255, 255, 255, 0.82);
            color: var(--ink);
            font-family: var(--mono);
            pointer-events: none;
        }
        .perf-panel.show { display: block; }
        .perf-panel strong {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 8px;
            font-size: 12px;
            font-weight: 900;
        }
        .perf-panel strong span {
            color: var(--muted);
            font-size: 10px;
            font-weight: 800;
        }
        .perf-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 6px;
        }
        .perf-item {
            padding: 7px 8px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.50);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.64);
        }
        .perf-item span {
            display: block;
            color: var(--muted);
            font-size: 10px;
            line-height: 1.2;
        }
        .perf-item b {
            display: block;
            margin-top: 3px;
            color: var(--ink);
            font-size: 13px;
            font-weight: 900;
            line-height: 1.15;
            font-variant-numeric: tabular-nums;
        }
        body.full-glass-mode .rail,
        body.full-glass-mode .panel,
        body.full-glass-mode .quick-stat,
        body.full-glass-mode .metric-card,
        body.full-glass-mode .description-card,
        body.full-glass-mode .holding,
        body.full-glass-mode .kpi,
        body.full-glass-mode .table-wrap,
        body.full-glass-mode .status,
        body.full-glass-mode .mark,
        body.full-glass-mode .workspace-nav button,
        body.full-glass-mode .perf-panel {
            backdrop-filter: var(--liquid-glass-filter);
            -webkit-backdrop-filter: var(--liquid-glass-filter);
        }
        body.lite-mode {
            --page-backdrop:
                linear-gradient(180deg, #fcfdff 0%, #f1f6fd 48%, #e8eef6 100%);
            --liquid-glass-bg: rgba(255, 255, 255, 0.88);
            --liquid-glass-border: 1px solid rgba(210, 220, 234, 0.78);
            --liquid-glass-shadow: 0 8px 18px rgba(15, 23, 42, 0.035);
            --liquid-glass-filter: none;
            --liquid-glass-lens: transparent;
            --liquid-glass-lens-shadow: none;
            --liquid-glass-glow: transparent;
        }
        body.no-fixed-bg-mode,
        body.lite-mode {
            background-attachment: scroll;
        }
        body.no-blur-mode {
            --liquid-glass-filter: none;
        }
        body.no-blur-mode .rail,
        body.no-blur-mode .panel,
        body.no-blur-mode .quick-stat,
        body.no-blur-mode .metric-card,
        body.no-blur-mode .description-card,
        body.no-blur-mode .holding,
        body.no-blur-mode .kpi,
        body.no-blur-mode .table-wrap,
        body.no-blur-mode .status,
        body.no-blur-mode .mark,
        body.no-blur-mode .workspace-nav button,
        body.no-blur-mode .score-tooltip,
        body.no-blur-mode .perf-panel,
        body.lite-mode .rail,
        body.lite-mode .panel,
        body.lite-mode .quick-stat,
        body.lite-mode .metric-card,
        body.lite-mode .description-card,
        body.lite-mode .holding,
        body.lite-mode .kpi,
        body.lite-mode .table-wrap,
        body.lite-mode .status,
        body.lite-mode .mark,
        body.lite-mode .workspace-nav button,
        body.lite-mode .score-tooltip,
        body.lite-mode .perf-panel {
            backdrop-filter: none;
            -webkit-backdrop-filter: none;
        }
        body.no-lens-mode {
            --liquid-glass-lens: transparent;
            --liquid-glass-lens-shadow: none;
            --liquid-glass-glow: transparent;
        }
        body.no-lens-mode .rail::before,
        body.no-lens-mode .rail::after,
        body.no-lens-mode .panel::before,
        body.no-lens-mode .panel::after,
        body.no-lens-mode .quick-stat::before,
        body.no-lens-mode .quick-stat::after,
        body.no-lens-mode .metric-card::before,
        body.no-lens-mode .metric-card::after,
        body.no-lens-mode .description-card::before,
        body.no-lens-mode .description-card::after,
        body.no-lens-mode .holding::before,
        body.no-lens-mode .holding::after,
        body.no-lens-mode .kpi::before,
        body.no-lens-mode .kpi::after,
        body.no-lens-mode .table-wrap::before,
        body.no-lens-mode .table-wrap::after,
        body.lite-mode .rail::before,
        body.lite-mode .rail::after,
        body.lite-mode .panel::before,
        body.lite-mode .panel::after,
        body.lite-mode .quick-stat::before,
        body.lite-mode .quick-stat::after,
        body.lite-mode .metric-card::before,
        body.lite-mode .metric-card::after,
        body.lite-mode .description-card::before,
        body.lite-mode .description-card::after,
        body.lite-mode .holding::before,
        body.lite-mode .holding::after,
        body.lite-mode .kpi::before,
        body.lite-mode .kpi::after,
        body.lite-mode .table-wrap::before,
        body.lite-mode .table-wrap::after {
            display: none;
        }
        body.no-motion-mode .panel:hover,
        body.no-motion-mode .btn:hover,
        body.no-motion-mode .workspace-nav button:hover,
        body.no-motion-mode .weight-step:hover,
        body.lite-mode .panel:hover,
        body.lite-mode .btn:hover,
        body.lite-mode .workspace-nav button:hover,
        body.lite-mode .weight-step:hover {
            transform: none;
            box-shadow: var(--liquid-glass-shadow);
        }
        body.no-sticky-mode th,
        body.no-sticky-mode .score-matrix th:first-child,
        body.no-sticky-mode .score-matrix td:first-child,
        body.no-sticky-mode .setup-side,
        body.no-sticky-mode .rail,
        body.lite-mode th,
        body.lite-mode .score-matrix th:first-child,
        body.lite-mode .score-matrix td:first-child,
        body.lite-mode .setup-side,
        body.lite-mode .rail {
            position: static;
        }
        body.no-sticky-mode .rail,
        body.lite-mode .rail {
            height: auto;
        }
        body.soft-shadow-mode {
            --liquid-glass-shadow: 0 8px 18px rgba(15, 23, 42, 0.035);
        }
        body.soft-shadow-mode .score-cell,
        body.soft-shadow-mode .score-metric,
        body.soft-shadow-mode .score-detail-btn,
        body.soft-shadow-mode .score-weight-panel,
        body.lite-mode .score-cell,
        body.lite-mode .score-metric,
        body.lite-mode .score-detail-btn,
        body.lite-mode .score-weight-panel {
            box-shadow: none;
        }
        body.no-motion-mode *,
        body.lite-mode * {
            transition: none !important;
        }
        .score-detail-btn {
            width: 100%;
            min-height: 22px;
            margin-top: 2px;
            padding: 2px 6px;
            border-radius: 8px;
            font-size: 10px;
            background: rgba(255, 255, 255, 0.8);
            color: var(--blue);
            border-color: rgba(17, 103, 216, 0.22);
        }
        .summary-title { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
        .summary-title h2 { margin: 0; font-size: 16px; color: var(--ink); letter-spacing: 0; }
        .small { color: var(--muted); font-size: 13px; }
        .section-code {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: auto;
            min-width: 26px;
            height: 26px;
            margin-right: 8px;
            padding: 0 8px;
            border-radius: 999px;
            background: rgba(17, 103, 216, 0.08);
            color: var(--blue);
            font-family: var(--mono);
            font-size: 11px;
            font-weight: 900;
            vertical-align: middle;
        }
        .explain-drawer {
            margin-top: 12px;
            border: 0;
            border-radius: var(--radius-sm);
            background: rgba(248, 250, 252, 0.42);
        }
        .explain-drawer summary {
            cursor: pointer;
            padding: 11px 12px;
            color: var(--blue);
            font-weight: 800;
            user-select: none;
        }
        .explain-drawer .description-grid { padding: 0 12px 12px; }
        .detail-controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 8px 0 14px; }
        .detail-controls select { width: auto; min-width: 160px; }
        .detail-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 10px; margin-bottom: 12px; }
        .metric-card { padding: 11px; }
        .metric-card span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 5px; }
        .metric-card strong { font-family: var(--mono); font-size: 17px; color: var(--ink); }
        @media (max-width: 1120px) {
            .setup-grid, .charts { grid-template-columns: 1fr; }
            .setup-side { position: static; }
            .quick-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
            .holding-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .fieldset { grid-template-columns: 1fr; }
            .fieldset .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        @media (max-width: 760px) {
            .shell { grid-template-columns: 1fr; }
            .rail {
                position: static;
                height: auto;
                margin: 12px 14px 0;
                padding: 10px;
                border-radius: 22px;
                display: flex;
                align-items: center;
                justify-content: space-between;
            }
            .workspace-nav { display: flex; margin-top: 0; }
            .container { padding: 18px 14px 28px; }
            .header { grid-template-columns: 1fr; }
            .header-actions { justify-content: flex-start; }
            .command-bar { grid-template-columns: 1fr; }
            .command-actions { justify-content: flex-start; }
            .quick-stats, .kpi-grid, .holding-grid { grid-template-columns: 1fr; }
            .fieldset .grid { grid-template-columns: 1fr; }
            .score-weight-panel { align-items: stretch; flex-direction: column; }
            .score-weight-fields { grid-template-columns: 1fr 1fr; }
            .score-weight-note { max-width: none; text-align: left; }
            .chart { height: 340px; }
            .history-item { grid-template-columns: 1fr; }
            .history-meta { text-align: left; }
            .history-actions { justify-content: flex-start; }
            .preset-actions input { width: 100%; }
        }
    </style>
</head>
<body>
    <div class="shell">
        <aside class="rail">
            <div class="mark">SL</div>
            <nav class="workspace-nav">
                <button class="active" type="button" data-tab="setup" data-short="配置" onclick="activateTab('setup')">实验配置</button>
                <button type="button" data-tab="results" data-short="演算" onclick="activateTab('results')">组合演算</button>
                <button type="button" data-tab="scorecard" data-short="评分" onclick="activateTab('scorecard')">策略评分</button>
                <button type="button" data-tab="scan" data-short="扫描" onclick="activateTab('scan')">参数扫描</button>
                <button type="button" data-tab="history" data-short="历史" onclick="activateTab('history')">运行历史</button>
            </nav>
        </aside>

        <main class="container">
        <div class="header">
            <div class="title-block">
                <div class="title-kicker">Strategy Lab</div>
                <h1>仓位策略实验室</h1>
                <p>按交易日时序演算股票回撤加仓、卖出规则和期权影子仓位。</p>
            </div>
            <div class="header-actions">
                <a href="/" class="btn btn-secondary">BOLL 首页</a>
                <a href="/drawdown" class="btn btn-secondary">Drawdown</a>
            </div>
        </div>

        <div class="quick-stats">
            <div class="quick-stat"><strong>6 × 4</strong><span>买入与卖出策略矩阵</span></div>
            <div class="quick-stat"><strong>4</strong><span>默认组合标的，可直接改权重</span></div>
            <div class="quick-stat"><strong>20%</strong><span>默认期权影子仓位比例</span></div>
            <div class="quick-stat"><strong>T+0</strong><span>按日线时序滚动演算</span></div>
        </div>

        <div id="status" class="status"></div>

        <div id="jobPanel" class="job-panel" aria-live="polite">
            <div class="job-head">
                <strong id="jobTitle">后台任务</strong>
                <span id="jobStage">QUEUED</span>
            </div>
            <div class="job-progress"><span id="jobProgress"></span></div>
            <div id="jobMessage" class="job-message">等待任务开始。</div>
        </div>

        <div class="command-bar" aria-label="策略实验室操作条">
            <div class="command-main">
                <div class="command-title">
                    <span>当前工作参数</span>
                    <strong id="workspaceMode">参数设置</strong>
                </div>
                <div id="commandSummary" class="command-summary"></div>
                <div id="freshnessState" class="freshness-state idle">修改参数后，可在任意页面直接运行演算或评分。</div>
            </div>
            <div class="command-actions">
                <button class="btn" type="button" onclick="runLab()">运行演算</button>
                <button class="btn btn-secondary" type="button" onclick="runScorecard()">运行评分</button>
                <button class="btn btn-secondary" type="button" onclick="activateTab('scan')">参数扫描</button>
                <button class="btn btn-secondary" type="button" onclick="saveStrategyDefaults()">保存默认值</button>
                <button class="btn btn-secondary" type="button" onclick="activateTab('setup')">编辑参数</button>
                <button class="btn btn-secondary" type="button" onclick="activateTab('history')">运行历史</button>
            </div>
        </div>

        <div id="setup" class="setup-grid" data-tab-panel="setup">
            <div class="setup-main">
        <div class="panel">
            <div class="tool-head">
                <h2>演算设置</h2>
                <div style="display:flex;align-items:center;gap:8px;">
                    <button class="btn" type="button" onclick="runLab()">运行演算</button>
                    <button class="btn btn-secondary" type="button" onclick="saveStrategyDefaults()">保存默认值</button>
                    <button class="btn btn-secondary" type="button" onclick="applyRangePreset('1y')">1Y</button>
                    <button class="btn btn-secondary" type="button" onclick="applyRangePreset('3y')">3Y</button>
                    <button class="btn btn-secondary" type="button" onclick="applyRangePreset('5y')">5Y</button>
                    <span class="code">01 / INPUTS</span>
                </div>
            </div>
            <div class="tool-body">
            <div class="fieldsets">
                <div class="fieldset">
                    <div class="fieldset-title">资金与时间</div>
                    <div class="grid">
                        <div>
                            <label for="initialCash">初始资金 USD</label>
                            <input id="initialCash" type="number" min="1" step="100" value="{{ default_config.default_initial_cash }}">
                        </div>
                        <div>
                            <label for="monthlyContribution">每月注入 USD</label>
                            <input id="monthlyContribution" type="number" min="0" step="100" value="{{ default_config.default_monthly_contribution }}">
                        </div>
                        <div>
                            <label for="start">开始日期</label>
                            <input id="start" type="date" value="{{ default_start }}">
                        </div>
                        <div>
                            <label for="end">结束日期</label>
                            <input id="end" type="date" value="{{ default_end }}">
                        </div>
                        <div>
                            <label for="tradeFee">单笔手续费 USD</label>
                            <input id="tradeFee" type="number" min="0" step="0.01" value="{{ default_config.default_trade_fee }}">
                        </div>
                        <div>
                            <label for="hkdToUsd">HKD/USD</label>
                            <input id="hkdToUsd" type="number" min="0.01" step="0.001" value="{{ default_config.default_hkd_to_usd }}">
                        </div>
                    </div>
                </div>
                <div class="fieldset">
                    <div class="fieldset-title">买入规则</div>
                    <div class="grid">
                        <div>
                            <label for="buyStrategy">买入策略</label>
                            <select id="buyStrategy">
                                <option value="all" {% if default_config.default_buy_strategy == 'all' %}selected{% endif %}>全部买入策略</option>
                                {% for key, label in buy_strategy_labels.items() %}
                                    <option value="{{ key }}" {% if default_config.default_buy_strategy == key %}selected{% endif %}>{{ label }}</option>
                                {% endfor %}
                            </select>
                        </div>
                        <div>
                            <label for="maxDrawdown">最大可接受回撤 %</label>
                            <input id="maxDrawdown" type="number" min="1" max="95" step="0.5" value="{{ default_config.default_max_drawdown_pct }}">
                        </div>
                        <div>
                            <label for="drawdownBasis">回撤口径</label>
                            <select id="drawdownBasis">
                                <option value="ath" {% if default_config.default_drawdown_basis == 'ath' %}selected{% endif %}>全部回撤</option>
                                <option value="rolling_120" {% if default_config.default_drawdown_basis == 'rolling_120' %}selected{% endif %}>120天回撤</option>
                            </select>
                        </div>
                        <div>
                            <label for="stepPct">细切步长 %</label>
                            <input id="stepPct" type="number" min="0.5" max="50" step="0.5" value="{{ default_config.default_slice_step_pct }}">
                        </div>
                        <div>
                            <label for="equalSliceAllocation">等距每档仓位 %</label>
                            <input id="equalSliceAllocation" type="number" min="0.1" max="100" step="0.5" value="{{ default_config.default_equal_slice_allocation_pct }}">
                        </div>
                    </div>
                </div>
                <div class="fieldset">
                    <div class="fieldset-title">卖出规则</div>
                    <div class="grid">
                        <div>
                            <label for="sellStrategy">卖出策略</label>
                            <select id="sellStrategy">
                                <option value="all" {% if default_config.default_sell_strategy == 'all' %}selected{% endif %}>全部卖出策略</option>
                                {% for key, label in sell_strategy_labels.items() %}
                                    <option value="{{ key }}" {% if default_config.default_sell_strategy == key %}selected{% endif %}>{{ label }}</option>
                                {% endfor %}
                            </select>
                        </div>
                        <div>
                            <label for="reservePosition">保留底仓 %</label>
                            <input id="reservePosition" type="number" min="0" max="100" step="1" value="{{ default_config.default_reserve_position_pct }}">
                        </div>
                        <div>
                            <label for="sellMinProfit">最小卖出盈利 %</label>
                            <input id="sellMinProfit" type="number" min="0" step="0.5" value="{{ default_config.default_sell_min_profit_pct }}">
                        </div>
                        <div>
                            <label for="repairSellCooldown">修复卖出冷却天数</label>
                            <input id="repairSellCooldown" type="number" min="0" step="1" value="{{ default_config.default_repair_sell_cooldown_days }}">
                        </div>
                        <div>
                            <label for="repairStageSellPct">修复单档卖出 %</label>
                            <input id="repairStageSellPct" type="number" min="0" max="100" step="0.5" value="{{ default_config.default_repair_stage_sell_pct }}">
                        </div>
                    </div>
                </div>
            </div>
            <div class="hint" style="margin-top: 12px;">演算按交易日从早到晚推进，每天只使用截至当天的价格、回撤、现金和持仓状态；不会提前读取未来走势。价格修复到接近 ATH 后会进入下一轮交易周期，买入档位和分档卖出规则可重新触发。阶梯修复卖出每次只执行一个修复档，并在卖出后进入交易日冷却期；等距细切、底仓、手续费、汇率、评分权重和期权参数都可以通过“保存默认值”写入配置，下次打开自动带出。HK 标的按页面汇率折算成 USD。</div>
            </div>
        </div>

        <div class="panel">
            <div class="tool-head">
                <h2>期权叠加</h2>
                <span class="code">SHADOW</span>
            </div>
            <div class="tool-body">
            <div class="switch-row">
                <div><strong>启用期权叠加</strong><span>买点对齐股票买点，收益独立展示</span></div>
                <div class="toggle {% if default_config.default_option_enabled %}on{% endif %}" id="optionToggle" onclick="toggleOption()"></div>
                <input id="optionEnabled" type="checkbox" style="display:none" {% if default_config.default_option_enabled %}checked{% endif %}>
            </div>
            <div class="fieldsets" style="margin-top: 10px;">
                <div class="fieldset">
                    <div class="fieldset-title">仓位与合约</div>
                    <div class="grid">
                        <div>
                            <label for="optionAllocation">期权资金比例 %</label>
                            <input id="optionAllocation" type="number" min="0" max="100" step="1" value="{{ default_config.default_option_allocation_pct }}">
                        </div>
                        <div>
                            <label for="optionMoneyness">行权价规则</label>
                            <select id="optionMoneyness">
                                <option value="atm" {% if default_config.default_option_moneyness == "atm" %}selected{% endif %}>ATM</option>
                                <option value="itm_10" {% if default_config.default_option_moneyness == "itm_10" %}selected{% endif %}>ITM 10%</option>
                                <option value="otm_10" {% if default_config.default_option_moneyness == "otm_10" %}selected{% endif %}>OTM 10%</option>
                            </select>
                        </div>
                        <div>
                            <label for="optionMaxTrades">每组合最多期权买点</label>
                            <input id="optionMaxTrades" type="number" min="1" max="200" step="1" value="{{ default_config.default_option_max_trades_per_strategy }}">
                        </div>
                        <div>
                            <label for="optionTradeFee">期权单笔手续费 USD</label>
                            <input id="optionTradeFee" type="number" min="0" step="0.01" value="{{ default_config.default_option_trade_fee }}">
                        </div>
                    </div>
                </div>
                <div class="fieldset">
                    <div class="fieldset-title">DTE 与止盈</div>
                    <div class="grid">
                        <div>
                            <label for="optionTargetDte">目标 DTE</label>
                            <input id="optionTargetDte" type="number" min="30" step="1" value="{{ default_config.default_option_target_dte }}">
                        </div>
                        <div>
                            <label for="optionMinDte">最小 DTE</label>
                            <input id="optionMinDte" type="number" min="1" step="1" value="{{ default_config.default_option_min_dte }}">
                        </div>
                        <div>
                            <label for="optionMaxDte">最大 DTE</label>
                            <input id="optionMaxDte" type="number" min="1" step="1" value="{{ default_config.default_option_max_dte }}">
                        </div>
                        <div>
                            <label for="optionExitDte">DTE 小于此值退出</label>
                            <input id="optionExitDte" type="number" min="1" step="1" value="{{ default_config.default_option_exit_dte }}">
                        </div>
                        <div>
                            <label for="optionProfitTake">止盈涨幅 %</label>
                            <input id="optionProfitTake" type="number" min="1" step="5" value="{{ default_config.default_option_profit_take_pct }}">
                        </div>
                        <div>
                            <label for="optionProfitSell">止盈卖出 %</label>
                            <input id="optionProfitSell" type="number" min="1" max="100" step="5" value="{{ default_config.default_option_profit_take_sell_pct }}">
                        </div>
                    </div>
                </div>
            </div>
            <details class="explain-drawer">
                <summary>查看期权参数说明</summary>
            <div class="description-grid">
                <div class="description-card">
                    <strong>期权资金比例</strong>
                    <p>股票策略触发买入时，按这笔股票买入金额的一定比例估算期权投入。默认 20%，表示股票买入 1000 USD 时，同步用 200 USD 做期权影子仓位。</p>
                </div>
                <div class="description-card">
                    <strong>DTE</strong>
                    <p>DTE 是 Days To Expiration，也就是距离期权到期还剩多少天。目标 DTE 默认 365，表示优先找大约一年后到期的 Call。</p>
                </div>
                <div class="description-card">
                    <strong>最小 / 最大 DTE</strong>
                    <p>这是可接受的到期日范围。默认 300-450 天，如果没有刚好一年后的期权，就在这个范围里找最接近目标 DTE 的合约。</p>
                </div>
                <div class="description-card">
                    <strong>ATM / ITM / OTM</strong>
                    <p>ATM 是行权价接近当前股价；ITM 10% 是行权价约低于股价 10%，更贵但更稳；OTM 10% 是行权价约高于股价 10%，更便宜但归零风险更高。</p>
                </div>
                <div class="description-card">
                    <strong>止盈涨幅 / 止盈卖出</strong>
                    <p>默认期权价格涨 100% 时卖出 50%。比如 10 美元买入的期权涨到 20 美元，就先卖掉一半，剩余部分继续博弹性。</p>
                </div>
                <div class="description-card">
                    <strong>DTE 小于此值退出</strong>
                    <p>用于避免期权太接近到期。默认小于 120 天就卖剩余仓位，因为越接近到期，时间价值损耗通常越明显。</p>
                </div>
                <div class="description-card">
                    <strong>每组合最多期权买点</strong>
                    <p>限制每个策略组合最多处理多少个期权买入点，避免一次演算请求太多 Polygon 数据导致页面等待过久或触发限流。</p>
                </div>
                <div class="description-card">
                    <strong>不并入主组合</strong>
                    <p>期权是独立的高弹性影子仓位。主表里的股票收益曲线不被期权改变，期权结果只显示在期权收益率、期权投入和详情明细中。</p>
                </div>
            </div>
            </details>
            </div>
        </div>
            </div>

            <div class="setup-side">
        <div class="panel">
            <div class="tool-head">
                <h2>策略参考</h2>
                <span class="code">RULES</span>
            </div>
            <div class="tool-body">
            <div class="reference-list">
                <div class="reference-item"><span class="tag">BUY</span><div><strong>三档金字塔</strong><p>以最大可接受回撤为锚点，在 20%、50%、100% 三个回撤档触发买入，对应投入 20%、30%、50% 的标的预算。</p></div></div>
                <div class="reference-item"><span class="tag">BUY</span><div><strong>等距细切</strong><p>按细切步长逐档触发，默认每回撤 5% 买入该标的预算的 5%，交易更平滑，但固定手续费影响更明显。</p></div></div>
                <div class="reference-item"><span class="tag">BUY</span><div><strong>线性递增加权细切</strong><p>同样按细切步长触发，投入按档位序号 1、2、3... 递增并归一化到 100% 预算，前期投入比平方递增更有存在感。</p></div></div>
                <div class="reference-item"><span class="tag">BUY</span><div><strong>平方递增加权细切</strong><p>同样按细切步长触发，投入按档位序号平方递增并归一化到 100% 预算，更保守地把资金留给深度回撤。</p></div></div>
                <div class="reference-item"><span class="tag">BUY</span><div><strong>每周定投</strong><p>在所选时间段内，每周第一个可交易日等额买入，把该标的预算平均分配到所有周，不依赖回撤触发。</p></div></div>
                <div class="reference-item"><span class="tag">BUY</span><div><strong>工资流定投</strong><p>每周首个交易日按每月注入资金动态买入，默认保留累计投入的 10% 现金垫；回撤越深，当周投入按 1.0x、1.3x、1.8x、2.5x 放大。</p></div></div>
                <div class="reference-item"><span class="tag">SELL</span><div><strong>不卖出</strong><p>只执行买入策略，不触发任何卖出，用来观察纯回撤加仓在所选时间段内的结果。</p></div></div>
                <div class="reference-item"><span class="tag">SELL</span><div><strong>阶梯修复卖出</strong><p>每笔买入独立判断：该笔回撤修复到买入回撤的 50%、20% 以及接近 ATH 时分批卖出，默认每次卖 12%、冷却 30 天，并要求至少 10% 盈利。</p></div></div>
                <div class="reference-item"><span class="tag">SELL</span><div><strong>网格回弹卖出</strong><p>每笔买入独立配对退出：回撤修复 1 个 step 卖该笔一半，修复 2 个 step 卖该笔剩余部分。</p></div></div>
                <div class="reference-item"><span class="tag">SELL</span><div><strong>成本区间去杠杆</strong><p>按整体持仓成本触发，价格高于平均成本 8%、15%、25% 时分批降仓，保留设定底仓。</p></div></div>
            </div>
            </div>
        </div>
            </div>
        </div>

        <div id="portfolio" class="panel" data-tab-panel="setup">
            <div class="tool-head">
                <h2>组合权重</h2>
                <span class="code">PORTFOLIO</span>
            </div>
            <div class="tool-body">
            <div id="holdingGrid" class="holding-grid"></div>
            <div class="hint" style="margin-top: 12px;">点击标的、名称、权重和评分回撤上限可直接编辑；权重在运行时自动归一化到 100%，评分会按 symbol 把单股回撤上限应用到对应题目。</div>
            </div>
        </div>

        <div id="scanWorkspace" class="panel tab-hidden" data-tab-panel="scan">
            <div class="tool-head">
                <h2>卖出参数扫描</h2>
                <div style="display:flex;align-items:center;gap:8px;">
                    <span class="small">单参数扫描用于局部调参；稳健榜用分阶段搜索找跨题目 Top10。</span>
                    <button class="btn btn-secondary btn-small" type="button" onclick="runSellParameterScan()">运行扫描</button>
                    <button class="btn btn-secondary btn-small" type="button" onclick="runRobustLeaderboard()">稳健 Top10</button>
                    <span class="code">SCAN</span>
                </div>
            </div>
            <div class="tool-body">
            <div class="hint">扫描用于优化阶梯修复卖出参数；稳健 Top10 会读取评分页勾选的股票/组合与时间阶段，先用代表性题目粗筛，再局部加密，最后对候选做全题验证。行情数据统一准备，避免每个候选重复请求外部 API。</div>
            <div class="scan-panel" aria-label="卖出参数扫描">
                <div class="scan-controls">
                    <label>买入规则
                        <select id="scanBuyStrategy">
                            {% for key, label in buy_strategy_labels.items() %}
                                <option value="{{ key }}" {% if key == default_config.default_scan_buy_strategy %}selected{% endif %}>{{ label }}</option>
                            {% endfor %}
                        </select>
                    </label>
                    <label>扫描周期
                        <select id="scanPeriod">
                            <option value="252" data-fetch-days="365" {% if default_config.default_scan_period_trading_days == 252 %}selected{% endif %}>近 252 交易日</option>
                            <option value="756" data-fetch-days="1095" {% if default_config.default_scan_period_trading_days == 756 %}selected{% endif %}>近 756 交易日</option>
                            <option value="1260" data-fetch-days="1825" {% if default_config.default_scan_period_trading_days == 1260 %}selected{% endif %}>近 1260 交易日</option>
                        </select>
                    </label>
                    <label>最小盈利 %
                        <input id="scanSellMinProfits" type="text" value="{{ default_config.default_scan_sell_min_profit_values }}">
                    </label>
                    <label>冷却天数
                        <input id="scanCooldowns" type="text" value="{{ default_config.default_scan_repair_cooldown_values }}">
                    </label>
                    <label>单档卖出 %
                        <input id="scanStageSells" type="text" value="{{ default_config.default_scan_repair_stage_sell_values }}">
                    </label>
                </div>
                <div class="scan-actions">
                    <button class="btn btn-secondary btn-small" type="button" onclick="runSellParameterScan()">运行扫描</button>
                    <button class="btn btn-secondary btn-small" type="button" onclick="runRobustLeaderboard()">运行稳健 Top10</button>
                    <label class="scan-mode">评分口径
                        <select id="scanScoreMode" onchange="rerenderSellScan()">
                            <option value="balanced" {% if default_config.default_scan_score_mode == 'balanced' %}selected{% endif %}>综合参数评分</option>
                            <option value="return_drawdown" {% if default_config.default_scan_score_mode == 'return_drawdown' %}selected{% endif %}>仅收益&回撤</option>
                        </select>
                    </label>
                    <span class="scan-note">当前设置作为基准；点击热力格可把参数回填到实验配置。</span>
                </div>
                <div id="scanResult" class="scan-result">
                    <div id="scanStrip" class="scan-strip"></div>
                    <div class="scan-view-tabs">
                        <button id="scanView2dBtn" class="scan-view-tab active" type="button" onclick="setScanView('2d')">2D 热力表</button>
                        <button id="scanView3dBtn" class="scan-view-tab" type="button" onclick="setScanView('3d')">3D 参数图</button>
                    </div>
                    <div id="scan2dView">
                        <div id="scanStageTabs" class="scan-stage-tabs"></div>
                        <div class="table-wrap">
                            <table class="scan-table">
                                <thead id="scanMatrixHead"></thead>
                                <tbody id="scanMatrixBody"></tbody>
                            </table>
                        </div>
                    </div>
                    <div id="scan3dView" class="scan-view-hidden">
                        <div id="scan3dChart" class="scan-3d-chart"></div>
                    </div>
                    <div class="scan-legend"><i></i><span id="scanLegendText">颜色按综合参数评分从低到高；蓝框是当前参数，绿框是本次扫描最佳。</span></div>
                </div>
                <div id="robustBoard" class="robust-board">
                    <div class="summary-title">
                        <h2>稳健 Top10</h2>
                        <span id="robustRange" class="small"></span>
                    </div>
                    <div id="robustStrip" class="robust-strip"></div>
                    <div class="table-wrap">
                        <table class="robust-table">
                            <thead>
                                <tr>
                                    <th>策略 / 参数</th>
                                    <th>稳健分</th>
                                    <th>均分</th>
                                    <th>P25 <button class="metric-help score-info-btn" type="button" aria-label="解释 P25" data-tooltip="P25\n所有题目得分的第 25 分位数。\n它代表偏弱场景里的保底表现，越高说明策略不容易只靠少数题目拉高均值。">?</button></th>
                                    <th>Top10% <button class="metric-help score-info-btn" type="button" aria-label="解释 Top10%" data-tooltip="Top10%\n该策略在多少比例的题目中进入候选排名前 10%。\n这是强势命中率，越高说明跨股票/阶段更常排在前列。">?</button></th>
                                    <th>Bottom10% <button class="metric-help score-info-btn" type="button" aria-label="解释 Bottom10%" data-tooltip="Bottom10%\n该策略在多少比例的题目中落入候选排名后 10%。\n这是踩坑率，越低说明跨股票/阶段更少垫底。">?</button></th>
                                    <th>均收益</th>
                                    <th>均回撤</th>
                                    <th>最强 / 最弱题目</th>
                                </tr>
                            </thead>
                            <tbody id="robustBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>
            </div>
        </div>

        <div id="historyWorkspace" class="panel tab-hidden" data-tab-panel="history">
            <div class="tool-head">
                <h2>运行历史</h2>
                <div style="display:flex;align-items:center;gap:8px;">
                    <button class="btn btn-secondary btn-small" type="button" onclick="refreshHistoryWorkspace()">刷新</button>
                    <span class="small">成功完成的演算、评分和扫描会保存到服务端。</span>
                    <span class="code">HISTORY</span>
                </div>
            </div>
            <div class="tool-body">
            <div class="hint">这里是跨页面和跨容器重启的研究记录：可以从历史恢复参数，再回到配置、演算、评分或扫描继续迭代。记录只保存参数与摘要，不保存完整大结果。</div>
            <div class="history-section">
                <div class="history-section-head">
                    <h3>参数预设</h3>
                    <div class="preset-actions">
                        <input id="presetName" type="text" maxlength="80" placeholder="预设名称">
                        <button class="btn btn-secondary btn-small" type="button" onclick="saveCurrentPreset()">保存当前参数</button>
                    </div>
                </div>
                <div id="presetList" class="history-list"></div>
            </div>
            <div class="history-section">
                <div class="history-section-head">
                    <h3>运行记录</h3>
                </div>
                <div id="runHistoryList" class="history-list"></div>
            </div>
            </div>
        </div>

        <div id="kpiGrid" class="kpi-grid tab-hidden" data-tab-panel="results">
            <div class="kpi" id="kpiBestReturn"><span>最佳组合收益</span><strong>--</strong></div>
            <div class="kpi" id="kpiWorstDrawdown"><span>最大组合回撤</span><strong>--</strong></div>
            <div class="kpi" id="kpiCashUsage"><span>现金峰值使用</span><strong>--</strong></div>
            <div class="kpi" id="kpiTrades"><span>买入 / 卖出</span><strong>--</strong></div>
            <div class="kpi" id="kpiOptionReturn"><span>期权影子收益</span><strong>--</strong></div>
        </div>

        <div id="comparison" class="panel tab-hidden" data-tab-panel="results">
            <div class="tool-head">
                <h2>买卖组合核心对比</h2>
                <div style="display:flex;align-items:center;gap:8px;">
                    <span id="rangeLabel" class="small"></span>
                    <label style="display:contents;" for="summarySort"><span class="code">排序</span></label>
                    <select id="summarySort" onchange="rerenderSummaryFromSort()" style="width: auto; min-width: 150px;">
                        <option value="return_desc">收益从高到低</option>
                        <option value="return_asc">收益从低到高</option>
                        <option value="drawdown_desc">回撤从大到小</option>
                        <option value="drawdown_asc">回撤从小到大</option>
                        <option value="original">默认顺序</option>
                    </select>
                </div>
            </div>
            <div class="tool-body">
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>详情</th>
                            <th>买入策略</th>
                            <th>卖出策略</th>
                            <th>最终市值</th>
                            <th>累计投入</th>
                            <th>总收益率</th>
                            <th>累计盈利</th>
                            <th>最大组合回撤</th>
                            <th>现金使用率</th>
                            <th>买入次数</th>
                            <th>卖出次数</th>
                            <th>卖出质量</th>
                            <th>卖出盈利</th>
                            <th>卖出回撤</th>
                            <th>现金复用</th>
                            <th>期权收益率</th>
                            <th>期权投入</th>
                            <th>累计手续费</th>
                            <th>手续费/投入</th>
                        </tr>
                    </thead>
                    <tbody id="summaryBody">
                        <tr><td colspan="19">尚未运行。</td></tr>
                    </tbody>
                </table>
            </div>
            </div>
        </div>

        <div id="scorecard" class="panel tab-hidden" data-tab-panel="scorecard">
            <div class="tool-head">
                <h2>策略评分</h2>
                <div style="display:flex;align-items:center;gap:8px;">
                    <span id="scorecardRange" class="small">12 个题目：全仓 TSM / GOOGL / TSLA / 当前组合，分别跑近 252 / 756 / 1260 个交易日。</span>
                    <button class="btn btn-secondary btn-small" type="button" onclick="runScorecard()">运行评分</button>
                    <span class="code">SCORE</span>
                </div>
            </div>
            <div class="tool-body">
            <div class="hint">评分只比较收益率和最大回撤。若设置每月注入，收益率按累计投入计算；汇总表总分按平均收益、平均回撤归一化计算；卖出质量、卖出盈利、卖出回撤、现金复用只做观察诊断，不参与总分。题目矩阵单元格保留每个题目内部的归一化评分。期权叠加不参与评分。</div>
            <div class="score-weight-panel" aria-label="评分权重">
                <div class="score-weight-fields">
                    <label>收益权重 %
                        <input id="scoreReturnWeight" type="number" min="0" max="100" step="1" value="{{ default_config.default_score_return_weight_pct }}">
                    </label>
                    <label>回撤权重 %
                        <input id="scoreDrawdownWeight" type="number" min="0" max="100" step="1" value="{{ default_config.default_score_drawdown_weight_pct }}">
                    </label>
                </div>
                <div class="score-weight-note">运行评分时读取当前权重；后端会按比例归一化，例如 80 / 20 与 4 / 1 等价。</div>
            </div>
            <div class="score-topic-panel" aria-label="评分题目">
                <div class="score-topic-title">题目矩阵</div>
                <div>
                    <div class="score-topic-options">
                        {% for item in scorecard_portfolios %}
                            <label class="score-topic-option">
                                <input type="checkbox" name="scoreTopic" value="{{ item.key }}" onchange="updateScorecardQuestionHint()" {% if item.key in default_scorecard_portfolio_keys %}checked{% endif %}>
                                {{ item.short_label }}
                            </label>
                        {% endfor %}
                    </div>
                    <div class="score-period-grid">
                        {% for period in scorecard_periods %}
                            <div class="score-period-card">
                                <input class="score-period-name" type="text" value="{{ period.label }}" data-score-period="{{ period.key }}" data-period-field="label" aria-label="{{ period.label }} 名称">
                                <div class="score-period-fields">
                                    <label>开始
                                        <input type="date" value="{{ period.start }}" data-score-period="{{ period.key }}" data-period-field="start">
                                    </label>
                                    <label>结束
                                        <input type="date" value="{{ period.end }}" data-score-period="{{ period.key }}" data-period-field="end">
                                    </label>
                                </div>
                            </div>
                        {% endfor %}
                    </div>
                </div>
            </div>
            <div class="table-wrap" style="margin-top: 12px;">
                <table>
                    <thead>
                        <tr>
                            <th>排名</th>
                            <th>策略组合</th>
                            <th>总分</th>
                            <th>平均收益</th>
                            <th>平均回撤</th>
                            <th>卖出质量</th>
                            <th>卖出盈利</th>
                            <th>卖出回撤</th>
                            <th>现金复用</th>
                            <th>平均名次</th>
                            <th>最好名次</th>
                            <th>最差名次</th>
                        </tr>
                    </thead>
                    <tbody id="scorecardBody">
                        <tr><td colspan="12">尚未运行评分。</td></tr>
                    </tbody>
                </table>
            </div>
            <div class="summary-title" style="margin-top: 16px;">
                <h2>题目矩阵</h2>
                <span class="small">单元格显示排名 / 分数；点详情可直接查看该题目下的买卖点。</span>
            </div>
            <div class="table-wrap">
                <table class="score-matrix">
                    <thead id="scoreMatrixHead">
                        <tr><th>策略组合</th></tr>
                    </thead>
                    <tbody id="scoreMatrixBody">
                        <tr><td>尚未运行评分。</td></tr>
                    </tbody>
                </table>
            </div>
            </div>
        </div>

        <div class="panel tab-hidden" id="detailPanel" data-tab-panel="results" hidden>
            <div class="tool-head">
                <h2 id="detailTitle">买卖点图</h2>
                <span class="code">BS</span>
            </div>
            <div class="tool-body">
            <div id="scoreReturnBanner" class="context-banner hidden"></div>
            <div class="detail-controls">
                <label for="detailSymbol" style="display:contents;"><span style="color:var(--muted);font-size:12px;font-weight:800;">标的</span></label>
                <select id="detailSymbol" onchange="renderActiveDetail()"></select>
                <button class="btn btn-secondary btn-small" type="button" onclick="hideDetail()">关闭详情</button>
            </div>
            <div id="detailMetrics" class="detail-metrics"></div>
            <div id="detailChart" class="chart chart-combined"></div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>动作</th>
                            <th>日期</th>
                            <th>标的</th>
                            <th>触发值</th>
                            <th>实际回撤</th>
                            <th>价格</th>
                            <th>交易金额</th>
                            <th>股数</th>
                            <th>手续费</th>
                        </tr>
                    </thead>
                    <tbody id="detailTradeBody"></tbody>
                </table>
            </div>
            <div class="summary-title" style="margin-top: 16px;">
                <h2>期权叠加明细</h2>
                <span class="small">期权收益不并入主组合收益。</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>状态</th>
                            <th>期权代码</th>
                            <th>买入日</th>
                            <th>到期日</th>
                            <th>Strike</th>
                            <th>DTE</th>
                            <th>入场价</th>
                            <th>投入</th>
                            <th>合约数</th>
                            <th>当前/总价值</th>
                            <th>收益率</th>
                            <th>退出</th>
                        </tr>
                    </thead>
                    <tbody id="detailOptionBody"></tbody>
                </table>
            </div>
            </div>
        </div>

        <div id="trades" class="panel tab-hidden" data-tab-panel="results">
            <div class="tool-head">
                <h2>最近触发交易</h2>
                <span class="code">TRADES</span>
            </div>
            <div class="tool-body">
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>策略</th>
                            <th>动作</th>
                            <th>日期</th>
                            <th>标的</th>
                            <th>触发回撤</th>
                            <th>实际回撤</th>
                            <th>价格</th>
                            <th>交易金额</th>
                            <th>手续费</th>
                        </tr>
                    </thead>
                    <tbody id="tradeBody">
                        <tr><td colspan="9">尚未运行。</td></tr>
                    </tbody>
                </table>
            </div>
            </div>
        </div>
        </main>
    </div>
    <div id="scoreTooltip" class="score-tooltip" aria-hidden="true"></div>
    <div id="perfPanel" class="perf-panel" aria-live="polite"></div>

    <script>
        const defaultPortfolio = {{ default_portfolio|tojson }};
        const buyStrategyLabels = {{ buy_strategy_labels|tojson }};
        const sellStrategyLabels = {{ sell_strategy_labels|tojson }};
        const scorecardPortfolioLabels = {{ scorecard_portfolio_labels|tojson }};
        const scorecardPeriods = {{ scorecard_periods|tojson }};
        const strategyColors = ['#07689f', '#ff7e67', '#5aaeda', '#054d76', '#ff9a87', '#2e8fc4', '#a2d5f2', '#d95f4b', '#07547f', '#7fc2e8', '#c95442', '#2b769f'];
        const defaultSellStrategyKeys = Object.keys(sellStrategyLabels).filter((key) => key !== 'grid_rebound');
        let lastResult = null;
        let activeDetailIndex = null;
        let lastScorecard = null;
        let lastSellScan = null;
        let lastRobustLeaderboard = null;
        let activeScanStageSell = null;
        let activeScanScoreMode = {{ default_config.default_scan_score_mode|tojson }};
        let activeScanView = '2d';
        let activeTabName = 'setup';
        let lastLabSignature = null;
        let lastScorecardSignature = null;
        let scoreDetailContext = null;
        let runHistory = [];
        let experimentPresets = [];
        const urlParams = new URLSearchParams(window.location.search);
        const perfEnabled = urlParams.get('perf') === '1';
        const liteMode = urlParams.get('lite') === '1';
        const fullGlassMode = urlParams.get('glass') === '1';
        const perfToggles = {
            noBlur: liteMode || urlParams.get('noBlur') === '1',
            noLens: liteMode || urlParams.get('noLens') === '1',
            noSticky: liteMode || urlParams.get('noSticky') === '1',
            noMotion: liteMode || urlParams.get('noMotion') === '1',
            noFixedBg: liteMode || urlParams.get('noFixedBg') === '1',
            softShadow: liteMode || urlParams.get('softShadow') === '1'
        };
        const perfState = {
            fps: 0,
            frames: 0,
            lastFpsAt: performance.now(),
            longTasks: 0,
            longTaskMs: 0,
            apiLabMs: null,
            apiScoreMs: null,
            apiDetailMs: null,
            chartsMs: null,
            detailChartMs: null,
            scorecardRenderMs: null,
            summaryRenderMs: null,
            tradesRenderMs: null,
            loadMs: null
        };

        if (liteMode) {
            document.body.classList.add('lite-mode');
        } else if (fullGlassMode) {
            document.body.classList.add('full-glass-mode');
        }
        Object.entries(perfToggles).forEach(([key, enabled]) => {
            if (!enabled || liteMode) {
                return;
            }
            const className = key.replace(/[A-Z]/g, (char) => `-${char.toLowerCase()}`) + '-mode';
            document.body.classList.add(className);
        });

        function formatPerfMs(value) {
            return value === null || value === undefined ? '--' : `${Math.round(value)} ms`;
        }

        function formatPerfMemory() {
            if (!performance.memory) {
                return '--';
            }
            return `${Math.round(performance.memory.usedJSHeapSize / 1024 / 1024)} MB`;
        }

        function setPerfMetric(key, value) {
            if (!perfEnabled) {
                return;
            }
            perfState[key] = value;
            renderPerfPanel();
        }

        function renderPerfPanel() {
            if (!perfEnabled) {
                return;
            }
            const panel = document.getElementById('perfPanel');
            if (!panel) {
                return;
            }
            const nodeCount = document.getElementsByTagName('*').length;
            const activeToggles = Object.entries(perfToggles)
                .filter(([, enabled]) => enabled)
                .map(([key]) => key)
                .join(' ');
            const modeLabel = activeToggles || (fullGlassMode ? 'glass=1' : 'static glass');
            panel.innerHTML = `
                <strong>PERF <span>${modeLabel}</span></strong>
                <div class="perf-grid">
                    <div class="perf-item"><span>FPS</span><b>${Math.round(perfState.fps)}</b></div>
                    <div class="perf-item"><span>Long Task</span><b>${perfState.longTasks} / ${Math.round(perfState.longTaskMs)}ms</b></div>
                    <div class="perf-item"><span>API 演算</span><b>${formatPerfMs(perfState.apiLabMs)}</b></div>
                    <div class="perf-item"><span>API 评分</span><b>${formatPerfMs(perfState.apiScoreMs)}</b></div>
                    <div class="perf-item"><span>API 详情</span><b>${formatPerfMs(perfState.apiDetailMs)}</b></div>
                    <div class="perf-item"><span>图表渲染</span><b>${formatPerfMs(perfState.chartsMs)}</b></div>
                    <div class="perf-item"><span>详情图表</span><b>${formatPerfMs(perfState.detailChartMs)}</b></div>
                    <div class="perf-item"><span>评分矩阵</span><b>${formatPerfMs(perfState.scorecardRenderMs)}</b></div>
                    <div class="perf-item"><span>页面加载</span><b>${formatPerfMs(perfState.loadMs)}</b></div>
                    <div class="perf-item"><span>DOM 节点</span><b>${nodeCount}</b></div>
                    <div class="perf-item"><span>JS Heap</span><b>${formatPerfMemory()}</b></div>
                </div>
            `;
        }

        function initPerfPanel() {
            if (!perfEnabled) {
                return;
            }
            const panel = document.getElementById('perfPanel');
            if (!panel) {
                return;
            }
            panel.classList.add('show');
            if ('PerformanceObserver' in window) {
                try {
                    const observer = new PerformanceObserver((list) => {
                        list.getEntries().forEach((entry) => {
                            perfState.longTasks += 1;
                            perfState.longTaskMs += entry.duration;
                        });
                        renderPerfPanel();
                    });
                    observer.observe({ entryTypes: ['longtask'] });
                } catch (error) {
                    // Long Task is Chromium-only; ignore unsupported browsers.
                }
            }
            function tick(now) {
                perfState.frames += 1;
                const elapsed = now - perfState.lastFpsAt;
                if (elapsed >= 1000) {
                    perfState.fps = perfState.frames * 1000 / elapsed;
                    perfState.frames = 0;
                    perfState.lastFpsAt = now;
                    renderPerfPanel();
                }
                requestAnimationFrame(tick);
            }
            requestAnimationFrame(tick);
            window.addEventListener('load', () => {
                setPerfMetric('loadMs', performance.now());
            });
            setInterval(renderPerfPanel, 1200);
            renderPerfPanel();
        }

        function activateTab(tab) {
            activeTabName = tab;
            document.querySelectorAll('[data-tab-panel]').forEach((panel) => {
                panel.classList.toggle('tab-hidden', panel.dataset.tabPanel !== tab);
            });
            document.querySelectorAll('.workspace-nav [data-tab]').forEach((button) => {
                button.classList.toggle('active', button.dataset.tab === tab);
            });
            updateCommandBar();
            if (tab === 'results') {
                setTimeout(() => {
                    const ids = ['detailChart'];
                    ids.forEach((id) => {
                        const el = document.getElementById(id);
                        if (el && el.data) {
                            Plotly.Plots.resize(el);
                        }
                    });
                }, 0);
            }
            if (tab === 'scan' && activeScanView === '3d') {
                setTimeout(() => renderScan3d(), 0);
            }
        }

        function toggleOption() {
            const cb = document.getElementById('optionEnabled');
            const toggle = document.getElementById('optionToggle');
            cb.checked = !cb.checked;
            toggle.classList.toggle('on', cb.checked);
            updateCommandBar();
        }

        function initPortfolioRows(portfolio = defaultPortfolio) {
            const grid = document.getElementById('holdingGrid');
            const rows = Array.isArray(portfolio) && portfolio.length ? portfolio : defaultPortfolio;
            const totalWeight = rows.reduce((sum, item) => sum + Number(item.weight || 0), 0) || 1;
            const fallbackMaxDrawdown = readNumber('maxDrawdown') || 50;
            grid.innerHTML = rows.map((item, index) => {
                const pct = Math.round(Number(item.weight || 0) / totalWeight * 100);
                const maxDrawdown = Number(item.max_drawdown_pct ?? fallbackMaxDrawdown) || fallbackMaxDrawdown;
                return `
                <div class="holding" data-holding="${index}">
                    <div class="holding-head">
                        <span class="ticker" contenteditable="true" data-row="${index}" data-field="symbol" onblur="updateHoldingBar(${index})">${escapeHtml(item.symbol)}</span>
                        <span class="weight-editor">
                            <button class="weight-step" type="button" onclick="adjustHoldingWeight(${index}, -10)">-</button>
                            <span class="weight-value" contenteditable="true" data-row="${index}" data-field="weight" onblur="updateHoldingBar(${index})">${Number(item.weight || 0)}</span>
                            <button class="weight-step" type="button" onclick="adjustHoldingWeight(${index}, 10)">+</button>
                        </span>
                    </div>
                    <div class="holding-inputs">
                        <label class="holding-field">
                            名称
                            <span contenteditable="true" data-row="${index}" data-field="name">${escapeHtml(item.name || item.symbol || '')}</span>
                        </label>
                        <label class="holding-field">
                            评分回撤 %
                            <input type="number" min="1" max="100" step="0.5" data-row="${index}" data-field="max_drawdown_pct" value="${maxDrawdown}">
                        </label>
                    </div>
                    <div class="weight-bar"><span style="width:${pct}%"></span></div>
                </div>`;
            }).join('');
        }

        function adjustHoldingWeight(index, delta) {
            const grid = document.getElementById('holdingGrid');
            const weightEl = grid.querySelector(`[data-holding="${index}"] [data-field="weight"]`);
            if (!weightEl) {
                return;
            }
            const current = Number((weightEl.textContent || '').trim()) || 0;
            weightEl.textContent = String(Math.max(0, current + delta));
            updateHoldingBar(index);
        }

        function updateHoldingBar(index) {
            const grid = document.getElementById('holdingGrid');
            const allWeights = Array.from(grid.querySelectorAll('[data-field="weight"]')).map((el) => Number(el.textContent.trim()) || 0);
            const total = allWeights.reduce((sum, w) => sum + w, 0) || 1;
            allWeights.forEach((w, i) => {
                const bar = grid.querySelector(`[data-holding="${i}"] .weight-bar span`);
                if (bar) {
                    bar.style.width = `${Math.round(w / total * 100)}%`;
                }
            });
            updateCommandBar();
        }

        function readPortfolio() {
            const grid = document.getElementById('holdingGrid');
            const rows = [];
            grid.querySelectorAll('[data-holding]').forEach((card) => {
                const symbol = (card.querySelector('[data-field="symbol"]').textContent || '').trim().toUpperCase();
                const name = (card.querySelector('[data-field="name"]').textContent || '').trim();
                const weight = Number((card.querySelector('[data-field="weight"]').textContent || '').trim()) || 0;
                const maxDrawdownEl = card.querySelector('[data-field="max_drawdown_pct"]');
                const maxDrawdown = maxDrawdownEl ? Number(maxDrawdownEl.value || 0) : 0;
                if (symbol && weight > 0) {
                    const row = { symbol, name, weight };
                    if (maxDrawdown > 0) {
                        row.max_drawdown_pct = maxDrawdown;
                    }
                    rows.push(row);
                }
            });
            return rows;
        }

        function readNumber(id) {
            return Number(document.getElementById(id).value || 0);
        }

        function selectedStrategies(id, labels) {
            const value = document.getElementById(id).value;
            return value === 'all' ? Object.keys(labels) : [value];
        }

        function selectedSellStrategies() {
            const value = document.getElementById('sellStrategy').value;
            return value === 'all' ? defaultSellStrategyKeys : [value];
        }

        function selectedScorecardPortfolios() {
            return Array.from(document.querySelectorAll('input[name="scoreTopic"]:checked'))
                .map((input) => input.value);
        }

        function scorecardPeriodPayload() {
            return scorecardPeriods.map((period) => {
                const labelEl = document.querySelector(`[data-score-period="${period.key}"][data-period-field="label"]`);
                const startEl = document.querySelector(`[data-score-period="${period.key}"][data-period-field="start"]`);
                const endEl = document.querySelector(`[data-score-period="${period.key}"][data-period-field="end"]`);
                return {
                    key: period.key,
                    label: labelEl && labelEl.value.trim() ? labelEl.value.trim() : period.label,
                    start: startEl ? startEl.value : '',
                    end: endEl ? endEl.value : ''
                };
            });
        }

        function updateScorecardQuestionHint() {
            const topicCount = selectedScorecardPortfolios().length;
            const periodCount = scorecardPeriods.length;
            document.getElementById('scorecardRange').textContent = `${topicCount * periodCount} 个题目：${topicCount} 个标的/组合 × ${periodCount} 档时间。未填写日期时使用默认 252 / 756 / 1260 个交易日。`;
            updateCommandBar();
        }

        function buildLabPayload() {
            return {
                start: document.getElementById('start').value,
                end: document.getElementById('end').value,
                initial_cash: readNumber('initialCash'),
                monthly_contribution: readNumber('monthlyContribution'),
                max_drawdown_pct: readNumber('maxDrawdown'),
                drawdown_basis: document.getElementById('drawdownBasis').value,
                step_pct: readNumber('stepPct'),
                equal_slice_allocation_pct: readNumber('equalSliceAllocation'),
                trade_fee: readNumber('tradeFee'),
                hkd_to_usd: readNumber('hkdToUsd'),
                reserve_position_pct: readNumber('reservePosition'),
                sell_min_profit_pct: readNumber('sellMinProfit'),
                repair_sell_cooldown_days: readNumber('repairSellCooldown'),
                repair_stage_sell_pct: readNumber('repairStageSellPct'),
                option_overlay: {
                    enabled: document.getElementById('optionEnabled').checked,
                    allocation_pct: readNumber('optionAllocation'),
                    target_dte: readNumber('optionTargetDte'),
                    min_dte: readNumber('optionMinDte'),
                    max_dte: readNumber('optionMaxDte'),
                    moneyness: document.getElementById('optionMoneyness').value,
                    profit_take_pct: readNumber('optionProfitTake'),
                    profit_take_sell_pct: readNumber('optionProfitSell'),
                    exit_dte: readNumber('optionExitDte'),
                    trade_fee: readNumber('optionTradeFee'),
                    max_trades_per_strategy: readNumber('optionMaxTrades')
                },
                buy_strategies: selectedStrategies('buyStrategy', buyStrategyLabels),
                sell_strategies: selectedSellStrategies(),
                targets: readPortfolio()
            };
        }

        function currentExperimentPayload() {
            return {
                ...buildLabPayload(),
                return_weight: readNumber('scoreReturnWeight') / 100,
                drawdown_weight: readNumber('scoreDrawdownWeight') / 100,
                scorecard_portfolio_keys: selectedScorecardPortfolios(),
                scorecard_periods: scorecardPeriodPayload(),
                buy_strategy: document.getElementById('scanBuyStrategy').value,
                trading_days: Number(document.getElementById('scanPeriod').value || 1260),
                sell_min_profit_values: parseScanValues('scanSellMinProfits', false, 100),
                repair_sell_cooldown_values: parseScanValues('scanCooldowns', true),
                repair_stage_sell_values: parseScanValues('scanStageSells', false, 100),
                scan_score_mode: document.getElementById('scanScoreMode').value
            };
        }

        function stableSignature(value) {
            return JSON.stringify(value);
        }

        function strategyLabelFromSelect(selectId, labels, allLabel) {
            const value = document.getElementById(selectId).value;
            if (value === 'all') {
                return allLabel;
            }
            return labels[value] || value;
        }

        function currentPortfolioSummary() {
            const targets = readPortfolio();
            const totalWeight = targets.reduce((sum, item) => sum + Number(item.weight || 0), 0);
            return `${targets.length} 标的 / ${number(totalWeight)} 权重`;
        }

        function setFreshness(message, state) {
            const node = document.getElementById('freshnessState');
            if (!node) {
                return;
            }
            node.className = `freshness-state ${state || 'idle'}`;
            node.textContent = message;
        }

        function updateCommandBar() {
            const summary = document.getElementById('commandSummary');
            if (!summary || !document.getElementById('holdingGrid')) {
                return;
            }
            const modeLabels = {
                setup: '实验配置',
                results: '组合演算',
                scorecard: '策略评分',
                scan: '参数扫描',
                history: '运行历史'
            };
            document.getElementById('workspaceMode').textContent = modeLabels[activeTabName] || '策略实验室';
            const topicCount = selectedScorecardPortfolios().length;
            const periodCount = scorecardPeriods.length;
            const chips = [
                ['时间', `${document.getElementById('start').value || '--'} 至 ${document.getElementById('end').value || '--'}`],
                ['组合', currentPortfolioSummary()],
                ['买入', strategyLabelFromSelect('buyStrategy', buyStrategyLabels, '全部买入策略')],
                ['卖出', strategyLabelFromSelect('sellStrategy', sellStrategyLabels, '默认卖出策略组')],
                ['评分', `${topicCount * periodCount} 题`],
                ['期权', document.getElementById('optionEnabled').checked ? '启用' : '关闭']
            ];
            summary.innerHTML = chips.map(([label, value]) => (
                `<div class="command-chip"><span>${escapeHtml(label)}</span>${escapeHtml(value)}</div>`
            )).join('');

            if (activeTabName === 'results') {
                if (!lastResult) {
                    setFreshness('尚未运行演算。', 'idle');
                } else if (scoreDetailContext) {
                    setFreshness('当前演算页来自评分详情，返回评分可继续横向比较。', 'synced');
                } else if (lastLabSignature === stableSignature(buildLabPayload())) {
                    setFreshness('演算结果匹配当前参数。', 'synced');
                } else {
                    setFreshness('参数已变更，当前演算结果可能不是最新。', 'stale');
                }
                return;
            }
            if (activeTabName === 'scorecard') {
                if (!lastScorecard) {
                    setFreshness('尚未运行评分。', 'idle');
                } else if (lastScorecardSignature === stableSignature(scorecardPayload())) {
                    setFreshness('评分结果匹配当前参数。', 'synced');
                } else {
                    setFreshness('参数已变更，当前评分结果可能不是最新。', 'stale');
                }
                return;
            }
            if (activeTabName === 'scan') {
                if (!lastSellScan) {
                    setFreshness('尚未运行参数扫描。', 'idle');
                } else {
                    setFreshness('扫描结果来自当前页面会话；点击热力格可回填卖出参数。', 'synced');
                }
                return;
            }
            if (activeTabName === 'history') {
                setFreshness(runHistory.length ? `服务端已有 ${runHistory.length} 条运行记录。` : '服务端尚无运行记录。', 'idle');
                return;
            }
            setFreshness('修改参数后，可在任意工作区直接运行演算或评分。', 'idle');
        }

        function scheduleCommandBarUpdate() {
            window.requestAnimationFrame(updateCommandBar);
        }

        function initCommandBarWatchers() {
            const container = document.querySelector('.container');
            if (!container) {
                return;
            }
            container.addEventListener('input', scheduleCommandBarUpdate);
            container.addEventListener('change', scheduleCommandBarUpdate);
            container.addEventListener('blur', scheduleCommandBarUpdate, true);
        }

        function experimentSummaryText() {
            return [
                `${document.getElementById('start').value || '--'} 至 ${document.getElementById('end').value || '--'}`,
                currentPortfolioSummary(),
                `买入 ${strategyLabelFromSelect('buyStrategy', buyStrategyLabels, '全部买入策略')}`,
                `卖出 ${strategyLabelFromSelect('sellStrategy', sellStrategyLabels, '默认卖出策略组')}`
            ].join(' · ');
        }

        function addRunHistory() {
            loadRunHistory();
        }

        function runHistoryTitle(item) {
            const summary = item.result_summary || {};
            if (item.kind === 'run') {
                return `${item.kind_label || '组合演算'} · ${summary.strategy_count || 0} 个组合`;
            }
            if (item.kind === 'score') {
                return `${item.kind_label || '策略评分'} · ${summary.strategy_count || 0} 个组合 / ${summary.question_count || 0} 题`;
            }
            if (item.kind === 'scan') {
                return `${item.kind_label || '参数扫描'} · ${summary.cell_count || 0} 个参数`;
            }
            if (item.kind === 'robust') {
                return `${item.kind_label || '稳健 Top10'} · ${summary.leaderboard_count || 0} 个结果 / ${summary.task_count || 0} 题`;
            }
            return item.kind_label || '运行记录';
        }

        function runHistorySummary(item) {
            const summary = item.result_summary || {};
            const config = item.config_summary || {};
            const warnings = Array.isArray(summary.warnings) && summary.warnings.length ? `；告警 ${summary.warnings.length} 条` : '';
            if (item.kind === 'run') {
                return `${summary.best_label || '最佳组合 --'} 收益 ${pctCompact(summary.best_return_pct)}；最大回撤 ${pctCompact(summary.worst_drawdown_pct)}${warnings}`;
            }
            if (item.kind === 'score') {
                return `${summary.top_label || '最高分 --'} 分数 ${number(summary.top_score)}；均收 ${pctCompact(summary.top_return_pct)} / 均回撤 ${pctCompact(summary.top_drawdown_pct)}${warnings}`;
            }
            if (item.kind === 'scan') {
                return `${summary.buy_strategy_label || config.buy_strategy || '扫描'}；最佳 ${pctCompact(summary.best_sell_min_profit_pct)} 盈利 / ${number(summary.best_repair_sell_cooldown_days)} 日冷却 / ${pctCompact(summary.best_repair_stage_sell_pct)} 单档${warnings}`;
            }
            if (item.kind === 'robust') {
                return `${summary.top_label || '最高稳健分 --'} 分数 ${number(summary.top_robust_score)}；均收 ${pctCompact(summary.top_return_pct)} / 均回撤 ${pctCompact(summary.top_drawdown_pct)}${warnings}`;
            }
            return warnings ? warnings.replace(/^；/, '') : '运行摘要';
        }

        function runHistoryContext(item) {
            const config = item.config_summary || {};
            const range = `${config.start || '--'} 至 ${config.end || '--'}`;
            const portfolio = `${config.target_count || 0} 标的 / ${number(config.target_weight)} 权重`;
            const buy = config.buy_strategy ? `买入 ${config.buy_strategy}` : '';
            const sell = config.sell_strategy ? `卖出 ${config.sell_strategy}` : '';
            return [range, portfolio, buy, sell].filter(Boolean).join(' · ');
        }

        function formatRunTime(value) {
            if (!value) {
                return '--';
            }
            const date = new Date(value);
            return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
        }

        async function loadExperimentPresets() {
            const list = document.getElementById('presetList');
            if (!list) {
                return;
            }
            list.innerHTML = '<div class="history-empty">正在读取参数预设...</div>';
            try {
                const response = await fetch('/api/strategy-lab/presets?limit=50');
                const payload = await response.json();
                if (!response.ok || !payload.success) {
                    throw new Error(payload.message || '读取参数预设失败');
                }
                experimentPresets = payload.presets || [];
                renderExperimentPresets();
            } catch (error) {
                list.innerHTML = `<div class="history-empty">参数预设读取失败：${escapeHtml(error.message || error)}</div>`;
            }
        }

        async function refreshHistoryWorkspace() {
            await Promise.all([
                loadExperimentPresets(),
                loadRunHistory()
            ]);
        }

        function renderExperimentPresets() {
            const list = document.getElementById('presetList');
            if (!list) {
                return;
            }
            if (!experimentPresets.length) {
                list.innerHTML = '<div class="history-empty">尚无参数预设。可以把当前配置保存为预设，后续直接恢复。</div>';
                return;
            }
            list.innerHTML = experimentPresets.map((item) => `
                <div class="history-item">
                    <div>
                        <strong>${escapeHtml(item.name || '未命名预设')}</strong>
                        <span>${escapeHtml(runHistoryContext(item))}</span>
                    </div>
                    <div class="history-meta">
                        <small>${escapeHtml(formatRunTime(item.updated_at || item.created_at))}</small>
                        <div class="history-actions">
                            <button class="btn btn-secondary btn-small" type="button" onclick="restorePresetConfig('${escapeHtml(item.id)}')">恢复参数</button>
                            <button class="btn btn-secondary btn-small" type="button" onclick="deletePreset('${escapeHtml(item.id)}')">删除</button>
                        </div>
                    </div>
                </div>
            `).join('');
        }

        async function saveCurrentPreset() {
            const nameEl = document.getElementById('presetName');
            const name = nameEl ? nameEl.value.trim() : '';
            if (!name) {
                setStatus('error', '请先填写预设名称。');
                return;
            }
            try {
                const response = await fetch('/api/strategy-lab/presets', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, payload: currentExperimentPayload() })
                });
                const payload = await response.json();
                if (!response.ok || !payload.success) {
                    throw new Error(payload.message || '保存参数预设失败');
                }
                if (nameEl) {
                    nameEl.value = '';
                }
                await loadExperimentPresets();
                setStatus('success', '参数预设已保存。');
            } catch (error) {
                setStatus('error', `保存预设失败: ${error.message || error}`);
            }
        }

        async function restorePresetConfig(presetId) {
            try {
                const response = await fetch(`/api/strategy-lab/presets/${encodeURIComponent(presetId)}`);
                const payload = await response.json();
                if (!response.ok || !payload.success) {
                    throw new Error(payload.message || '读取参数预设失败');
                }
                applyRunConfigPayload(payload.preset.config_payload || {});
                activateTab('setup');
                setStatus('success', '已从参数预设恢复配置。');
            } catch (error) {
                setStatus('error', `恢复预设失败: ${error.message || error}`);
            }
        }

        async function deletePreset(presetId) {
            if (!window.confirm('删除这个参数预设？')) {
                return;
            }
            try {
                const response = await fetch(`/api/strategy-lab/presets/${encodeURIComponent(presetId)}`, { method: 'DELETE' });
                const payload = await response.json();
                if (!response.ok || !payload.success) {
                    throw new Error(payload.message || '删除参数预设失败');
                }
                await loadExperimentPresets();
                setStatus('success', '参数预设已删除。');
            } catch (error) {
                setStatus('error', `删除预设失败: ${error.message || error}`);
            }
        }

        async function loadRunHistory() {
            const list = document.getElementById('runHistoryList');
            if (!list) {
                return;
            }
            list.innerHTML = '<div class="history-empty">正在读取服务端运行历史...</div>';
            try {
                const response = await fetch('/api/strategy-lab/runs?limit=50');
                const payload = await response.json();
                if (!response.ok || !payload.success) {
                    throw new Error(payload.message || '读取运行历史失败');
                }
                runHistory = payload.runs || [];
                renderRunHistory();
                updateCommandBar();
            } catch (error) {
                list.innerHTML = `<div class="history-empty">运行历史读取失败：${escapeHtml(error.message || error)}</div>`;
            }
        }

        function renderRunHistory() {
            const list = document.getElementById('runHistoryList');
            if (!list) {
                return;
            }
            if (!runHistory.length) {
                list.innerHTML = '<div class="history-empty">尚无运行记录。完成演算、评分或参数扫描后，这里会保存服务端摘要。</div>';
                return;
            }
            list.innerHTML = runHistory.map((item) => `
                <div class="history-item">
                    <div>
                        <strong>${escapeHtml(runHistoryTitle(item))}</strong>
                        <span>${escapeHtml(runHistorySummary(item))}</span>
                        <span>${escapeHtml(runHistoryContext(item))}</span>
                    </div>
                    <div class="history-meta">
                        <small>${escapeHtml(formatRunTime(item.created_at))}</small>
                        <div class="history-actions">
                            <button class="btn btn-secondary btn-small" type="button" onclick="restoreRunConfig('${escapeHtml(item.id)}')">恢复参数</button>
                            <button class="btn btn-secondary btn-small" type="button" onclick="deleteRunHistory('${escapeHtml(item.id)}')">删除</button>
                        </div>
                    </div>
                </div>
            `).join('');
        }

        function setFieldValue(id, value) {
            if (value === undefined || value === null) {
                return;
            }
            const element = document.getElementById(id);
            if (element) {
                element.value = value;
            }
        }

        function setSelectValue(id, value) {
            if (value === undefined || value === null) {
                return;
            }
            const element = document.getElementById(id);
            if (element && Array.from(element.options).some((option) => option.value === String(value))) {
                element.value = String(value);
            }
        }

        function strategySelectorFromPayload(values, allKeys, allValue = 'all') {
            if (!Array.isArray(values) || !values.length) {
                return null;
            }
            if (values.length === 1) {
                return values[0];
            }
            const sortedValues = [...values].map(String).sort().join('|');
            const sortedKeys = [...allKeys].map(String).sort().join('|');
            return sortedValues === sortedKeys || values.length > 1 ? allValue : null;
        }

        function setOptionEnabled(enabled) {
            const checkbox = document.getElementById('optionEnabled');
            const toggle = document.getElementById('optionToggle');
            if (!checkbox || !toggle || enabled === undefined || enabled === null) {
                return;
            }
            checkbox.checked = Boolean(enabled);
            toggle.classList.toggle('on', checkbox.checked);
        }

        function applyScorecardPeriods(periods) {
            if (!Array.isArray(periods)) {
                return;
            }
            periods.forEach((period) => {
                if (!period || !period.key) {
                    return;
                }
                ['label', 'start', 'end'].forEach((field) => {
                    const element = document.querySelector(`[data-score-period="${period.key}"][data-period-field="${field}"]`);
                    if (element && period[field] !== undefined && period[field] !== null) {
                        element.value = period[field] || '';
                    }
                });
            });
        }

        function applyScanValues(id, values) {
            if (Array.isArray(values) && values.length) {
                setFieldValue(id, values.join(','));
            }
        }

        function applyRunConfigPayload(payload) {
            setFieldValue('start', payload.start);
            setFieldValue('end', payload.end);
            setFieldValue('initialCash', payload.initial_cash);
            setFieldValue('monthlyContribution', payload.monthly_contribution);
            setFieldValue('maxDrawdown', payload.max_drawdown_pct);
            setSelectValue('drawdownBasis', payload.drawdown_basis);
            setFieldValue('stepPct', payload.step_pct);
            setFieldValue('equalSliceAllocation', payload.equal_slice_allocation_pct);
            setFieldValue('tradeFee', payload.trade_fee);
            setFieldValue('hkdToUsd', payload.hkd_to_usd);
            setFieldValue('reservePosition', payload.reserve_position_pct);
            setFieldValue('sellMinProfit', payload.sell_min_profit_pct);
            setFieldValue('repairSellCooldown', payload.repair_sell_cooldown_days);
            setFieldValue('repairStageSellPct', payload.repair_stage_sell_pct);

            const buySelector = strategySelectorFromPayload(payload.buy_strategies, Object.keys(buyStrategyLabels));
            setSelectValue('buyStrategy', buySelector);
            const sellSelector = strategySelectorFromPayload(payload.sell_strategies, defaultSellStrategyKeys);
            setSelectValue('sellStrategy', sellSelector);

            if (Array.isArray(payload.targets) && payload.targets.length) {
                initPortfolioRows(payload.targets);
                updateHoldingBar();
            }

            const option = payload.option_overlay || {};
            setOptionEnabled(option.enabled);
            setFieldValue('optionAllocation', option.allocation_pct);
            setFieldValue('optionTargetDte', option.target_dte);
            setFieldValue('optionMinDte', option.min_dte);
            setFieldValue('optionMaxDte', option.max_dte);
            setSelectValue('optionMoneyness', option.moneyness);
            setFieldValue('optionProfitTake', option.profit_take_pct);
            setFieldValue('optionProfitSell', option.profit_take_sell_pct);
            setFieldValue('optionExitDte', option.exit_dte);
            setFieldValue('optionTradeFee', option.trade_fee);
            setFieldValue('optionMaxTrades', option.max_trades_per_strategy);

            if (payload.return_weight !== undefined) {
                setFieldValue('scoreReturnWeight', Number(payload.return_weight || 0) * 100);
            }
            if (payload.drawdown_weight !== undefined) {
                setFieldValue('scoreDrawdownWeight', Number(payload.drawdown_weight || 0) * 100);
            }
            if (Array.isArray(payload.scorecard_portfolio_keys)) {
                document.querySelectorAll('input[name="scoreTopic"]').forEach((input) => {
                    input.checked = payload.scorecard_portfolio_keys.includes(input.value);
                });
            }
            applyScorecardPeriods(payload.scorecard_periods);

            setSelectValue('scanBuyStrategy', payload.buy_strategy);
            setFieldValue('scanPeriod', payload.trading_days);
            applyScanValues('scanSellMinProfits', payload.sell_min_profit_values);
            applyScanValues('scanCooldowns', payload.repair_sell_cooldown_values);
            applyScanValues('scanStageSells', payload.repair_stage_sell_values);
            setSelectValue('scanScoreMode', payload.scan_score_mode);

            updateScorecardQuestionHint();
            updateCommandBar();
        }

        async function restoreRunConfig(runId) {
            try {
                const response = await fetch(`/api/strategy-lab/runs/${encodeURIComponent(runId)}`);
                const payload = await response.json();
                if (!response.ok || !payload.success) {
                    throw new Error(payload.message || '读取运行记录失败');
                }
                applyRunConfigPayload(payload.run.config_payload || {});
                activateTab('setup');
                setStatus('success', '已从运行历史恢复参数，可直接重新演算、评分或扫描。');
            } catch (error) {
                setStatus('error', `恢复参数失败: ${error.message || error}`);
            }
        }

        async function deleteRunHistory(runId) {
            if (!window.confirm('删除这条运行历史？')) {
                return;
            }
            try {
                const response = await fetch(`/api/strategy-lab/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' });
                const payload = await response.json();
                if (!response.ok || !payload.success) {
                    throw new Error(payload.message || '删除运行记录失败');
                }
                await loadRunHistory();
                setStatus('success', '运行历史已删除。');
            } catch (error) {
                setStatus('error', `删除失败: ${error.message || error}`);
            }
        }

        function parseScanValues(id, integerOnly = false, maximum = null) {
            const raw = document.getElementById(id).value || '';
            const values = raw
                .split(/[,，\\s]+/)
                .map((item) => item.trim())
                .filter(Boolean)
                .map((item) => integerOnly ? Math.round(Number(item)) : Number(item));
            if (!values.length || values.some((value) => !Number.isFinite(value) || value < 0)) {
                throw new Error('扫描参数需要填写非负数字，用逗号分隔。');
            }
            if (maximum !== null && values.some((value) => value > maximum)) {
                throw new Error(`扫描参数必须在 0 到 ${maximum} 之间。`);
            }
            return [...new Set(values)].sort((a, b) => a - b);
        }

        function setStatus(type, message) {
            const status = document.getElementById('status');
            status.className = `status ${type}`;
            status.textContent = message;
        }

        function updateJobPanel(job, fallbackTitle = '后台任务') {
            const panel = document.getElementById('jobPanel');
            if (!panel || !job) {
                return;
            }
            const titleByKind = {
                run: '组合演算',
                score: '策略评分',
                scan: '参数扫描',
                robust: '稳健 Top10'
            };
            panel.classList.add('show');
            document.getElementById('jobTitle').textContent = titleByKind[job.kind] || fallbackTitle;
            document.getElementById('jobStage').textContent = `${String(job.status || '').toUpperCase()} / ${String(job.stage || '').toUpperCase()}`;
            document.getElementById('jobProgress').style.width = `${Math.max(0, Math.min(100, Number(job.progress || 0)))}%`;
            document.getElementById('jobMessage').textContent = job.message || fallbackTitle;
        }

        function hideJobPanelLater() {
            window.setTimeout(() => {
                const panel = document.getElementById('jobPanel');
                if (panel) {
                    panel.classList.remove('show');
                }
            }, 1800);
        }

        async function runStrategyJob(kind, payload, options = {}) {
            const apiStart = performance.now();
            const createResponse = await fetch('/api/strategy-lab/jobs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ kind, payload })
            });
            const created = await createResponse.json();
            if (!createResponse.ok || !created.success) {
                throw new Error(created.message || '创建后台任务失败');
            }
            let job = created.job;
            updateJobPanel(job, options.title);
            const pollDelay = options.pollDelay || 900;
            for (;;) {
                if (job.status === 'succeeded') {
                    setPerfMetric(options.perfKey || 'apiScoreMs', performance.now() - apiStart);
                    hideJobPanelLater();
                    return job.data;
                }
                if (job.status === 'failed') {
                    hideJobPanelLater();
                    throw new Error(job.error || job.message || '后台任务失败');
                }
                await new Promise((resolve) => window.setTimeout(resolve, pollDelay));
                const statusResponse = await fetch(`/api/strategy-lab/jobs/${encodeURIComponent(job.id)}`);
                const statusPayload = await statusResponse.json();
                if (!statusResponse.ok || !statusPayload.success) {
                    throw new Error(statusPayload.message || '读取后台任务失败');
                }
                job = statusPayload.job;
                updateJobPanel(job, options.title);
            }
        }

        function formatDateInput(date) {
            const year = date.getFullYear();
            const month = String(date.getMonth() + 1).padStart(2, '0');
            const day = String(date.getDate()).padStart(2, '0');
            return `${year}-${month}-${day}`;
        }

        function applyRangePreset(preset) {
            const end = new Date();
            const start = new Date(end);
            if (preset === '1y') {
                start.setFullYear(start.getFullYear() - 1);
            } else if (preset === '3y') {
                start.setFullYear(start.getFullYear() - 3);
            } else if (preset === '5y') {
                start.setFullYear(start.getFullYear() - 5);
            }
            document.getElementById('start').value = formatDateInput(start);
            document.getElementById('end').value = formatDateInput(end);
            updateCommandBar();
        }

        function money(value) {
            return Number(value || 0).toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });
        }

        function pct(value) {
            return `${Number(value || 0).toFixed(2)}%`;
        }

        function pctCompact(value) {
            return `${Number(value || 0).toFixed(1)}%`;
        }

        function number(value) {
            return Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
        }

        function scoreNumber(value) {
            return Number(value || 0).toFixed(1);
        }

        function scanCellStyle(value, minValue, maxValue) {
            const min = Number(minValue || 0);
            const max = Number(maxValue || 0);
            const ratio = Math.max(0, Math.min(1, max === min ? 1 : (Number(value || 0) - min) / (max - min)));
            const stops = [
                { at: 0.00, fill: [255, 184, 176], alpha: 0.92 },
                { at: 0.42, fill: [255, 231, 184], alpha: 0.88 },
                { at: 0.72, fill: [204, 240, 222], alpha: 0.92 },
                { at: 1.00, fill: [143, 218, 188], alpha: 0.98 }
            ];
            let start = stops[0];
            let end = stops[stops.length - 1];
            for (let i = 0; i < stops.length - 1; i += 1) {
                if (ratio >= stops[i].at && ratio <= stops[i + 1].at) {
                    start = stops[i];
                    end = stops[i + 1];
                    break;
                }
            }
            const local = start.at === end.at ? 0 : (ratio - start.at) / (end.at - start.at);
            const mix = (a, b) => Math.round(a + (b - a) * local);
            const alpha = start.alpha + (end.alpha - start.alpha) * local;
            return `--scan-fill: rgba(${mix(start.fill[0], end.fill[0])}, ${mix(start.fill[1], end.fill[1])}, ${mix(start.fill[2], end.fill[2])}, ${alpha.toFixed(3)});`;
        }

        function normalizedScore(value, values) {
            const parsed = values.map((item) => Number(item || 0));
            const min = Math.min(...parsed);
            const max = Math.max(...parsed);
            if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) {
                return 100;
            }
            return (Number(value || 0) - min) / (max - min) * 100;
        }

        function scanScoreWeights() {
            const returnWeight = Math.max(0, readNumber('scoreReturnWeight'));
            const drawdownWeight = Math.max(0, readNumber('scoreDrawdownWeight'));
            const total = returnWeight + drawdownWeight;
            if (total <= 0) {
                return { return: 0.9, drawdown: 0.1 };
            }
            return { return: returnWeight / total, drawdown: drawdownWeight / total };
        }

        function scanReturnDrawdownScore(cell, cells) {
            const weights = scanScoreWeights();
            const returnScore = normalizedScore(cell.return_pct, cells.map((item) => item.return_pct));
            const drawdownScore = normalizedScore(cell.max_drawdown_pct, cells.map((item) => item.max_drawdown_pct));
            return returnScore * weights.return + drawdownScore * weights.drawdown;
        }

        function scanDisplayScore(cell, cells) {
            const returnDrawdownScore = scanReturnDrawdownScore(cell, cells);
            if (activeScanScoreMode === 'return_drawdown') {
                return returnDrawdownScore;
            }
            const returnScore = normalizedScore(cell.return_pct, cells.map((item) => item.return_pct));
            const drawdownScore = normalizedScore(cell.max_drawdown_pct, cells.map((item) => item.max_drawdown_pct));
            return (
                returnScore * 0.45
                + drawdownScore * 0.25
                + Number(cell.sell_quality_score || 0) * 0.20
                + Number(cell.cash_reuse_pct || 0) * 0.10
            );
        }

        function scanScoreLabel() {
            return activeScanScoreMode === 'return_drawdown' ? '收益&回撤分' : '综合参数评分';
        }

        function updateScanLegend() {
            const legend = document.getElementById('scanLegendText');
            if (!legend) {
                return;
            }
            const scoreLabel = scanScoreLabel();
            legend.textContent = activeScanView === '3d'
                ? `颜色按${scoreLabel}从低到高；绿点是最佳参数，蓝点是当前参数。`
                : `颜色按${scoreLabel}从低到高；蓝框是当前参数，绿框是本次扫描最佳。`;
        }

        function scanBestCell(cells) {
            return [...cells].sort((a, b) => {
                const scoreDiff = scanDisplayScore(b, cells) - scanDisplayScore(a, cells);
                if (Math.abs(scoreDiff) > 1e-9) {
                    return scoreDiff;
                }
                return Number(b.return_pct || 0) - Number(a.return_pct || 0);
            })[0] || null;
        }

        function downsampleSeries(xValues, yValues, maxPoints = 720) {
            const length = Math.min((xValues || []).length, (yValues || []).length);
            if (length <= maxPoints) {
                return {
                    x: (xValues || []).slice(0, length),
                    y: (yValues || []).slice(0, length)
                };
            }
            const x = [];
            const y = [];
            const step = (length - 1) / (maxPoints - 1);
            let previousIndex = -1;
            for (let i = 0; i < maxPoints; i += 1) {
                const index = Math.min(length - 1, Math.round(i * step));
                if (index === previousIndex) {
                    continue;
                }
                x.push(xValues[index]);
                y.push(yValues[index]);
                previousIndex = index;
            }
            return { x, y };
        }

        function escapeHtml(value) {
            return String(value ?? '').replace(/[&<>"']/g, (char) => ({
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#39;'
            }[char]));
        }

        function lotHoverText(trade) {
            if (trade.lot_buy_price_usd === null || trade.lot_buy_price_usd === undefined) {
                return '';
            }
            return `<br>对应买入价: ${number(trade.lot_buy_price_usd)}<br>对应买入回撤: ${pct(trade.lot_buy_drawdown_pct)}`;
        }

        function clamp(value, min, max) {
            return Math.max(min, Math.min(max, value));
        }

        function scoreDistanceStyle(strategy, cells) {
            const returnValues = cells.map((item) => Number(item.return_pct || 0));
            const drawdownValues = cells.map((item) => Number(item.max_drawdown_pct || 0));
            const bestReturn = Math.max(...returnValues);
            const bestDrawdown = Math.min(...drawdownValues);
            const returnGap = Math.max(0, bestReturn - Number(strategy.return_pct || 0));
            const drawdownGap = Math.max(0, Number(strategy.max_drawdown_pct || 0) - bestDrawdown);
            const returnRatio = clamp(returnGap / 18, 0, 1);
            const drawdownRatio = clamp(drawdownGap / 8, 0, 1);
            const ratio = clamp(returnRatio * 0.58 + drawdownRatio * 0.42, 0, 1);
            const stops = [
                { at: 0.00, fill: [170, 230, 205], alpha: 0.96 },
                { at: 0.28, fill: [214, 241, 226], alpha: 0.92 },
                { at: 0.55, fill: [244, 248, 233], alpha: 0.86 },
                { at: 0.78, fill: [255, 235, 194], alpha: 0.88 },
                { at: 1.00, fill: [255, 199, 194], alpha: 0.94 }
            ];
            let start = stops[0];
            let end = stops[stops.length - 1];
            for (let i = 0; i < stops.length - 1; i += 1) {
                if (ratio >= stops[i].at && ratio <= stops[i + 1].at) {
                    start = stops[i];
                    end = stops[i + 1];
                    break;
                }
            }
            const local = start.at === end.at ? 0 : (ratio - start.at) / (end.at - start.at);
            const mix = (a, b) => Math.round(a + (b - a) * local);
            const fill = start.fill.map((value, index) => mix(value, end.fill[index]));
            const alpha = start.alpha + (end.alpha - start.alpha) * local;
            return {
                style: `--score-fill: rgba(${fill.join(',')}, ${alpha.toFixed(3)});`,
                returnGap,
                drawdownGap,
                bestReturn,
                bestDrawdown
            };
        }

        function showScoreTooltip(trigger, event) {
            const tooltip = document.getElementById('scoreTooltip');
            const raw = trigger.dataset.tooltip || '';
            if (!tooltip || !raw) {
                return;
            }
            const lines = raw.split('\\n');
            const title = lines.slice(0, 2).join(' · ');
            const body = lines.slice(2).join('\\n');
            tooltip.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(body)}</span>`;
            tooltip.classList.add('show');
            tooltip.setAttribute('aria-hidden', 'false');
            positionScoreTooltip(event);
        }

        function positionScoreTooltip(event) {
            const tooltip = document.getElementById('scoreTooltip');
            if (!tooltip || !tooltip.classList.contains('show')) {
                return;
            }
            const gap = 14;
            const rect = tooltip.getBoundingClientRect();
            let left = event.clientX + gap;
            let top = event.clientY + gap;
            if (left + rect.width > window.innerWidth - 10) {
                left = event.clientX - rect.width - gap;
            }
            if (top + rect.height > window.innerHeight - 10) {
                top = event.clientY - rect.height - gap;
            }
            tooltip.style.left = `${Math.max(10, left)}px`;
            tooltip.style.top = `${Math.max(10, top)}px`;
        }

        function hideScoreTooltip() {
            const tooltip = document.getElementById('scoreTooltip');
            if (!tooltip) {
                return;
            }
            tooltip.classList.remove('show');
            tooltip.setAttribute('aria-hidden', 'true');
        }

        function initScoreTooltip() {
            const containers = [
                document.getElementById('scoreMatrixBody'),
                document.getElementById('robustBoard')
            ].filter(Boolean);
            if (!containers.length) {
                return;
            }
            containers.forEach((container) => {
                container.addEventListener('mouseover', (event) => {
                    const trigger = event.target.closest('.score-info-btn');
                    if (trigger && container.contains(trigger)) {
                        showScoreTooltip(trigger, event);
                    }
                });
                container.addEventListener('mouseout', (event) => {
                    const trigger = event.target.closest('.score-info-btn');
                    if (trigger && !trigger.contains(event.relatedTarget)) {
                        hideScoreTooltip();
                    }
                });
                container.addEventListener('focusin', (event) => {
                    const trigger = event.target.closest('.score-info-btn');
                    if (trigger && container.contains(trigger)) {
                        const rect = trigger.getBoundingClientRect();
                        showScoreTooltip(trigger, {
                            clientX: rect.right,
                            clientY: rect.top
                        });
                    }
                });
                container.addEventListener('focusout', (event) => {
                    const trigger = event.target.closest('.score-info-btn');
                    if (trigger && !trigger.contains(event.relatedTarget)) {
                        hideScoreTooltip();
                    }
                });
            });
        }

        function sortedStrategyEntries(result) {
            const entries = result.strategies.map((strategy, index) => ({ strategy, index }));
            const mode = document.getElementById('summarySort').value;
            if (mode === 'return_desc') {
                entries.sort((a, b) => b.strategy.metrics.return_pct - a.strategy.metrics.return_pct);
            } else if (mode === 'return_asc') {
                entries.sort((a, b) => a.strategy.metrics.return_pct - b.strategy.metrics.return_pct);
            } else if (mode === 'drawdown_desc') {
                entries.sort((a, b) => a.strategy.metrics.max_drawdown_pct - b.strategy.metrics.max_drawdown_pct);
            } else if (mode === 'drawdown_asc') {
                entries.sort((a, b) => b.strategy.metrics.max_drawdown_pct - a.strategy.metrics.max_drawdown_pct);
            }
            return entries;
        }

        function rerenderSummaryFromSort() {
            if (lastResult) {
                renderSummary(lastResult);
            }
        }

        function renderSummary(result) {
            const perfStart = performance.now();
            document.getElementById('rangeLabel').textContent = `${result.range.start} 至 ${result.range.end}`;
            // update kpi grid
            const strategies = result.strategies;
            if (strategies.length) {
                const best = strategies.reduce((a, b) => b.metrics.return_pct > a.metrics.return_pct ? b : a);
                const worstDd = strategies.reduce((a, b) => b.metrics.max_drawdown_pct > a.metrics.max_drawdown_pct ? b : a);
                const maxCash = strategies.reduce((a, b) => b.metrics.cash_usage_pct > a.metrics.cash_usage_pct ? b : a);
                const totalBuys = strategies.reduce((sum, s) => sum + (s.metrics.buy_trade_count || 0), 0);
                const totalSells = strategies.reduce((sum, s) => sum + (s.metrics.sell_trade_count || 0), 0);
                const optionReturns = strategies.filter((s) => s.option_overlay && s.option_overlay.metrics).map((s) => s.option_overlay.metrics.return_pct);
                const bestReturn = best.metrics.return_pct;
                const kpiBestReturn = document.getElementById('kpiBestReturn');
                kpiBestReturn.querySelector('strong').textContent = (bestReturn >= 0 ? '+' : '') + bestReturn.toFixed(1) + '%';
                kpiBestReturn.className = 'kpi ' + (bestReturn >= 0 ? 'positive' : 'negative');
                const ddVal = worstDd.metrics.max_drawdown_pct;
                const kpiDd = document.getElementById('kpiWorstDrawdown');
                kpiDd.querySelector('strong').textContent = '-' + ddVal.toFixed(1) + '%';
                kpiDd.className = 'kpi negative';
                const cashVal = maxCash.metrics.cash_usage_pct;
                const kpiCash = document.getElementById('kpiCashUsage');
                kpiCash.querySelector('strong').textContent = cashVal.toFixed(1) + '%';
                kpiCash.className = 'kpi' + (cashVal > 80 ? ' warning' : '');
                document.getElementById('kpiTrades').querySelector('strong').textContent = `${totalBuys} / ${totalSells}`;
                const kpiOpt = document.getElementById('kpiOptionReturn');
                if (optionReturns.length) {
                    const bestOpt = Math.max(...optionReturns);
                    kpiOpt.querySelector('strong').textContent = (bestOpt >= 0 ? '+' : '') + bestOpt.toFixed(1) + '%';
                    kpiOpt.className = 'kpi ' + (bestOpt >= 0 ? 'positive' : 'negative');
                } else {
                    kpiOpt.querySelector('strong').textContent = '--';
                    kpiOpt.className = 'kpi';
                }
            }
            document.getElementById('summaryBody').innerHTML = sortedStrategyEntries(result).map((entry) => {
                const strategy = entry.strategy;
                const metrics = strategy.metrics;
                const optionMetrics = strategy.option_overlay && strategy.option_overlay.metrics;
                return `
                    <tr>
                        <td><button class="btn btn-small" type="button" onclick="showDetail(${entry.index})">详情</button></td>
                        <td>${buyStrategyLabels[strategy.buy_strategy] || strategy.buy_strategy}</td>
                        <td>${sellStrategyLabels[strategy.sell_strategy] || strategy.sell_strategy}</td>
                        <td>${money(metrics.final_value)}</td>
                        <td>${money(metrics.total_contributed)}</td>
                        <td>${pct(metrics.return_pct)}</td>
                        <td>${money(metrics.profit)}</td>
                        <td>${pct(metrics.max_drawdown_pct)}</td>
                        <td>${pct(metrics.cash_usage_pct)}</td>
                        <td>${number(metrics.buy_trade_count)}</td>
                        <td>${number(metrics.sell_trade_count)}</td>
                        <td>${number(metrics.sell_quality_score)}</td>
                        <td>${pct(metrics.avg_sell_profit_pct)}</td>
                        <td>${pct(metrics.avg_sell_drawdown_pct)}</td>
                        <td>${pct(metrics.cash_reuse_pct)}</td>
                        <td>${optionMetrics ? pct(optionMetrics.return_pct) : '--'}</td>
                        <td>${optionMetrics ? money(optionMetrics.total_premium) : '--'}</td>
                        <td>${money(metrics.total_fees)}</td>
                        <td>${pct(metrics.fee_ratio_pct)}</td>
                    </tr>
                `;
            }).join('');
            setPerfMetric('summaryRenderMs', performance.now() - perfStart);
        }

        function renderTrades(result) {
            const perfStart = performance.now();
            const rows = [];
            result.strategies.forEach((strategy) => {
                strategy.trades.slice(-80).forEach((trade) => {
                    rows.push({ strategy: strategy.label, ...trade });
                });
            });
            rows.sort((a, b) => String(b.date).localeCompare(String(a.date)));
            document.getElementById('tradeBody').innerHTML = rows.length ? rows.slice(0, 80).map((trade) => {
                const trigger = trade.threshold_pct ?? trade.trigger_value ?? 0;
                return `
                    <tr>
                        <td>${trade.strategy}</td>
                        <td>${trade.action === 'sell' ? '卖出' : '买入'}</td>
                        <td>${trade.date}</td>
                        <td>${trade.symbol}</td>
                        <td>${pct(trigger)}</td>
                        <td>${pct(trade.drawdown_pct)}</td>
                        <td>${number(trade.price)}</td>
                        <td>${money(trade.gross_amount)}</td>
                        <td>${money(trade.fee)}</td>
                    </tr>
                `;
            }).join('') : '<tr><td colspan="9">没有触发交易。</td></tr>';
            setPerfMetric('tradesRenderMs', performance.now() - perfStart);
        }

        function showDetail(index, shouldScroll = true) {
            activeDetailIndex = index;
            const panel = document.getElementById('detailPanel');
            panel.hidden = false;
            document.getElementById('detailChart').hidden = false;
            renderScoreReturnBanner();
            renderDetailSymbolOptions();
            renderActiveDetail();
            if (shouldScroll) {
                panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }

        function hideDetail() {
            document.getElementById('detailPanel').hidden = true;
            activeDetailIndex = null;
            Plotly.purge('detailChart');
            document.getElementById('detailChart').hidden = true;
        }

        function renderScoreReturnBanner() {
            const banner = document.getElementById('scoreReturnBanner');
            if (!banner) {
                return;
            }
            if (!scoreDetailContext) {
                banner.classList.add('hidden');
                banner.innerHTML = '';
                return;
            }
            const portfolio = scoreDetailContext.portfolio_label || '';
            const period = scoreDetailContext.period_label || '';
            banner.classList.remove('hidden');
            banner.innerHTML = `
                <div>
                    <strong>来自评分题目</strong>
                    <span>${escapeHtml(portfolio)} ${escapeHtml(period)}；当前图表使用评分矩阵里点开的买入 / 卖出策略。</span>
                </div>
                <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                    <button class="btn btn-secondary btn-small" type="button" onclick="returnToScorecard()">返回评分</button>
                    <button class="btn btn-secondary btn-small" type="button" onclick="openFullRunFromScorecard()">查看完整演算</button>
                </div>
            `;
        }

        function returnToScorecard() {
            activateTab('scorecard');
            const scorePanel = document.getElementById('scorecard');
            if (scorePanel) {
                scorePanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }

        function openFullRunFromScorecard() {
            activateTab('results');
            const panel = document.getElementById('detailPanel');
            if (panel && !panel.hidden) {
                panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }

        function renderDetailSymbolOptions() {
            const select = document.getElementById('detailSymbol');
            const previous = select.value;
            const symbols = (lastResult && lastResult.targets ? lastResult.targets : []).map((target) => target.symbol);
            select.innerHTML = symbols.map((symbol) => {
                const target = lastResult.targets.find((item) => item.symbol === symbol) || {};
                const label = target.name && target.name !== symbol ? `${target.name} · ${symbol}` : symbol;
                return `<option value="${escapeHtml(symbol)}">${escapeHtml(label)}</option>`;
            }).join('');
            if (symbols.includes(previous)) {
                select.value = previous;
            }
        }

        function renderActiveDetail() {
            if (!lastResult || activeDetailIndex === null) {
                return;
            }
            const strategy = lastResult.strategies[activeDetailIndex];
            if (!strategy) {
                return;
            }
            const symbol = document.getElementById('detailSymbol').value || (lastResult.targets[0] && lastResult.targets[0].symbol);
            document.getElementById('detailTitle').textContent = `买卖点图 · ${strategy.label}`;
            renderDetailMetrics(strategy, symbol);
            renderDetailChart(strategy, symbol);
            renderDetailTrades(strategy, symbol);
            renderDetailOptions(strategy, symbol);
        }

        function renderDetailMetrics(strategy, symbol) {
            const metrics = strategy.metrics;
            const symbolState = (strategy.symbols || []).find((item) => item.symbol === symbol) || {};
            const items = [
                ['最终市值', money(metrics.final_value)],
                ['累计投入', money(metrics.total_contributed)],
                ['总收益率', pct(metrics.return_pct)],
                ['累计盈利', money(metrics.profit)],
                ['最大回撤', pct(metrics.max_drawdown_pct)],
                ['现金使用率', pct(metrics.cash_usage_pct)],
                ['单股盈利', money(symbolState.profit)],
                ['单股收益率', pct(symbolState.return_pct)],
                ['买入次数', number(metrics.buy_trade_count)],
                ['卖出次数', number(metrics.sell_trade_count)],
                ['卖出质量', number(metrics.sell_quality_score)],
                ['卖出盈利均值', pct(metrics.avg_sell_profit_pct)],
                ['卖出回撤均值', pct(metrics.avg_sell_drawdown_pct)],
                ['现金复用率', pct(metrics.cash_reuse_pct)],
                ['当前股数', number(symbolState.shares)],
                ['平均成本 USD', money(symbolState.avg_cost_usd)]
            ];
            if (strategy.option_overlay && strategy.option_overlay.metrics) {
                const optionMetrics = strategy.option_overlay.metrics;
                items.push(['期权投入', money(optionMetrics.total_premium)]);
                items.push(['期权收益率', pct(optionMetrics.return_pct)]);
            }
            document.getElementById('detailMetrics').innerHTML = items.map(([label, value]) => `
                <div class="metric-card">
                    <span>${escapeHtml(label)}</span>
                    <strong>${escapeHtml(value)}</strong>
                </div>
            `).join('');
        }

        function renderDetailChart(strategy, symbol) {
            const perfStart = performance.now();
            const series = lastResult.price_series && lastResult.price_series[symbol];
            if (!series) {
                Plotly.purge('detailChart');
                setPerfMetric('detailChartMs', performance.now() - perfStart);
                return;
            }
            const trades = (strategy.trades || []).filter((trade) => trade.symbol === symbol);
            const buys = trades.filter((trade) => trade.action !== 'sell');
            const sells = trades.filter((trade) => trade.action === 'sell');
            const optionPositions = ((strategy.option_overlay && strategy.option_overlay.positions) || [])
                .filter((position) => position.stock_symbol === symbol);
            const optionEntries = optionPositions.map((position) => ({
                date: position.entry_date,
                price: position.stock_buy_price,
                text: `${position.option_ticker}<br>投入: ${money(position.premium)}<br>期权入场价: ${number(position.entry_price)}<br>Strike: ${number(position.strike)}`
            }));
            const optionExits = [];
            optionPositions.forEach((position) => {
                (position.exits || []).forEach((exit) => {
                    optionExits.push({
                        date: exit.date,
                        price: position.stock_buy_price,
                        text: `${position.option_ticker}<br>退出: ${exit.reason}<br>期权价: ${number(exit.price)}<br>价值: ${money(exit.value)}`
                    });
                });
            });
            const sampledPrice = downsampleSeries(series.dates, series.closes, 900);
            const sampledCash = downsampleSeries(strategy.series.dates, strategy.series.cash_values, 900);
            const traces = [
                {
                    x: sampledPrice.x,
                    y: sampledPrice.y,
                    type: 'scatter',
                    mode: 'lines',
                    name: `${symbol} Price`,
                    xaxis: 'x',
                    yaxis: 'y',
                    line: { color: '#07689f', width: 2.2 }
                },
                {
                    x: buys.map((trade) => trade.date),
                    y: buys.map((trade) => trade.display_price ?? trade.price),
                    type: 'scatter',
                    mode: 'markers',
                    name: '买点 / 触发档位',
                    xaxis: 'x',
                    yaxis: 'y',
                    marker: {
                        color: '#ff7e67',
                        size: buys.map((trade) => Math.max(8, Math.min(18, 7 + Number(trade.allocation_pct || 0) / 4))),
                        line: { color: '#fafafa', width: 1 }
                    },
                    text: buys.map((trade) => {
                        const dcaText = trade.drawdown_boost ? `<br>计划投入: ${money(trade.scheduled_amount)}<br>回撤加速: ${number(trade.drawdown_boost)}x` : '';
                        return `触发回撤: ${pct(trade.threshold_pct)}<br>实际回撤: ${pct(trade.drawdown_pct)}<br>触发价: ${number(trade.display_price ?? trade.price)}<br>成交收盘价: ${number(trade.price)}<br>买入金额: ${money(trade.gross_amount)}${dcaText}<br>手续费: ${money(trade.fee)}`;
                    }),
                    hovertemplate: '日期: %{x}<br>%{text}<extra></extra>'
                },
                {
                    x: sells.map((trade) => trade.date),
                    y: sells.map((trade) => trade.price),
                    type: 'scatter',
                    mode: 'markers',
                    name: '卖点',
                    xaxis: 'x',
                    yaxis: 'y',
                    marker: { color: '#054d76', symbol: 'diamond', size: 11, line: { color: '#fafafa', width: 1 } },
                    text: sells.map((trade) => `卖出金额: ${money(trade.gross_amount)}<br>回撤: ${pct(trade.drawdown_pct)}${lotHoverText(trade)}<br>手续费: ${money(trade.fee)}`),
                    hovertemplate: '日期: %{x}<br>价格: %{y:.2f}<br>%{text}<extra></extra>'
                },
                {
                    x: optionEntries.map((item) => item.date),
                    y: optionEntries.map((item) => item.price),
                    type: 'scatter',
                    mode: 'markers',
                    name: '期权买入',
                    xaxis: 'x',
                    yaxis: 'y',
                    marker: { color: '#a2d5f2', symbol: 'triangle-up', size: 12, line: { color: '#07689f', width: 1 } },
                    text: optionEntries.map((item) => item.text),
                    hovertemplate: '日期: %{x}<br>%{text}<extra></extra>'
                },
                {
                    x: optionExits.map((item) => item.date),
                    y: optionExits.map((item) => item.price),
                    type: 'scatter',
                    mode: 'markers',
                    name: '期权退出',
                    xaxis: 'x',
                    yaxis: 'y',
                    marker: { color: '#ff7e67', symbol: 'triangle-down', size: 12, line: { color: '#fafafa', width: 1 } },
                    text: optionExits.map((item) => item.text),
                    hovertemplate: '日期: %{x}<br>%{text}<extra></extra>'
                },
                {
                    x: sampledCash.x,
                    y: sampledCash.y,
                    type: 'scatter',
                    mode: 'lines',
                    name: '现金余额',
                    xaxis: 'x2',
                    yaxis: 'y2',
                    hoverinfo: 'skip',
                    line: { color: '#00856f', width: 2.0 }
                }
            ];
            const detailPlot = Plotly.react('detailChart', traces, {
                title: `${symbol} 买卖点 / 现金余额`,
                grid: { rows: 2, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
                margin: { t: 54, r: 24, b: 44, l: 64 },
                paper_bgcolor: '#fafafa',
                plot_bgcolor: '#fafafa',
                font: { color: '#06324c', family: 'Aptos, IBM Plex Sans, Noto Sans SC, sans-serif' },
                xaxis: { gridcolor: '#d7ecf8', zerolinecolor: '#a2d5f2', anchor: 'y', domain: [0, 1], showticklabels: false },
                yaxis: { title: symbol.endsWith('.HK') ? 'HKD' : 'USD', gridcolor: '#d7ecf8', zerolinecolor: '#a2d5f2', domain: [0.36, 1] },
                xaxis2: { gridcolor: '#d7ecf8', zerolinecolor: '#a2d5f2', anchor: 'y2', matches: 'x', domain: [0, 1] },
                yaxis2: { title: '现金 USD', gridcolor: '#d7ecf8', zerolinecolor: '#a2d5f2', domain: [0, 0.26] },
                legend: { orientation: 'h', font: { color: '#315d78' } }
            }, { responsive: true, displaylogo: false, displayModeBar: false });
            Promise.resolve(detailPlot).then(() => {
                setPerfMetric('detailChartMs', performance.now() - perfStart);
            });
        }

        function renderDetailTrades(strategy, symbol) {
            const rows = (strategy.trades || []).filter((trade) => trade.symbol === symbol);
            document.getElementById('detailTradeBody').innerHTML = rows.length ? rows.map((trade) => {
                const trigger = trade.threshold_pct ?? trade.trigger_value ?? 0;
                return `
                    <tr>
                        <td>${trade.action === 'sell' ? '卖出' : '买入'}</td>
                        <td>${trade.date}</td>
                        <td>${trade.symbol}</td>
                        <td>${pct(trigger)}</td>
                        <td>${pct(trade.drawdown_pct)}</td>
                        <td>${number(trade.price)}</td>
                        <td>${money(trade.gross_amount)}</td>
                        <td>${number(trade.shares)}</td>
                        <td>${money(trade.fee)}</td>
                    </tr>
                `;
            }).join('') : '<tr><td colspan="9">这个标的在该策略组合下没有触发交易。</td></tr>';
        }

        function renderDetailOptions(strategy, symbol) {
            const overlay = strategy.option_overlay || {};
            const positions = (overlay.positions || []).filter((position) => position.stock_symbol === symbol);
            const skipped = (overlay.skipped || []).filter((item) => item.stock_symbol === symbol);
            const rows = [];
            positions.forEach((position) => {
                const exits = (position.exits || []).map((exit) => `${exit.date} ${exit.reason}`).join('<br>') || '未退出';
                rows.push(`
                    <tr>
                        <td>${position.status === 'open' ? '持有中' : '已退出'}</td>
                        <td>${escapeHtml(position.option_ticker)}</td>
                        <td>${position.entry_date}</td>
                        <td>${position.expiration}</td>
                        <td>${number(position.strike)}</td>
                        <td>${number(position.dte_at_entry)}</td>
                        <td>${number(position.entry_price)}</td>
                        <td>${money(position.premium)}</td>
                        <td>${number(position.contracts)}</td>
                        <td>${money(position.total_value)}</td>
                        <td>${pct(position.return_pct)}</td>
                        <td>${exits}</td>
                    </tr>
                `);
            });
            skipped.forEach((item) => {
                rows.push(`
                    <tr>
                        <td>跳过</td>
                        <td>${escapeHtml(item.option_ticker || '')}</td>
                        <td>${escapeHtml(item.stock_buy_date || '')}</td>
                        <td>${escapeHtml(item.expiration || '')}</td>
                        <td>${number(item.strike)}</td>
                        <td>--</td>
                        <td>--</td>
                        <td>${money(item.stock_buy_amount || 0)}</td>
                        <td>--</td>
                        <td>--</td>
                        <td>--</td>
                        <td>${escapeHtml(item.reason || '')}</td>
                    </tr>
                `);
            });
            document.getElementById('detailOptionBody').innerHTML = rows.length ? rows.join('') : '<tr><td colspan="12">这个标的没有期权叠加记录。</td></tr>';
        }

        function scorecardPayload() {
            return {
                end: document.getElementById('end').value,
                initial_cash: readNumber('initialCash'),
                monthly_contribution: readNumber('monthlyContribution'),
                max_drawdown_pct: readNumber('maxDrawdown'),
                drawdown_basis: document.getElementById('drawdownBasis').value,
                step_pct: readNumber('stepPct'),
                equal_slice_allocation_pct: readNumber('equalSliceAllocation'),
                trade_fee: readNumber('tradeFee'),
                hkd_to_usd: readNumber('hkdToUsd'),
                reserve_position_pct: readNumber('reservePosition'),
                sell_min_profit_pct: readNumber('sellMinProfit'),
                repair_sell_cooldown_days: readNumber('repairSellCooldown'),
                repair_stage_sell_pct: readNumber('repairStageSellPct'),
                buy_strategies: selectedStrategies('buyStrategy', buyStrategyLabels),
                sell_strategies: selectedSellStrategies(),
                return_weight: readNumber('scoreReturnWeight') / 100,
                drawdown_weight: readNumber('scoreDrawdownWeight') / 100,
                scorecard_portfolio_keys: selectedScorecardPortfolios(),
                scorecard_periods: scorecardPeriodPayload(),
                targets: readPortfolio()
            };
        }

        function strategyDefaultsPayload() {
            const scanPeriod = document.getElementById('scanPeriod');
            return {
                default_initial_cash: readNumber('initialCash'),
                default_monthly_contribution: readNumber('monthlyContribution'),
                default_max_drawdown_pct: readNumber('maxDrawdown'),
                default_trade_fee: readNumber('tradeFee'),
                default_slice_step_pct: readNumber('stepPct'),
                default_equal_slice_allocation_pct: readNumber('equalSliceAllocation'),
                default_hkd_to_usd: readNumber('hkdToUsd'),
                default_reserve_position_pct: readNumber('reservePosition'),
                default_sell_min_profit_pct: readNumber('sellMinProfit'),
                default_repair_sell_cooldown_days: readNumber('repairSellCooldown'),
                default_repair_stage_sell_pct: readNumber('repairStageSellPct'),
                default_drawdown_basis: document.getElementById('drawdownBasis').value,
                default_buy_strategy: document.getElementById('buyStrategy').value,
                default_sell_strategy: document.getElementById('sellStrategy').value,
                default_score_return_weight_pct: readNumber('scoreReturnWeight'),
                default_score_drawdown_weight_pct: readNumber('scoreDrawdownWeight'),
                default_scorecard_portfolio_keys: selectedScorecardPortfolios(),
                default_scorecard_periods: scorecardPeriodPayload(),
                default_scan_buy_strategy: document.getElementById('scanBuyStrategy').value,
                default_scan_period_trading_days: Number(scanPeriod.value || 1260),
                default_scan_sell_min_profit_values: document.getElementById('scanSellMinProfits').value,
                default_scan_repair_cooldown_values: document.getElementById('scanCooldowns').value,
                default_scan_repair_stage_sell_values: document.getElementById('scanStageSells').value,
                default_scan_score_mode: document.getElementById('scanScoreMode').value,
                default_option_enabled: document.getElementById('optionEnabled').checked,
                default_option_allocation_pct: readNumber('optionAllocation'),
                default_option_target_dte: readNumber('optionTargetDte'),
                default_option_min_dte: readNumber('optionMinDte'),
                default_option_max_dte: readNumber('optionMaxDte'),
                default_option_moneyness: document.getElementById('optionMoneyness').value,
                default_option_profit_take_pct: readNumber('optionProfitTake'),
                default_option_profit_take_sell_pct: readNumber('optionProfitSell'),
                default_option_exit_dte: readNumber('optionExitDte'),
                default_option_trade_fee: readNumber('optionTradeFee'),
                default_option_max_trades_per_strategy: readNumber('optionMaxTrades'),
                default_portfolio: readPortfolio()
            };
        }

        async function saveStrategyDefaults() {
            const password = window.prompt('请输入配置密码，保存后刷新页面会自动使用这些默认值。');
            if (password === null) {
                return;
            }
            setStatus('info', '正在保存默认值...');
            try {
                const response = await fetch('/api/strategy-lab/defaults', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        password,
                        defaults: strategyDefaultsPayload()
                    })
                });
                const result = await response.json();
                if (!response.ok || !result.success) {
                    throw new Error(result.message || '保存失败');
                }
                setStatus('success', '默认值已保存；下次打开 strategy-lab 会自动使用当前参数。');
            } catch (error) {
                setStatus('error', `默认值保存失败: ${error.message || error}`);
            }
        }

        function renderScorecard(data) {
            const perfStart = performance.now();
            lastScorecard = data;
            const warnings = data.warnings && data.warnings.length ? `；${data.warnings.join('；')}` : '';
            const topicLabels = (data.portfolios || []).map((key) => scorecardPortfolioLabels[key] || key).join(' / ');
            document.getElementById('scorecardRange').textContent = `${data.range.start} 至 ${data.range.end}；题目 ${topicLabels}；周期按最近 252 / 756 / 1260 个交易日截取；当前组合使用首页组合权重，单股题目读取对应 symbol 的评分回撤上限；收益权重 ${number(data.weights.return * 100)}%，回撤权重 ${number(data.weights.drawdown * 100)}%${warnings}`;
            document.getElementById('scorecardBody').innerHTML = data.summary.map((item, index) => `
                <tr>
                    <td>${index + 1}</td>
                    <td>${escapeHtml(item.label)}</td>
                    <td>${number(item.score)}</td>
                    <td>${pct(item.avg_return_pct)}</td>
                    <td>${pct(item.avg_drawdown_pct)}</td>
                    <td>${number(item.avg_sell_quality_score)}</td>
                    <td>${pct(item.avg_sell_profit_pct)}</td>
                    <td>${pct(item.avg_sell_drawdown_pct)}</td>
                    <td>${pct(item.avg_cash_reuse_pct)}</td>
                    <td>${number(item.avg_rank)}</td>
                    <td>${number(item.best_rank)}</td>
                    <td>${number(item.worst_rank)}</td>
                </tr>
            `).join('');

            document.getElementById('scoreMatrixHead').innerHTML = `
                <tr>
                    <th>策略组合</th>
                    ${data.questions.map((question) => `<th>${escapeHtml(question.portfolio_label)}<br>${escapeHtml(question.period_label)}</th>`).join('')}
                </tr>
            `;
            document.getElementById('scoreMatrixBody').innerHTML = data.summary.map((item) => {
                const cells = data.questions.map((question) => {
                    const strategy = (question.strategies || []).find((entry) => entry.key === item.key);
                    if (!strategy) {
                        return '<td>--</td>';
                    }
                    const styleInfo = scoreDistanceStyle(strategy, question.strategies);
                    const style = styleInfo.style;
                    const title = [
                        `${question.portfolio_label} ${question.period_label}`,
                        `${item.label}`,
                        `总分: ${number(strategy.score)}`,
                        `排名: #${number(strategy.rank)}`,
                        `收益率: ${pct(strategy.return_pct)}`,
                        `最大回撤: ${pct(strategy.max_drawdown_pct)}`,
                        `距本题最佳收益: ${pct(styleInfo.returnGap)}`,
                        `距本题最佳回撤: ${pct(styleInfo.drawdownGap)}`,
                        `收益分: ${scoreNumber(strategy.return_score)}`,
                        `回撤分: ${scoreNumber(strategy.drawdown_score)}`,
                        `卖出质量: ${number(strategy.sell_quality_score)}`,
                        `卖出盈利: ${pct(strategy.avg_sell_profit_pct)}`,
                        `卖出回撤: ${pct(strategy.avg_sell_drawdown_pct)}`,
                        `现金复用: ${pct(strategy.cash_reuse_pct)}`
                    ].join('\\n');
                    return `
                        <td class="score-cell" style="${style}">
                            <button class="score-info-btn" type="button" aria-label="查看评分明细" data-tooltip="${escapeHtml(title)}">i</button>
                            <div class="score-cell-rank"><strong>#${number(strategy.rank)}</strong><span>${number(strategy.score)}</span></div>
                            <div class="score-metrics">
                                <div class="score-metric return"><strong>${pctCompact(strategy.return_pct)}</strong></div>
                                <div class="score-metric drawdown"><strong>${pctCompact(strategy.max_drawdown_pct)}</strong></div>
                            </div>
                            <button class="btn btn-secondary btn-small score-detail-btn" type="button" onclick="loadScorecardDetail('${question.key}', '${item.buy_strategy}', '${item.sell_strategy}')">详情</button>
                        </td>
                    `;
                }).join('');
                return `<tr><td><strong>${escapeHtml(item.label)}</strong></td>${cells}</tr>`;
            }).join('');
            setPerfMetric('scorecardRenderMs', performance.now() - perfStart);
        }

        function scanCellKey(cell) {
            return [
                Number(cell.sell_min_profit_pct ?? 0),
                Number(cell.repair_sell_cooldown_days ?? 0),
                Number(cell.repair_stage_sell_pct ?? 0)
            ].join('|');
        }

        function rerenderSellScan() {
            activeScanScoreMode = document.getElementById('scanScoreMode').value || 'balanced';
            if (lastSellScan) {
                activeScanStageSell = null;
                renderSellParameterScan(lastSellScan);
            }
        }

        function setScanView(view) {
            activeScanView = view === '3d' ? '3d' : '2d';
            document.getElementById('scanView2dBtn').classList.toggle('active', activeScanView === '2d');
            document.getElementById('scanView3dBtn').classList.toggle('active', activeScanView === '3d');
            document.getElementById('scan2dView').classList.toggle('scan-view-hidden', activeScanView !== '2d');
            document.getElementById('scan3dView').classList.toggle('scan-view-hidden', activeScanView !== '3d');
            if (activeScanView === '3d') {
                renderScan3d();
            } else {
                Plotly.purge('scan3dChart');
                renderScanMatrix();
            }
            updateScanLegend();
        }

        function renderSellParameterScan(data) {
            lastSellScan = data;
            activeScanScoreMode = document.getElementById('scanScoreMode').value || 'balanced';
            const result = document.getElementById('scanResult');
            result.classList.add('show');
            document.getElementById('scanView2dBtn').classList.toggle('active', activeScanView === '2d');
            document.getElementById('scanView3dBtn').classList.toggle('active', activeScanView === '3d');
            document.getElementById('scan2dView').classList.toggle('scan-view-hidden', activeScanView !== '2d');
            document.getElementById('scan3dView').classList.toggle('scan-view-hidden', activeScanView !== '3d');
            const cells = data.cells || [];
            const best = scanBestCell(cells) || data.best || {};
            const baseline = data.baseline || null;
            const bestScore = scanDisplayScore(best, cells);
            const baselineScore = baseline ? scanDisplayScore(baseline, cells) : null;
            const bestDelta = baseline ? bestScore - baselineScore : null;
            const scoreLabel = scanScoreLabel();
            document.getElementById('scanStrip').innerHTML = `
                <div class="scan-stat">
                    <span>本次最佳</span>
                    <strong>${number(bestScore)}</strong>
                    <small>${scoreLabel}；${pct(best.sell_min_profit_pct)} 盈利 / ${number(best.repair_sell_cooldown_days)} 个交易日冷却 / ${pct(best.repair_stage_sell_pct)} 单档卖出<br>收益 ${pct(best.return_pct)}，回撤 ${pct(best.max_drawdown_pct)}，卖出质量 ${number(best.sell_quality_score)}</small>
                </div>
                <div class="scan-stat">
                    <span>相对当前参数</span>
                    <strong>${bestDelta === null ? '--' : `${bestDelta >= 0 ? '+' : ''}${number(bestDelta)}`}</strong>
                    <small>${baseline ? `当前${scoreLabel} ${number(baselineScore)}，收益 ${pct(baseline.return_pct)}，回撤 ${pct(baseline.max_drawdown_pct)}，卖出质量 ${number(baseline.sell_quality_score)}` : '当前参数不在扫描范围内。'}</small>
                </div>
            `;
            const stageValues = data.axes.repair_stage_sell_pct || [];
            const hasActiveStage = activeScanStageSell !== null
                && stageValues.some((value) => Number(value) === Number(activeScanStageSell));
            activeScanStageSell = hasActiveStage
                ? Number(activeScanStageSell)
                : Number(best.repair_stage_sell_pct ?? stageValues[0] ?? 0);
            document.getElementById('scanStageTabs').innerHTML = stageValues.map((value) => `
                <button class="scan-stage-tab ${Number(value) === Number(activeScanStageSell) ? 'active' : ''}" type="button" onclick="setScanStage(${value})">单档 ${pct(value)}</button>
            `).join('');
            updateScanLegend();
            if (activeScanView === '3d') {
                renderScan3d();
            } else {
                renderScanMatrix();
            }
        }

        function renderRobustLeaderboard(data) {
            lastRobustLeaderboard = data;
            const board = document.getElementById('robustBoard');
            board.classList.add('show');
            const tasks = data.tasks || [];
            const counts = data.candidate_counts || {};
            document.getElementById('robustRange').textContent = `${data.range.start} 至 ${data.range.end}；${tasks.length} 个题目`;
            document.getElementById('robustStrip').innerHTML = `
                <div class="robust-stat"><span>粗筛候选</span><strong>${number(counts.coarse)}</strong></div>
                <div class="robust-stat"><span>局部加密候选</span><strong>${number(counts.fine)}</strong></div>
                <div class="robust-stat"><span>最终验证候选</span><strong>${number(counts.final)}</strong></div>
                <div class="robust-stat"><span>输出 Top</span><strong>${number((data.leaderboard || []).length)}</strong></div>
            `;
            const rows = (data.leaderboard || []).map((row) => {
                const candidate = row.candidate || {};
                const strongest = row.strongest_task || {};
                const weakest = row.weakest_task || {};
                return `
                    <tr>
                        <td>
                            <strong>${escapeHtml(candidate.label || candidate.key || '--')}</strong>
                            <div class="robust-task">${escapeHtml(candidate.sell_strategy === 'repair_step'
                                ? `${pct(candidate.sell_min_profit_pct)} 盈利 / ${number(candidate.repair_sell_cooldown_days)} 日冷却 / ${pct(candidate.repair_stage_sell_pct)} 单档`
                                : '无阶梯参数')}</div>
                            <div style="margin-top:8px;">
                                <button class="btn btn-secondary btn-small" type="button" onclick="applyRobustCandidate('${escapeHtml(candidate.key)}')">应用并看评分</button>
                            </div>
                        </td>
                        <td>${number(row.robust_score)}</td>
                        <td>${number(row.mean_score)}</td>
                        <td>${number(row.p25_score)}</td>
                        <td>${pct(row.top10_rate)}</td>
                        <td>${pct(row.bottom10_rate)}</td>
                        <td>${pct(row.avg_return_pct)}</td>
                        <td>${pct(row.avg_drawdown_pct)}</td>
                        <td>
                            <div class="robust-task">强：${escapeHtml(strongest.label || '--')} / ${number(strongest.score)}</div>
                            <div class="robust-task">弱：${escapeHtml(weakest.label || '--')} / ${number(weakest.score)}</div>
                        </td>
                    </tr>
                `;
            }).join('');
            document.getElementById('robustBody').innerHTML = rows || '<tr><td colspan="9">暂无稳健榜结果。</td></tr>';
        }

        function applyRobustCandidate(candidateKey) {
            if (!lastRobustLeaderboard) {
                setStatus('error', '暂无稳健榜结果可应用。');
                return;
            }
            const row = (lastRobustLeaderboard.leaderboard || []).find((item) => item.candidate && item.candidate.key === candidateKey);
            const candidate = row && row.candidate;
            if (!candidate) {
                setStatus('error', '未找到对应策略参数。');
                return;
            }
            setSelectValue('buyStrategy', candidate.buy_strategy);
            setSelectValue('sellStrategy', candidate.sell_strategy);
            if (candidate.sell_strategy === 'repair_step') {
                setFieldValue('sellMinProfit', candidate.sell_min_profit_pct);
                setFieldValue('repairSellCooldown', candidate.repair_sell_cooldown_days);
                setFieldValue('repairStageSellPct', candidate.repair_stage_sell_pct);
            }
            updateCommandBar();
            activateTab('scorecard');
            setStatus('success', '已应用稳健榜策略参数。点击“运行评分”即可查看它在每个股票和时间阶段下的表现。');
        }

        function setScanStage(value) {
            activeScanStageSell = Number(value);
            if (lastSellScan) {
                renderSellParameterScan(lastSellScan);
            }
        }

        function renderScanMatrix() {
            if (!lastSellScan || activeScanStageSell === null) {
                return;
            }
            const minProfits = lastSellScan.axes.sell_min_profit_pct || [];
            const cooldowns = lastSellScan.axes.repair_sell_cooldown_days || [];
            const cells = lastSellScan.cells || [];
            const stageCells = cells.filter((cell) => Number(cell.repair_stage_sell_pct) === Number(activeScanStageSell));
            if (!stageCells.length) {
                document.getElementById('scanMatrixHead').innerHTML = '';
                document.getElementById('scanMatrixBody').innerHTML = '<tr><td>暂无数据</td></tr>';
                return;
            }
            const displayScores = stageCells.map((cell) => scanDisplayScore(cell, cells));
            const minScore = Math.min(...displayScores);
            const maxScore = Math.max(...displayScores);
            const best = scanBestCell(cells);
            const bestKey = best ? scanCellKey(best) : '';
            const baselineKey = lastSellScan.baseline ? scanCellKey(lastSellScan.baseline) : '';
            const byKey = new Map(stageCells.map((cell) => [`${Number(cell.repair_sell_cooldown_days ?? 0)}|${Number(cell.sell_min_profit_pct ?? 0)}`, cell]));
            const scoreLabel = scanScoreLabel();
            document.getElementById('scanMatrixHead').innerHTML = `
                <tr>
                    <th>冷却 \\ 盈利</th>
                    ${minProfits.map((value) => `<th>${pct(value)}</th>`).join('')}
                </tr>
            `;
            document.getElementById('scanMatrixBody').innerHTML = cooldowns.map((cooldown) => {
                const row = minProfits.map((profit) => {
                    const cell = byKey.get(`${Number(cooldown ?? 0)}|${Number(profit ?? 0)}`);
                    if (!cell) {
                        return '<td>--</td>';
                    }
                    const key = scanCellKey(cell);
                    const displayScore = scanDisplayScore(cell, cells);
                    const title = [
                        `最小盈利: ${pct(cell.sell_min_profit_pct)}`,
                        `冷却交易日: ${number(cell.repair_sell_cooldown_days)}`,
                        `单档卖出: ${pct(cell.repair_stage_sell_pct)}`,
                        `${scoreLabel}: ${number(displayScore)}`,
                        `卖出质量: ${number(cell.sell_quality_score)}`,
                        `收益: ${pct(cell.return_pct)}`,
                        `最大回撤: ${pct(cell.max_drawdown_pct)}`,
                        `卖出盈利: ${pct(cell.avg_sell_profit_pct)}`,
                        `卖出回撤: ${pct(cell.avg_sell_drawdown_pct)}`,
                        `现金复用: ${pct(cell.cash_reuse_pct)}`,
                        `卖出次数: ${number(cell.sell_trade_count)}`
                    ].join('\\n');
                    return `
                        <td class="scan-cell ${key === bestKey ? 'best' : ''} ${key === baselineKey ? 'baseline' : ''}"
                            style="${scanCellStyle(displayScore, minScore, maxScore)}"
                            title="${escapeHtml(title)}"
                            onclick="applyScanCell('${cell.sell_min_profit_pct}', '${cell.repair_sell_cooldown_days}', '${cell.repair_stage_sell_pct}')">
                            <strong>${number(displayScore)}</strong>
                            <span>收益 ${pctCompact(cell.return_pct)}</span>
                            <span>回撤 ${pctCompact(cell.max_drawdown_pct)}</span>
                            <em>质量 ${number(cell.sell_quality_score)}</em>
                        </td>
                    `;
                }).join('');
                return `<tr><td>${number(cooldown)} 交易日</td>${row}</tr>`;
            }).join('');
        }

        function renderScan3d() {
            if (!lastSellScan || activeScanView !== '3d') {
                return;
            }
            const cells = lastSellScan.cells || [];
            if (!cells.length) {
                Plotly.purge('scan3dChart');
                document.getElementById('scan3dChart').innerHTML = '<div class="hint">暂无 3D 扫描数据。</div>';
                return;
            }
            const best = scanBestCell(cells);
            const baseline = lastSellScan.baseline || null;
            const bestKey = best ? scanCellKey(best) : '';
            const baselineKey = baseline ? scanCellKey(baseline) : '';
            const scoreLabel = scanScoreLabel();
            const scores = cells.map((cell) => scanDisplayScore(cell, cells));
            const minScore = Math.min(...scores);
            const maxScore = Math.max(...scores);
            const hoverText = cells.map((cell, index) => [
                `${scoreLabel}: ${number(scores[index])}`,
                `最小盈利: ${pct(cell.sell_min_profit_pct)}`,
                `冷却交易日: ${number(cell.repair_sell_cooldown_days)}`,
                `单档卖出: ${pct(cell.repair_stage_sell_pct)}`,
                `收益: ${pct(cell.return_pct)}`,
                `最大回撤: ${pct(cell.max_drawdown_pct)}`,
                `卖出质量: ${number(cell.sell_quality_score)}`,
                `现金复用: ${pct(cell.cash_reuse_pct)}`,
                `卖出次数: ${number(cell.sell_trade_count)}`
            ].join('<br>'));
            const customData = cells.map((cell) => [
                Number(cell.sell_min_profit_pct ?? 0),
                Number(cell.repair_sell_cooldown_days ?? 0),
                Number(cell.repair_stage_sell_pct ?? 0)
            ]);
            const traces = [
                {
                    type: 'scatter3d',
                    mode: 'markers',
                    name: '参数组合',
                    x: cells.map((cell) => Number(cell.sell_min_profit_pct ?? 0)),
                    y: cells.map((cell) => Number(cell.repair_sell_cooldown_days ?? 0)),
                    z: cells.map((cell) => Number(cell.repair_stage_sell_pct ?? 0)),
                    customdata: customData,
                    text: hoverText,
                    hovertemplate: '%{text}<extra></extra>',
                    marker: {
                        size: cells.map((cell) => 7 + Math.min(5, Number(cell.sell_trade_count || 0) * 0.30)),
                        color: scores,
                        cmin: minScore,
                        cmax: maxScore,
                        colorscale: [
                            [0.00, '#d04437'],
                            [0.28, '#ff7e67'],
                            [0.52, '#ffd166'],
                            [0.74, '#2ec4b6'],
                            [1.00, '#00856f']
                        ],
                        opacity: 0.92,
                        line: { color: 'rgba(6, 50, 76, 0.34)', width: 1.2 },
                        colorbar: {
                            title: scoreLabel,
                            thickness: 14,
                            len: 0.72
                        }
                    }
                }
            ];
            const specialTrace = (cell, name, color, symbol) => ({
                type: 'scatter3d',
                mode: 'markers',
                name,
                x: [Number(cell.sell_min_profit_pct ?? 0)],
                y: [Number(cell.repair_sell_cooldown_days ?? 0)],
                z: [Number(cell.repair_stage_sell_pct ?? 0)],
                customdata: [[
                    Number(cell.sell_min_profit_pct ?? 0),
                    Number(cell.repair_sell_cooldown_days ?? 0),
                    Number(cell.repair_stage_sell_pct ?? 0)
                ]],
                text: [`${name}<br>${scoreLabel}: ${number(scanDisplayScore(cell, cells))}<br>收益: ${pct(cell.return_pct)}<br>回撤: ${pct(cell.max_drawdown_pct)}`],
                hovertemplate: '%{text}<extra></extra>',
                marker: {
                    size: name.includes('最佳') || name.includes('当前即最佳') ? 18 : 15,
                    color,
                    symbol,
                    line: { color: name.includes('最佳') ? '#06324c' : '#ffffff', width: 3 },
                    opacity: 1
                }
            });
            if (baseline && baselineKey === bestKey) {
                traces.push(specialTrace(baseline, '当前即最佳', '#00a884', 'diamond'));
            } else {
                if (best) {
                    traces.push(specialTrace(best, '最佳参数', '#00a884', 'diamond'));
                }
                if (baseline) {
                    traces.push(specialTrace(baseline, '当前参数', '#1167d8', 'circle'));
                }
            }
            const chart = document.getElementById('scan3dChart');
            const plot = Plotly.react(chart, traces, {
                margin: { t: 18, r: 18, b: 12, l: 12 },
                paper_bgcolor: '#ffffff',
                plot_bgcolor: '#ffffff',
                font: { color: '#06324c', family: 'Aptos, IBM Plex Sans, Noto Sans SC, sans-serif' },
                scene: {
                    xaxis: { title: '最小盈利 %', gridcolor: '#d7ecf8', zerolinecolor: '#a2d5f2' },
                    yaxis: { title: '冷却天数', gridcolor: '#d7ecf8', zerolinecolor: '#a2d5f2' },
                    zaxis: { title: '单档卖出 %', gridcolor: '#d7ecf8', zerolinecolor: '#a2d5f2' },
                    camera: { eye: { x: 1.55, y: 1.55, z: 1.05 } }
                },
                legend: { orientation: 'h', x: 0, y: 1.04, font: { color: '#315d78' } }
            }, { responsive: true, displaylogo: false, displayModeBar: false });
            Promise.resolve(plot).then(() => {
                if (chart.removeAllListeners) {
                    chart.removeAllListeners('plotly_click');
                }
                chart.on('plotly_click', (event) => {
                    const point = event.points && event.points[0];
                    if (!point || !point.customdata) {
                        return;
                    }
                    applyScanCell(point.customdata[0], point.customdata[1], point.customdata[2]);
                });
            });
        }

        function applyScanCell(minProfit, cooldown, stageSell) {
            document.getElementById('sellMinProfit').value = Number(minProfit);
            document.getElementById('repairSellCooldown').value = Number(cooldown);
            document.getElementById('repairStageSellPct').value = Number(stageSell);
            updateCommandBar();
            setStatus('success', `已回填卖出参数: 最小盈利 ${pct(minProfit)}，冷却 ${number(cooldown)} 个交易日，单档卖出 ${pct(stageSell)}`);
        }

        async function runSellParameterScan() {
            setStatus('info', '正在扫描阶梯修复卖出参数...');
            try {
                const endDate = document.getElementById('end').value;
                const end = new Date(`${endDate}T00:00:00`);
                const start = new Date(end);
                const periodSelect = document.getElementById('scanPeriod');
                const selectedPeriod = periodSelect.options[periodSelect.selectedIndex];
                const tradingDays = Number(periodSelect.value || 1260);
                const fetchDays = Number(selectedPeriod.dataset.fetchDays || Math.ceil(tradingDays * 1.6));
                start.setDate(start.getDate() - fetchDays);
                const payload = {
                    ...scorecardPayload(),
                    start: formatDateInput(start),
                    end: endDate,
                    trading_days: tradingDays,
                    buy_strategy: document.getElementById('scanBuyStrategy').value,
                    sell_min_profit_values: parseScanValues('scanSellMinProfits', false, 100),
                    repair_sell_cooldown_values: parseScanValues('scanCooldowns', true),
                    repair_stage_sell_values: parseScanValues('scanStageSells', false, 100)
                };
                const data = await runStrategyJob('scan', payload, {
                    title: '参数扫描',
                    perfKey: 'apiScoreMs'
                });
                activeScanStageSell = null;
                activeScanView = '2d';
                Plotly.purge('scan3dChart');
                renderSellParameterScan(data);
                const warnings = data.warnings && data.warnings.length ? `；${data.warnings.join('；')}` : '';
                addRunHistory(
                    'scan',
                    '参数扫描完成',
                    `组合数 ${number((data.cells || []).length)}；${warnings ? warnings.replace(/^；/, '') : '无缓存告警'}`
                );
                setStatus('success', `卖出参数扫描完成${warnings}`);
            } catch (error) {
                setStatus('error', `扫描失败: ${error.message || error}`);
            }
        }

        async function runRobustLeaderboard() {
            setStatus('info', '正在运行稳健 Top10：先粗筛，再局部加密，最后做全题验证...');
            try {
                const payload = {
                    ...scorecardPayload(),
                    start: document.getElementById('start').value,
                    end: document.getElementById('end').value,
                    top_n: 10
                };
                const data = await runStrategyJob('robust', payload, {
                    title: '稳健 Top10',
                    perfKey: 'apiScoreMs',
                    pollDelay: 1200
                });
                renderRobustLeaderboard(data);
                activateTab('scan');
                updateCommandBar();
                addRunHistory(
                    'robust',
                    '稳健 Top10 完成',
                    `${number((data.leaderboard || []).length)} 个结果；${number((data.tasks || []).length)} 个题目`
                );
                setStatus('success', '稳健 Top10 完成');
            } catch (error) {
                setStatus('error', `稳健 Top10 失败: ${error.message || error}`);
            }
        }

        async function loadScorecardDetail(questionKey, buyStrategy, sellStrategy) {
            setStatus('info', '正在加载评分题目详情...');
            try {
                const apiStart = performance.now();
                const payload = {
                    ...scorecardPayload(),
                    question_key: questionKey,
                    buy_strategy: buyStrategy,
                    sell_strategy: sellStrategy
                };
                const response = await fetch('/api/strategy-lab/score/detail', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const result = await response.json();
                setPerfMetric('apiDetailMs', performance.now() - apiStart);
                if (!response.ok || !result.success) {
                    throw new Error(result.message || '详情加载失败');
                }
                lastResult = result.data;
                lastLabSignature = null;
                renderSummary(result.data);
                renderTrades(result.data);
                const meta = result.data.scorecard_detail || {};
                scoreDetailContext = meta;
                showDetail(0);
                activateTab('results');
                const panel = document.getElementById('detailPanel');
                if (panel) {
                    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
                setStatus('success', `已加载评分详情: ${meta.portfolio_label || ''} ${meta.period_label || ''}；可用详情上方按钮返回评分。`);
                addRunHistory(
                    'detail',
                    '评分详情已加载',
                    `${meta.portfolio_label || ''} ${meta.period_label || ''}`.trim() || '评分题目详情'
                );
                updateCommandBar();
            } catch (error) {
                setStatus('error', `详情加载失败: ${error.message || error}`);
            }
        }

        async function runScorecard() {
            setStatus('info', '正在运行策略评分：优先读取本机日线缓存，缺口再请求 Longbridge...');
            const payload = scorecardPayload();
            try {
                const data = await runStrategyJob('score', payload, {
                    title: '策略评分',
                    perfKey: 'apiScoreMs'
                });
                renderScorecard(data);
                lastScorecardSignature = stableSignature(payload);
                activateTab('scorecard');
                updateCommandBar();
                addRunHistory(
                    'scorecard',
                    '策略评分完成',
                    `${number((data.summary || []).length)} 个策略组合；${number((data.questions || []).length)} 个题目`
                );
                setStatus('success', '策略评分完成');
            } catch (error) {
                setStatus('error', `评分失败: ${error.message || error}`);
            }
        }

        async function runLab() {
            setStatus('info', '正在运行组合演算：优先读取本机日线缓存，缺口再请求 Longbridge...');
            const payload = buildLabPayload();
            try {
                const data = await runStrategyJob('run', payload, {
                    title: '组合演算',
                    perfKey: 'apiLabMs'
                });
                lastResult = data;
                lastLabSignature = stableSignature(payload);
                scoreDetailContext = null;
                hideDetail();
                renderSummary(data);
                activateTab('results');
                if (data.strategies && data.strategies.length) {
                    showDetail(0, false);
                }
                renderTrades(data);
                updateCommandBar();
                const warningText = data.warnings && data.warnings.length ? `；${data.warnings.join('；')}` : '';
                addRunHistory(
                    'lab',
                    '组合演算完成',
                    `${number((data.strategies || []).length)} 个策略组合；${warningText ? warningText.replace(/^；/, '') : '无缓存告警'}`
                );
                setStatus('success', `演算完成${warningText}`);
            } catch (error) {
                setStatus('error', `请求失败: ${error.message || error}`);
            }
        }

        initPortfolioRows();
        updateScorecardQuestionHint();
        initScoreTooltip();
        initCommandBarWatchers();
        refreshHistoryWorkspace();
        updateCommandBar();
        initPerfPanel();
    </script>
</body>
</html>
"""

HISTORY_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>历史报告 - BOLL指标筛选系统</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .btn { padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
        .btn:hover { background: #5568d3; }
        .btn-secondary { background: #6c757d; }
        .btn-secondary:hover { background: #5a6268; }
        .report-list { margin-top: 20px; }
        .report-item { 
            padding: 15px; 
            margin-bottom: 10px; 
            border: 1px solid #ddd; 
            border-radius: 4px; 
            display: flex; 
            justify-content: space-between; 
            align-items: center;
            background: #fafafa;
            transition: background 0.2s;
        }
        .report-item:hover { background: #f0f0f0; }
        .report-info { flex: 1; }
        .report-name { font-weight: bold; font-size: 16px; color: #333; }
        .report-time { color: #666; font-size: 14px; margin-top: 5px; }
        .report-actions { display: flex; gap: 10px; }
        .empty { text-align: center; padding: 40px; color: #999; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>历史报告</h1>
            <div>
                <a href="/" class="btn btn-secondary">返回首页</a>
            </div>
        </div>
        
        <div class="report-list">
            {% if reports %}
                {% for report in reports %}
                <div class="report-item">
                    <div class="report-info">
                        <div class="report-name">{{ report.name }}</div>
                        <div class="report-time">生成时间: {{ report.time }}</div>
                    </div>
                    <div class="report-actions">
                        <a href="/report/{{ report.filename }}" class="btn" target="_blank">查看</a>
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="empty">
                    <p>暂无历史报告</p>
                    <p style="margin-top: 10px;"><a href="/">返回首页</a> 手动触发分析生成报告</p>
                </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

SCHEDULE_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>定时任务配置 - BOLL指标筛选系统</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input[type="number"] { width: 100px; padding: 10px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        .btn { padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; }
        .btn:hover { background: #5568d3; }
        .status { padding: 10px; margin: 10px 0; border-radius: 4px; }
        .status.success { background: #d4edda; color: #155724; }
        .status.error { background: #f8d7da; color: #721c24; }
        .status.info { background: #d1ecf1; color: #0c5460; }
        .back-link { margin-top: 20px; }
        .info-box { background: #f8f9fa; padding: 15px; border-radius: 4px; margin-bottom: 20px; }
        .info-box p { margin: 5px 0; color: #666; }
        .time-input-group { display: flex; align-items: center; gap: 10px; }
        .time-input-group input { width: 80px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>定时任务配置</h1>
        <div id="status"></div>
        
        <div class="info-box">
            <p><strong>当前配置：</strong></p>
            <p>执行时间: <span id="current-time">{{ current_hour }}:{{ current_minute }}</span> (北京时间)</p>
            <p>下次执行: <span id="next-run">{{ next_run_time }}</span></p>
        </div>
        
        <form id="scheduleForm" onsubmit="updateSchedule(event)">
            <div class="form-group">
                <label for="hour">执行小时（0-23）：</label>
                <input type="number" id="hour" name="hour" min="0" max="23" value="{{ current_hour }}" required>
            </div>
            <div class="form-group">
                <label for="minute">执行分钟（0-59）：</label>
                <input type="number" id="minute" name="minute" min="0" max="59" value="{{ current_minute }}" required>
            </div>
            <button type="submit" class="btn">保存配置</button>
        </form>
        
        <div class="back-link">
            <a href="/">返回首页</a>
        </div>
    </div>
    
    <script>
        function updateSchedule(event) {
            event.preventDefault();
            const hour = parseInt(document.getElementById('hour').value);
            const minute = parseInt(document.getElementById('minute').value);
            
            if (hour < 0 || hour > 23) {
                document.getElementById('status').innerHTML = '<div class="status error">小时必须在0-23之间</div>';
                return;
            }
            
            if (minute < 0 || minute > 59) {
                document.getElementById('status').innerHTML = '<div class="status error">分钟必须在0-59之间</div>';
                return;
            }
            
            fetch('/api/schedule', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ hour: hour, minute: minute })
            })
            .then(response => response.json())
            .then(data => {
                const statusDiv = document.getElementById('status');
                if (data.success) {
                    statusDiv.innerHTML = '<div class="status success">定时任务配置已更新！</div>';
                    // 更新显示
                    document.getElementById('current-time').textContent = hour + ':' + (minute < 10 ? '0' + minute : minute);
                    if (data.next_run_time) {
                        document.getElementById('next-run').textContent = data.next_run_time;
                    }
                } else {
                    statusDiv.innerHTML = '<div class="status error">' + data.message + '</div>';
                }
            })
            .catch(error => {
                document.getElementById('status').innerHTML = '<div class="status error">请求失败: ' + error + '</div>';
            });
        }
    </script>
</body>
</html>
"""

UPDATE_TOKEN_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>更新Token</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input[type="text"], input[type="password"] { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        .btn { padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; }
        .btn:hover { background: #5568d3; }
        .status { padding: 10px; margin: 10px 0; border-radius: 4px; }
        .status.success { background: #d4edda; color: #155724; }
        .status.error { background: #f8d7da; color: #721c24; }
        .back-link { margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>更新Token</h1>
        <div id="status"></div>
        <form id="tokenForm" onsubmit="updateToken(event)">
            <div class="form-group">
                <label for="password">密码:</label>
                <input type="password" id="password" name="password" required>
            </div>
            <div class="form-group">
                <label for="token">新的Access Token:</label>
                <input type="text" id="token" name="token" required placeholder="输入新的token">
            </div>
            <button type="submit" class="btn">更新Token</button>
        </form>
        <div class="back-link">
            <a href="/">返回首页</a>
        </div>
    </div>
    
    <script>
        function updateToken(event) {
            event.preventDefault();
            const password = document.getElementById('password').value;
            const token = document.getElementById('token').value;
            
            fetch('/api/update-token', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: password, token: token })
            })
            .then(response => response.json())
            .then(data => {
                const statusDiv = document.getElementById('status');
                if (data.success) {
                    statusDiv.innerHTML = '<div class="status success">Token更新成功！</div>';
                    document.getElementById('tokenForm').reset();
                } else {
                    statusDiv.innerHTML = '<div class="status error">' + data.message + '</div>';
                }
            })
            .catch(error => {
                document.getElementById('status').innerHTML = '<div class="status error">请求失败: ' + error + '</div>';
            });
        }
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    """首页：显示最新分析结果"""
    global latest_result

    # 每次访问都重新加载最新结果，确保显示定时任务触发的最新结果
    try:
        from watchlist_boll_filter import load_latest_result
        current_result = load_latest_result()
        if current_result:
            latest_result = current_result  # 更新全局变量
    except Exception as e:
        # 如果加载失败，使用全局变量作为fallback
        print(f"加载最新结果失败: {e}")

    result_html = ""
    if latest_result:
        drawdown_snapshots = {}
        try:
            from drawdown.snapshot import collect_drawdown_snapshots

            symbols = []
            for bucket in (
                latest_result.below_lower,
                latest_result.near_lower,
                latest_result.near_upper,
                latest_result.above_upper,
            ):
                symbols.extend(stock.symbol for stock in bucket)
            symbols = list(dict.fromkeys(symbols))
            if symbols:
                drawdown_snapshots, drawdown_errors = collect_drawdown_snapshots(symbols)
                if drawdown_errors:
                    print(
                        "首页加载回撤快照时以下股票失败，将显示为 --: "
                        + ", ".join(sorted(drawdown_errors))
                    )
        except Exception as e:
            print(f"首页加载回撤快照失败: {e}")
        result_html = generate_html_report(latest_result, drawdown_snapshots=drawdown_snapshots)

    response = app.make_response(render_template_string(INDEX_TEMPLATE, result=latest_result, result_html=result_html))
    # 禁用浏览器缓存，确保每次都显示最新报告
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/api/result')
def api_result():
    """获取最新结果（JSON格式）"""
    global latest_result

    # 每次访问都重新加载最新结果
    try:
        from watchlist_boll_filter import load_latest_result
        current_result = load_latest_result()
        if current_result:
            latest_result = current_result  # 更新全局变量
    except Exception as e:
        # 如果加载失败，使用全局变量作为fallback
        print(f"加载最新结果失败: {e}")

    if latest_result is None:
        return jsonify({"success": False, "message": "暂无分析结果"}), 404

    response = jsonify({
        "success": True,
        "result": latest_result.to_dict()
    })
    # 禁用浏览器缓存
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/update-token')
def update_token_page():
    """Token更新页面"""
    return render_template_string(UPDATE_TOKEN_TEMPLATE)


@app.route('/api/update-token', methods=['POST'])
def api_update_token():
    """更新token接口"""
    global config_manager
    
    if config_manager is None:
        try:
            config_manager = ConfigManager()
        except Exception as e:
            return jsonify({"success": False, "message": f"配置管理器初始化失败: {str(e)}"}), 500
    
    data = request.get_json()
    password = data.get("password", "")
    token = data.get("token", "")
    
    # 验证密码
    web_config = config_manager.get_web_config()
    update_password = web_config.get("update_password", "")
    
    if not update_password:
        return jsonify({"success": False, "message": "未配置更新密码，请先设置update_password"}), 400
    
    if password != update_password:
        return jsonify({"success": False, "message": "密码错误"}), 401
    
    # 更新token
    if config_manager.update_token(token):
        return jsonify({"success": True, "message": "Token更新成功"})
    else:
        return jsonify({"success": False, "message": "Token更新失败"}), 500


@app.route('/api/trigger', methods=['POST'])
def api_trigger():
    """手动触发分析"""
    global latest_result

    if config_manager is None:
        return jsonify({"success": False, "message": "配置管理器未初始化"}), 500

    try:
        # 获取请求参数
        data = request.get_json() or {}
        option_delay = data.get('option_delay', False)

        result = run_analysis_and_notify(
            config_manager=config_manager,
            send_email=True,  # 手动触发也发送邮件
            save_html=True,
            option_delay=option_delay
        )

        if result:
            latest_result = result
            return jsonify({"success": True, "message": "分析完成"})
        else:
            return jsonify({"success": False, "message": "分析失败"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"分析出错: {str(e)}"}), 500


@app.route('/api/trade-sync', methods=['POST'])
def api_trade_sync():
    """接收 Google Sheets 交易日志推送。"""
    is_allowed, message = _check_trade_sync_auth()
    if not is_allowed:
        status_code = 401 if "token" in message.lower() else 403
        return _json_error(message, status_code)

    payload = request.get_json(silent=True)
    if not payload:
        return _json_error("请求体必须是 JSON", 400)

    rows = payload.get("rows")
    if not isinstance(rows, list):
        return _json_error("rows 必须是数组", 400)

    trade_sync_config = _get_trade_sync_config()
    max_sync_rows = int(trade_sync_config.get("max_sync_rows", 20000))
    if len(rows) > max_sync_rows:
        return _json_error(f"rows 超过上限 {max_sync_rows}", 400)

    allowed_spreadsheet_ids = trade_sync_config.get("allowed_spreadsheet_ids", [])
    spreadsheet_id = str(payload.get("spreadsheet_id", "")).strip()
    if allowed_spreadsheet_ids and spreadsheet_id not in allowed_spreadsheet_ids:
        return _json_error("spreadsheet_id 不在允许列表中", 403)

    normalized_rows = normalize_trade_rows(rows)
    if not normalized_rows:
        return _json_error("没有解析出有效交易行", 400)

    result = save_sync_payload(payload, normalized_rows)
    cleanup_summary = run_trade_sync_cleanup(_get_trade_sync_cleanup_config())
    result["cleanup"] = cleanup_summary
    return jsonify(result)


@app.route('/drawdown')
def drawdown_home():
    """Drawdown 控制台页面。"""
    default_symbol = ""
    symbols = list_synced_symbols()
    synced_watchlist_symbols, remaining_watchlist_symbols, watchlist_error = _build_watchlist_overview(symbols)
    if symbols:
        default_symbol = symbols[0]
    return render_template_string(
        DRAWDOWN_TEMPLATE,
        default_symbol=default_symbol,
        synced_watchlist_symbols=synced_watchlist_symbols,
        remaining_watchlist_symbols=remaining_watchlist_symbols,
        watchlist_error=watchlist_error,
        selected_start=(request.args.get("start") or "").strip(),
        selected_end=(request.args.get("end") or "").strip(),
        symbols=symbols,
    )


@app.route('/drawdown/<symbol>')
def drawdown_symbol(symbol: str):
    """按需生成并返回某只股票的回撤图。"""
    try:
        canonical = canonical_symbol(symbol)
        start_date, end_date = _parse_drawdown_range(
            request.args.get("start"),
            request.args.get("end"),
        )
        force = request.args.get("force", "0") in {"1", "true", "True"}
        html_path, _snapshot = _ensure_drawdown_report(
            canonical,
            start_date=start_date,
            end_date=end_date,
            force=force,
        )
        return html_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        return str(exc), 404
    except ValueError as exc:
        return str(exc), 400
    except Exception as exc:
        return f"生成图表失败: {exc}", 500


@app.route('/api/drawdown/generate', methods=['POST'])
def api_drawdown_generate():
    """手动触发某只股票图表生成。"""
    payload = request.get_json(silent=True) or {}
    symbol = payload.get("symbol", "")
    if not symbol:
        return _json_error("缺少 symbol", 400)

    web_config = config_manager.get_web_config() if config_manager else {}
    configured_password = web_config.get("update_password", "")
    if configured_password:
        if payload.get("password", "") != configured_password:
            return _json_error("密码错误", 401)

    try:
        canonical = canonical_symbol(symbol)
        start_date, end_date = _parse_drawdown_range(
            payload.get("start"),
            payload.get("end"),
        )
        force = bool(payload.get("force", False))
        html_path, snapshot = _ensure_drawdown_report(
            canonical,
            start_date=start_date,
            end_date=end_date,
            force=force,
        )
        return jsonify(
            {
                "success": True,
                "symbol": snapshot["symbol"],
                "url": _build_drawdown_url(snapshot["symbol"], start_date, end_date),
                "report_path": str(html_path),
            }
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    except Exception as exc:
        return _json_error(f"生成图表失败: {exc}", 500)


@app.route('/strategy-lab')
def strategy_lab_page():
    """组合仓位策略实验室。"""
    strategy_config = _get_position_strategy_config()
    lab_config = StrategyLabConfig.from_saved_defaults(strategy_config)
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=365 * 3)
    scorecard_portfolios = [
        {
            "key": item["key"],
            "label": item["label"],
            "short_label": {
                "tsm_100": "TSM",
                "googl_100": "GOOGL",
                "tsla_100": "TSLA",
                "tencent_100": "腾讯",
                "qqq_100": "QQQ",
                "core_50_30_20": "当前组合",
            }.get(str(item["key"]), str(item["label"])),
        }
        for item in SCORECARD_PORTFOLIOS
    ]
    default_scorecard_portfolio_keys = lab_config.selected_scorecard_keys()
    period_overrides = {
        period.key: period
        for period in lab_config.scorecard_periods
    }
    scorecard_periods = []
    for period in SCORECARD_PERIODS:
        override = period_overrides.get(str(period["key"]), {})
        scorecard_periods.append({
            **period,
            "label": str(getattr(override, "label", "") or period["label"]),
            "start": str(getattr(override, "start", "") or ""),
            "end": str(getattr(override, "end", "") or ""),
        })
    default_portfolio = lab_config.portfolio_or_default()
    return render_template_string(
        STRATEGY_LAB_TEMPLATE,
        default_config=lab_config.to_legacy_defaults(),
        default_portfolio=default_portfolio,
        buy_strategy_labels=STRATEGY_LABELS,
        sell_strategy_labels=SELL_STRATEGY_LABELS,
        scorecard_portfolios=scorecard_portfolios,
        scorecard_portfolio_labels={item["key"]: item["short_label"] for item in scorecard_portfolios},
        scorecard_periods=scorecard_periods,
        default_scorecard_portfolio_keys=default_scorecard_portfolio_keys,
        default_start=start_date.isoformat(),
        default_end=end_date.isoformat(),
    )


@app.route('/demo/strategy-lab')
def strategy_lab_demo_page():
    """策略实验室视觉 demo。"""
    demo_dir = Path(__file__).parent.parent / "demo"
    return send_from_directory(demo_dir, "strategy-lab-demo.html")


@app.route('/api/strategy-lab/defaults', methods=['POST'])
def api_strategy_lab_defaults():
    """保存仓位策略实验室默认参数。"""
    global config_manager
    if config_manager is None:
        config_manager = ConfigManager()
    payload = request.get_json(silent=True) or {}
    web_config = config_manager.get_web_config()
    configured_password = web_config.get("update_password", "")
    if configured_password and payload.get("password", "") != configured_password:
        return _json_error("密码错误", 401)
    try:
        defaults_payload = payload.get("defaults")
        if not isinstance(defaults_payload, dict):
            return _json_error("缺少 defaults", 400)
        values = StrategyLabConfig.from_defaults_payload(
            defaults_payload,
            _get_position_strategy_config(),
        ).to_legacy_defaults()
        if not config_manager.update_position_strategy_config(values):
            return _json_error("保存默认值失败", 500)
        return jsonify({
            "success": True,
            "message": "默认值已保存",
            "config": config_manager.get_position_strategy_config(),
        })
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _json_error(f"保存默认值失败: {exc}", 500)


def _score_target_max_drawdown_by_symbol(targets: object) -> dict[str, float]:
    if not isinstance(targets, list):
        return {}
    result: dict[str, float] = {}
    for raw in targets:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        raw_value = raw.get("max_drawdown_pct")
        if raw_value in (None, ""):
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and 0 < value <= 100:
            result[normalize_longbridge_symbol(symbol)] = value
    return result


def _apply_score_target_max_drawdowns(targets: list[dict[str, object]], max_drawdowns: dict[str, float]) -> list[dict[str, object]]:
    if not max_drawdowns:
        return targets
    applied: list[dict[str, object]] = []
    for target in targets:
        item = dict(target)
        symbol = normalize_longbridge_symbol(str(item.get("symbol", "")))
        if symbol in max_drawdowns:
            item["max_drawdown_pct"] = max_drawdowns[symbol]
        applied.append(item)
    return applied


def _run_strategy_lab_payload(payload: dict[str, object]) -> dict[str, object]:
    start_date, end_date = parse_date_range(payload.get("start"), payload.get("end"))
    lab_config = StrategyLabConfig.from_runtime_payload(payload, _get_position_strategy_config())
    targets = lab_config.portfolio_or_default()
    if not isinstance(targets, list):
        raise ValueError("targets 必须是数组")
    buy_strategies = payload.get("buy_strategies") or list(STRATEGY_LABELS)
    sell_strategies = payload.get("sell_strategies") or list(SELL_STRATEGY_LABELS)
    if not isinstance(buy_strategies, list) or not isinstance(sell_strategies, list):
        raise ValueError("buy_strategies 和 sell_strategies 必须是数组")
    result = run_longbridge_strategy_lab(
        targets,
        lab_config.to_strategy_inputs(),
        start_date,
        end_date,
        buy_strategies=buy_strategies,
        sell_strategies=sell_strategies,
    )
    option_settings = lab_config.option_settings()
    if option_settings.enabled:
        polygon_config = config_manager.get_polygon_config() if config_manager else {}
        result = apply_option_overlay(
            result,
            api_key=polygon_config.get("api_key", ""),
            settings=option_settings,
        )
    else:
        result["option_overlay"] = {"enabled": False}
    return result


def _run_strategy_score_payload(payload: dict[str, object]) -> dict[str, object]:
    end_date = date.fromisoformat(payload.get("end")) if payload.get("end") else datetime.now().date()
    lab_config = StrategyLabConfig.from_runtime_payload(payload, _get_position_strategy_config())
    targets = lab_config.portfolio_or_default()
    if targets is not None and not isinstance(targets, list):
        raise ValueError("targets 必须是数组")
    return_weight, drawdown_weight = lab_config.score_weights()
    return run_longbridge_strategy_scorecard(
        lab_config.to_strategy_inputs(),
        end_date=end_date,
        core_targets=targets,
        portfolio_keys=payload.get("scorecard_portfolio_keys"),
        scorecard_periods=payload.get("scorecard_periods"),
        return_weight=return_weight,
        drawdown_weight=drawdown_weight,
    )


def _run_strategy_scan_payload(payload: dict[str, object]) -> dict[str, object]:
    start_date, end_date = parse_date_range(payload.get("start"), payload.get("end"))
    lab_config = StrategyLabConfig.from_runtime_payload(payload, _get_position_strategy_config())
    targets = lab_config.portfolio_or_default()
    if not isinstance(targets, list):
        raise ValueError("targets 必须是数组")
    return run_longbridge_sell_parameter_scan(
        targets,
        lab_config.to_strategy_inputs(),
        start_date,
        end_date,
        buy_strategy=lab_config.scan_buy_strategy,
        sell_min_profit_values=payload.get("sell_min_profit_values") or [5, 10, 15, 20, 25],
        repair_cooldown_values=payload.get("repair_sell_cooldown_values") or [0, 15, 30, 45, 60],
        repair_stage_sell_values=payload.get("repair_stage_sell_values") or [8, 12, 16, 20, 25],
        trading_days=lab_config.scan_period_trading_days,
    )


def _run_strategy_robust_payload(payload: dict[str, object]) -> dict[str, object]:
    end_date = date.fromisoformat(payload.get("end")) if payload.get("end") else datetime.now().date()
    lab_config = StrategyLabConfig.from_runtime_payload(payload, _get_position_strategy_config())
    targets = lab_config.portfolio_or_default()
    if targets is not None and not isinstance(targets, list):
        raise ValueError("targets 必须是数组")
    top_n = int(payload.get("top_n") or 10)
    return run_longbridge_robust_leaderboard(
        lab_config.to_strategy_inputs(),
        end_date=end_date,
        core_targets=targets,
        portfolio_keys=payload.get("scorecard_portfolio_keys"),
        scorecard_periods=payload.get("scorecard_periods"),
        buy_strategies=payload.get("buy_strategies"),
        top_n=top_n,
    )


def _strategy_lab_job_snapshot(job: dict[str, object]) -> dict[str, object]:
    snapshot = {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "stage": job.get("stage", ""),
        "message": job.get("message", ""),
        "progress": job.get("progress", 0),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }
    if job.get("status") == "succeeded":
        snapshot["data"] = job.get("data")
        if job.get("run_snapshot"):
            snapshot["run_snapshot"] = job.get("run_snapshot")
    if job.get("status") == "failed":
        snapshot["error"] = job.get("error", "任务失败")
    return snapshot


def _update_strategy_lab_job(job_id: str, **updates: object) -> None:
    with strategy_lab_jobs_lock:
        job = strategy_lab_jobs.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()


def _cleanup_strategy_lab_jobs(now: float | None = None) -> None:
    now = now or time.time()
    with strategy_lab_jobs_lock:
        stale_ids = [
            job_id
            for job_id, job in strategy_lab_jobs.items()
            if now - float(job.get("created_monotonic", now)) > STRATEGY_LAB_JOB_TTL_SECONDS
        ]
        for job_id in stale_ids:
            strategy_lab_jobs.pop(job_id, None)


def _run_strategy_lab_job(job_id: str, runner: Callable[[dict[str, object]], dict[str, object]]) -> None:
    _update_strategy_lab_job(
        job_id,
        status="running",
        stage="cache",
        progress=10,
        message="检查本机日线缓存，缺口会请求 Longbridge。",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        with strategy_lab_jobs_lock:
            job = strategy_lab_jobs[job_id]
            payload = dict(job.get("payload") or {})
        _update_strategy_lab_job(
            job_id,
            stage="market_data",
            progress=35,
            message="准备行情数据；腾讯云网络较慢时会优先使用已有缓存。",
        )
        result = runner(payload)
        with strategy_lab_jobs_lock:
            job = strategy_lab_jobs[job_id]
            kind = str(job.get("kind", ""))
        run_snapshot = None
        try:
            run_snapshot = save_run_snapshot(kind, payload, result, job_id=job_id)
        except Exception as snapshot_exc:
            run_snapshot = {"error": f"运行历史保存失败: {snapshot_exc}"}
        _update_strategy_lab_job(
            job_id,
            status="succeeded",
            stage="completed",
            progress=100,
            message="任务完成。",
            data=result,
            run_snapshot=run_snapshot,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        _update_strategy_lab_job(
            job_id,
            status="failed",
            stage="failed",
            progress=100,
            message="任务失败。",
            error=str(exc),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )


def _create_strategy_lab_job(kind: str, payload: dict[str, object]) -> dict[str, object]:
    runners: dict[str, Callable[[dict[str, object]], dict[str, object]]] = {
        "run": _run_strategy_lab_payload,
        "score": _run_strategy_score_payload,
        "scan": _run_strategy_scan_payload,
        "robust": _run_strategy_robust_payload,
    }
    if kind not in runners:
        raise ValueError("未知 strategy-lab job 类型。")
    _cleanup_strategy_lab_jobs()
    now_iso = datetime.now(timezone.utc).isoformat()
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "kind": kind,
        "status": "queued",
        "stage": "queued",
        "message": "任务已排队。",
        "progress": 0,
        "payload": dict(payload),
        "created_at": now_iso,
        "updated_at": now_iso,
        "created_monotonic": time.time(),
    }
    with strategy_lab_jobs_lock:
        strategy_lab_jobs[job_id] = job
    thread = threading.Thread(
        target=_run_strategy_lab_job,
        args=(job_id, runners[kind]),
        daemon=True,
        name=f"strategy-lab-{kind}-{job_id[:8]}",
    )
    thread.start()
    return _strategy_lab_job_snapshot(job)


@app.route('/api/strategy-lab/jobs', methods=['POST'])
def api_strategy_lab_jobs():
    payload = request.get_json(silent=True) or {}
    try:
        kind = str(payload.get("kind", "")).strip()
        job_payload = payload.get("payload")
        if not isinstance(job_payload, dict):
            return _json_error("缺少 job payload", 400)
        job = _create_strategy_lab_job(kind, job_payload)
        return jsonify({"success": True, "job": job}), 202
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _json_error(f"创建任务失败: {exc}", 500)


@app.route('/api/strategy-lab/jobs/<job_id>', methods=['GET'])
def api_strategy_lab_job_status(job_id: str):
    _cleanup_strategy_lab_jobs()
    with strategy_lab_jobs_lock:
        job = strategy_lab_jobs.get(job_id)
        if not job:
            return _json_error("任务不存在或已过期", 404)
        snapshot = _strategy_lab_job_snapshot(job)
    return jsonify({"success": True, "job": snapshot})


@app.route('/api/strategy-lab/runs', methods=['GET'])
def api_strategy_lab_runs():
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    kind = request.args.get("kind")
    if kind not in {None, "", "run", "score", "scan", "robust"}:
        return _json_error("未知运行记录类型", 400)
    return jsonify({
        "success": True,
        "runs": list_run_snapshots(limit=limit, kind=kind or None),
    })


@app.route('/api/strategy-lab/runs/<run_id>', methods=['GET'])
def api_strategy_lab_run_snapshot(run_id: str):
    snapshot = load_run_snapshot(run_id)
    if not snapshot:
        return _json_error("运行记录不存在", 404)
    return jsonify({"success": True, "run": snapshot})


@app.route('/api/strategy-lab/runs/<run_id>', methods=['DELETE'])
def api_strategy_lab_delete_run_snapshot(run_id: str):
    if not delete_run_snapshot(run_id):
        return _json_error("运行记录不存在", 404)
    return jsonify({"success": True})


@app.route('/api/strategy-lab/presets', methods=['GET', 'POST'])
def api_strategy_lab_presets():
    if request.method == 'GET':
        try:
            limit = int(request.args.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        return jsonify({
            "success": True,
            "presets": list_experiment_presets(limit=limit),
        })
    payload = request.get_json(silent=True) or {}
    try:
        config_payload = payload.get("payload")
        if not isinstance(config_payload, dict):
            return _json_error("缺少预设参数", 400)
        preset = save_experiment_preset(str(payload.get("name", "")), config_payload)
        return jsonify({"success": True, "preset": preset}), 201
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _json_error(f"保存预设失败: {exc}", 500)


@app.route('/api/strategy-lab/presets/<preset_id>', methods=['GET'])
def api_strategy_lab_preset(preset_id: str):
    preset = load_experiment_preset(preset_id)
    if not preset:
        return _json_error("参数预设不存在", 404)
    return jsonify({"success": True, "preset": preset})


@app.route('/api/strategy-lab/presets/<preset_id>', methods=['DELETE'])
def api_strategy_lab_delete_preset(preset_id: str):
    if not delete_experiment_preset(preset_id):
        return _json_error("参数预设不存在", 404)
    return jsonify({"success": True})


@app.route('/api/strategy-lab/run', methods=['POST'])
def api_strategy_lab_run():
    """运行三策略组合实时演算。"""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify({"success": True, "data": _run_strategy_lab_payload(payload)})
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _json_error(f"策略演算失败: {exc}", 500)


@app.route('/api/strategy-lab/score', methods=['POST'])
def api_strategy_lab_score():
    """运行固定题目的策略评分。"""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify({"success": True, "data": _run_strategy_score_payload(payload)})
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _json_error(f"策略评分失败: {exc}", 500)


@app.route('/api/strategy-lab/sell-scan', methods=['POST'])
def api_strategy_lab_sell_scan():
    """扫描当前组合下阶梯修复卖出的参数敏感性。"""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify({"success": True, "data": _run_strategy_scan_payload(payload)})
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _json_error(f"卖出参数扫描失败: {exc}", 500)


@app.route('/api/strategy-lab/score/detail', methods=['POST'])
def api_strategy_lab_score_detail():
    """加载某个评分题目的单个策略详情。"""
    payload = request.get_json(silent=True) or {}
    try:
        question_key = str(payload.get("question_key", "")).strip()
        buy_strategy = str(payload.get("buy_strategy", "")).strip()
        sell_strategy = str(payload.get("sell_strategy", "")).strip()
        if buy_strategy not in STRATEGY_LABELS:
            return _json_error("未知买入策略", 400)
        if sell_strategy not in SELL_STRATEGY_LABELS:
            return _json_error("未知卖出策略", 400)

        portfolio_by_key = {str(item["key"]): item for item in SCORECARD_PORTFOLIOS}
        period_by_key = {str(item["key"]): item for item in SCORECARD_PERIODS}
        custom_periods = payload.get("scorecard_periods")
        if custom_periods is not None and not isinstance(custom_periods, list):
            return _json_error("scorecard_periods 必须是数组", 400)
        period_overrides = {
            str(item.get("key", "")): item
            for item in (custom_periods or [])
            if isinstance(item, dict)
        }
        lab_config = StrategyLabConfig.from_runtime_payload(payload, _get_position_strategy_config())
        custom_targets = lab_config.portfolio_or_default()
        if custom_targets is not None and not isinstance(custom_targets, list):
            return _json_error("targets 必须是数组", 400)
        target_max_drawdowns = _score_target_max_drawdown_by_symbol(custom_targets)
        selected_portfolio = None
        selected_period = None
        for portfolio_key, portfolio in portfolio_by_key.items():
            for period_key, period in period_by_key.items():
                if question_key == f"{portfolio_key}__{period_key}":
                    selected_portfolio = portfolio
                    selected_period = period
                    break
            if selected_portfolio:
                break
        if not selected_portfolio or not selected_period:
            return _json_error("未知评分题目", 400)
        if selected_portfolio["key"] == "core_50_30_20" and custom_targets:
            selected_portfolio = {
                **selected_portfolio,
                "label": "当前组合",
                "targets": custom_targets,
            }
        else:
            selected_portfolio = {
                **selected_portfolio,
                "targets": _apply_score_target_max_drawdowns(
                    list(selected_portfolio["targets"]),
                    target_max_drawdowns,
                ),
            }

        end_date = date.fromisoformat(payload.get("end")) if payload.get("end") else datetime.now().date()
        period_override = period_overrides.get(str(selected_period["key"]), {})
        selected_period = {
            **selected_period,
            "label": str(period_override.get("label") or selected_period["label"]),
        }
        start_raw = period_override.get("start") or period_override.get("start_date")
        end_raw = period_override.get("end") or period_override.get("end_date")
        exact_start_date = date.fromisoformat(str(start_raw)) if start_raw else None
        exact_end_date = date.fromisoformat(str(end_raw)) if end_raw else end_date
        if exact_start_date and exact_start_date > exact_end_date:
            return _json_error("评分详情周期开始日期不能晚于结束日期", 400)
        start_date = exact_start_date or (exact_end_date - timedelta(days=int(selected_period["fetch_days"])))
        result = run_longbridge_strategy_lab(
            selected_portfolio["targets"],
            lab_config.to_strategy_inputs(),
            start_date,
            exact_end_date,
            buy_strategies=[buy_strategy],
            sell_strategies=[sell_strategy],
            trading_days=None if exact_start_date else int(selected_period["trading_days"]),
        )
        result["scorecard_detail"] = {
            "question_key": question_key,
            "portfolio_key": selected_portfolio["key"],
            "portfolio_label": selected_portfolio["label"],
            "period_key": selected_period["key"],
            "period_label": selected_period["label"],
        }
        result["option_overlay"] = {"enabled": False}
        return jsonify({"success": True, "data": result})
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _json_error(f"评分详情加载失败: {exc}", 500)


@app.route('/history')
def history():
    """历史报告列表页面"""
    report_dir = Path(__file__).parent.parent / "report"
    reports = []
    
    if report_dir.exists():
        # 获取所有HTML报告文件
        html_files = sorted(report_dir.glob("boll_report_*.html"), reverse=True)
        
        for html_file in html_files:
            # 从文件名解析时间信息
            # 文件名格式: boll_report_YYYYMMDD_HHMMSS.html
            filename = html_file.name
            try:
                # 提取时间部分
                time_str = filename.replace("boll_report_", "").replace(".html", "")
                date_part = time_str[:8]  # YYYYMMDD
                time_part = time_str[9:]  # HHMMSS
                
                # 格式化时间显示
                formatted_time = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]} {time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}"
                
                # 获取文件修改时间作为备用
                mtime = datetime.fromtimestamp(html_file.stat().st_mtime)
                
                reports.append({
                    "filename": filename,
                    "name": f"BOLL指标筛选报告 - {formatted_time}",
                    "time": formatted_time,
                    "mtime": mtime
                })
            except Exception:
                # 如果解析失败，使用文件修改时间
                mtime = datetime.fromtimestamp(html_file.stat().st_mtime)
                reports.append({
                    "filename": filename,
                    "name": f"BOLL指标筛选报告 - {mtime.strftime('%Y-%m-%d %H:%M:%S')}",
                    "time": mtime.strftime('%Y-%m-%d %H:%M:%S'),
                    "mtime": mtime
                })
    
    return render_template_string(HISTORY_TEMPLATE, reports=reports)


@app.route('/report/<filename>')
def view_report(filename):
    """查看单个历史报告"""
    report_dir = Path(__file__).parent.parent / "report"
    report_file = report_dir / filename
    
    # 安全检查：确保文件在report目录内，防止路径遍历攻击
    try:
        report_file.resolve().relative_to(report_dir.resolve())
    except ValueError:
        return "无效的报告文件", 404
    
    if not report_file.exists() or not filename.endswith('.html'):
        return "报告文件不存在", 404
    
    # 读取并返回HTML内容
    try:
        with open(report_file, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except Exception as e:
        return f"读取报告失败: {str(e)}", 500


def _get_scheduler():
    """获取scheduler实例"""
    try:
        from scheduler import get_scheduler
        return get_scheduler()
    except ImportError:
        return None

@app.route('/schedule')
def schedule_page():
    """定时任务配置页面"""
    global config_manager
    
    if config_manager is None:
        config_manager = ConfigManager()
    
    schedule_config = config_manager.get_schedule_config()
    current_hour = schedule_config.get("hour", 23)
    current_minute = schedule_config.get("minute", 0)
    
    # 获取下次运行时间
    next_run_time = "未知"
    scheduler = _get_scheduler()
    if scheduler:
        next_run = scheduler.get_next_run_time()
        if next_run:
            import pytz
            beijing_tz = pytz.timezone('Asia/Shanghai')
            if next_run.tzinfo:
                next_run_beijing = next_run.astimezone(beijing_tz)
            else:
                next_run_beijing = beijing_tz.localize(next_run)
            next_run_time = next_run_beijing.strftime('%Y-%m-%d %H:%M:%S')
    
    return render_template_string(
        SCHEDULE_TEMPLATE,
        current_hour=current_hour,
        current_minute=current_minute,
        next_run_time=next_run_time
    )


@app.route('/api/schedule', methods=['GET', 'POST'])
def api_schedule():
    """获取或更新定时任务配置"""
    global config_manager
    
    if config_manager is None:
        config_manager = ConfigManager()
    
    if request.method == 'GET':
        # 获取当前配置
        schedule_config = config_manager.get_schedule_config()
        scheduler = _get_scheduler()
        
        next_run_time = None
        if scheduler:
            next_run = scheduler.get_next_run_time()
            if next_run:
                import pytz
                beijing_tz = pytz.timezone('Asia/Shanghai')
                if next_run.tzinfo:
                    next_run_beijing = next_run.astimezone(beijing_tz)
                else:
                    next_run_beijing = beijing_tz.localize(next_run)
                next_run_time = next_run_beijing.strftime('%Y-%m-%d %H:%M:%S')
        
        return jsonify({
            "success": True,
            "config": schedule_config,
            "next_run_time": next_run_time
        })
    
    elif request.method == 'POST':
        # 更新配置
        data = request.get_json()
        hour = data.get("hour")
        minute = data.get("minute")
        
        if hour is None or minute is None:
            return jsonify({"success": False, "message": "缺少必要参数"}), 400
        
        if not (0 <= hour <= 23):
            return jsonify({"success": False, "message": "小时必须在0-23之间"}), 400
        
        if not (0 <= minute <= 59):
            return jsonify({"success": False, "message": "分钟必须在0-59之间"}), 400
        
        # 更新调度器
        scheduler = _get_scheduler()
        if scheduler:
            if scheduler.update_schedule(hour, minute):
                # 获取更新后的下次运行时间
                next_run = scheduler.get_next_run_time()
                next_run_time = None
                if next_run:
                    import pytz
                    beijing_tz = pytz.timezone('Asia/Shanghai')
                    if next_run.tzinfo:
                        next_run_beijing = next_run.astimezone(beijing_tz)
                    else:
                        next_run_beijing = beijing_tz.localize(next_run)
                    next_run_time = next_run_beijing.strftime('%Y-%m-%d %H:%M:%S')
                
                return jsonify({
                    "success": True,
                    "message": "定时任务配置已更新",
                    "next_run_time": next_run_time
                })
            else:
                return jsonify({"success": False, "message": "更新定时任务失败"}), 500
        else:
            return jsonify({"success": False, "message": "调度器未初始化"}), 500


@app.route('/api/option-quote', methods=['POST'])
def api_option_quote():
    """
    期权价格查询接口（需要密码验证）

    请求格式：
    {
        "password": "密码",
        "symbol": "期权代码"
    }

    返回格式：
    {
        "success": true,
        "data": {
            "symbol": "期权代码",
            "last_done": 最新成交价,
            "open": 开盘价,
            "high": 最高价,
            "low": 最低价,
            "volume": 成交量,
            "turnover": 成交额,
            "timestamp": 时间戳
        }
    }
    """
    global config_manager

    if config_manager is None:
        return jsonify({"success": False, "message": "配置管理器未初始化"}), 500

    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "请求数据格式错误"}), 400

        password = data.get("password", "")
        symbol = data.get("symbol", "")

        # 验证密码
        web_config = config_manager.get_web_config()
        api_password = web_config.get("update_password", "")

        if not api_password:
            return jsonify({"success": False, "message": "API密码未配置"}), 500

        if password != api_password:
            return jsonify({"success": False, "message": "密码错误"}), 401

        # 验证期权代码
        if not symbol:
            return jsonify({"success": False, "message": "期权代码不能为空"}), 400

        # 获取Polygon.io配置
        polygon_config = config_manager.get_polygon_config()
        api_key = polygon_config.get("api_key")

        if not api_key:
            return jsonify({"success": False, "message": "Polygon.io API Key未配置"}), 500

        # 查询期权价格
        option_service = OptionQuoteService(api_key)
        quote_data = option_service.get_option_quote(symbol)

        if quote_data:
            return jsonify({
                "success": True,
                "data": quote_data
            })
        else:
            return jsonify({"success": False, "message": f"未找到期权 {symbol} 的报价"}), 404

    except Exception as e:
        return jsonify({"success": False, "message": f"查询失败: {str(e)}"}), 500


if __name__ == '__main__':
    init_app()
    web_config = config_manager.get_web_config()
    app.run(
        host=web_config.get("host", "0.0.0.0"),
        port=web_config.get("port", 5000),
        debug=False
    )
