# Stock Price Data Sources

Reference for fetching OHLCV data inside the agent sandbox. Both sources below are
well-known — **do not use `fetch_url` to look up their documentation**. Use `python_exec`
to verify calls and check data shape instead.

---

## yfinance (primary — no API key required)

**Install:** `pip install yfinance`

**Basic usage:**
```python
import yfinance as yf

# Single ticker, date range
df = yf.download("AAPL", start="2024-01-01", end="2024-12-31", auto_adjust=True)

# Single ticker, relative period
df = yf.download("AAPL", period="6mo", interval="1d", auto_adjust=True)

# Multiple tickers → MultiIndex columns
df = yf.download(["AAPL", "MSFT", "GOOGL"], period="1y", auto_adjust=True)

# Ticker metadata
info = yf.Ticker("AAPL").info  # dict with sector, marketCap, PE, etc.
```

**Column names** (with `auto_adjust=True`):
| Column | Description |
|---|---|
| `Open` | Opening price (adjusted) |
| `High` | Daily high (adjusted) |
| `Low` | Daily low (adjusted) |
| `Close` | Closing price (adjusted for splits and dividends) |
| `Volume` | Share volume |

**Important:** Always use `auto_adjust=True` so `Close` is the split/dividend-adjusted
price — this is required for accurate technical indicator calculations.

**Multi-ticker column access:**
```python
# With multiple tickers, columns become a MultiIndex
close_prices = df["Close"]          # DataFrame: one column per ticker
aapl_close   = df["Close"]["AAPL"]  # Series for AAPL
```

**Period shortcuts:** `1d`, `5d`, `1mo`, `3mo`, `6mo`, `1y`, `2y`, `5y`, `10y`, `ytd`, `max`

**Interval options:** `1m`, `2m`, `5m`, `15m`, `30m`, `60m`, `90m`, `1h`, `1d`, `5d`, `1wk`, `1mo`, `3mo`
Note: intraday data (< `1d`) is only available for the last 60 days.

**Empty DataFrame guard:**
```python
if df.empty:
    raise ValueError(f"No data returned for ticker — check symbol and date range")
```

---

## Alpha Vantage (secondary — requires API key)

**Install:** `pip install alpha_vantage`

**Free tier limits:** 25 requests/day, 5 requests/minute.

**Basic usage:**
```python
from alpha_vantage.timeseries import TimeSeries
import os

ts = TimeSeries(key=os.environ["ALPHA_VANTAGE_API_KEY"], output_format="pandas")

# Daily adjusted (split/dividend adjusted)
df, meta = ts.get_daily_adjusted("AAPL", outputsize="full")
```

**Column names** (Alpha Vantage returns numbered keys by default):
| Key | Description |
|---|---|
| `1. open` | Open price |
| `2. high` | High price |
| `3. low` | Low price |
| `4. close` | Unadjusted close |
| `5. adjusted close` | Split and dividend adjusted close ← use this |
| `6. volume` | Share volume |
| `7. dividend amount` | Dividend paid |
| `8. split coefficient` | Split ratio |

**Rename for consistency with yfinance:**
```python
df = df.rename(columns={
    "1. open": "Open",
    "2. high": "High",
    "3. low": "Low",
    "5. adjusted close": "Close",  # use adjusted close
    "6. volume": "Volume",
})
df.index = pd.to_datetime(df.index)
df = df.sort_index()  # Alpha Vantage returns newest-first
```

---

## Choosing a Source

| Criterion | yfinance | Alpha Vantage |
|---|---|---|
| API key required | No | Yes |
| Rate limit | Unofficial (generous) | 25 req/day free |
| Historical depth | ~50 years | 20 years |
| Reliability | Depends on Yahoo | Stable |
| Recommendation | **Default** | Fallback / verification |

Use yfinance as the default. Fall back to Alpha Vantage only if the API key is configured
(`ALPHA_VANTAGE_API_KEY` env var is set) and yfinance returns empty data.
