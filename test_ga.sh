#!/usr/bin/env bash
# GA full test suite – must be run before EVERY GA-related commit.
# Usage: ./test_ga.sh
set -euo pipefail
cd "$(dirname "$0")"

PASS=0
FAIL=0

echo "=== GA Test Suite ==="
echo ""

# ── Python backend tests ──
echo "--- Python: strategy_parameter_genetic ---"
if python3 -m unittest test_strategy_parameter_genetic -q 2>&1; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
fi

echo "--- Python: strategy_parameter_registry (regression) ---"
if python3 -m unittest test_strategy_parameter_registry -q 2>&1; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
fi

echo "--- Python: GA E2E (worker lifecycle, continuous params, load) ---"
if python3 -m unittest test_ga_e2e -q 2>&1; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
fi

# ── Frontend JS tests ──
echo "--- JavaScript: GA operations (mutate/crossover/select/NaN) ---"
if node test_parameter_lab_ga.js 2>&1; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
fi

echo "--- JavaScript: Sell quality metrics (buy_quality, sell_quality) ---"
if node test_sell_quality_metrics_js.js 2>&1; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=========================================="
echo " Results: $PASS passed, $FAIL failed"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
    echo "❌ GA TEST SUITE FAILED – fix before committing!"
    exit 1
else
    echo "✅ GA test suite passed"
    exit 0
fi
