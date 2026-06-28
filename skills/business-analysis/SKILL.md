---
name: business-analysis
description: Structured workflow for company sustainability assessment, future outlook analysis, and competitive benchmarking across ESG, strategy, and market positioning dimensions.
---

# Business Analysis

Structured workflow for company sustainability assessment, future outlook analysis, and competitive benchmarking across ESG, strategy, and market positioning dimensions.

## Reference Documents

- `sustainability_framework.md` — ESG scoring methodologies, reporting frameworks (GRI, SASB, TCFD, UN SDGs), data sources, and common pitfalls
- `competitive_analysis.md` — Competitor identification, benchmarking metrics, Porter's Five Forces, positioning matrix, and peer selection criteria
- `future_outlook.md` — Growth driver analysis, market sizing, scenario planning, management guidance synthesis, and analyst consensus reading

---

## Overview

Use this skill when producing a holistic company assessment that goes beyond financial ratios to cover sustainability posture, strategic trajectory, and competitive standing. It coordinates three analytical lenses — sustainability, future outlook, and competitive comparison — into a unified output.

### Workflow

1. **Company profiling** — Establish baseline facts before any scoring:
   - Business model, revenue segments, geographies
   - Key stakeholders: customers, suppliers, regulators, communities
   - Industry classification (GICS sector/sub-industry or NAICS code)
   - Relevant regulatory environment (emissions rules, labor law, data privacy)

2. **Sustainability assessment** — Follow `sustainability_framework.md`:
   - Collect ESG ratings (MSCI, Sustainalytics, CDP, ISS) via `web_search` and `fetch_url`
   - Map reported metrics against the applicable SASB standard for the industry
   - Score E, S, G pillars separately (1–10 scale) with evidence citations
   - Identify material ESG risks specific to the sector

3. **Future outlook analysis** — Follow `future_outlook.md`:
   - Extract management guidance from latest earnings call transcript and investor day
   - Identify 3–5 key growth drivers (organic and inorganic)
   - Apply market sizing for the primary addressable market
   - Model three scenarios (bull / base / bear) with explicit assumptions
   - Note analyst consensus (revenue CAGR, margin trajectory, price targets)

4. **Competitive benchmarking** — Follow `competitive_analysis.md`:
   - Select 3–5 direct competitors using `competitive_analysis.md` peer criteria
   - Build a side-by-side table: financial metrics, ESG scores, strategic initiatives
   - Apply Porter's Five Forces to the industry context
   - Assess relative positioning: where is the subject company stronger or weaker

5. **Synthesis and output** — Combine all three lenses into the structured JSON schema
   defined in the agent system prompt. Every claim must cite a source URL and date.

### Common Pitfalls

- **Greenwashing signals** — Companies may report ESG metrics selectively; always cross-check
  self-reported data against third-party ratings and regulatory filings.
- **Survivorship bias in peers** — Include competitors that have struggled, not only the
  industry darlings; this reveals structural vs. company-specific risks.
- **Conflating short-term guidance with long-term outlook** — Management guidance covers
  1–2 quarters; future outlook requires a 3–5 year strategic view.
- **Treating ESG scores as comparable across providers** — MSCI and Sustainalytics use
  different methodologies; state which provider's score you are using and why.
- **Ignoring regulatory pipeline** — Pending legislation (carbon pricing, due diligence
  directives) can materially shift sustainability scores and competitive dynamics.
