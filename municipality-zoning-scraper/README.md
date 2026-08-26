# Municipality Zoning Scraper

Tooling to determine whether industrial/data-center use is allowed *by right* under a municipality's zoning code — i.e. without a discretionary approval step — by scraping municipal code hosting platforms. Sweeping close to 4,000 municipalities across 46 states. Written for the data center opposition project's zoning-regulation research.

No scraped results or source data are included — code only.

## The problem

Municipal zoning codes are scattered across a handful of hosting platforms (Municode, eCode360, American Legal), inconsistently structured, often JavaScript-rendered, and sometimes behind Cloudflare. There's no unified API. Getting a reliable Yes/No/Unknown answer for "is industrial use by-right here" at scale required a layered approach, escalating in cost only when a cheaper method failed — and iterating through several versions as new blockers showed up.

## What's here

- **`br_sweep.py`** (2,200 lines) — the main tool. For each city, tries sources in order: a Municode deep-link (found via search — the generic Municode landing page is never scraped directly, since it's always JS-rendered and unusable), eCode360, American Legal, the city's own website, then PDF zoning documents. Extracts evidence windows (surrounding text + full HTML table blocks) around key phrase hits for later review, makes its own Yes/No/Unknown determination with a confidence score, and is fully resumable — designed to sweep hundreds of cities across multiple runs without redoing completed work.
- **`pw_test_municode.py`** — the discovery script that found Municode's undocumented internal JSON API (`library.municode.com/api/...`), which returns structured data directly and avoids needing to render Municode's Angular front-end at all.
- **`auto_research_tx_br_v4.py`** — a later iteration adding `cloudscraper` to get past Cloudflare's JS challenge on eCode360/American Legal, and DuckDuckGo search (instead of Google, which was rate-limiting) to locate each city's code page.
- **`browser_research_tx_br.py`** — the escalation path for cities where none of the above worked: real browser automation via Playwright, for pages that only render their content after JavaScript executes.

Together these show the actual iteration path — start cheap (direct API), get cheaper information first (static HTML), and only pay the cost of a real browser for the sites that genuinely require it.
