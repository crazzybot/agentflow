# Technical Indicator Formulas and Pandas Implementations

All examples assume a pandas Series named `close` (adjusted close prices) and a
DataFrame `df` with columns `Open`, `High`, `Low`, `Close`, `Volume`.

---

## SMA — Simple Moving Average

**Formula:** `SMA_n(t) = (P_t + P_{t-1} + ... + P_{t-n+1}) / n`

**Pandas:**
```python
def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()

df["SMA_20"] = sma(df["Close"], 20)
df["SMA_50"] = sma(df["Close"], 50)
df["SMA_200"] = sma(df["Close"], 200)
```

**Interpretation:** Price above SMA → bullish; price below → bearish. Golden cross
(SMA_50 crossing above SMA_200) is a long-term bullish signal.

---

## EMA — Exponential Moving Average

**Formula:** `EMA_t = P_t * k + EMA_{t-1} * (1 - k)` where `k = 2 / (n + 1)`

**Pandas:**
```python
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

df["EMA_12"] = ema(df["Close"], 12)
df["EMA_26"] = ema(df["Close"], 26)
```

**Note:** EMA reacts faster to recent price changes than SMA. Common periods: 9, 12, 20, 26, 50, 200.

---

## RSI — Relative Strength Index

**Formula:**
```
RS = avg_gain / avg_loss   (Wilder smoothing, 14-period default)
RSI = 100 - (100 / (1 + RS))
```

**Pandas:**
```python
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))

df["RSI_14"] = rsi(df["Close"], 14)
```

**Interpretation:**
- RSI > 70 → overbought (potential reversal down)
- RSI < 30 → oversold (potential reversal up)
- Divergence between RSI and price is a leading signal

**Warm-up:** Drop the first `period` rows — they contain NaN.

---

## MACD — Moving Average Convergence Divergence

**Formula:**
```
MACD line  = EMA_12 - EMA_26
Signal line = EMA_9(MACD line)
Histogram   = MACD line - Signal line
```

**Pandas:**
```python
def macd(series: pd.Series,
         fast: int = 12,
         slow: int = 26,
         signal: int = 9) -> pd.DataFrame:
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({
        "MACD": macd_line,
        "Signal": signal_line,
        "Histogram": histogram,
    })

macd_df = macd(df["Close"])
df = df.join(macd_df)
```

**Interpretation:**
- MACD crossing above Signal → bullish momentum
- MACD crossing below Signal → bearish momentum
- Histogram growing → strengthening momentum in the current direction

**Warm-up:** First 33 bars (26 + 9 - 2) will contain NaN.

---

## Bollinger Bands

**Formula:**
```
Middle Band = SMA_20
Upper Band  = SMA_20 + 2 * std_20
Lower Band  = SMA_20 - 2 * std_20
Bandwidth   = (Upper - Lower) / Middle
%B          = (Price - Lower) / (Upper - Lower)
```

**Pandas:**
```python
def bollinger_bands(series: pd.Series,
                    period: int = 20,
                    num_std: float = 2.0) -> pd.DataFrame:
    middle = series.rolling(window=period).mean()
    std = series.rolling(window=period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    bandwidth = (upper - lower) / middle
    pct_b = (series - lower) / (upper - lower).replace(0, float("nan"))
    return pd.DataFrame({
        "BB_Middle": middle,
        "BB_Upper": upper,
        "BB_Lower": lower,
        "BB_Bandwidth": bandwidth,
        "BB_PctB": pct_b,
    })

bb_df = bollinger_bands(df["Close"])
df = df.join(bb_df)
```

**Interpretation:**
- Price touching upper band → potentially overbought; lower band → potentially oversold
- Bandwidth squeezing → low volatility period often followed by a breakout
- %B > 1 or < 0 → price outside the bands (strong trend or reversal signal)

---

## ATR — Average True Range

**Formula:**
```
True Range = max(High - Low, |High - Prev_Close|, |Low - Prev_Close|)
ATR_14     = Wilder EMA of True Range over 14 periods
```

**Pandas:**
```python
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()

df["ATR_14"] = atr(df, 14)
```

**Interpretation:** ATR measures volatility in price units. High ATR → wide swings, higher risk.
Use ATR to size stop-losses: a common rule is stop = entry ± 2 × ATR.

---

## Daily Returns and Volatility

```python
def daily_returns(series: pd.Series) -> pd.Series:
    return series.pct_change()

def annualised_volatility(series: pd.Series, trading_days: int = 252) -> float:
    return daily_returns(series).std() * (trading_days ** 0.5)

df["Daily_Return"] = daily_returns(df["Close"])
vol = annualised_volatility(df["Close"])
```

---

## OBV — On-Balance Volume

```python
def obv(df: pd.DataFrame) -> pd.Series:
    direction = df["Close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (direction * df["Volume"]).cumsum()

df["OBV"] = obv(df)
```

**Interpretation:** Rising OBV with rising price confirms the trend. Divergence warns of reversal.

---

## Complete Example: All Indicators

```python
import pandas as pd
import yfinance as yf

df = yf.download("AAPL", period="1y", auto_adjust=True)

# Trend
df["SMA_20"]  = df["Close"].rolling(20).mean()
df["SMA_50"]  = df["Close"].rolling(50).mean()
df["EMA_20"]  = df["Close"].ewm(span=20, adjust=False).mean()

# Momentum
delta = df["Close"].diff()
gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
df["RSI_14"] = 100 - 100 / (1 + gain.ewm(alpha=1/14, adjust=False).mean()
                                 / loss.ewm(alpha=1/14, adjust=False).mean())

macd_line   = df["Close"].ewm(span=12, adjust=False).mean() - df["Close"].ewm(span=26, adjust=False).mean()
df["MACD"]   = macd_line
df["Signal"] = macd_line.ewm(span=9, adjust=False).mean()
df["Hist"]   = df["MACD"] - df["Signal"]

# Volatility
mid = df["Close"].rolling(20).mean()
std = df["Close"].rolling(20).std(ddof=0)
df["BB_Upper"]  = mid + 2 * std
df["BB_Lower"]  = mid - 2 * std

prev = df["Close"].shift(1)
tr = pd.concat([df["High"]-df["Low"], (df["High"]-prev).abs(), (df["Low"]-prev).abs()], axis=1).max(axis=1)
df["ATR_14"] = tr.ewm(alpha=1/14, adjust=False).mean()

# Drop warm-up rows
df = df.dropna()
```
