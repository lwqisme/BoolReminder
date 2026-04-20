# Drawdown Reports

This folder contains the standalone drawdown report generator for single-stock trade logs.

## Modes

The script now supports two parallel price-source modes:

1. `embedded`
   Uses the price series already embedded inside an exported workbook such as `TSLATradingLogs.xlsx`.
2. `longbridge`
   Uses the workbook only as the trade log source, then pulls daily prices from Longbridge.

`auto` is the default. It prefers embedded prices and falls back to Longbridge when the workbook does not contain a usable time series.

## Inputs

### Embedded workbook mode

Expected legacy layout:

- Column `A/B`: date and close price time series
- Columns `F/G/H`: buy date, shares, buy price
- Columns `K/L/M`: sell date, shares, sell price

Example files:

- `~/Documents/TSLATradingLogs.xlsx`
- `~/Documents/XIACYTradingLogs.xlsx`
- `~/Documents/msftTradeLogs.xlsx`

### Operations-only workbook mode

Use this when the workbook only contains trade operations, for example a `tradeLogs.xlsx` exported from Google Sheets.

The parser tries to auto-detect a table with columns like:

- `date` / `日期`
- `type` / `side` / `操作`
- `shares` / `股数`
- `price` / `成交价`
- `amount` / `金额`
- `symbol` / `ticker` / `股票代码`

If the workbook contains multiple symbols, pass `--symbol`.

## Usage

Generate the default report from an embedded workbook:

```bash
python drawdown/generate_drawdown_report.py
```

Generate a report for another embedded workbook:

```bash
python drawdown/generate_drawdown_report.py \
  --input ~/Documents/XIACYTradingLogs.xlsx \
  --price-source embedded
```

Generate a Longbridge-backed version without replacing the embedded one:

```bash
python drawdown/generate_drawdown_report.py \
  --input ~/Documents/tradeLogs.xlsx \
  --price-source longbridge \
  --symbol MSFT
```

Force auto mode on a workbook and let the script choose:

```bash
python drawdown/generate_drawdown_report.py \
  --input ~/Documents/tradeLogs.xlsx \
  --symbol TSLA
```

Generate to a custom file:

```bash
python drawdown/generate_drawdown_report.py \
  --input ~/Documents/tradeLogs.xlsx \
  --price-source longbridge \
  --symbol MSFT \
  --output drawdown/output/msft_drawdown_manual.html
```

Select a specific sheet explicitly:

```bash
python drawdown/generate_drawdown_report.py \
  --input ~/Documents/tradeLogs.xlsx \
  --sheet singleChart
```

## Outputs

- Embedded mode default output: `drawdown/output/<ticker>_drawdown_draft.html`
- Longbridge mode default output: `drawdown/output/<ticker>_drawdown_longbridge.html`

Each report includes:

- price series
- all-time-high drawdown
- rolling-120d drawdown
- buy/sell markers
- buy/sell amount bars

## Optional Trade CSV

You can still merge an extra CSV with columns like `date,amount,shares,type`:

```bash
python drawdown/generate_drawdown_report.py \
  --input ~/Documents/TSLATradingLogs.xlsx \
  --trades ~/Documents/tsla_extra_trades.csv
```

## Longbridge Requirements

Longbridge mode requires:

- `longbridge` installed in the Python environment
- a working BoolReminder Longbridge config or OAuth token setup

This repo already uses Longbridge elsewhere, so drawdown mode reuses the same config path:

- `config/config.yaml`
- environment overrides from `config/config_manager.py`

Official references:

- LLM docs index: `https://open.longbridge.com/llms.txt`
- Historical candlestick API: `https://open.longbridge.com/docs/quote/pull/history-candlestick`
