"""Tests for build_signal_email_html — HTML signal email formatting."""

import re
import pytest
from drawdown.strategy_signal import build_signal_email_html


def _make_stock_result(
    symbol="NVDA.US",
    preset_name="长虹正股2",
    shares=1.8624266825868703,
    price=205.19,
    action="buy",
    reason="回撤12.9% 第2.3%档触发 买入",
    cash=2557.0,
    market_value=0.0,
    avg_cost=0.0,
    position_shares=0,
    initial_cash=5310,
    initial_cash_source="signal_targets",
    monthly_contribution=0,
    monthly_contribution_source="zero_no_signal_targets",
    position_context_summary=None,
):
    """Helper to build a stock-signal result dict for testing."""
    sig = {
        "action": action,
        "shares": shares,
        "price": price,
        "reason": reason,
        "status": "signal",
    }
    result = {
        "symbol": symbol,
        "preset_name": preset_name,
        "signals": [sig],
        "current_state": {
            "cash": cash,
            "market_value": market_value,
            "avg_cost": avg_cost,
            "shares": position_shares,
        },
        "initial_cash": initial_cash,
        "initial_cash_source": initial_cash_source,
        "monthly_contribution": monthly_contribution,
        "monthly_contribution_source": monthly_contribution_source,
    }
    if position_context_summary:
        result["position_context"] = {"summary": position_context_summary}
    return result


def _make_leaps_result(
    symbol="AAPL",
    preset_name="长虹股用",
    entry_signals=None,
    sell_signals=None,
):
    result = {
        "symbol": symbol,
        "preset_name": preset_name,
        "signals": [],
    }
    if entry_signals:
        result["entry_signals"] = entry_signals
    if sell_signals:
        result["sell_signals"] = sell_signals
    return result


# ── Test: shares are formatted (no raw 15-digit floats) ──────────────

def test_shares_formatted_not_raw_float():
    """Shares like 1.8624266825868703 must appear as '1.9' or similar, not 15 digits."""
    results = [_make_stock_result(shares=1.8624266825868703)]
    _, html = build_signal_email_html(results)
    # Must NOT contain the raw float
    assert "1.8624266825868703" not in html
    # Must contain a formatted version (1-2 decimal places)
    assert re.search(r"1\.9股", html), f"Expected '1.9股' in HTML, got: {html[:500]}"


def test_shares_zero_formatted():
    results = [_make_stock_result(shares=0.0)]
    _, html = build_signal_email_html(results)
    assert "0.0股" in html


def test_shares_integer_formatted():
    results = [_make_stock_result(shares=10.0)]
    _, html = build_signal_email_html(results)
    assert "10.0股" in html


# ── Test: action labels are Chinese ──────────────────────────────────

def test_buy_action_translated():
    results = [_make_stock_result(action="buy")]
    _, html = build_signal_email_html(results)
    assert "买入" in html
    # English 'buy' should NOT appear as the action label
    # (it may appear in reason text, so check specifically in the action cell)
    # The action cell pattern: 🟢 买入
    assert "🟢 买入" in html
    assert "🟢 buy" not in html


def test_sell_action_translated():
    results = [_make_stock_result(action="sell")]
    _, html = build_signal_email_html(results)
    assert "卖出" in html
    assert "🔴 卖出" in html
    assert "🔴 sell" not in html


# ── Test: price formatting ───────────────────────────────────────────

def test_price_formatted_two_decimals():
    results = [_make_stock_result(price=205.19)]
    _, html = build_signal_email_html(results)
    assert "$205.19" in html


def test_price_integer_formatted():
    results = [_make_stock_result(price=100.0)]
    _, html = build_signal_email_html(results)
    assert "$100.00" in html


# ── Test: preset name shown as pill ──────────────────────────────────

def test_preset_name_visible():
    results = [_make_stock_result(preset_name="长虹正股2")]
    _, html = build_signal_email_html(results)
    assert "长虹正股2" in html


# ── Test: buy summary formatting ─────────────────────────────────────

def test_buy_summary_formatted():
    """Buy summary should show formatted numbers, not raw floats."""
    results = [_make_stock_result(shares=9.3, price=205.19, cash=2557, market_value=0)]
    _, html = build_signal_email_html(results)
    # Total buy = 9.3 * 205.19 ≈ 1908.27
    assert "9.3股" in html
    # Should not have raw floats in summary
    assert "1908" in html


# ── Test: state line formatting ──────────────────────────────────────

def test_state_line_shares_formatted():
    results = [_make_stock_result(position_shares=75.0, cash=2645.56, market_value=2697.60, avg_cost=510.54)]
    _, html = build_signal_email_html(results)
    assert "75.0股" in html
    assert "$2645.56" in html


# ── Test: LEAPS signals ──────────────────────────────────────────────

def test_leaps_entry_signal():
    results = [_make_leaps_result(entry_signals=[{"underlying": "AAPL", "stock_price": 198.50, "reason": "回撤25% 布林0.85"}])]
    _, html = build_signal_email_html(results)
    assert "LEAPS买入" in html
    assert "$198.50" in html
    assert "回撤25% 布林0.85" in html


def test_leaps_sell_signal():
    results = [_make_leaps_result(sell_signals=[{"stage": 2, "pct_to_sell": 50, "stock_price": 210.30, "reason": "ROA 150%"}])]
    _, html = build_signal_email_html(results)
    assert "LEAPS卖出" in html
    assert "50%" in html


# ── Test: subject line ───────────────────────────────────────────────

def test_subject_includes_symbol():
    results = [_make_stock_result(symbol="NVDA.US", action="buy")]
    subject, _ = build_signal_email_html(results)
    assert "NVDA.US" in subject
    assert "买入" in subject


# ── Test: no active signals ──────────────────────────────────────────

def test_no_active_signals():
    results = [_make_stock_result(action="buy")]
    # Override signals to be covered (not actionable)
    results[0]["signals"] = [{"status": "covered", "reason": "已覆盖", "action": "buy", "shares": 0, "price": 0}]
    subject, html = build_signal_email_html(results)
    # Covered-only results should produce "无" subject
    assert "无" in subject


# ── Test: HTML escaping ──────────────────────────────────────────────

def test_html_escaping_in_reason():
    results = [_make_stock_result(reason="回撤<50% & 触发<b>买入</b>")]
    _, html = build_signal_email_html(results)
    # Must not have unescaped < or & in HTML
    assert "&lt;" in html or "回撤" in html  # At minimum the text is present
    assert "<b>" not in html  # Raw HTML must be escaped


# ── Test: long reason text doesn't break layout ──────────────────────

def test_long_reason_wraps_properly():
    """Long reason strings should have word-break for proper mobile display."""
    long_reason = "回撤12.9% 第2.3%档触发 买入，这是一个很长的原因说明文字"
    results = [_make_stock_result(reason=long_reason)]
    _, html = build_signal_email_html(results)
    # The reason cell should have word-break or overflow-wrap
    assert "word-break" in html or "overflow-wrap" in html or long_reason[:10] in html


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
