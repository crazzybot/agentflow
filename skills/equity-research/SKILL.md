---
name: equity-research
description: Systematic workflow for equity research: source selection, due diligence, thesis construction, and risk identification.
---

# Equity Research

Systematic workflow for equity research: source selection, due diligence, thesis construction, and risk identification.

## Reference Documents

- `sources_guide.md` — Authoritative data sources, what each covers, and how to read them efficiently

---

## Overview

This skill guides the full equity research process from initial screening to a written investment thesis.
It is designed to work alongside the `financial_analysis` skill for quantitative work.

### Research Workflow

1. **Define the question** — Be precise:
   - "Is this stock fairly valued vs. peers?" → valuation-focused
   - "What are the key risks to the thesis?" → risk-focused
   - "Is management executing on guidance?" → operational tracking

2. **Primary sources first** — Always read the source document before secondary commentary:
   - SEC filings (10-K, 10-Q, 8-K, DEF 14A proxy)
   - Earnings call transcripts (management tone, guidance language, analyst Q&A)
   - Investor day presentations
   See `sources_guide.md` for URLs and reading strategy.

3. **Identify the key drivers** — For each business, find the 2–3 metrics that determine
   95 % of earnings variability. Common examples:
   - Retail: same-store sales growth, margin
   - SaaS: ARR, NRR, CAC/LTV
   - Banks: NIM, credit quality, loan growth
   - Industrials: utilization rate, backlog

4. **Construct the thesis** — One paragraph answering:
   - What does the market believe? (consensus)
   - What do you believe differently? (differentiated view)
   - What would prove you wrong? (variant risk)

5. **Stress test** — Model a bear case where the key driver disappoints by 1 standard
   deviation. Is the downside acceptable? What is the expected value across scenarios?

### Red Flags in Filings

- **Related-party transactions** — listed in footnotes; warrant scrutiny
- **Frequent restatements or auditor changes**
- **Revenue concentration** > 20 % from one customer
- **Insider selling** > 30 % of holdings in a 6-month window
- **"Non-GAAP" adjustments** that are recurring in practice
- **Going-concern language** in auditor notes

### Writing the Investment Thesis

Structure: Situation → Complication → Resolution

- **Situation**: What is the business and what does the market currently price in?
- **Complication**: What has changed or what does the market misunderstand?
- **Resolution**: Why will the gap close, and over what timeframe?

Keep it to three paragraphs maximum. Every claim must be traceable to a specific data source.
