# Equity Research Sources Guide

Authoritative sources for equity research, what each contains, and how to read them efficiently.

## SEC Filings (EDGAR)

**Base URL**: `https://www.sec.gov/cgi-bin/browse-edgar`

| Filing | Frequency | Key sections |
|--------|-----------|-------------|
| 10-K | Annual | Business description, Risk Factors, MD&A, Financial Statements, Notes |
| 10-Q | Quarterly | MD&A, condensed financials, legal proceedings |
| 8-K | Event-driven | Earnings releases, M&A, CEO changes, guidance updates |
| DEF 14A | Annual | Executive compensation, board composition, shareholder votes |

**Reading strategy for 10-K**:
1. Start with **Item 1A (Risk Factors)** — note any new risks vs. prior year
2. Read **MD&A** for management's own explanation of results
3. Check **Notes to Financial Statements** for accounting policy changes, off-balance-sheet items
4. Compare **auditor's report** — scope limitations or going-concern language are serious

**Direct EDGAR search**: `https://efts.sec.gov/LATEST/search-index?q=%22TICKER%22&dateRange=custom&startdt=YYYY-01-01&forms=10-K`

## Earnings Transcripts

- **Seeking Alpha**: `https://seekingalpha.com/symbol/TICKER/earnings/transcripts`
- **Motley Fool**: often has free transcripts
- **Company IR page**: look for "Events & Presentations" or "Earnings"

**What to focus on**:
- Prepared remarks: any change in guidance language ("we expect" vs. "we are confident")
- Analyst Q&A: note which questions management deflects or answers vaguely
- Tone shifts: unusual defensiveness about a metric often signals a problem

## Financial Data

| Source | Best for | Notes |
|--------|----------|-------|
| Yahoo Finance (`finance.yahoo.com/quote/TICKER`) | Quick snapshot, trailing multiples | TTM figures; check data freshness |
| Macrotrends (`macrotrends.net/stocks/charts/TICKER`) | Historical time series | 10-20 year histories; good for trend analysis |
| Wisesheets / Stockanalysis.com | Structured financials | `stockanalysis.com/stocks/TICKER/financials/` |
| FRED (`fred.stlouisfed.org`) | Macro context | Risk-free rates, sector indices, GDP |

## Analyst Estimates & Consensus

- **Yahoo Finance Earnings page**: `https://finance.yahoo.com/quote/TICKER/analysis`
  - Consensus EPS and revenue estimates
  - Number of analysts; estimate revision trend (more important than absolute level)
- **Estimate revision trend**: if consensus EPS has been cut 3+ times in a quarter, that is a
  strong negative signal regardless of the absolute multiple.

## Short Interest

- **FINRA**: `https://finra-markets.morningstar.com/MarketData/EquityOptions/detail.jsp?query=TICKER`
- **Ortex / Shortsight (free tier)**: short interest as % of float; days to cover
- High short interest (> 20 % of float) means the bear case is already known; look for
  what the shorts might be missing.

## News & Sentiment

Use `web_search` with targeted queries rather than generic news aggregators:
- `"TICKER" site:sec.gov` — recent filings
- `"TICKER" earnings call transcript 2025` — most recent transcript
- `"TICKER" CFO departure OR restatement OR SEC investigation` — risk flags
