# Strategy Lab Parameter Experiment Plan

## Purpose

Build a dedicated buy/sell strategy parameter experiment lab.

The lab must compare every valid strategy parameter combination against the user-selected score topics, then rank results by a single formula:

- Return weight: 90%.
- Drawdown-control weight: 10%.

The UI must show each strategy parameter combination against each selected stock or portfolio time span, using the same matrix-style presentation as the current scorecard page, with colored cells for fast comparison.

This document is the implementation contract for a new development session. Do not start coding until the feasibility checks in this document pass.

## Core Principles

- One source of truth for strategy definitions.
- One source of truth for experiment payloads.
- One source of truth for scoring and ranking.
- Parameter combinations belong to each strategy definition, not to page-specific code.
- The same input snapshot must always reproduce the same result, unless market data freshness changes.
- Client-side computation is allowed and expected for large searches, but it must use real parallel workers across CPU cores.
- No feature ships without both functional verification and unit tests.

## Product Shape

The new workspace should be named `Parameter Lab` or `参数实验室`.

It must support:

- User-selected score topics:
  - single stocks,
  - portfolios,
  - custom investment universe symbols,
  - selected time spans.
- Buy strategy selection.
- Sell strategy selection.
- Full parameter-grid expansion from each selected strategy's own definition.
- Cross product of:
  - buy strategy parameter variants,
  - sell strategy parameter variants,
  - shared experiment inputs,
  - selected score topics.
- Ranking by return 90% and drawdown-control 10%.
- Scorecard-like matrix display:
  - one row per strategy parameter combination,
  - one column per selected score topic/time span,
  - each cell shows return, max drawdown, score, and rank,
  - color scale should be based on score, with return and drawdown available in tooltip/detail.
- Ability to inspect the exact parameter snapshot behind a row.
- Ability to apply a selected parameter row back into the main experiment configuration without silently changing the comparison universe.

## Non-Goals

- Do not keep patching the existing `收益 Top10` behavior until the new model is defined.
- Do not mix parameter search, scorecard display, and single-run details in one implicit payload.
- Do not add more strategy parameters directly in `web/app.py`.
- Do not implement a server-only exhaustive search for large parameter spaces.
- Do not rely on a single Web Worker that processes all work serially.

## Domain Model

Introduce explicit concepts before UI work.

### Strategy Definition

Each strategy must define:

- `strategy_key`.
- `strategy_label`.
- `strategy_type`: `buy` or `sell`.
- `compatible_buy_strategies` or `compatible_sell_strategies` when needed.
- `base_parameters`.
- `parameter_space`.
- `validate_parameters(params)`.
- `expand_parameter_variants(context)`.
- `describe_parameters(params)`.

Parameter expansion must live near the strategy implementation or in a dedicated strategy registry module, not in the template.

### Parameter Variant

Each expanded parameter variant must have:

- stable `variant_key`,
- `strategy_key`,
- normalized parameter object,
- display label,
- compatibility metadata,
- source definition version.

`variant_key` must be deterministic. It should be generated from strategy key plus canonical JSON of normalized parameters.

### Strategy Combination

A strategy combination is:

- one buy variant,
- one sell variant,
- shared experiment inputs,
- selected score topics.

The combination key must include both buy and sell variant keys.

### Score Topic

A score topic is:

- portfolio key or custom symbol key,
- normalized targets,
- period key,
- start date,
- end date,
- market-data version/freshness metadata.

## Scoring Rules

Use one ranking implementation for server and client.

For each score topic:

1. Simulate every strategy combination.
2. Collect raw `return_pct` and `max_drawdown_pct`.
3. Normalize return within that topic: bigger is better.
4. Normalize drawdown control within that topic: less negative drawdown is better.
5. Calculate topic score:
   `topic_score = return_score * 0.9 + drawdown_score * 0.1`.
6. Rank combinations inside the topic by `topic_score`.

For each strategy combination:

1. Average raw return across topics.
2. Average raw max drawdown across topics.
3. Average topic score across topics.
4. Produce final score using the same declared formula.
5. Sort descending by final score.

The result payload must include enough data to explain every rank:

- raw return per topic,
- raw max drawdown per topic,
- return score per topic,
- drawdown score per topic,
- topic score,
- topic rank,
- final aggregate score,
- final aggregate rank.

## Client-Side Compute Design

Large parameter searches should run in the browser with Web Workers.

Requirements:

- Use a worker pool sized by `navigator.hardwareConcurrency`, capped by a configurable maximum.
- Split work into independent chunks by strategy combination, not by an inner loop inside one worker.
- Verify true parallelism with timing instrumentation:
  - worker count,
  - chunks completed per worker,
  - wall time,
  - total CPU work estimate.
- Avoid shared mutable state between workers.
- Send immutable packet data to workers:
  - normalized inputs,
  - score topics,
  - candle arrays,
  - strategy combinations.
- Workers return partial rows.
- Main thread merges partial rows, computes final sorting, and renders.
- Support pause, resume, cancel.
- Progress must be based on completed simulations, not only queued chunks.

Do not claim multi-CPU acceleration until a feasibility test shows multiple workers completing chunks concurrently.

## Cache Design

Caching is allowed, but cache keys and freshness must be explicit.

Cache separately:

- market data,
- expanded parameter variants,
- simulation results,
- rendered result snapshots.

### Market Data Cache

Key must include:

- normalized symbol,
- date range,
- provider,
- adjusted/unadjusted mode if applicable,
- currency conversion assumptions if applicable.

Freshness metadata must include:

- fetched at,
- last candle date,
- provider,
- cache mode: fresh, stale-allowed, forced-refresh, offline-only.

### Simulation Result Cache

Key must include:

- app algorithm version,
- strategy definition version,
- normalized experiment inputs,
- buy variant key,
- sell variant key,
- score topic key,
- market-data cache identity.

Result cache must be invalidated when:

- strategy logic changes,
- parameter definitions change,
- scoring formula changes,
- input payload changes,
- market data changes,
- fee/currency/reserve assumptions change.

The UI must show whether results are fresh or reused from cache.

## Feasibility Gates

Before implementation, create a short feasibility note or checklist in the PR/session output.

Do not implement production code until these pass:

1. Strategy registry feasibility:
   - Can every existing buy/sell strategy expose its parameter space without duplicating template code?
   - Can incompatible combinations be excluded deterministically?
2. Scoring parity feasibility:
   - Can one small fixture produce the same rank on server and client?
   - Can the scorecard matrix and parameter lab use the same scoring helper?
3. Worker parallelism feasibility:
   - Can two or more workers process separate chunks concurrently?
   - Does increasing worker count reduce wall time on a synthetic workload?
4. Cache feasibility:
   - Is there a deterministic cache key for a simulation result?
   - Can stale market data be detected and surfaced?
5. UI feasibility:
   - Can the scorecard-like matrix render at the expected row/column size without blocking the main thread?
   - Is virtualization needed for large result sets?

If any gate fails, stop and document the blocker instead of coding around it.

## Implementation Phases

### Phase 1: Strategy Registry

Deliverables:

- Add a strategy registry module.
- Move parameter-space definitions out of frontend code.
- Add deterministic variant expansion.
- Add compatibility validation.
- Add unit tests for variant expansion and invalid combinations.

Acceptance criteria:

- Existing strategies can list their parameter variants from Python.
- Variant keys are stable across runs.
- Existing default parameters are represented as one valid variant.

### Phase 2: Shared Scoring Contract

Deliverables:

- Add shared scoring documentation and fixtures.
- Implement server scoring helper.
- Implement client scoring helper with parity fixture.
- Define result payload schema.

Acceptance criteria:

- A fixed fixture ranks identically in backend tests and frontend/client tests.
- Return and drawdown normalization are documented.
- Aggregation behavior is deterministic.

### Phase 3: Market Data Packet

Deliverables:

- Build a normalized client compute packet:
  - inputs,
  - score topics,
  - candle data,
  - strategy combinations,
  - cache metadata.
- Add payload size checks.
- Add server endpoint for preparing the packet only.

Acceptance criteria:

- Packet contains no UI-only state.
- Packet can be replayed in tests.
- Missing or stale candles are reported before compute starts.

### Phase 4: Worker Pool

Deliverables:

- Implement Web Worker pool.
- Chunk simulations by combination ranges.
- Add pause/resume/cancel.
- Add progress and timing metrics.

Acceptance criteria:

- Multiple workers complete chunks independently.
- Cancelling stops outstanding work.
- Worker errors surface to UI with actionable messages.

### Phase 5: Matrix UI

Deliverables:

- Add Parameter Lab workspace.
- Render scorecard-style matrix.
- Show final leaderboard.
- Show per-cell return/drawdown/score/rank.
- Add row detail panel for exact parameters.
- Add apply action that preserves or explicitly asks about comparison scope.

Acceptance criteria:

- Applying a row does not silently switch to a different strategy universe.
- Matrix colors reflect score consistently.
- Large result sets do not freeze the page.

### Phase 6: Caching

Deliverables:

- Add explicit cache keys.
- Add freshness indicators.
- Add result reuse path.
- Add cache invalidation rules.

Acceptance criteria:

- Same packet can reuse cached simulation results.
- Changed strategy version invalidates old simulation cache.
- Stale market data is visible before ranking is trusted.

### Phase 7: Verification and Release

Deliverables:

- Functional verification checklist.
- Unit test suite.
- Regression tests for existing scorecard and strategy lab behavior.
- Deployment checklist for Docker/Tencent Cloud.

Acceptance criteria:

- All tests pass locally.
- Existing scorecard still works.
- Existing run/score APIs are not broken.
- Docker image is rebuilt and deployed from the verified commit.

## Required Functional Verification

Before merge or deployment, verify manually or with browser automation:

- User can select score topics and periods.
- Parameter variants expand as expected.
- Invalid buy/sell combinations are excluded.
- Client packet prepares successfully.
- Worker pool uses more than one worker when available.
- Progress, pause, resume, and cancel work.
- Ranking matches the 90/10 formula.
- Matrix shows per-topic return and drawdown.
- Row detail shows exact parameters.
- Apply action preserves expected comparison scope.
- Cached result is marked as cached.
- Stale market data is marked as stale.

## Required Unit Tests

Add or update tests for:

- strategy parameter expansion,
- deterministic variant keys,
- compatibility filtering,
- score normalization,
- aggregate ranking,
- backend/client scoring parity fixture,
- cache key generation,
- cache invalidation when strategy version changes,
- payload schema validation,
- worker chunk partitioning,
- apply-action payload behavior,
- regression coverage for existing scorecard payloads.

No deployment until all unit tests pass.

## Deployment Rules

For Tencent Cloud Docker deployment:

- Build a new image from the tested commit.
- Record commit hash and image tag.
- Restart the container only after tests pass.
- Verify `/strategy-lab` loads after deployment.
- Run a small Parameter Lab smoke test in production.
- Confirm frontend assets are not served from an old browser/container cache.

## Open Design Questions

Resolve these before coding:

- Should final aggregate ranking use average topic score only, or score normalized aggregate return/drawdown again?
- What is the maximum matrix size before virtualization is mandatory?
- Should simulation result cache live in browser storage, server storage, or both?
- How should custom user-defined parameter ranges be represented?
- Should applying a row switch only the main run config, or also narrow the scorecard comparison scope?

## Definition of Done

The feature is done only when:

- Strategy parameters are defined in a reusable registry.
- The parameter lab can compute selected topics using client workers.
- Results show a scorecard-like colored matrix.
- Ranking is explainable from raw per-topic numbers.
- Cache freshness is visible.
- Functional verification is completed.
- Unit tests are comprehensive and passing.
- Docker deployment has been verified from the tested commit.
