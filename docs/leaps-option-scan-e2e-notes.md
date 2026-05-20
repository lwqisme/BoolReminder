# LEAPS Option Scan E2E Notes

## Scope

This note records the accepted behavior and verification path for the Strategy Lab LEAPS option scan.

The end-to-end chain is:

1. Browser loads `/strategy-lab/parameter-lab`.
2. Browser posts to `/api/strategy-lab/parameter-lab/option-packet`.
3. Docker backend fetches real option chains and option bars through the configured provider.
4. Browser runs `/static/option_scan_worker.js`.
5. The worker replays option wallet trades against the stock strategy timeline.

## Trading Rules

- Default LEAPS window is `min_dte=200`, `target_dte=250`, `max_dte=300`.
- Default moneyness is `otm_10`, which targets call strikes near `stock_price * 1.10`.
- Legacy `option_dtes=365,500` remains supported. Each legacy target expands to `target +/- 60` days.
- Option buys use whole contracts only. If wallet budget cannot buy at least one contract after fee, the signal is skipped with `contracts_too_small`.
- Option capital is modeled as a wallet:
  - initial cash is `wallet_pct * initial_cash / 100`;
  - monthly injection is `wallet_pct * monthly_contribution / 100`;
  - each buy signal may use `trade_allocation_pct` of current wallet cash.
- The replay timeline interleaves monthly injections, stock buy/sell signals, profit-taking exits, DTE exits, and backtest-end exits.
- A stock sell closes all open option positions for the same underlying.
- Profit-take proceeds return to wallet cash and may fund later option buys.

## Packet Contract

The option packet endpoint may accept precomputed `stock_strategies`. This is the preferred E2E smoke path because it verifies option data and worker replay without introducing unrelated stock simulation variability.

The response packet includes:

- `option_variants` with explicit `min_dte`, `target_dte`, `max_dte`, `moneyness`, wallet, allocation, profit-take, and exit settings.
- `option_data_lookup` keyed as `UNDERLYING|BUY_DATE|MONEYNESS|TARGET_DTE`.
- `stock_strategies` normalized to supported option underlyings.
- `stock_inputs` so browser workers replay wallets with the same `initial_cash` and `monthly_contribution` used by the server request.

## Docker Verification

Run from the repository root:

```bash
docker-compose build boll-reminder
docker-compose up -d --force-recreate boll-reminder
docker-compose exec -T boll-reminder python -m unittest \
  test_option_overlay \
  test_strategy_parameter_registry \
  test_strategy_lab_config \
  test_position_strategy \
  test_strategy_parameter_lab_worker
docker-compose exec -T boll-reminder python -m unittest \
  test_strategy_lab_frontend \
  test_strategy_lab_score_payload \
  test_strategy_lab_jobs \
  test_strategy_lab_robust
```

For a real API smoke, post to:

```text
http://127.0.0.1:5000/api/strategy-lab/parameter-lab/option-packet
```

Use representative `stock_strategies` for `TSLA.US`, `GOOGL.US`, and `TSM.US`, and pass:

```json
{
  "option_dte_min": 200,
  "option_dte_target": 250,
  "option_dte_max": 300,
  "option_moneyness_values": "otm_10",
  "option_profit_takes": "100",
  "option_profit_take_sells": "50",
  "option_exit_dtes": "60"
}
```

Expected smoke assertions:

- response has `success=true`;
- at least one lookup entry is non-empty;
- returned variants include `200/250/300` and `otm_10`;
- selected contracts have entry DTE inside `200..300`;
- selected strikes are near OTM 10% relative to the stock buy price;
- worker replay produces readable positions and/or skipped reasons.

## Frontend JavaScript Smoke

When Chromium is unavailable or too expensive for the environment, verify the frontend path with Node.js scripts instead of a browser. Check the HTML defaults and execute `/static/option_scan_worker.js` in a `vm` context against a real option packet.

The page defaults should be:

- `#optDteMin`: `200`
- `#optDteTarget`: `250`
- `#optDteMax`: `300`
- `#optMoneyness`: `otm_10`

Worker smoke assertions:

- `node --check web/static/option_scan_worker.js` passes.
- `node --check web/static/strategy_parameter_lab_worker.js` passes.
- Running `option_scan_worker.js` against the real packet posts a `done` message.
- The result contains finite aggregate metrics and either option positions or readable skipped reasons. For the standard high-cash smoke payload, it should produce option buys.

## Real API Notes

- `config/config.yaml` must contain valid Polygon configuration. Longbridge can provide stock data, but historical option bars use Polygon.
- Polygon historical option coverage may be sparse for some contracts. Treat `entry_price_not_found` and empty history warnings as data quality findings, not replay logic failures.
- Rate limits can affect contract and history calls. The backend batches chains by underlying and DTE window and de-duplicates history by ticker, but real smoke requests should still keep strategy/trade counts small.
- Check `docker-compose logs --tail=200 boll-reminder` after real smoke requests; there should be no traceback for the request.
