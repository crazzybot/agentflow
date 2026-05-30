---
name: financial-analysis
description: Expert guidance on DCF modeling, financial ratio calculation, equity valuation, and earnings quality assessment.
---

# Financial Analysis

Expert guidance on DCF modeling, financial ratio calculation, equity valuation, and earnings quality assessment.

## Reference Documents

- `dcf_model.md` — Step-by-step discounted cash flow model with worked example
- `ratio_cheatsheet.md` — Quick reference card for key financial ratios and their interpretation

---

## Overview

Use this skill when you need to produce rigorous, quantitative financial analysis. It covers
the full workflow from data gathering through model building to investment conclusion.

### Workflow

1. **Data gathering** — Use `web_search` and `fetch_url` to retrieve:
   - Latest earnings release (income statement, balance sheet, cash flow statement)
   - Trailing twelve months (TTM) figures where available
   - Analyst consensus estimates (EPS, revenue, EBITDA)
   - Macro context (sector multiples, risk-free rate, equity risk premium)

2. **Ratio analysis** — Compute the standard ratio sets with `python_exec`:
   - Profitability: gross margin, EBIT margin, net margin, ROE, ROIC
   - Leverage: net debt / EBITDA, interest coverage, debt / equity
   - Liquidity: current ratio, quick ratio, FCF conversion
   - Valuation: P/E, EV/EBITDA, P/FCF, EV/Sales
   See `ratio_cheatsheet.md` for formulas.

3. **Valuation** — Apply at least two methods and triangulate:
   - DCF (see `dcf_model.md`)
   - Comparable company analysis (sector median multiples)
   - Precedent transactions if M&A context is relevant

4. **Quality checks** — Flag any of:
   - Revenue recognition changes or one-time items inflating earnings
   - Working capital deterioration masking cash flow weakness
   - Goodwill / intangibles > 50 % of total assets
   - Auditor changes or going-concern language

5. **Output** — Return structured JSON per the system prompt schema. Every data point
   must include a source URL and date. Flag figures older than 90 days as potentially stale.

### Common Pitfalls

- **Using GAAP net income for DCF** — Always start from operating cash flow or EBITDA
  and adjust for capex, working capital changes, and taxes.
- **Ignoring dilution** — Use diluted share count for per-share metrics.
- **Stale data** — Verify that fetched pages reflect the most recent quarter/year.
- **Survivorship bias in comps** — Include sector peers that have underperformed, not only
  the commonly cited names.
