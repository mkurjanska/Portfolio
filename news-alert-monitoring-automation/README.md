# News Alert & County Meeting Monitoring Automation

A maintained, cron-scheduled monitoring system (GitHub Actions) that tracks data center news coverage and county government meeting activity, rather than a one-off scraping script. In continuous unattended operation since June 2026. News monitoring covers 408 curated local outlets plus 22 dedicated trade/watchdog sources, and has processed 20,000+ alerts to date; county meeting monitoring covers 2,247 county source URLs. Part of the data center opposition project.

No scraped output data is included — code and workflow configuration only.

## What's here

- **`workflows/`** — the GitHub Actions workflow definitions, each on its own schedule, each committing its results back to the repo automatically:
  - `fetch_alerts.yml` — daily RSS pull. `process_alerts.yml` — analysis pass, Monday and Thursday, diffing against the previous commit to find what `fetch_alerts.yml` added since.
  - `scrape-county-meetings.yml` — scrapes county government meeting agendas/minutes for data-center-relevant items.
  - `verify-legistar.yml`, `find-county-urls.yml`, `find-missing-urls.yml`, `check-urls.yml`, `verify-and-fix-urls.yml`, `process-county-hits.yml` — a URL-discovery and validation pipeline keeping the list of county meeting-portal URLs accurate over time.
- **`code/`** — the scripts each workflow runs:
  - `fetch_alerts.py` — reads RSS feeds, fetches full article text per entry, and detects "link-dump" pages (pages that are really just a list of links to real articles) — expanding each qualifying link into its own row rather than storing the container page, with dedup checked before fetching so the same child URLs are never re-added.
  - `verify_legistar_clients.py` — validates which counties are real clients of the Legistar government-meeting platform by calling its API directly, rather than guessing from URL patterns.
  - `scrape_county_meetings.py`, `find_county_urls.py`, `find_missing_urls.py`, `check_urls.py`, `verify_and_fix_urls.py`, `process_county_hits.py` — the rest of the URL-discovery/validation/meeting-scraping pipeline that keeps 2,247 counties' worth of source URLs current.

## Why this is here

Most scraping code shown in a portfolio is a script that ran once. This is a system that runs unattended on a schedule, handles its own dedup and URL rot, and commits its own results — the kind of thing that has to keep working without anyone watching it.
