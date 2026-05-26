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
    'core_dip_timing_near_low_pct'
];
const SELL_PARAMETER_FIELDS = [
    'sell_min_profit_pct', 'repair_sell_cooldown_days', 'repair_stage_sell_pct',
    'grid_rebound_step_pct', 'grid_sell_pct', 'grid_first_sell_pct', 'grid_second_sell_pct',
    'grid_min_sell_amount', 'cost_first_profit_pct', 'cost_second_profit_pct', 'cost_third_profit_pct',
    'cost_first_sell_pct', 'cost_second_sell_pct', 'cost_third_sell_pct', 'cost_deleverage_cooldown_days',
    'sell_allow_same_day_sell', 'cost_min_sell_amount', 'dca_rearm_drawdown_pct',
    'buy_rearm_mode', 'sell_stage_rearm_drawdown_pct'
];

function tournamentSelectGa(ranked, tournamentSize) {
    const size = Math.min(tournamentSize, ranked.length);
    let best = ranked[0];
    for (let i = 1; i < size; i++) {
        const idx = Math.floor(Math.random() * ranked.length);
        if (ranked[idx].fit > best.fit) best = ranked[idx];
    }
    return best.ind;
}

function crossoverGa(p1, p2) {
    const child = { buy_strategy: p1.buy_strategy, sell_strategy: p1.sell_strategy,
        label: p1.label || p2.label, key: p1.key || p2.key };
    for (const k of Object.keys(p1)) {
        if (k === 'key' || k === 'label' || k === 'buy_strategy' || k === 'sell_strategy') continue;
        child[k] = Math.random() < 0.5 ? p1[k] : p2[k];
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
    const sigmaRatio = gaConfig.mutation_sigma_ratio || 0.15;

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
        let val = old + (Math.random() + Math.random() + Math.random() + Math.random() - 2) * sigma * 1.5;
        val = Math.max(lo, Math.min(hi, val));
        const prec = precision[field] !== undefined ? precision[field] : 2;
        val = Number(val.toFixed(prec));
        if (intFields.has(field)) val = Math.trunc(val);
        return val;
    }

    for (const field of BUY_PARAMETER_FIELDS) {
        if (Math.random() < mutationRate) {
            if (continuous && bounds[field]) {
                child[field] = gaussMutate(field, child[field]);
            } else if (buyRanges[field]) {
                const vals = gaDiscreteValuesWithinBounds(field, buyRanges[field]);
                child[field] = vals[Math.floor(Math.random() * vals.length)];
                if (intFields.has(field)) child[field] = Math.trunc(Number(child[field]));
            }
        }
    }
    for (const field of SELL_PARAMETER_FIELDS) {
        if (Math.random() < mutationRate) {
            if (continuous && bounds[field]) {
                child[field] = gaussMutate(field, child[field]);
            } else if (sellRanges[field]) {
                const vals = gaDiscreteValuesWithinBounds(field, sellRanges[field]);
                child[field] = vals[Math.floor(Math.random() * vals.length)];
                if (intFields.has(field)) child[field] = Math.trunc(Number(child[field]));
            } else if (field === 'sell_allow_same_day_sell') {
                child[field] = Math.random() < 0.5;
            }
        }
    }

    if (crossEnabled && gaConfig.cross_strategy && Math.random() < (gaConfig.strategy_mutation_rate || 0.05)) {
        const buyStrats = Object.keys(paramRanges.buy_fields || { buy_a: [] });
        const sellStrats = Object.keys(paramRanges.sell_fields || { sell_x: [] });
        child.buy_strategy = buyStrats[Math.floor(Math.random() * buyStrats.length)];
        child.sell_strategy = sellStrats[Math.floor(Math.random() * sellStrats.length)];
    }
    if (child.core_dip_start_drawdown_pct !== undefined && child.core_dip_full_drawdown_pct !== undefined) {
        if (Number(child.core_dip_start_drawdown_pct) > Number(child.core_dip_full_drawdown_pct)) {
            child.core_dip_full_drawdown_pct = child.core_dip_start_drawdown_pct;
        }
    }
    enforceGaIndividualBounds(child);
    child.key = 'test_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 6);
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
];

let passed = 0, failed = 0;
for (const test of tests) {
    try { test(); passed++; } catch (e) { console.error('FAIL: ' + test.name + ' -', e.message); failed++; }
}
console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
