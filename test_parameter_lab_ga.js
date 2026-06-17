/**
 * Frontend unit tests for Genetic Algorithm JS operations.
 * Run: node test_parameter_lab_ga.js
 * No browser required - pure logic tests.
 */

const assert = require('assert');

// ── Recreate the JS environment (extracted from template) ──

const BUY_PARAMETER_FIELDS = [
    'step_pct', 'equal_slice_allocation_pct', 'core_dip_initial_core_pct', 'core_dip_weekly_core_pct',
    'core_dip_cash_reserve_pct', 'core_dip_start_drawdown_pct', 'core_dip_full_drawdown_pct',
    'core_dip_timing_enabled', 'core_dip_timing_max_delay_days', 'core_dip_timing_rise_threshold_pct',
    'core_dip_timing_near_low_pct',
    'max_drawdown_pct'
];
const SELL_PARAMETER_FIELDS = [
    'sell_min_profit_pct', 'repair_sell_cooldown_days', 'repair_stage_sell_pct',
    'grid_rebound_step_pct', 'grid_sell_pct', 'grid_first_sell_pct', 'grid_second_sell_pct',
    'grid_min_sell_amount', 'cost_first_profit_pct', 'cost_second_profit_pct', 'cost_third_profit_pct',
    'cost_first_sell_pct', 'cost_second_sell_pct', 'cost_third_sell_pct', 'cost_deleverage_cooldown_days',
    'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
    'buy_rearm_mode', 'sell_stage_rearm_drawdown_pct'
];

function formatCompact(value) {
    const n = Number(value || 0);
    if (!Number.isFinite(n)) return '0';
    return Number.isInteger(n) ? String(n) : String(Number(n.toPrecision(6))).replace(/\.0+$/, '');
}
function buildCandidateKey(buyStrategy, sellStrategy, buyParams, sellParams) {
    const parts = [buyStrategy];
    if (buyParams.step_pct !== null && buyParams.step_pct !== undefined) parts.push('step' + formatCompact(buyParams.step_pct));
    if (buyParams.equal_slice_allocation_pct !== null && buyParams.equal_slice_allocation_pct !== undefined) parts.push('alloc' + formatCompact(buyParams.equal_slice_allocation_pct));
    parts.push(sellStrategy);
    if (sellStrategy === 'grid_rebound' || sellStrategy === 'price_rise_grid') {
        if (sellParams.grid_rebound_step_pct !== undefined && sellParams.grid_rebound_step_pct !== null) parts.push('g' + formatCompact(sellParams.grid_rebound_step_pct));
        parts.push('gsell' + formatCompact(sellParams.grid_sell_pct ?? sellParams.grid_second_sell_pct));
        parts.push('gmin' + formatCompact(sellParams.grid_min_sell_amount));
    }
    if (sellParams.sell_min_profit_pct !== undefined && sellParams.sell_min_profit_pct !== null) parts.push('smp' + formatCompact(sellParams.sell_min_profit_pct));
    if (sellParams.dca_rearm_drawdown_pct !== undefined && sellParams.dca_rearm_drawdown_pct !== null) parts.push('rearm' + formatCompact(sellParams.dca_rearm_drawdown_pct));
    if (sellParams.buy_rearm_mode === 'restart_from_rearm') parts.push('rearmmode_restart');
    if (sellParams.buy_rearm_mode === 'cumulative') parts.push('rearmmode_cum');
    if (sellParams.sell_stage_rearm_drawdown_pct !== undefined && sellParams.sell_stage_rearm_drawdown_pct !== null) parts.push('sellrearm' + formatCompact(sellParams.sell_stage_rearm_drawdown_pct));
    if (sellParams.sell_allow_same_day_sell) parts.push('same1');
    return parts.join('__');
}
function gaParamKey(ind) {
    const bp = {}, sp = {};
    for (let fi = 0; fi < BUY_PARAMETER_FIELDS.length; fi++) {
        const f = BUY_PARAMETER_FIELDS[fi];
        if (ind[f] !== undefined && ind[f] !== null) bp[f] = ind[f];
    }
    for (let fi = 0; fi < SELL_PARAMETER_FIELDS.length; fi++) {
        const f = SELL_PARAMETER_FIELDS[fi];
        if (ind[f] !== undefined && ind[f] !== null) sp[f] = ind[f];
    }
    return buildCandidateKey(ind.buy_strategy || '', ind.sell_strategy || '', bp, sp);
}
function gaDedupByDisplayStats(finalRows, skipStatsDedup) {
    if (skipStatsDedup) return finalRows.slice();
    const deduped = [];
    const seen = {};
    for (let ri = 0; ri < finalRows.length; ri++) {
        const item = finalRows[ri];
        const sig = (item.avg_return || 0).toFixed(1) + '|' + (item.avg_drawdown || 0).toFixed(1) + '|' + (item.avg_sell_quality || 0).toFixed(1);
        if (!seen[sig]) { seen[sig] = true; deduped.push(item); }
    }
    return deduped;
}

let gaRandomFn = Math.random;
function mulberry32(seed) {
    let a = seed >>> 0;
    return function() {
        a |= 0; a = (a + 0x6D2B79F5) | 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}
function gaRandom() { return gaRandomFn(); }

function tournamentSelectGa(ranked, tournamentSize) {
    const size = Math.min(tournamentSize, ranked.length);
    let best = ranked[0];
    for (let i = 1; i < size; i++) {
        const idx = Math.floor(gaRandom() * ranked.length);
        if (ranked[idx].fit > best.fit) best = ranked[idx];
    }
    return best.ind;
}

function crossoverGa(p1, p2) {
    const child = { buy_strategy: p1.buy_strategy, sell_strategy: p1.sell_strategy,
        label: p1.label || p2.label, key: p1.key || p2.key };
    for (const k of Object.keys(p1)) {
        if (k === 'key' || k === 'label' || k === 'buy_strategy' || k === 'sell_strategy') continue;
        child[k] = gaRandom() < 0.5 ? p1[k] : p2[k];
    }
    return child;
}

function mutateGa(ind, mutationRate, paramRanges, gaConfig, crossEnabled) {
    const child = { ...ind };
    const buyRanges = paramRanges.buy_ranges || {};
    const sellRanges = paramRanges.sell_ranges || {};
    const bounds = paramRanges.bounds || {};
    const precision = paramRanges.precision || {};
    const intFields = new Set(paramRanges.int_fields || []);
    const continuous = gaConfig.continuous_mutation || false;
    const sigmaRatio = gaConfig.mutation_sigma_ratio || 0.25;

    function gaNumericBounds(field) {
        const b = bounds[field];
        if (!b || b.length < 2) return null;
        const lo = Number(b[0]);
        const hi = Number(b[1]);
        if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
        return lo <= hi ? [lo, hi] : [hi, lo];
    }

    function gaNormalizeBoundedValue(field, value) {
        if (value === null || value === undefined || typeof value === 'boolean' || typeof value === 'string') return value;
        const b = gaNumericBounds(field);
        const n = Number(value);
        if (!b || !Number.isFinite(n)) return value;
        let bounded = Math.max(b[0], Math.min(b[1], n));
        if (intFields.has(field)) {
            bounded = Math.trunc(bounded);
        } else {
            const prec = precision[field] !== undefined ? precision[field] : 2;
            bounded = Number(bounded.toFixed(prec));
        }
        return bounded;
    }

    function gaValueWithinBounds(field, value) {
        if (value === null || value === undefined) return true;
        const b = gaNumericBounds(field);
        if (!b) return true;
        const n = Number(value);
        return Number.isFinite(n) && n >= b[0] && n <= b[1];
    }

    function gaDiscreteValuesWithinBounds(field, values) {
        const b = gaNumericBounds(field);
        if (!b || !(paramRanges._custom_bounds_fields || []).includes(field)) return values;
        const seen = new Set();
        const out = [];
        function add(value) {
            if (!gaValueWithinBounds(field, value)) return;
            const normalized = gaNormalizeBoundedValue(field, value);
            const key = normalized === null || normalized === undefined ? 'null' : String(normalized);
            if (!seen.has(key)) {
                seen.add(key);
                out.push(normalized);
            }
        }
        (values || []).forEach(add);
        add(b[0]);
        add(b[1]);
        return out;
    }

    function enforceGaIndividualBounds(child) {
        for (const field of BUY_PARAMETER_FIELDS.concat(SELL_PARAMETER_FIELDS)) {
            if (!Object.prototype.hasOwnProperty.call(child, field)) continue;
            child[field] = gaNormalizeBoundedValue(field, child[field]);
        }
        return child;
    }

    function gaussMutate(field, currentVal) {
        const b = bounds[field];
        if (!b || b.length < 2) return currentVal;
        const lo = Number(b[0]), hi = Number(b[1]);
        const old = Number(currentVal || (lo + hi) / 2);
        const sigma = Math.max(Math.abs(old), 0.01) * sigmaRatio;
        let val = old + (gaRandom() + gaRandom() + gaRandom() + gaRandom() - 2) * sigma * 1.5;
        val = Math.max(lo, Math.min(hi, val));
        const prec = precision[field] !== undefined ? precision[field] : 2;
        val = Number(val.toFixed(prec));
        if (intFields.has(field)) val = Math.trunc(val);
        return val;
    }

    for (const field of BUY_PARAMETER_FIELDS) {
        if (gaRandom() < mutationRate) {
            if (continuous && bounds[field]) {
                child[field] = gaussMutate(field, child[field]);
            } else if (buyRanges[field]) {
                const vals = gaDiscreteValuesWithinBounds(field, buyRanges[field]);
                child[field] = vals[Math.floor(gaRandom() * vals.length)];
                if (intFields.has(field)) child[field] = Math.trunc(Number(child[field]));
            }
        }
    }
    for (const field of SELL_PARAMETER_FIELDS) {
        if (gaRandom() < mutationRate) {
            if (continuous && bounds[field]) {
                child[field] = gaussMutate(field, child[field]);
            } else if (sellRanges[field]) {
                const vals = gaDiscreteValuesWithinBounds(field, sellRanges[field]);
                child[field] = vals[Math.floor(gaRandom() * vals.length)];
                if (intFields.has(field)) child[field] = Math.trunc(Number(child[field]));
            } else if (field === 'sell_allow_same_day_sell') {
                if (gaConfig.sell_allow_same_day_sell === 'true' || gaConfig.sell_allow_same_day_sell === 'false') {
                    child[field] = gaConfig.sell_allow_same_day_sell === 'true';
                } else {
                    child[field] = gaRandom() < 0.5;
                }
            }
        }
    }

    if (crossEnabled && gaConfig.cross_strategy && gaRandom() < (gaConfig.strategy_mutation_rate || 0.05)) {
        const buyStrats = (Array.isArray(gaConfig.ga_buy_strategies) && gaConfig.ga_buy_strategies.length)
            ? gaConfig.ga_buy_strategies
            : Object.keys(paramRanges.buy_fields || { buy_a: [] });
        const sellStrats = (Array.isArray(gaConfig.ga_sell_strategies) && gaConfig.ga_sell_strategies.length)
            ? gaConfig.ga_sell_strategies
            : Object.keys(paramRanges.sell_fields || { sell_x: [] });
        child.buy_strategy = buyStrats[Math.floor(gaRandom() * buyStrats.length)];
        child.sell_strategy = sellStrats[Math.floor(gaRandom() * sellStrats.length)];
    }
    if (child.core_dip_start_drawdown_pct !== undefined && child.core_dip_full_drawdown_pct !== undefined) {
        if (Number(child.core_dip_start_drawdown_pct) > Number(child.core_dip_full_drawdown_pct)) {
            child.core_dip_full_drawdown_pct = child.core_dip_start_drawdown_pct;
        }
    }
    enforceGaIndividualBounds(child);
    child.key = 'test_' + Date.now().toString(36) + '_' + gaRandom().toString(36).slice(2, 6);
    child.label = null;
    return child;
}

function extractGaParams(ind, fields) {
    const params = {};
    for (const field of fields) {
        if (ind[field] !== undefined && ind[field] !== null) params[field] = ind[field];
    }
    return params;
}

// ── Tests ────────────────────────────────────────────────────────────────

const paramRanges = {
    buy_ranges: { step_pct: [2.5, 5.0, 10.0], equal_slice_allocation_pct: [2.5, 5.0, 7.5, 10.0] },
    sell_ranges: {
        sell_min_profit_pct: [5, 10, 20],
        repair_sell_cooldown_days: [0, 30, 60],
        repair_stage_sell_pct: [8, 15, 25],
        grid_rebound_step_pct: [2.5, 5.0, 7.5],
        grid_sell_pct: [15, 25, 40, 50],
        grid_min_sell_amount: [200, 500, 1000],
        cost_first_profit_pct: [8, 10, 15],
        cost_second_profit_pct: [15, 20, 25],
        cost_third_profit_pct: [25, 30, 40],
        cost_first_sell_pct: [20, 30, 40],
        cost_second_sell_pct: [20, 30, 40],
        cost_third_sell_pct: [20, 30, 40],
        cost_deleverage_cooldown_days: [0, 15, 30],
        cost_min_sell_amount: [0, 200, 500],
        dca_rearm_drawdown_pct: [0, 5, 10, 15, 20],
        sell_stage_rearm_drawdown_pct: [null, 10, 15]
    },
    int_fields: ['repair_sell_cooldown_days', 'cost_deleverage_cooldown_days', 'core_dip_timing_max_delay_days'],
    bounds: {
        step_pct: [2.5, 10], equal_slice_allocation_pct: [2.5, 10],
        core_dip_initial_core_pct: [70, 95], core_dip_weekly_core_pct: [85, 100],
        core_dip_cash_reserve_pct: [3, 12], core_dip_start_drawdown_pct: [3, 10],
        core_dip_full_drawdown_pct: [15, 30], core_dip_timing_max_delay_days: [1, 5],
        core_dip_timing_rise_threshold_pct: [1, 2.5], core_dip_timing_near_low_pct: [1, 3],
        sell_min_profit_pct: [5, 20], repair_sell_cooldown_days: [0, 60],
        repair_stage_sell_pct: [8, 25], grid_rebound_step_pct: [2.5, 15],
        grid_sell_pct: [15, 50], grid_min_sell_amount: [200, 1000],
        cost_first_profit_pct: [8, 15], cost_second_profit_pct: [15, 25], cost_third_profit_pct: [25, 40],
        cost_first_sell_pct: [20, 40], cost_second_sell_pct: [20, 30], cost_third_sell_pct: [20, 30],
        cost_deleverage_cooldown_days: [0, 30], cost_min_sell_amount: [0, 500],
        dca_rearm_drawdown_pct: [0, 20], sell_stage_rearm_drawdown_pct: [10, 15]
    },
    precision: { step_pct: 2, sell_min_profit_pct: 2, grid_sell_pct: 1 },
    buy_fields: { equal_slice: ['step_pct', 'equal_slice_allocation_pct'], pyramid_3: [] },
    sell_fields: { repair_step: ['sell_min_profit_pct', 'repair_sell_cooldown_days', 'repair_stage_sell_pct'], none: [] }
};

// Test 1: Discrete mutation produces values from candidate ranges
function test_discrete_mutation_stays_in_ranges() {
    const ind = { buy_strategy: 'equal_slice', sell_strategy: 'repair_step', step_pct: 5, equal_slice_allocation_pct: 5, sell_min_profit_pct: 10 };
    const gaConfig = { continuous_mutation: false, mutation_sigma_ratio: 0.15 };
    // Run many mutations; all step_pct values must be in [2.5, 5.0, 10.0]
    const allowed = new Set([2.5, 5.0, 10.0]);
    for (let i = 0; i < 100; i++) {
        const child = mutateGa(ind, 1.0, paramRanges, gaConfig, false);
        if (child.step_pct !== undefined) {
            assert.ok(allowed.has(child.step_pct), `step_pct=${child.step_pct} not in allowed set`);
        }
    }
    console.log('PASS: test_discrete_mutation_stays_in_ranges');
}

// Test 2: Continuous mutation produces values OUTSIDE discrete ranges
function test_continuous_mutation_goes_wild() {
    const ind = { buy_strategy: 'equal_slice', sell_strategy: 'repair_step', step_pct: 5, equal_slice_allocation_pct: 5, sell_min_profit_pct: 10 };
    const gaConfig = { continuous_mutation: true, mutation_sigma_ratio: 0.15 };
    const allowed = new Set([2.5, 5.0, 10.0]);
    let wildCount = 0;
    for (let i = 0; i < 100; i++) {
        const child = mutateGa(ind, 1.0, paramRanges, gaConfig, false);
        if (child.step_pct !== undefined && !allowed.has(child.step_pct)) wildCount++;
    }
    assert.ok(wildCount > 0, 'Continuous mutation should produce values outside discrete set');
    console.log('PASS: test_continuous_mutation_goes_wild (wild count: ' + wildCount + ')');
}

// Test 3: Continuous mutation respects bounds
function test_continuous_mutation_respects_bounds() {
    const ind = { buy_strategy: 'equal_slice', sell_strategy: 'repair_step', step_pct: 5, sell_min_profit_pct: 10 };
    const gaConfig = { continuous_mutation: true, mutation_sigma_ratio: 0.3 };
    for (let i = 0; i < 200; i++) {
        const child = mutateGa(ind, 1.0, paramRanges, gaConfig, false);
        if (child.step_pct !== undefined) {
            assert.ok(child.step_pct >= 0.5, `step_pct=${child.step_pct} < 0.5`);
            assert.ok(child.step_pct <= 30, `step_pct=${child.step_pct} > 30`);
        }
        if (child.sell_min_profit_pct !== undefined) {
            assert.ok(child.sell_min_profit_pct >= 1, `sell_min_profit_pct=${child.sell_min_profit_pct} < 1`);
            assert.ok(child.sell_min_profit_pct <= 50, `sell_min_profit_pct=${child.sell_min_profit_pct} > 50`);
        }
    }
    console.log('PASS: test_continuous_mutation_respects_bounds');
}

// Test 4: Crossover combines parameters from both parents
function test_crossover_combines_parents() {
    const p1 = { buy_strategy: 'equal_slice', step_pct: 2.5, equal_slice_allocation_pct: 10 };
    const p2 = { buy_strategy: 'equal_slice', step_pct: 10.0, equal_slice_allocation_pct: 2.5 };
    let gotP1 = false, gotP2 = false;
    for (let i = 0; i < 50; i++) {
        const child = crossoverGa(p1, p2);
        if (child.step_pct === 2.5) gotP1 = true;
        if (child.step_pct === 10.0) gotP2 = true;
    }
    assert.ok(gotP1 && gotP2, 'Crossover should sometimes pick from p1 and sometimes p2');
    assert.strictEqual(crossoverGa(p1, p2).buy_strategy, 'equal_slice');
    console.log('PASS: test_crossover_combines_parents');
}

// Test 5: Tournament select returns best
function test_tournament_select_returns_best() {
    const ranked = [
        { ind: { key: 'weak' }, fit: 1 },
        { ind: { key: 'mid' }, fit: 5 },
        { ind: { key: 'strong' }, fit: 10 }
    ];
    // With tournament_size=3 and deterministic selection of top 3 indices, best wins
    // We'll run multiple times; eventually strong should win
    let strongWins = 0;
    for (let i = 0; i < 20; i++) {
        if (tournamentSelectGa(ranked, 3).key === 'strong') strongWins++;
    }
    assert.ok(strongWins > 0, 'Tournament should sometimes select the best');
    console.log('PASS: test_tournament_select_returns_best (strong wins: ' + strongWins + ')');
}

// Test 6: extractGaParams returns only non-null fields
function test_extract_ga_params() {
    const ind = { step_pct: 5, equal_slice_allocation_pct: null, core_dip_initial_core_pct: undefined, extra: 'x' };
    const params = extractGaParams(ind, ['step_pct', 'equal_slice_allocation_pct', 'core_dip_initial_core_pct']);
    assert.ok('step_pct' in params);
    assert.ok(!('equal_slice_allocation_pct' in params));
    assert.ok(!('core_dip_initial_core_pct' in params));
    assert.ok(!('extra' in params));
    console.log('PASS: test_extract_ga_params');
}

// Test 7: core_dip constraint enforced after mutation
function test_core_dip_constraint_enforced() {
    const ind = { buy_strategy: 'core_dip_dca', sell_strategy: 'none',
        core_dip_start_drawdown_pct: 50, core_dip_full_drawdown_pct: 10 };
    const gaConfig = { continuous_mutation: false, mutation_sigma_ratio: 0.15 };
    const child = mutateGa(ind, 0, paramRanges, gaConfig, false);
    assert.ok(Number(child.core_dip_start_drawdown_pct) <= Number(child.core_dip_full_drawdown_pct),
        `start=${child.core_dip_start_drawdown_pct} should be <= full=${child.core_dip_full_drawdown_pct}`);
    console.log('PASS: test_core_dip_constraint_enforced');
}

// Test 8: NaN fitness values filtered, Math.max NaN → indexOf -1 bug exposed
function test_nan_fitness_filtered() {
    const fitnesses = [10, NaN, 5, 20, NaN, 15];
    const valid = fitnesses.filter(function(f) { return isFinite(f); });
    assert.strictEqual(valid.length, 4);
    assert.strictEqual(Math.max.apply(null, valid), 20);
    assert.ok(isNaN(Math.max(1, NaN, 3)), 'Math.max with NaN returns NaN');
    assert.strictEqual([1, NaN, 3].indexOf(NaN), -1, 'indexOf(NaN) must be -1 – this is the root bug');
    const best = valid.length ? Math.max.apply(null, valid) : 0;
    const idx = valid.length ? fitnesses.indexOf(best) : 0;
    assert.strictEqual(idx, 3, 'indexOf should find 20 at index 3');
    console.log('PASS: test_nan_fitness_filtered');
}

// Test 9: bestEver null fallback
function test_bestever_fallback() {
    let bestEver = null;
    const pop = [{ key: 'a' }, { key: 'b' }];
    if (!bestEver && pop.length) bestEver = pop[0];
    assert.strictEqual(bestEver.key, 'a');
    console.log('PASS: test_bestever_fallback');
}

// Test 10: crossover preserves label and key from parents
function test_crossover_preserves_label_and_key() {
    const p1 = { buy_strategy: 'eq', step_pct: 5, label: '等距细切', key: 'k1' };
    const p2 = { buy_strategy: 'eq', step_pct: 10, label: '等距', key: 'k2' };
    const child = crossoverGa(p1, p2);
    assert.ok(child.label, 'crossover must preserve label');
    assert.ok(child.key, 'crossover must preserve key');
    console.log('PASS: test_crossover_preserves_label_and_key');
}

// Test 11: mutateGa generates a non-null key
function test_mutate_generates_key() {
    const ind = { buy_strategy: 'eq', sell_strategy: 'none', step_pct: 5, label: 'T', key: 'old' };
    const gaConfig = { continuous_mutation: false, mutation_sigma_ratio: 0.15 };
    const child = mutateGa(ind, 0, paramRanges, gaConfig, false);
    assert.ok(child.key, 'mutateGa must generate a non-null key');
    assert.notStrictEqual(child.key, 'old', 'mutated key should differ from parent');
    console.log('PASS: test_mutate_generates_key');
}

// Test 12: mutate clears stale label so status display rebuilds from current params
function test_mutate_clears_label() {
    const ind = { buy_strategy: 'eq', sell_strategy: 'none', step_pct: 5, label: 'OLD LABEL', key: 'k' };
    const gaConfig = { continuous_mutation: false, mutation_sigma_ratio: 0.15, cross_strategy: false };
    const child = mutateGa(ind, 0.01, paramRanges, gaConfig, false);
    assert.strictEqual(child.label, null, 'mutate must clear label to force regeneration');
    assert.ok(child.key, 'key must still exist');
    assert.notStrictEqual(child.key, 'k', 'key should be regenerated');
    console.log('PASS: test_mutate_clears_label');
}

// Test 13: custom bounds constrain discrete mutation ranges and inherited values
function test_custom_bounds_constrain_rearm_params() {
    const boundedRanges = JSON.parse(JSON.stringify(paramRanges));
    boundedRanges.sell_ranges.sell_stage_rearm_drawdown_pct = [null, 10, 15];
    boundedRanges.bounds.dca_rearm_drawdown_pct = [0, 2];
    boundedRanges.bounds.sell_stage_rearm_drawdown_pct = [0, 2];
    boundedRanges._custom_bounds_fields = ['dca_rearm_drawdown_pct', 'sell_stage_rearm_drawdown_pct'];
    const ind = {
        buy_strategy: 'equal_slice', sell_strategy: 'grid_rebound',
        dca_rearm_drawdown_pct: 10, sell_stage_rearm_drawdown_pct: 15
    };
    const gaConfig = { continuous_mutation: false, mutation_sigma_ratio: 0.15 };
    for (let i = 0; i < 100; i++) {
        const child = mutateGa(ind, 1, boundedRanges, gaConfig, false);
        assert.ok(child.dca_rearm_drawdown_pct >= 0 && child.dca_rearm_drawdown_pct <= 2,
            `dca_rearm_drawdown_pct=${child.dca_rearm_drawdown_pct} outside custom bounds`);
        if (child.sell_stage_rearm_drawdown_pct !== null && child.sell_stage_rearm_drawdown_pct !== undefined) {
            assert.ok(child.sell_stage_rearm_drawdown_pct >= 0 && child.sell_stage_rearm_drawdown_pct <= 2,
                `sell_stage_rearm_drawdown_pct=${child.sell_stage_rearm_drawdown_pct} outside custom bounds`);
        }
    }
    const inherited = mutateGa(ind, 0, boundedRanges, gaConfig, false);
    assert.strictEqual(inherited.dca_rearm_drawdown_pct, 2);
    assert.strictEqual(inherited.sell_stage_rearm_drawdown_pct, 2);
    console.log('PASS: test_custom_bounds_constrain_rearm_params');
}

// ── gaParamKey & display dedup tests ──

function test_gaParamKey_same_params_same_key() {
    const ind1 = { buy_strategy: 'equal_slice', sell_strategy: 'price_rise_grid', step_pct: 1, equal_slice_allocation_pct: 20, grid_rebound_step_pct: 10, grid_sell_pct: 10.5, grid_min_sell_amount: 200, sell_min_profit_pct: 30, buy_rearm_mode: 'cumulative' };
    const ind2 = { buy_strategy: 'equal_slice', sell_strategy: 'price_rise_grid', step_pct: 1, equal_slice_allocation_pct: 20, grid_rebound_step_pct: 10, grid_sell_pct: 10.5, grid_min_sell_amount: 200, sell_min_profit_pct: 30, buy_rearm_mode: 'cumulative' };
    assert.strictEqual(gaParamKey(ind1), gaParamKey(ind2));
    console.log('PASS: test_gaParamKey_same_params_same_key');
}

function test_gaParamKey_diff_params_diff_key() {
    const ind1 = { buy_strategy: 'equal_slice', sell_strategy: 'price_rise_grid', step_pct: 1, equal_slice_allocation_pct: 20, grid_rebound_step_pct: 10, grid_sell_pct: 10.5, grid_min_sell_amount: 200, sell_min_profit_pct: 30, buy_rearm_mode: 'cumulative' };
    const ind2 = { buy_strategy: 'equal_slice', sell_strategy: 'price_rise_grid', step_pct: 1, equal_slice_allocation_pct: 20, grid_rebound_step_pct: 10, grid_sell_pct: 10.5, grid_min_sell_amount: 201, sell_min_profit_pct: 30, buy_rearm_mode: 'cumulative' };
    assert.notStrictEqual(gaParamKey(ind1), gaParamKey(ind2));
    console.log('PASS: test_gaParamKey_diff_params_diff_key');
}

function test_gaParamKey_ignores_random_key_field() {
    const ind1 = { buy_strategy: 'equal_slice', sell_strategy: 'price_rise_grid', step_pct: 1, equal_slice_allocation_pct: 20, grid_rebound_step_pct: 10, grid_sell_pct: 10.5, grid_min_sell_amount: 200, sell_min_profit_pct: 30, key: 'random_abc', label: 'foo' };
    const ind2 = { buy_strategy: 'equal_slice', sell_strategy: 'price_rise_grid', step_pct: 1, equal_slice_allocation_pct: 20, grid_rebound_step_pct: 10, grid_sell_pct: 10.5, grid_min_sell_amount: 200, sell_min_profit_pct: 30, key: 'random_xyz', label: 'bar' };
    assert.strictEqual(gaParamKey(ind1), gaParamKey(ind2));
    console.log('PASS: test_gaParamKey_ignores_random_key_field');
}

function test_gaDedupByDisplayStats_removes_duplicate_stats() {
    const rows = [
        { avg_return: 310.19, avg_drawdown: -36.47, avg_sell_quality: 84.57, fitness: 90, label: 'a' },
        { avg_return: 310.19, avg_drawdown: -36.47, avg_sell_quality: 84.57, fitness: 85, label: 'b' },
        { avg_return: 300.00, avg_drawdown: -30.00, avg_sell_quality: 80.00, fitness: 88, label: 'c' },
    ];
    const deduped = gaDedupByDisplayStats(rows);
    assert.strictEqual(deduped.length, 2);
    assert.strictEqual(deduped[0].fitness, 90);
    assert.strictEqual(deduped[1].fitness, 88);
    console.log('PASS: test_gaDedupByDisplayStats_removes_duplicate_stats');
}

function test_gaDedupByDisplayStats_preserves_unique_entries() {
    const rows = [
        { avg_return: 100, avg_drawdown: -10, avg_sell_quality: 50, fitness: 70 },
        { avg_return: 200, avg_drawdown: -20, avg_sell_quality: 60, fitness: 80 },
        { avg_return: 300, avg_drawdown: -30, avg_sell_quality: 70, fitness: 90 },
    ];
    const deduped = gaDedupByDisplayStats(rows);
    assert.strictEqual(deduped.length, 3);
    console.log('PASS: test_gaDedupByDisplayStats_preserves_unique_entries');
}

function test_gaDedupByDisplayStats_handles_empty() {
    assert.strictEqual(gaDedupByDisplayStats([]).length, 0);
    console.log('PASS: test_gaDedupByDisplayStats_handles_empty');
}

function test_gaDedupByDisplayStats_skips_dedup_in_continuous_mode() {
    // 野蛮生长 mode: same avg stats but different params must ALL survive so the
    // user can see parameter-space diversity (regression: TSLA equal_slice+cost_deleverage
    // collapsed every run to one row because cost tiers never triggered).
    const rows = [
        { avg_return: 310.19, avg_drawdown: -36.47, avg_sell_quality: 84.57, fitness: 90, label: 'a' },
        { avg_return: 310.19, avg_drawdown: -36.47, avg_sell_quality: 84.57, fitness: 85, label: 'b' },
        { avg_return: 310.19, avg_drawdown: -36.47, avg_sell_quality: 84.57, fitness: 80, label: 'c' },
    ];
    const deduped = gaDedupByDisplayStats(rows, true /* skipStatsDedup */);
    assert.strictEqual(deduped.length, 3, 'continuous mode must keep all unique-param rows');
    console.log('PASS: test_gaDedupByDisplayStats_skips_dedup_in_continuous_mode');
}

function test_cross_strategy_mutation_uses_selected_strategy_pool() {
    const ranges = {
        ...paramRanges,
        buy_fields: { equal_slice: ['step_pct'], pyramid_3: [], weekly_dca: [], salary_flow_dca: [] },
        sell_fields: { repair_step: ['sell_min_profit_pct'], none: [], cost_deleverage: [] },
    };
    const gaConfig = {
        continuous_mutation: false,
        cross_strategy: true,
        strategy_mutation_rate: 1,
        ga_buy_strategies: ['equal_slice'],
        ga_sell_strategies: ['none'],
    };
    gaRandomFn = mulberry32(123);
    for (let i = 0; i < 50; i++) {
        const child = mutateGa({ buy_strategy: 'pyramid_3', sell_strategy: 'repair_step', step_pct: 5, sell_min_profit_pct: 10 }, 0, ranges, gaConfig, true);
        assert.strictEqual(child.buy_strategy, 'equal_slice', 'must only mutate into selected buy strategies');
        assert.strictEqual(child.sell_strategy, 'none', 'must only mutate into selected sell strategies');
    }
    gaRandomFn = Math.random;
    console.log('PASS: test_cross_strategy_mutation_uses_selected_strategy_pool');
}

function test_seeded_ga_mutation_is_reproducible() {
    const ind = { buy_strategy: 'equal_slice', sell_strategy: 'repair_step', step_pct: 5, equal_slice_allocation_pct: 5, sell_min_profit_pct: 10 };
    const gaConfig = { continuous_mutation: false, mutation_sigma_ratio: 0.15, cross_strategy: true, strategy_mutation_rate: 0.5,
        ga_buy_strategies: ['equal_slice', 'pyramid_3'], ga_sell_strategies: ['repair_step', 'none'] };
    function run(seed) {
        gaRandomFn = mulberry32(seed);
        return Array.from({ length: 10 }, function() {
            const child = mutateGa(ind, 0.4, paramRanges, gaConfig, true);
            return [child.buy_strategy, child.sell_strategy, child.step_pct, child.equal_slice_allocation_pct, child.sell_min_profit_pct].join('|');
        });
    }
    assert.deepStrictEqual(run(42), run(42), 'same seed must reproduce the same mutation sequence');
    assert.notDeepStrictEqual(run(42), run(43), 'different seed should produce a different mutation sequence');
    gaRandomFn = Math.random;
    console.log('PASS: test_seeded_ga_mutation_is_reproducible');
}

// ── LEAPS preset payload builder ──
function buildLeapsPresetPayload(row, note) {
    const fields = [
        'drawdown_threshold_pct', 'entry_mode',
        'stage1_days', 'stage1_profit', 'stage1_sell',
        'stage2_days', 'stage2_profit', 'stage2_sell',
        'position_pct', 'cooldown_days',
    ];
    const p = { type: 'leaps', leaps_note: String(note || '') };
    for (const f of fields) {
        if (row[f] != null) p[f] = row[f];
    }
    return p;
}

function test_buildLeapsPresetPayload_all_fields() {
    const row = {
        drawdown_threshold_pct: 20, entry_mode: 'both',
        stage1_days: 15, stage1_profit: 80, stage1_sell: 50,
        stage2_days: 60, stage2_profit: 60, stage2_sell: 50,
        position_pct: 20, cooldown_days: 5,
    };
    const p = buildLeapsPresetPayload(row, 'test note');
    assert.strictEqual(p.type, 'leaps');
    assert.strictEqual(p.leaps_note, 'test note');
    assert.strictEqual(p.drawdown_threshold_pct, 20);
    assert.strictEqual(p.entry_mode, 'both');
    assert.strictEqual(p.stage1_days, 15);
    assert.strictEqual(p.stage1_profit, 80);
    assert.strictEqual(p.stage1_sell, 50);
    assert.strictEqual(p.stage2_days, 60);
    assert.strictEqual(p.stage2_profit, 60);
    assert.strictEqual(p.stage2_sell, 50);
    assert.strictEqual(p.position_pct, 20);
    assert.strictEqual(p.cooldown_days, 5);
    console.log('PASS: test_buildLeapsPresetPayload_all_fields');
}

function test_buildLeapsPresetPayload_no_note() {
    const row = { drawdown_threshold_pct: 25, entry_mode: 'touch', stage1_days: 10,
        stage1_profit: 100, stage1_sell: 40, stage2_days: 45, stage2_profit: 70, stage2_sell: 60,
        position_pct: 30, cooldown_days: 10 };
    const p = buildLeapsPresetPayload(row, '');
    assert.strictEqual(p.leaps_note, '');
    console.log('PASS: test_buildLeapsPresetPayload_no_note');
}

function test_buildLeapsPresetPayload_excludes_non_leaps_fields() {
    const row = { drawdown_threshold_pct: 15, entry_mode: 'bounce', stage1_days: 20,
        stage1_profit: 90, stage1_sell: 60, stage2_days: 70, stage2_profit: 50, stage2_sell: 45,
        position_pct: 10, cooldown_days: 3,
        fitness: 1.5, rank: 2, cagr: 25, total_roi: 150, final_equity: 25000,
        trade_count: 8, trade_details: [], max_drawdown_pct: 5 };
    const p = buildLeapsPresetPayload(row, '');
    assert.strictEqual(p.fitness, undefined);
    assert.strictEqual(p.rank, undefined);
    assert.strictEqual(p.cagr, undefined);
    assert.strictEqual(p.trade_details, undefined);
    console.log('PASS: test_buildLeapsPresetPayload_excludes_non_leaps_fields');
}

// ── Two-stage cross-strategy GA helpers (extracted copies of template logic) ──
// These mirror web/templates/strategy_parameter_lab.html so the two-stage GA's
// pure-logic pieces get unit coverage (worker-bound evaluation is covered by the
// browser verification step in docs/ga-cross-strategy-diagnosis.md).

function gaPairKey(ind) { return (ind?.buy_strategy || '') + '/' + (ind?.sell_strategy || ''); }

function partitionByPair(population) {
    const m = new Map();
    (population || []).forEach(function (ind) {
        const k = gaPairKey(ind);
        if (!m.has(k)) m.set(k, []);
        m.get(k).push(ind);
    });
    return m;
}

// Module-level bound helpers operating on the global paramRanges (the template
// passes paramRanges explicitly; here it is the module const).
function _tNumericBounds(field) {
    const b = paramRanges.bounds[field];
    if (!b || b.length < 2) return null;
    const lo = Number(b[0]), hi = Number(b[1]);
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
    return lo <= hi ? [lo, hi] : [hi, lo];
}
function _tNormalize(field, value) {
    if (value === null || value === undefined || typeof value === 'boolean' || typeof value === 'string') return value;
    const b = _tNumericBounds(field);
    const n = Number(value);
    if (!b || !Number.isFinite(n)) return value;
    let v = Math.max(b[0], Math.min(b[1], n));
    const intF = new Set(paramRanges.int_fields || []);
    if (intF.has(field)) v = Math.trunc(v);
    else { const prec = paramRanges.precision && paramRanges.precision[field] !== undefined ? paramRanges.precision[field] : 2; v = Number(v.toFixed(prec)); }
    return v;
}
function _tUniform(field) {
    const b = _tNumericBounds(field);
    if (!b) return null;
    return _tNormalize(field, b[0] + gaRandom() * (b[1] - b[0]));
}
function _tEnforce(child) {
    for (const f of BUY_PARAMETER_FIELDS.concat(SELL_PARAMETER_FIELDS)) {
        if (Object.prototype.hasOwnProperty.call(child, f)) child[f] = _tNormalize(f, child[f]);
    }
    return child;
}

function randomGaIndividual(bs, ss, gaConfig, template) {
    const ind = { ...(template || {}) };
    ind.buy_strategy = bs; ind.sell_strategy = ss;
    const continuous = gaConfig.continuous_mutation || false;
    const buyRanges = paramRanges.buy_ranges || {};
    const sellRanges = paramRanges.sell_ranges || {};
    const buyFields = (paramRanges.buy_fields || {})[bs] || [];
    let sellFields = (paramRanges.sell_fields || {})[ss] || [];
    if (ss !== 'none') {
        if (sellFields.indexOf('sell_allow_same_day_sell') < 0) sellFields.push('sell_allow_same_day_sell');
        if (sellFields.indexOf('dca_rearm_drawdown_pct') < 0) sellFields.push('dca_rearm_drawdown_pct');
        if (sellFields.indexOf('sell_stage_rearm_drawdown_pct') < 0) sellFields.push('sell_stage_rearm_drawdown_pct');
        if (sellFields.indexOf('buy_rearm_mode') < 0) sellFields.push('buy_rearm_mode');
    }
    const pickDiscrete = function (field, values) {
        if (!values || !values.length) return template && template[field] !== undefined ? template[field] : null;
        return values[Math.floor(gaRandom() * values.length)];
    };
    for (const field of buyFields) {
        if (continuous && _tNumericBounds(field)) ind[field] = _tUniform(field);
        else if (buyRanges[field]) ind[field] = pickDiscrete(field, buyRanges[field]);
    }
    for (const field of sellFields) {
        if (field === 'buy_rearm_mode') {
            const modes = sellRanges[field] || ['cumulative', 'restart_from_rearm'];
            ind[field] = (gaConfig.buy_rearm_mode && modes.indexOf(gaConfig.buy_rearm_mode) >= 0) ? gaConfig.buy_rearm_mode : modes[Math.floor(gaRandom() * modes.length)];
        } else if (field === 'sell_allow_same_day_sell') {
            if (gaConfig.sell_allow_same_day_sell === 'true' || gaConfig.sell_allow_same_day_sell === 'false') ind[field] = gaConfig.sell_allow_same_day_sell === 'true';
            else ind[field] = gaRandom() < 0.5;
        } else if (continuous && _tNumericBounds(field)) {
            ind[field] = _tUniform(field);
        } else if (sellRanges[field]) {
            ind[field] = pickDiscrete(field, sellRanges[field]);
        }
    }
    if (gaConfig.buy_rearm_mode === 'cumulative' || gaConfig.buy_rearm_mode === 'restart_from_rearm') ind.buy_rearm_mode = gaConfig.buy_rearm_mode;
    if (gaConfig.sell_allow_same_day_sell === 'true' || gaConfig.sell_allow_same_day_sell === 'false') ind.sell_allow_same_day_sell = gaConfig.sell_allow_same_day_sell === 'true';
    _tEnforce(ind);
    return ind;
}

// Pure-logic Stage-2 quota elitism selector (mirrors runTwoStageCrossStrategyGa).
function selectQuotaElites(ranked, finalistPairs, minQuota, popSize) {
    const next = []; const q = {};
    for (const r of ranked) {
        if (next.length >= popSize) break;
        const pk = r.pair;
        if (finalistPairs.has(pk) && (q[pk] || 0) < minQuota) { next.push(r); q[pk] = (q[pk] || 0) + 1; }
    }
    return next;
}

// Test: partitionByPair groups correctly.
function test_partition_by_pair() {
    const pop = [
        { buy_strategy: 'equal_slice', sell_strategy: 'cost_deleverage' },
        { buy_strategy: 'pyramid_3', sell_strategy: 'price_rise_grid' },
        { buy_strategy: 'equal_slice', sell_strategy: 'cost_deleverage' },
    ];
    const m = partitionByPair(pop);
    assert.strictEqual(m.get('equal_slice/cost_deleverage').length, 2);
    assert.strictEqual(m.get('pyramid_3/price_rise_grid').length, 1);
    assert.strictEqual(m.size, 2);
    console.log('PASS: test_partition_by_pair');
}

// Test: randomGaIndividual locks to the requested pair and respects bounds / categorical lock-ins.
function test_random_ga_individual_locks_strategy_and_bounds() {
    gaRandomFn = mulberry32(42);
    const gaConfig = { continuous_mutation: false, mutation_sigma_ratio: 0.25, buy_rearm_mode: 'cumulative', sell_allow_same_day_sell: 'true' };
    const template = { buy_strategy: 'equal_slice', sell_strategy: 'repair_step', step_pct: 5, equal_slice_allocation_pct: 5, sell_min_profit_pct: 10 };
    for (let i = 0; i < 50; i++) {
        const ind = randomGaIndividual('equal_slice', 'repair_step', gaConfig, template);
        assert.strictEqual(ind.buy_strategy, 'equal_slice', 'buy strategy must be locked');
        assert.strictEqual(ind.sell_strategy, 'repair_step', 'sell strategy must be locked');
        assert.strictEqual(ind.buy_rearm_mode, 'cumulative', 'buy_rearm_mode categorical lock-in');
        assert.strictEqual(ind.sell_allow_same_day_sell, true, 'sell_allow_same_day_sell categorical lock-in');
        if (ind.step_pct !== undefined) {
            assert.ok(ind.step_pct >= 2.5 && ind.step_pct <= 10, 'step_pct within bounds: ' + ind.step_pct);
        }
    }
    console.log('PASS: test_random_ga_individual_locks_strategy_and_bounds');
}

// Test (core regression): Stage-1 island breeding with crossEnabled=false NEVER
// escapes the pair — descendants stay (bs, ss). This is the invariant that lets a
// late-bloomer pair evolve undisturbed instead of being monocultured out.
function test_stage1_island_breeding_never_escapes_pair() {
    gaRandomFn = mulberry32(7);
    const gaConfig = { continuous_mutation: false, mutation_sigma_ratio: 0.2, cross_strategy: true, strategy_mutation_rate: 1.0 };
    const template = { buy_strategy: 'equal_slice', sell_strategy: 'repair_step', step_pct: 5, equal_slice_allocation_pct: 5, sell_min_profit_pct: 10 };
    let population = [];
    for (let i = 0; i < 20; i++) population.push(randomGaIndividual('equal_slice', 'repair_step', gaConfig, template));
    // fitness gradient on step_pct so selection has signal
    const fit = function (ind) { return -Math.abs((ind.step_pct || 5) - 7.5); };
    for (let g = 0; g < 8; g++) {
        const ranked = population.map(function (ind) { return { ind: ind, fit: fit(ind) }; }).sort(function (a, b) { return b.fit - a.fit; });
        const next = ranked.slice(0, 3).map(function (r) { return r.ind; });
        while (next.length < 20) {
            const p1 = tournamentSelectGa(ranked, 4);
            const p2 = tournamentSelectGa(ranked, 4);
            const child = gaRandom() < 0.8 ? crossoverGa(p1, p2) : { ...p1 };
            next.push(mutateGa(child, 0.2, paramRanges, gaConfig, false));
        }
        population = next.slice(0, 20);
    }
    for (const ind of population) {
        assert.strictEqual(ind.buy_strategy, 'equal_slice', 'island leaked buy strategy');
        assert.strictEqual(ind.sell_strategy, 'repair_step', 'island leaked sell strategy');
    }
    console.log('PASS: test_stage1_island_breeding_never_escapes_pair');
}

// Test: Stage-1 island breeding is reproducible with a fixed seed and diverges with a different seed.
function test_stage1_island_breeding_reproducible() {
    const gaConfig = { continuous_mutation: false, mutation_sigma_ratio: 0.2 };
    const template = { buy_strategy: 'equal_slice', sell_strategy: 'repair_step', step_pct: 5, equal_slice_allocation_pct: 5, sell_min_profit_pct: 10 };
    const fit = function (ind) { return -Math.abs((ind.step_pct || 5) - 7.5); };
    function runOnce(seed) {
        gaRandomFn = mulberry32(seed);
        let population = [];
        for (let i = 0; i < 16; i++) population.push(randomGaIndividual('equal_slice', 'repair_step', gaConfig, template));
        for (let g = 0; g < 5; g++) {
            const ranked = population.map(function (ind) { return { ind: ind, fit: fit(ind) }; }).sort(function (a, b) { return b.fit - a.fit; });
            const next = ranked.slice(0, 3).map(function (r) { return r.ind; });
            while (next.length < 16) {
                const p1 = tournamentSelectGa(ranked, 4); const p2 = tournamentSelectGa(ranked, 4);
                const child = gaRandom() < 0.8 ? crossoverGa(p1, p2) : { ...p1 };
                next.push(mutateGa(child, 0.2, paramRanges, gaConfig, false));
            }
            population = next.slice(0, 16);
        }
        return population.map(gaParamKey).join('|');
    }
    const a1 = runOnce(123456789), a2 = runOnce(123456789);
    const b1 = runOnce(987654321);
    assert.strictEqual(a1, a2, 'same seed must reproduce identical island evolution');
    assert.notStrictEqual(a1, b1, 'different seed must diverge');
    console.log('PASS: test_stage1_island_breeding_reproducible');
}

// Test: Stage-2 quota elitism guarantees each finalist pair up to minQuota slots
// (even when its individuals rank low globally), and never exceeds popSize.
function test_stage2_quota_elitism_guarantees_min_quota() {
    const finalistPairs = new Set(['equal_slice/cost_deleverage', 'pyramid_3/price_rise_grid', 'linear_weighted_slice/none']);
    const minQuota = 5, popSize = 200;
    // Build ranked: pair A's individuals all rank at the very bottom.
    const ranked = [];
    for (let i = 0; i < 20; i++) ranked.push({ pair: 'pyramid_3/price_rise_grid', fit: 100 - i });
    for (let i = 0; i < 20; i++) ranked.push({ pair: 'linear_weighted_slice/none', fit: 70 - i });
    for (let i = 0; i < 20; i++) ranked.push({ pair: 'equal_slice/cost_deleverage', fit: 20 - i }); // low / bottom
    ranked.sort(function (a, b) { return b.fit - a.fit; });
    const elites = selectQuotaElites(ranked, finalistPairs, minQuota, popSize);
    const counts = {};
    elites.forEach(function (r) { counts[r.pair] = (counts[r.pair] || 0) + 1; });
    assert.ok(elites.length <= popSize, 'must not exceed popSize');
    // The bottom-ranked finalist pair still receives its full minQuota — anti-monoculture guarantee.
    assert.strictEqual(counts['equal_slice/cost_deleverage'], minQuota, 'bottom-ranked finalist must still get minQuota');
    assert.strictEqual(counts['pyramid_3/price_rise_grid'], minQuota);
    assert.strictEqual(counts['linear_weighted_slice/none'], minQuota);
    console.log('PASS: test_stage2_quota_elitism_guarantees_min_quota');
}

// Test: when minQuota * topK would exceed popSize, the selector still caps at popSize
// and never grants any pair more than minQuota.
function test_stage2_quota_respects_pop_cap() {
    const finalistPairs = new Set(['a/x', 'b/y', 'c/z', 'd/w']);
    const minQuota = 5, popSize = 12; // 5*4 = 20 > 12
    const ranked = [];
    for (const p of ['a/x', 'b/y', 'c/z', 'd/w']) for (let i = 0; i < 20; i++) ranked.push({ pair: p, fit: Math.random() * 100 });
    ranked.sort(function (a, b) { return b.fit - a.fit; });
    const elites = selectQuotaElites(ranked, finalistPairs, minQuota, popSize);
    const counts = {};
    elites.forEach(function (r) { counts[r.pair] = (counts[r.pair] || 0) + 1; });
    assert.ok(elites.length <= popSize, 'capped at popSize');
    Object.values(counts).forEach(function (c) { assert.ok(c <= minQuota, 'no pair exceeds minQuota'); });
    console.log('PASS: test_stage2_quota_respects_pop_cap');
}

// ── Run ──

const tests = [
    test_discrete_mutation_stays_in_ranges,
    test_continuous_mutation_goes_wild,
    test_continuous_mutation_respects_bounds,
    test_crossover_combines_parents,
    test_tournament_select_returns_best,
    test_extract_ga_params,
    test_core_dip_constraint_enforced,
    test_nan_fitness_filtered,
    test_bestever_fallback,
    test_crossover_preserves_label_and_key,
    test_mutate_generates_key,
    test_mutate_clears_label,
    test_custom_bounds_constrain_rearm_params,
    test_gaParamKey_same_params_same_key,
    test_gaParamKey_diff_params_diff_key,
    test_gaParamKey_ignores_random_key_field,
    test_gaDedupByDisplayStats_removes_duplicate_stats,
    test_gaDedupByDisplayStats_preserves_unique_entries,
    test_gaDedupByDisplayStats_handles_empty,
    test_gaDedupByDisplayStats_skips_dedup_in_continuous_mode,
    test_cross_strategy_mutation_uses_selected_strategy_pool,
    test_seeded_ga_mutation_is_reproducible,
    test_partition_by_pair,
    test_random_ga_individual_locks_strategy_and_bounds,
    test_stage1_island_breeding_never_escapes_pair,
    test_stage1_island_breeding_reproducible,
    test_stage2_quota_elitism_guarantees_min_quota,
    test_stage2_quota_respects_pop_cap,
    test_buildLeapsPresetPayload_all_fields,
    test_buildLeapsPresetPayload_no_note,
    test_buildLeapsPresetPayload_excludes_non_leaps_fields,
];

let passed = 0, failed = 0;
for (const test of tests) {
    try { test(); passed++; } catch (e) { console.error('FAIL: ' + test.name + ' -', e.message); failed++; }
}
console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
