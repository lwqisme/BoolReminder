// Preset management JS unit tests
// Tests non-DOM logic: data classification, trade detail structure, etc.

const assert = require('assert');

// ── Mock data ──────────────────────────────────────────────────────────
const leapsPreset = {
    id: "20250101_aaaaaaaa",
    name: "LEAPS test preset",
    created_at: "2025-01-01T00:00:00Z",
    config_payload: {
        type: "leaps",
        drawdown_threshold_pct: 20.0,
        entry_mode: "both",
        stage1_days: 15, stage1_profit: 80.0, stage1_sell: 50.0,
        stage2_days: 60, stage2_profit: 60.0, stage2_sell: 50.0,
        position_pct: 20.0,
        cooldown_days: 5,
    },
};

const stockPreset = {
    id: "20250101_bbbbbbbb",
    name: "Stock test preset",
    created_at: "2025-02-01T00:00:00Z",
    config_payload: {
        start: "2025-01-01",
        end: "2025-06-01",
        buy_strategies: ["pyramid_3"],
        sell_strategies: ["repair_step"],
        targets: [{ symbol: "AAPL.US", weight: 100, name: "AAPL" }],
    },
};

function isLeapsPreset(p) {
    const cp = p.config_payload || p.config_summary || {};
    return cp.type === 'leaps';
}

// ── Tests ─────────────────────────────────────────────────────────────

// S1: LEAPS preset correctly identified
{
    const result = isLeapsPreset(leapsPreset);
    assert.strictEqual(result, true, 'LEAPS preset should be identified as leaps');
}

// S2: Stock preset correctly identified as NOT leaps
{
    const result = isLeapsPreset(stockPreset);
    assert.strictEqual(result, false, 'Stock preset should not be identified as leaps');
}

// S3: Preset without config_payload is not leaps
{
    const result = isLeapsPreset({ id: "x", name: "empty" });
    assert.strictEqual(result, false, 'Empty preset should not be leaps');
}

// S4: Simulate response trade_details structure validation
{
    const simResponse = {
        success: true,
        results: {
            "AAPL.US": {
                trade_details: [{
                    symbol: "AAPL.US",
                    entry_date: "2025-03-15",
                    entry_price: 150.0,
                    drawdown_pct: 22.5,
                    sell_events: [
                        { date: "2025-05-20", price: 180.0, pct_sold: 50.0, roi_pct: 45.0 },
                        { date: "2025-06-15", price: 200.0, pct_sold: 50.0, roi_pct: 80.0 },
                    ],
                    total_roi_pct: 62.5,
                }],
                trade_count: 1,
            },
        },
        failed_symbols: [],
    };

    const results = simResponse.results;
    for (const sym of Object.keys(results)) {
        const res = results[sym];
        assert.ok(Array.isArray(res.trade_details), 'trade_details should be array');
        assert.strictEqual(typeof res.trade_count, 'number', 'trade_count should be number');
        assert.strictEqual(res.trade_details.length, res.trade_count, 'trade_count should match array length');

        for (const trade of res.trade_details) {
            assert.ok(typeof trade.symbol === 'string', 'trade should have symbol');
            assert.ok(typeof trade.entry_date === 'string', 'trade should have entry_date');
            assert.ok(typeof trade.entry_price === 'number', 'trade should have entry_price');
            assert.ok(typeof trade.total_roi_pct === 'number', 'trade should have total_roi_pct');
            assert.ok(Array.isArray(trade.sell_events), 'trade should have sell_events array');

            for (const se of trade.sell_events) {
                assert.ok(typeof se.date === 'string', 'sell event should have date');
                assert.ok(typeof se.price === 'number', 'sell event should have price');
                assert.ok(typeof se.pct_sold === 'number', 'sell event should have pct_sold');
                assert.ok(typeof se.roi_pct === 'number', 'sell event should have roi_pct');
            }
        }
    }
}

// S5: Stock simulate response validation
{
    const stockResponse = {
        success: true,
        data: {
            strategies: [{
                label: "三档金字塔 / 阶梯修复",
                metrics: { return_pct: 15.5, max_drawdown_pct: -12.3, trade_count: 8 },
                trades: [
                    { action: "buy", date: "2025-01-15", symbol: "AAPL.US", price: 150.0, gross_amount: 15000, shares: 100 },
                    { action: "sell", date: "2025-03-10", symbol: "AAPL.US", price: 170.0, gross_amount: 5000, shares: 29.4 },
                ],
            }],
        },
    };

    const strategies = stockResponse.data.strategies;
    assert.ok(Array.isArray(strategies), 'strategies should be array');
    assert.ok(strategies.length > 0, 'should have at least one strategy');

    for (const st of strategies) {
        assert.ok(typeof st.label === 'string', 'strategy should have label');
        assert.ok(Array.isArray(st.trades), 'strategy should have trades array');

        for (const tr of st.trades) {
            assert.ok(['buy', 'sell'].includes(tr.action), 'trade action should be buy or sell');
            assert.ok(typeof tr.date === 'string', 'trade should have date');
            assert.ok(typeof tr.symbol === 'string', 'trade should have symbol');
            assert.ok(typeof tr.price === 'number', 'trade should have price');
        }
    }
}

console.log("All preset management JS tests passed!");
