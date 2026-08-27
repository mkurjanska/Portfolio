# Portfolio

I am a data analyst and researcher building data products at LocalQ Labs, an early-stage startup mapping and predicting community opposition to data center development across U.S. markets. These projects are the actual- redacted- pipeline, modelling, and monitoring code from that work.

Each folder is self-contained: a README with context, code/notebooks, and a `requirements.txt` for what it actually imports. No proprietary or raw data is included anywhere in this repo — where a project needs data to be meaningful, the README says so and points to the source project instead.

## Projects

### [Municipality Zoning Scraper](municipality-zoning-scraper/)
A layered tool for determining whether industrial use is allowed by-right across hundreds of municipal zoning codes — escalating from a discovered internal API, to static HTML scraping, to full browser automation only where necessary.

**Skills:** iterative scraping-resilience engineering, API reverse-engineering, Playwright browser automation, anti-bot handling.

### [News Alert & County Meeting Monitoring Automation](news-alert-monitoring-automation/)
A maintained, cron-scheduled GitHub Actions system tracking data center news and county government meeting activity — not a one-off script, but something that runs unattended and keeps working.

**Skills:** GitHub Actions/CI scheduling, RSS + full-text scraping, third-party API validation (Legistar), URL-rot handling at scale.

### [Predictive Models](predictive-models/)
Calibrated logistic-regression models predicting whether a proposed data center will face — and whether it will be stopped by — community opposition, deployed as an input-aware scorer that picks a model variant based on what's actually known about a site rather than imputing blind. Includes a documented mid-project tier redesign, a cohort-drift check that revealed the model's headline accuracy overstates what a live query actually sees, and a live calibration bug (a thin fold silently inverting one model's score ranking) found by stress-testing under a different train/test split, diagnosed down to the exact seed, and fixed — all published rather than smoothed over.

**Skills:** applied statistics, model calibration (Platt scaling in logit space), `scikit-learn`, FastAPI production integration, methodology documentation, production-artifact validation.

### [Data Center Community Opposition](data-center-community-opposition/)
Pipeline notebooks — executed against the real project data, with real summary statistics and charts — that build the canonical facility/outcomes dataset and the full constructed-variable dataset the predictive models train on: opposition-issue topics, prior-moratorium exposure, distance to historic sites and tribal land, and nonprofit-capacity variables.

**Skills:** data pipeline design with a documented corrections log, `geopandas`/`BallTree` geospatial analysis, reproducible ETL over a hand-maintained source spreadsheet, real-data validation (this run surfaced and fixed a coordinate data-quality issue the original script didn't handle).

### [Data Center Facility Data Collection](data-center-collection-pipeline/)
The original data-collection stage of the data center project: a resumable web scraper for facility records, cleaning, missing-value imputation, and joining in utility-rate and ISO grid data.

**Skills:** web scraping (`requests`/`BeautifulSoup`), data cleaning, group-conditional missing-value imputation, external API integration.
