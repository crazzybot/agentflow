---
name: python-data-analysis
description: Patterns and best practices for data analysis with pandas, numpy, and matplotlib inside the agent's python_exec environment.
---

# Python Data Analysis

Patterns and best practices for data analysis with pandas, numpy, and matplotlib inside the agent's python_exec environment.

## Reference Documents

- `pandas_patterns.md` — Common pandas idioms for loading, cleaning, transforming, and summarising data

---

## Overview

This skill covers practical data analysis inside the `python_exec` sandbox. The sandbox has
access to pandas, numpy, scipy, and matplotlib (no display — save plots to files).

### Environment Assumptions

- Python 3.11+
- Available: `pandas`, `numpy`, `scipy`, `matplotlib`, `json`, `csv`, `pathlib`
- Working directory: the agent workspace (use relative paths)
- No internet access from within `python_exec` — fetch data with other tools first,
  save to a file, then read it in Python

### Typical Workflow

1. **Fetch / read data** — Use `file_read` or fetch tools to get raw data, then save to workspace
2. **Load in Python** — `pd.read_csv(...)`, `pd.read_json(...)`, or parse manually
3. **Inspect** — `.info()`, `.describe()`, `.head()`, `.value_counts()`
4. **Clean** — handle nulls, fix dtypes, remove duplicates, normalise strings
5. **Transform** — groupby, merge, pivot, resample
6. **Analyse** — compute statistics, run regressions, identify anomalies
7. **Output** — print structured JSON for the agent to capture; save charts to files

### Output Convention

Always end a python_exec block with a `print(json.dumps(result))` where `result` is
a dict containing the key findings. The agent captures stdout as the tool result.

```python
import json
result = {
    "summary": "...",
    "statistics": {"mean": ..., "std": ...},
    "insights": ["...", "..."],
}
print(json.dumps(result))
```

### Handling Large Data

- For files > 10 MB, use `pd.read_csv(..., chunksize=10_000)` and process iteratively
- Use `.astype("category")` for low-cardinality string columns to reduce memory
- Avoid `.iterrows()` — use vectorised operations or `.apply()` on Series

### Saving Charts

```python
import matplotlib
matplotlib.use("Agg")  # non-interactive backend — required in sandbox
import matplotlib.pyplot as plt

fig, ax = plt.subplots()
ax.plot(x, y)
fig.savefig("chart.png", dpi=150, bbox_inches="tight")
plt.close(fig)
print("Saved chart.png")
```

Then use `file_read` to confirm the file exists, or reference the path in the output JSON.

### Common Pitfalls

- **Mutable default args** — avoid `def f(df=pd.DataFrame()):`
- **Silent type coercion** — after a merge, verify dtypes haven't changed
- **Timezone-naive datetimes** — use `pd.to_datetime(..., utc=True)` for time series work
- **Division by zero** — guard with `np.where(denom != 0, num / denom, np.nan)`
