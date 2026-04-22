"""
Flask Web应用
提供Web界面查看结果、更新token、手动触发分析
"""

import os
import sys
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request, session, redirect, url_for
from typing import Optional
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.config_manager import ConfigManager
from drawdown.generate_drawdown_report import TradeOverlay, render_longbridge_drawdown_from_overlays
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
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .hero-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 20px; }
        .hero-card { display: block; padding: 18px; border-radius: 8px; text-decoration: none; color: white; background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); }
        .hero-card strong { display: block; font-size: 18px; margin-bottom: 6px; }
        .hero-card span { font-size: 14px; opacity: 0.92; }
        .hero-card.drawdown { background: linear-gradient(135deg, #0891b2 0%, #2563eb 100%); }
        .hero-card.portal { background: linear-gradient(135deg, #475569 0%, #0f172a 100%); }
        .btn { padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
        .btn:hover { background: #5568d3; }
        .btn-secondary { background: #6c757d; }
        .btn-secondary:hover { background: #5a6268; }
        .status { padding: 10px; margin: 10px 0; border-radius: 4px; }
        .status.success { background: #d4edda; color: #155724; }
        .status.error { background: #f8d7da; color: #721c24; }
        .status.info { background: #d1ecf1; color: #0c5460; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>BOLL指标筛选系统</h1>
            <div>
                <a href="/history" class="btn btn-secondary">历史报告</a>
                <a href="/drawdown" class="btn btn-secondary">Drawdown</a>
                <a href="/schedule" class="btn btn-secondary">定时任务</a>
                <a href="/update-token" class="btn btn-secondary">更新Token</a>
                <button onclick="triggerAnalysis(false)" class="btn">快速分析（无期权延迟）</button>
                <button onclick="triggerAnalysis(true)" class="btn" style="background: #48bb78;">完整分析（含期权延迟）</button>
            </div>
        </div>
        
        <div id="status"></div>

        <div class="hero-grid">
            <a href="/drawdown" class="hero-card drawdown">
                <strong>Drawdown 图表</strong>
                <span>查看单股票回撤、加仓与卖出图层</span>
            </a>
            <a href="http://aqcloud.ltd" class="hero-card portal" target="_blank">
                <strong>AQCloud 首页</strong>
                <span>返回总站首页查看其他服务入口</span>
            </a>
        </div>
        
        <div id="content">
            {% if result %}
                {{ result_html|safe }}
            {% else %}
                <p>暂无分析结果。点击"手动触发分析"按钮开始分析。</p>
            {% endif %}
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
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 960px; margin: 0 auto; background: white; padding: 24px; border-radius: 8px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .header-actions { display: flex; gap: 10px; flex-wrap: wrap; }
        .btn { padding: 10px 16px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
        .btn:hover { background: #5568d3; }
        .btn-secondary { background: #6c757d; }
        .btn-secondary:hover { background: #5a6268; }
        .panel { background: #fafafa; border: 1px solid #ddd; border-radius: 6px; padding: 16px; margin-bottom: 18px; }
        .field-row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
        .field-row input[type="text"] { flex: 1; min-width: 180px; padding: 10px; border: 1px solid #ccc; border-radius: 4px; }
        .field-row input[type="date"] { padding: 10px; border: 1px solid #ccc; border-radius: 4px; min-width: 160px; }
        .preset-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
        .preset-btn { padding: 8px 12px; background: #e2e8f0; color: #334155; border: none; border-radius: 999px; cursor: pointer; }
        .preset-btn:hover { background: #cbd5e1; }
        .symbols { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
        .symbol-chip { display: inline-block; padding: 8px 10px; background: #eef2ff; color: #334155; border-radius: 999px; text-decoration: none; cursor: pointer; }
        .symbol-chip:hover { filter: brightness(0.97); }
        .symbol-chip.secondary { background: #ecfeff; color: #155e75; }
        .symbol-chip.muted { background: #f1f5f9; color: #475569; }
        .panel-title { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
        .panel-title strong { font-size: 16px; }
        .panel-count { color: #64748b; font-size: 13px; }
        .hint { color: #666; font-size: 14px; margin-top: 8px; }
        .empty { color: #999; padding: 12px 0; }
        .error { color: #b91c1c; font-size: 14px; padding: 12px 0; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Drawdown</h1>
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
                    <button type="submit" class="btn">打开图表</button>
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
                <div class="hint">这里展示“Longbridge 自选列表”和“已同步交易股票”的交集，方便区分你关注且已经实际交易过的标的。</div>
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
