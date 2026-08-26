# Data Center Facility Data Collection

The original data-collection stage of the data center project: scraping facility-level data center records by state, cleaning and imputing missing values, and joining in utility rate and ISO interconnection-queue data. Predates and feeds into the later opposition-tracking research.

Extracts structured fields (county, power capacity, zip code) from unstructured HTML using regex against page titles and body text. A full national run takes hours; batching means a network failure or rate-limit doesn't lose progress without redoing completed work.

No data is included — code only.

## What's here

- **`scrape_facilities.py`** — batched, resumable scraper (`requests` + `BeautifulSoup`) against a data center facility directory site: dedups against the existing CSV before each run, saves in batches so a long run can be interrupted safely, rotates the User-Agent per request, and extracts county/power-capacity/zip code via regex against unstructured page titles and body text. Run directly, it scrapes Virginia.
- **`scrape_facilities_tx.py`** — the same scraper run for Texas, parameterized rather than duplicated.
- **`DataCleaning.py`** — checks the scraped output for duplicates/gaps against reference lists (exact and fuzzy matching) and merges in a second data source to fill missing values.
- **`ImputingData.py`** — status normalization and missing-value imputation: Power_Capacity range midpoints, Hyperscaler-conditional mode imputation for capacity and dates, Grid_Operator name cleanup, and expanding each facility into one row per year it was active.
- **`County_Prices.py`** — joins utility rate data to counties, computing average/std commercial, industrial, and residential electricity prices grouped by county, state, ownership type, and year.
- **`iso_queue_data.py`** — pulls interconnection-queue data from grid operators (CAISO and others) via the `gridstatus` API, isolating each operator's pull so one failure (an operator requiring an API key, or blocking scrapers outright) doesn't stop the rest — external energy-market data integration.
