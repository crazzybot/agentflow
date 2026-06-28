# Future Outlook Analysis

Reference guide for synthesising management guidance, growth drivers, market sizing, and scenario planning into a structured forward view.

---

## Sources for Forward-Looking Information

Collect in this order — primary sources before secondary commentary:

| Source | What to Extract | Where to Find |
|---|---|---|
| Latest earnings call transcript | Guidance (revenue, margin, capex), tone, analyst Q&A pushback | Seeking Alpha, Motley Fool Transcripts, company IR page |
| Investor Day / Capital Markets Day | 3–5 year targets, strategic roadmap, segment growth assumptions | Company IR page (Events section) |
| Annual Report (Letter to Shareholders) | CEO strategic narrative, priorities, acknowledged risks | Company IR page |
| SEC 10-K / 20-F (Outlook section) | Formal forward guidance, material uncertainties | sec.gov EDGAR |
| Sell-side consensus (Bloomberg, FactSet) | Revenue/EPS CAGR consensus, price targets, rating distribution | web_search for analyst estimates |
| Industry research reports | TAM/SAM sizing, sector growth rate, technology disruption timeline | Gartner, IDC, McKinsey, BCG public reports |

**Search queries:**
```
web_search: "{company} earnings call transcript Q{N} {year}"
web_search: "{company} investor day {year} presentation"
web_search: "{company} analyst price target consensus {year}"
web_search: "{industry} market size forecast {year} CAGR"
```

---

## Management Guidance Synthesis

### Reading Guidance Language

Management language carries signal. Categorise each statement:

| Language Pattern | Signal |
|---|---|
| "We expect", "we are guiding to", specific numeric range | Firm guidance — treat as base case |
| "We are well-positioned", "strong momentum" | Qualitative confidence — no numeric commitment |
| "Subject to macro conditions", "assuming no deterioration" | Contingent guidance — note the condition |
| Lowering guidance range vs. prior quarter | Execution risk signal — investigate root cause |
| Widening guidance range vs. prior quarter | Increased uncertainty — factor into bear case |
| Providing long-range targets for the first time | Strategy shift or confidence inflection — positive signal |
| Withdrawing guidance entirely | High uncertainty — widen scenario spread |

### Guidance vs. Track Record

Compare the last 4–8 quarters of guidance to actual results:
- Beat rate: what % of quarters did the company beat its own guidance?
- Magnitude: by how much on average?
- A consistent beater with conservative guidance → management credibility high
- Frequent misses → weight bear case more heavily; discount stated targets

---

## Growth Driver Identification

Identify 3–5 primary growth drivers for the next 3–5 years. For each driver, document:

```
Driver: {name}
Type: Organic / Inorganic / Macro tailwind / Regulatory
Description: {1–2 sentences}
Revenue impact: {$M or % revenue contribution in X years}
Confidence: High / Medium / Low
Evidence: {source, date}
Key risk: {single biggest thing that could neutralise this driver}
```

### Common Growth Driver Categories

| Category | Examples |
|---|---|
| Product/service expansion | New product lines, adjacent market entry |
| Geographic expansion | Emerging market penetration, new country launches |
| Pricing power | Price increases above inflation, premium tier migration |
| Volume growth | Market share gains, category growth |
| M&A / Inorganic | Pipeline of targets, integration of recent acquisitions |
| Regulatory tailwind | New mandates driving demand (e.g., EV charging, carbon markets) |
| Technology disruption (own) | AI/automation cost savings flowing to margin |
| Customer retention / NRR | Upsell, cross-sell, reduced churn |

---

## Market Sizing (TAM / SAM / SOM)

| Term | Definition | How to Estimate |
|---|---|---|
| **TAM** (Total Addressable Market) | Global revenue if 100% market share | Industry reports, regulatory filings, bottom-up unit × price |
| **SAM** (Serviceable Addressable Market) | TAM filtered to segments the company can realistically serve | Apply geographic, regulatory, and capability constraints |
| **SOM** (Serviceable Obtainable Market) | Near-term realistic capture given competitive position | Historical share growth rate × SAM |

Always triangulate TAM with at least two sources. Flag if company-disclosed TAM exceeds reputable third-party estimates — this is a common optimism signal.

**Market sizing search queries:**
```
web_search: "{industry} TAM market size {year} billion forecast"
web_search: "{industry} CAGR research report {year}"
fetch_url: Gartner / IDC / MarketsandMarkets report summaries (free tier)
```

---

## Scenario Planning Framework

Build three scenarios. Each must have explicit, falsifiable assumptions — not just "optimistic" labels.

### Bull Case
- Assumption set: top-quartile execution, favourable macro, 1–2 growth catalysts materialise early
- Revenue CAGR: upper end of management guidance or +200–400bps above consensus
- Margin: expansion driven by operating leverage + pricing
- Valuation: re-rate toward sector leaders if ESG / governance improves
- Trigger: what specific event would confirm this scenario? (e.g., new product launch, market share data, contract win)

### Base Case
- Assumption set: in-line with consensus analyst estimates; management delivers on stated targets
- Revenue CAGR: consensus median
- Margin: in-line with guidance; incremental improvement
- Valuation: current sector median multiple
- Trigger: steady state; quarterly results within ±5% of consensus

### Bear Case
- Assumption set: one key growth driver disappoints, macro headwind, or competitive pressure intensifies
- Revenue CAGR: bottom-quartile peer or -200–400bps below consensus
- Margin: compression from fixed cost deleveraging or pricing erosion
- Valuation: de-rate toward sector laggards; ESG controversies may amplify discount
- Trigger: what specific event would confirm this scenario? (e.g., customer churn spike, regulatory fine, guidance cut)

### Scenario Weighting
Assign probability weights that sum to 100%. Default starting point:
- Bull: 25%
- Base: 55%
- Bear: 20%

Adjust weights based on management credibility, competitive position strength, and macro environment clarity. Document the rationale for any deviation from defaults.

---

## Analyst Consensus Reading

Collect and summarise sell-side consensus:

| Metric | What to Report |
|---|---|
| Rating distribution | % Buy / Hold / Sell across all covering analysts |
| Median price target | And implied upside/downside from current price |
| Revenue consensus CAGR (3yr) | Median analyst estimate |
| EPS consensus CAGR (3yr) | Median analyst estimate |
| Number of analysts covering | More coverage = more efficient pricing; thin coverage = opportunity |
| Recent rating changes | Upgrades / downgrades in last 90 days — momentum signal |

**Important**: Consensus is the market's current view, not truth. A company with 80% Buy ratings is not necessarily a buy — it may already be fully priced. Use consensus as a reference point, not a conclusion.

---

## ESG and Sustainability Trajectory (Forward View)

Sustainability is not static — assess the direction of travel:

- Is the company on track to meet its own 2025/2030 targets? (compare actuals vs. linear path)
- Are near-term milestones (interim emissions targets, renewable energy % goals) being hit?
- Regulatory tailwinds/headwinds: what pending regulations could increase compliance cost or unlock revenue?
- Investor pressure: any active engagement from major institutional shareholders on ESG issues?
- Is the company increasing or decreasing R&D and capex in sustainable products/services?

A company with strong current ESG scores but deteriorating trajectory is more concerning than one with moderate scores and improving trajectory.

---

## Output: Forward View Summary

Synthesise all of the above into:

```json
{
  "outlook_horizon_years": 3,
  "revenue_cagr_bull": 0.18,
  "revenue_cagr_base": 0.12,
  "revenue_cagr_bear": 0.05,
  "margin_direction": "expanding | stable | contracting",
  "key_growth_drivers": ["driver_1", "driver_2", "driver_3"],
  "key_risks": ["risk_1", "risk_2", "risk_3"],
  "analyst_consensus_rating": "buy | hold | sell",
  "analyst_price_target_upside_pct": 0.14,
  "sustainability_trajectory": "improving | stable | deteriorating",
  "scenario_weights": {"bull": 0.25, "base": 0.55, "bear": 0.20},
  "confidence": "high | medium | low",
  "confidence_rationale": "..."
}
```
