# Drawdown Reports

This folder contains a standalone drawdown report generator for single-stock trade logs exported from Google Sheets as `.xlsx`.

## Inputs

- Default input: `~/Documents/TSLATradingLogs.xlsx`
- Supported format:
  - Column `A/B`: date and close price time series
  - Columns `F/G/H`: buy date, shares, buy price
  - Columns `K/L/M`: sell date, shares, sell price

## Usage

Generate the default TSLA report:

```bash
python drawdown/generate_drawdown_report.py
```

Generate a report for another exported workbook:

```bash
python drawdown/generate_drawdown_report.py \
  --input ~/Documents/XIACYTradingLogs.xlsx
```

Generate to a custom file:

```bash
python drawdown/generate_drawdown_report.py \
  --input ~/Documents/XIACYTradingLogs.xlsx \
  --output drawdown/output/xiacy_custom.html
```

## Outputs

- Default output path: `drawdown/output/<ticker>_drawdown_draft.html`
- The report includes:
  - price series
  - all-time-high drawdown
  - rolling-120d drawdown
  - buy/sell markers
  - buy/sell amount bars
