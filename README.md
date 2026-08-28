# Portfolio

I am a data analyst and researcher building data products at LocalQ Labs, an early-stage startup mapping and predicting community opposition to data center development across U.S. markets. These projects are the (redacted) pipeline, modelling, and monitoring code from that work.

Each folder is self-contained. No proprietary or raw data is included anywhere in this repo. Where a project needs data to be meaningful, the README says so and points to the source project instead.

## Projects

### [Municipality Zoning Scraper](municipality-zoning-scraper/)
A layered tool for determining whether industrial use is allowed by-right across hundreds of municipal zoning codes. The scraper escalates from a discovered internal API, to static HTML scraping, to full browser automation only where necessary.

**Skills:** iterative scraping-resilience engineering, API reverse-engineering, Playwright browser automation, anti-bot handling.

### [News Alert & County Meeting Monitoring Automation](news-alert-monitoring-automation/)
A maintained, cron-scheduled GitHub Actions system tracking data center news and county government meeting activity.

**Skills:** GitHub Actions/CI scheduling, RSS + full-text scraping, third-party API validation (Legistar), URL-rot handling at scale.

### [Predictive Models](predictive-models/)
Calibrated logistic-regression models predicting whether a proposed data center will face — and whether it will be stopped by — community opposition, deployed as an input-aware scorer that picks a model variant based on what's known about a site. Includes a documented mid-project tier redesign, a cohort-drift check, and a live calibration bug found by stress-testing under a different train/test split and fixed.

**Skills:** applied statistics, model calibration (Platt scaling in logit space), `scikit-learn`, FastAPI production integration, methodology documentation, production-artifact validation.

### [Data Center Community Opposition](data-center-community-opposition/)
Pipeline notebooks — executed against the real project data, with summary statistics and charts, which build the canonical facility/outcomes dataset and constructed-variable dataset. All constructed variables were tested. Some were rejected as insignificant (adding noise to the model) and some were added to the predictive models. Eg: opposition-issue topics, prior-moratorium exposure, distance to historic sites and tribal land, and nonprofit-capacity variables.

**Skills:** data pipeline design with a documented corrections log, `geopandas`/`BallTree` geospatial analysis, reproducible ETL over a hand-maintained source spreadsheet, real-data validation.

### [Data Center Facility Data Collection](data-center-collection-pipeline/)
The original data-collection stage of the data center project: a resumable web scraper for facility records, cleaning, missing-value imputation, and joining in utility-rate and ISO grid data.

**Skills:** web scraping (`requests`/`BeautifulSoup`), data cleaning, group-conditional missing-value imputation, external API integration.
