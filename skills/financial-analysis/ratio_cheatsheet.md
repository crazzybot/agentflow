# Financial Ratio Cheatsheet

Quick-reference formulas, typical ranges, and red-flag thresholds.

## Profitability

| Ratio | Formula | Healthy range | Red flag |
|-------|---------|---------------|----------|
| Gross Margin | Gross Profit / Revenue | Industry-dependent | YoY compression > 3 pp |
| EBIT Margin | EBIT / Revenue | > 10 % (most sectors) | Negative or declining |
| Net Margin | Net Income / Revenue | > 5 % | Diverges from EBIT trend |
| ROE | Net Income / Avg Equity | > 15 % | Inflated by high leverage |
| ROIC | NOPAT / Invested Capital | > WACC | < WACC = value destruction |
| FCF Conversion | FCF / Net Income | > 80 % | < 60 % — earnings quality concern |

```
NOPAT = EBIT × (1 − effective_tax_rate)
Invested Capital = Total Equity + Net Debt
```

## Leverage

| Ratio | Formula | Healthy range | Red flag |
|-------|---------|---------------|----------|
| Net Debt / EBITDA | Net Debt / EBITDA | < 3× | > 5× |
| Interest Coverage | EBIT / Interest Expense | > 3× | < 1.5× |
| Debt / Equity | Total Debt / Equity | < 1× | > 2× (non-financial) |

## Liquidity

| Ratio | Formula | Healthy range | Red flag |
|-------|---------|---------------|----------|
| Current Ratio | Current Assets / Current Liabilities | 1.5–3× | < 1× |
| Quick Ratio | (Cash + Receivables) / Current Liabilities | > 1× | < 0.7× |
| Cash Conversion Cycle | DIO + DSO − DPO | Shorter = better | Rising YoY |

```
DIO = Inventory / (COGS / 365)
DSO = Receivables / (Revenue / 365)
DPO = Payables / (COGS / 365)
```

## Valuation Multiples

| Multiple | Formula | Notes |
|----------|---------|-------|
| P/E | Price / EPS | Use forward EPS; adjust for non-recurring items |
| EV/EBITDA | EV / EBITDA | Most robust cross-sector comp; excludes D&A |
| EV/EBIT | EV / EBIT | Better for capex-heavy businesses |
| P/FCF | Price / FCF per Share | Best cash-based valuation signal |
| EV/Sales | EV / Revenue | Use for pre-profit or high-growth companies |
| PEG | P/E / EPS Growth Rate | < 1 often considered undervalued |

```
Enterprise Value = Market Cap + Net Debt + Minority Interest + Preferred Equity
```

## Python Snippet

```python
def compute_ratios(data: dict) -> dict:
    rev = data["revenue"]
    ebit = data["ebit"]
    net_income = data["net_income"]
    fcf = data["fcf"]
    equity = data["equity"]
    net_debt = data["net_debt"]
    ebitda = data["ebitda"]
    interest = data["interest_expense"]

    return {
        "ebit_margin": ebit / rev,
        "net_margin": net_income / rev,
        "roe": net_income / equity,
        "net_debt_ebitda": net_debt / ebitda,
        "interest_coverage": ebit / interest if interest else None,
        "fcf_conversion": fcf / net_income if net_income else None,
    }
```
