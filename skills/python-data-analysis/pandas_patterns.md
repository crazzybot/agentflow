# Pandas Patterns

Common pandas idioms for data wrangling inside python_exec.

## Loading Data

```python
import pandas as pd

# CSV
df = pd.read_csv("data.csv", parse_dates=["date_col"], dtype={"id": str})

# JSON (records orientation)
df = pd.read_json("data.json", orient="records")

# From a JSON string (e.g., from a prior tool result)
import json
records = json.loads(raw_json_string)
df = pd.DataFrame(records)

# Excel (if openpyxl is available)
df = pd.read_excel("data.xlsx", sheet_name="Sheet1")
```

## Inspection

```python
df.info()             # dtypes, null counts, memory
df.describe()         # stats for numeric cols
df.head(10)
df.dtypes
df.isnull().sum()     # null count per column
df.duplicated().sum() # duplicate row count
df["col"].value_counts(dropna=False)
```

## Cleaning

```python
# Drop fully-duplicate rows
df = df.drop_duplicates()

# Drop columns that are entirely null
df = df.dropna(axis=1, how="all")

# Fill nulls
df["col"] = df["col"].fillna(0)
df["col"] = df["col"].ffill()  # forward-fill time series

# Fix dtypes
df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True)
df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

# Normalise strings
df["name"] = df["name"].str.strip().str.lower()

# Rename columns to snake_case
df.columns = df.columns.str.strip().str.lower().str.replace(r"\s+", "_", regex=True)
```

## Filtering & Selection

```python
# Boolean filter
mask = (df["value"] > 100) & (df["category"] == "A")
filtered = df[mask]

# Select columns
subset = df[["col1", "col2", "col3"]]

# Query syntax (readable for complex conditions)
result = df.query("value > 100 and category == 'A'")

# Top N per group
top_n = df.groupby("group").apply(lambda g: g.nlargest(5, "value")).reset_index(drop=True)
```

## Aggregation

```python
# Simple groupby
summary = df.groupby("category").agg(
    total=("amount", "sum"),
    mean=("amount", "mean"),
    count=("amount", "count"),
    max=("amount", "max"),
).reset_index()

# Multiple group keys
by_date_cat = df.groupby(["date", "category"])["revenue"].sum().unstack(fill_value=0)

# Weighted average
def wavg(g, val_col, wt_col):
    return (g[val_col] * g[wt_col]).sum() / g[wt_col].sum()

df.groupby("group").apply(wavg, "return", "weight")
```

## Time Series

```python
# Set datetime index
df = df.set_index("date").sort_index()

# Resample to monthly
monthly = df["value"].resample("ME").sum()

# Rolling statistics
df["rolling_mean_30d"] = df["value"].rolling("30D").mean()

# Period-over-period change
df["yoy_pct"] = df["value"].pct_change(periods=252)  # ~1 trading year
```

## Merge & Join

```python
# Inner join
merged = pd.merge(left, right, on="id", how="inner")

# Left join with suffix disambiguation
merged = pd.merge(left, right, on="key", how="left", suffixes=("_left", "_right"))

# Verify no row explosion after merge
assert len(merged) == len(left), f"Row count changed: {len(left)} → {len(merged)}"
```

## Reshaping

```python
# Wide to long
long = pd.melt(df, id_vars=["date"], value_vars=["col1", "col2"], var_name="metric", value_name="value")

# Long to wide
wide = long.pivot_table(index="date", columns="metric", values="value", aggfunc="sum")

# Transpose
df_T = df.set_index("label").T
```

## Output as JSON

```python
import json

# Records (list of dicts)
records = df.to_dict(orient="records")

# Summary stats dict
stats = df["value"].describe().to_dict()

# Combine and print for agent capture
print(json.dumps({"records": records[:50], "stats": stats}, default=str))
```
