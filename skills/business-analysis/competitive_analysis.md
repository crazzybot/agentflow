# Competitive Analysis

Reference guide for identifying peers, benchmarking across financial and non-financial dimensions, and synthesising competitive positioning.

---

## Peer Selection Criteria

A valid peer set must share at least three of the following with the subject company:

1. **Same GICS sub-industry** or adjacent industry (primary filter)
2. **Revenue scale**: within 0.3×–3× of subject company revenue
3. **Geographic footprint**: overlapping primary markets (not just global vs. local)
4. **Business model**: similar revenue mix (e.g., subscription vs. transactional, B2B vs. B2C)
5. **Capital intensity**: similar capex/revenue ratio (avoid mixing asset-heavy and asset-light)
6. **Growth stage**: comparable maturity (do not benchmark a hyper-growth startup against a mature incumbent)

**Practical rule:** Aim for 3–5 peers. Fewer than 3 is insufficient for pattern recognition. More than 7 dilutes the signal.

**Finding peers:**
```
web_search: "{company} competitors OR peers OR comparable companies 2024 2025"
web_search: "{company} {industry} market share top players"
web_search: "site:sec.gov {company} 10-K competitors"  ← company's own peer disclosure
```
The 10-K "Competition" section and proxy statement "peer group" (used for executive compensation benchmarking) are authoritative peer lists — use them as the starting point.

---

## Benchmarking Metrics Table

Build a side-by-side table. Populate all cells; mark `N/A` only when data is genuinely unavailable, not merely hard to find.

### Financial Metrics

| Metric | Formula | Why It Matters |
|---|---|---|
| Revenue CAGR (3yr) | (Rev_t / Rev_t-3)^(1/3) – 1 | Growth trajectory vs. peers |
| Gross Margin | Gross Profit / Revenue | Pricing power and cost structure |
| EBIT Margin | EBIT / Revenue | Operating efficiency |
| EBITDA Margin | EBITDA / Revenue | Cash generation proxy |
| FCF Margin | Free Cash Flow / Revenue | True cash profitability |
| Return on Equity (ROE) | Net Income / Avg. Equity | Capital efficiency |
| ROIC | NOPAT / Invested Capital | Value creation vs. cost of capital |
| Net Debt / EBITDA | Net Debt / EBITDA | Leverage and financial flexibility |
| EV / EBITDA | Enterprise Value / EBITDA | Valuation relative to peers |
| P/E (Forward) | Price / Next-12m EPS consensus | Market growth expectation |
| R&D / Revenue | R&D Spend / Revenue | Innovation investment intensity |

### ESG / Sustainability Metrics

| Metric | Why It Matters |
|---|---|
| MSCI ESG Rating | Overall ESG leader/laggard vs. peers |
| Sustainalytics Unmanaged Risk Score | Absolute risk level, not relative |
| CDP Climate Score | Climate disclosure quality |
| Scope 1+2 Emissions Intensity | Emissions per $M revenue or per unit output |
| Net-Zero Target Year | Ambition and credibility |
| % Renewable Energy | Progress on decarbonisation |
| TRIR (safety) | Workforce safety vs. sector norm |
| Board Gender Diversity % | Governance best practice |

### Strategic / Operational Metrics (industry-dependent)

Select the 3–4 most relevant KPIs for the sector:

| Sector | Key Operational KPIs |
|---|---|
| SaaS / Tech | ARR, NRR, CAC payback, R&D % revenue |
| Retail | Same-store sales growth, inventory turnover, store count |
| Healthcare | Pipeline (Phase II/III count), patent cliff exposure, payer mix |
| Energy | Production cost per barrel/MWh, reserve life index |
| Banking | NIM, NPL ratio, CET1 capital ratio, loan growth |
| Industrials | Order backlog, capacity utilization, book-to-bill |
| Consumer | Brand equity index, market share %, pricing elasticity |

---

## Porter's Five Forces Assessment

Evaluate each force as **Low / Medium / High** threat to the subject company with a 2–3 sentence rationale. This contextualises the competitive metrics.

### 1. Threat of New Entrants
Key questions:
- What are the capital requirements to enter?
- Are there regulatory barriers (licenses, approvals)?
- Does the incumbent have network effects or switching costs that protect it?
- How fast is technology lowering entry barriers?

### 2. Bargaining Power of Suppliers
Key questions:
- How concentrated is the supplier base?
- Are inputs commoditised or proprietary?
- Can the company backward-integrate?
- What is the switching cost of changing suppliers?

### 3. Bargaining Power of Buyers
Key questions:
- How price-sensitive are customers?
- What are switching costs for buyers?
- Is revenue concentrated in a few large customers (>20% from one)?
- Do buyers have credible alternatives?

### 4. Threat of Substitute Products
Key questions:
- Are there fundamentally different ways to meet the same customer need?
- Is the substitute improving on cost/performance faster than the incumbent?
- What is the switching cost from incumbent to substitute?

### 5. Competitive Rivalry
Key questions:
- How many direct competitors are there, and how evenly matched?
- Is industry growth fast enough to accommodate all players, or is it zero-sum?
- Are fixed costs high (driving price wars to fill capacity)?
- Is the product commoditised or differentiated?

---

## Positioning Matrix

After completing the benchmarking table and Five Forces, place the subject company and each peer on a 2×2 matrix. Choose axes relevant to the industry — common options:

| X-Axis Options | Y-Axis Options |
|---|---|
| Profitability (EBIT margin) | Growth (Revenue CAGR) |
| ESG Score | Financial Performance |
| Market Share | Innovation (R&D %) |
| Leverage (Net Debt/EBITDA) | Valuation (EV/EBITDA) |

Summarise the positioning in 2–3 sentences:
- Is the subject company a "quality compounder" (high margin, moderate growth)?
- A "growth at a cost" player (high growth, low margin)?
- A "value trap" (cheap valuation, deteriorating fundamentals)?
- An "ESG leader" rewarded or penalised by the market vs. peers?

---

## Competitive Advantage Assessment

For each identified competitive advantage, rate **Strength (H/M/L)** and **Durability (H/M/L)**:

| Advantage Type | Examples | Tests for Durability |
|---|---|---|
| Cost leadership | Scale economies, vertical integration | Can a new entrant match at scale within 5 years? |
| Differentiation | Brand, IP, proprietary technology | Substitution threat? Patent expiry? |
| Network effects | Platform, marketplace, data flywheel | Does value compound with more users? |
| Switching costs | ERP integration, long-term contracts | Contract length, migration cost |
| Regulatory moat | Licenses, approvals, safety certifications | Pending deregulation? |
| Distribution | Exclusive channels, logistics network | Channel disintermediation risk? |

---

## Competitive Intelligence Sources

```
web_search: "{company} market share {year}"
web_search: "{company} vs {competitor} comparison"
web_search: "{industry} competitive landscape report {year}"
fetch_url: company investor day presentations (strategy slides)
fetch_url: industry analyst reports (Gartner, IDC, Forrester for tech; Wood Mackenzie for energy)
fetch_url: trade press (sector-specific publications)
```

For public companies: the 10-K "Competition" and "Risk Factors" sections are primary sources — read them before secondary commentary.
