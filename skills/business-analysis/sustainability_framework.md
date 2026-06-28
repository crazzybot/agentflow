# Sustainability Framework

Reference guide for ESG data collection, scoring, and reporting framework interpretation.

---

## ESG Reporting Frameworks

| Framework | Focus | Best Used For |
|---|---|---|
| **GRI (Global Reporting Initiative)** | Broad stakeholder impact — environment, social, governance | General sustainability reporting across all industries |
| **SASB (Sustainability Accounting Standards Board)** | Financially material ESG issues by industry | Sector-specific comparability; 77 industry standards |
| **TCFD (Task Force on Climate-related Financial Disclosures)** | Climate risk and opportunity | Physical and transition climate risk in financials and strategy |
| **UN SDGs (Sustainable Development Goals)** | Contribution to 17 global goals | Alignment with international development targets |
| **EU CSRD (Corporate Sustainability Reporting Directive)** | Mandatory EU ESG disclosure (2024+) | EU-listed and large EU-operating companies |
| **CDP (formerly Carbon Disclosure Project)** | Climate, water, forests disclosures | Carbon emissions and climate governance |

**Rule of thumb:** Always identify which framework the company reports against. If a company reports only against GRI but not SASB, cross-reference the SASB standard for their industry to identify unreported material topics.

---

## Third-Party ESG Rating Providers

| Provider | Score Range | Methodology Highlight | Where to Find |
|---|---|---|---|
| **MSCI ESG Ratings** | AAA – CCC (7 levels) | Risk-adjusted; compares to industry peers; laggard/average/leader | msci.com/our-solutions/esg-investing/esg-ratings-climate-search-tool |
| **Sustainalytics** | 0–100 (lower = better managed) | Unmanaged risk absolute score; sector-adjusted | sustainalytics.com/esg-ratings |
| **S&P Global ESG Score** | 0–100 | Basis for DJSI inclusion; industry-relative | spglobal.com/esg |
| **ISS ESG** | Prime / Not Prime | Governance-weighted; binary threshold | issgovernance.com |
| **CDP Score** | A–D (A = Leadership) | Self-reported + verification; climate/water/forests | cdp.net |
| **Refinitiv ESG** | 0–100 | 630 data points; available via LSEG terminal | lseg.com |

**Important:** Scores across providers often diverge significantly for the same company due to different weightings and data sources. Always state which provider you used and acknowledge provider divergence if present.

---

## ESG Pillar Scoring Guide (1–10 scale)

### Environmental (E)
Assess and score each dimension, then compute a weighted average:

| Dimension | Weight | Key Metrics |
|---|---|---|
| GHG emissions (Scope 1+2) | 30% | tCO2e, year-on-year trend, intensity vs. revenue |
| Scope 3 emissions | 15% | Coverage, trajectory, supply chain engagement |
| Net-zero / carbon target | 20% | Target year, interim milestones, Science Based Target (SBTi) validation |
| Energy mix | 15% | % renewable, energy intensity |
| Water & waste | 10% | Water intensity, waste diversion rate |
| Biodiversity / land use | 10% | Industry-specific (mining, agriculture, real estate) |

Scoring anchor:
- **8–10**: SBTi-validated net-zero target, >80% renewable energy, top-quartile intensity
- **5–7**: Reported targets without third-party validation, mixed energy mix
- **1–4**: No meaningful targets, high intensity vs. peers, regulatory violations

### Social (S)

| Dimension | Weight | Key Metrics |
|---|---|---|
| Employee health & safety | 25% | TRIR (Total Recordable Incident Rate), fatalities |
| Labor practices & wages | 20% | Living wage commitment, unionization rate, turnover |
| DEI (Diversity, Equity, Inclusion) | 20% | Board/leadership diversity %, gender pay gap reporting |
| Supply chain standards | 20% | Supplier code of conduct, audit coverage, modern slavery disclosure |
| Community impact | 15% | Community investment %, local employment |

### Governance (G)

| Dimension | Weight | Key Metrics |
|---|---|---|
| Board independence | 25% | % independent directors, separation of Chair/CEO |
| Executive compensation | 20% | ESG-linked pay %, pay ratio (CEO vs. median employee) |
| Transparency & disclosure | 20% | Timeliness and completeness of ESG reporting |
| Anti-corruption | 20% | Bribery/corruption incidents, whistleblower policy |
| Shareholder rights | 15% | Dual-class shares, poison pills, say-on-pay approval rate |

---

## Data Collection Checklist

### Primary Sources (fetch directly)
- [ ] Latest Annual Report / Sustainability Report (company IR page)
- [ ] CDP questionnaire response (cdp.net)
- [ ] SEC/regulatory filings with ESG disclosures (10-K Item 1C for climate risk)
- [ ] Proxy statement (DEF 14A) for governance data

### Secondary Sources (search then fetch if needed)
- [ ] MSCI ESG rating page
- [ ] Sustainalytics profile
- [ ] News: ESG controversies, regulatory actions, NGO reports (last 24 months)
- [ ] Industry body benchmarks (e.g., IPIECA for oil & gas, ICMM for mining)

### Controversy Screening
Before scoring, run `web_search` for:
- `"{company}" ESG controversy OR fine OR lawsuit OR scandal site:reuters.com OR site:ft.com`
- `"{company}" EEOC OR EPA OR OSHA violation`
- Any active major litigation related to environmental or social issues

Controversies can override high self-reported scores — flag and justify any downgrade.

---

## Industry-Specific Material Topics (SASB)

| Sector | Top Material ESG Topics |
|---|---|
| Energy (Oil & Gas) | GHG emissions, methane, water management, community relations |
| Technology (Software) | Data privacy, cybersecurity, DEI, employee retention |
| Financial Services | Data security, systemic risk, fair lending, financial inclusion |
| Consumer Staples | Supply chain labor, packaging waste, product safety, water |
| Healthcare | Drug pricing, clinical trial ethics, data privacy, waste |
| Industrials | Occupational safety, GHG from manufacturing, waste, product lifecycle |
| Real Estate | Energy efficiency, tenant health, land use, building certifications |
| Mining & Materials | Tailings management, water, community consent, biodiversity |

Always look up the specific SASB standard for the company's GICS sub-industry.

---

## Common Pitfalls

- **Cherry-picked metrics**: Companies highlight favorable ESG stats and omit unfavorable ones. Cross-check with third-party sources.
- **Base-year selection**: A company may show large % improvement because it chose a bad base year. Check absolute emissions levels, not just percentage changes.
- **Boundary creep**: Ensure reported Scope 1/2 figures cover the same operational boundary across years and include subsidiaries and joint ventures consistently.
- **Proxy for governance quality**: High ISS governance score does not mean effective governance — check activist investor history and actual board decisions.
