---
name: technical-analysis
description: Formulas and implementation guidance for price-based technical indicators including moving averages, momentum oscillators, and volatility bands.
---

# Technical Analysis

Formulas and implementation guidance for price-based technical indicators computed from OHLCV (Open, High, Low, Close, Volume) price data.

## Reference Documents

- `indicators.md` — Exact formulas and pandas implementations for SMA, EMA, RSI, MACD, Bollinger Bands, and ATR
- `data_sources.md` — yfinance and Alpha Vantage conventions, column names, and usage patterns

---

## Overview

Use this skill when implementing or specifying **price-derived** indicators. It covers the
full set of indicators commonly needed in stock analysis tools: trend, momentum, and volatility.

### When to use this skill (not `financial-analysis`)

| Task | Skill |
|---|---|
| Compute SMA, EMA, RSI, MACD, Bollinger Bands, ATR | `technical-analysis` |
| Compute P/E, EV/EBITDA, DCF, ROE | `financial-analysis` |
| Source stock price OHLCV data, test indicator math | `technical-analysis` |
| Source earnings, revenue, margins, analyst estimates | `financial-analysis` / `equity-research` |

### Typical Workflow

1. **Fetch OHLCV data** — Use `data_sources.md` to get the right column names for your library.
2. **Select indicators** — Choose from `indicators.md` based on what the task requires.
3. **Implement with pandas** — All formulas have a pandas idiom; prefer vectorised operations.
4. **Verify with `python_exec`** — Run a spot-check against known values before returning.
5. **Return structured output** — Include indicator values in the JSON result.

### Indicator Categories

- **Trend**: SMA, EMA — identify direction of price movement
- **Momentum**: RSI, MACD — measure speed and rate of change; signal overbought/oversold
- **Volatility**: Bollinger Bands, ATR — measure magnitude of price swings
- **Volume**: OBV, VWAP — confirm trend with volume context (see `indicators.md`)

### Common Pitfalls

- **Warm-up period**: RSI needs 14+ bars, MACD needs 26+, Bollinger Bands need 20+.
  Always drop NaN rows before returning results.
- **Adjusted vs. unadjusted close**: Use `Close` (adjusted) from yfinance for all calculations
  to account for splits and dividends.
- **EMA initialisation**: pandas `.ewm(adjust=False)` uses the recursive formula.
  `.ewm(adjust=True)` uses a weighted sum — they differ on short series.
- **RSI smoothing**: Use Wilder's smoothing (equivalent to EMA with `alpha=1/period`),
  not simple rolling average, to match standard RSI definitions.
