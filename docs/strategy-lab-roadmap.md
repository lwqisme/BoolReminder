# Strategy Lab Roadmap

## Product Role

Strategy Lab is a strategy research workspace. It should let us define one experiment context, run it through different engines, inspect the result, and reuse the conclusion later.

The experiment context is now represented server-side by `StrategyLabConfig` in `drawdown/strategy_lab_config.py`. Future UI and job work should build on that model instead of adding another copy of the same fields.

## Phase 1: Unified Configuration Model

Status: in progress.

Goals:

- Keep all strategy-lab defaults in one shared model.
- Convert saved YAML defaults, API runtime payloads, and save-defaults payloads through the same validation path.
- Preserve current frontend field names and API compatibility while removing backend duplication.
- Expose clear conversion points:
  - `to_strategy_inputs()`
  - `score_weights()`
  - `option_settings()`
  - `to_legacy_defaults()`

Non-goals:

- Do not redesign the UI in this phase.
- Do not introduce background jobs yet.
- Do not persist run history yet.

## Phase 2: Workspace Information Architecture

Goal: make the UI match the research workflow.

Planned shape:

- `Experiment Config`: all editable experiment inputs.
- `Portfolio Run`: single experiment result and trade details.
- `Scorecard`: batch comparison across topics and periods.
- `Parameter Scan`: sell-rule sensitivity analysis.
- `Run History`: previous snapshots and reusable conclusions.

Implementation notes:

- Frontend should keep one `labState` object that mirrors `StrategyLabConfig`.
- Each view should render from the same state and run against the same payload builder.
- Scorecard details should open inline first; full chart navigation should carry source context.
- Result panels should always show the parameter snapshot that generated them.

## Phase 3: Tencent Cloud Market-Data Reliability

Goal: make strategy-lab usable when Longbridge or Polygon is slow from Tencent Cloud.

Planned backend:

- Introduce async jobs:
  - `POST /api/strategy-lab/jobs`
  - `GET /api/strategy-lab/jobs/<job_id>`
- Add job stages:
  - cache check
  - missing candle fetch
  - stale-cache fallback
  - simulation
  - render payload assembly
- Add explicit run modes:
  - cache preferred
  - force refresh
  - offline/stale cache only
- Prewarm default scorecard symbols and current portfolio after market close.

Planned UI:

- Show cache freshness per symbol.
- Show stage progress instead of one long loading message.
- Make Polygon option overlay a separate optional job branch so it cannot block stock strategy scoring.

## Phase 4: Experiment History and Presets

Goal: make strategy-lab a research record, not only a calculator.

Planned objects:

- `ExperimentPreset`: named reusable configuration.
- `RunSnapshot`: config, market-data cache state, result summary, and created time.
- `PinnedBaseline`: a run selected for comparison.

Planned workflows:

- Save current context as a named preset.
- Compare two runs.
- Restore a prior run's config.
- Pin a baseline and show deltas in scorecard/scan views.

## Guardrails

- Do not add new strategy-lab defaults directly in templates or routes.
- Add new config fields to `StrategyLabConfig` first, then expose them to UI/API.
- Preserve legacy default keys until the frontend is migrated to a nested state payload.
- Keep market-data fetching behind cache-aware helpers; no view should call external APIs blindly.
