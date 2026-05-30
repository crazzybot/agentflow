# DCF Model Template

A five-step discounted cash flow model suitable for public equities.

## Inputs Required

| Input | Where to find |
|-------|--------------|
| Revenue (last 3 years) | Income statement |
| EBIT margin (last 3 years) | Income statement |
| D&A | Cash flow statement |
| Capex | Cash flow statement |
| Change in working capital | Cash flow statement or balance sheet delta |
| Net debt (total debt − cash) | Balance sheet |
| Diluted shares outstanding | Latest filing or earnings release |
| Risk-free rate | 10-year treasury yield |
| Equity risk premium | Use 5.5 % as default; adjust for market conditions |
| Beta | Finance data providers; or estimate from 2-year weekly returns |
| Tax rate | Effective rate from income statement; use 21 % if unavailable |

## Step 1 — Project Free Cash Flow (5 years)

```
FCF = EBIT × (1 − tax_rate) + D&A − Capex − ΔWorkingCapital
```

- Year 1–3: use analyst consensus revenue growth; apply mean EBIT margin from last 3 years
- Year 4–5: fade growth toward terminal rate
- D&A and Capex: hold at % of revenue from most recent year

## Step 2 — Terminal Value

Use the Gordon Growth Model:

```
TV = FCF_year5 × (1 + g) / (WACC − g)
```

- `g` (terminal growth rate): typically GDP growth, 2–3 % for developed markets
- Sanity-check: TV / Enterprise Value should be < 75 %; if higher, the business is
  valued almost entirely on terminal assumptions — flag this.

## Step 3 — Discount to Present Value

```
WACC = weight_equity × cost_equity + weight_debt × cost_debt × (1 − tax_rate)

cost_equity = risk_free_rate + beta × equity_risk_premium
cost_debt   = interest_expense / gross_debt  (use 5 % if unavailable)
```

Discount each year's FCF and the terminal value:

```
PV = FCF_t / (1 + WACC)^t
Enterprise Value = sum(PV_FCF) + PV_TV
```

## Step 4 — Bridge to Equity Value

```
Equity Value = Enterprise Value − Net Debt + Minority Interest adjustments
Intrinsic Price per Share = Equity Value / Diluted Shares
```

## Step 5 — Sensitivity Table

Run a 3×3 sensitivity across WACC (±1 %) and terminal growth rate (±0.5 %) using
`python_exec`. Present as a table in the output.

## Python Skeleton

```python
import numpy as np

def dcf(fcfs, terminal_growth, wacc):
    n = len(fcfs)
    tv = fcfs[-1] * (1 + terminal_growth) / (wacc - terminal_growth)
    years = np.arange(1, n + 1)
    pv_fcfs = fcfs / (1 + wacc) ** years
    pv_tv = tv / (1 + wacc) ** n
    return pv_fcfs.sum() + pv_tv

# Example
fcfs = np.array([120, 140, 160, 175, 185])  # $ millions
ev = dcf(fcfs, terminal_growth=0.025, wacc=0.09)
net_debt = 200
shares = 50  # millions
price = (ev - net_debt) / shares
print(f"Intrinsic value: ${price:.2f}")
```
