# Data Center Community Opposition

Applied research project tracking community opposition to data center development across the United States: how projects resolve (approved, delayed, cancelled), what issues surface (water use, energy costs, noise, property values), and the county-level geographic/regulatory context each facility sits in.

No raw data is included in this folder — the underlying datasets are proprietary research data. The notebooks below were run against the real project data; what's shown is summary statistics and charts, not the underlying row-level data.

The pipeline includes an inline corrections log — real bugs found during development are documented in place, not silently fixed: e.g. a Census geography collision that produced impossible values (100% homeownership in one county), and a manufacturing-share column that was actually pulling an unrelated "total female employment" subtotal. See `build_constructed_variables.ipynb`.

## What's here (`code/`)

All six pipeline stages are notebooks (`.ipynb`), executed against the real project data with the resulting summary stats and charts embedded:

- **`build_facilities_outcomes.ipynb`** — generates the canonical facilities/outcomes dataset from the hand-maintained master spreadsheet (1,228 facilities after excluding tombstoned/retired records). Charts: facility status breakdown, top states by facility count. 26.1% of facilities have confirmed direct opposition — consistent with the base rate reported independently in the [predictive-models](../predictive-models/) project.
- **`build_constructed_variables.ipynb`** — the full variable-construction pipeline that feeds the predictive models: jurisdiction-level ACS variables (homeownership, education, race/ethnicity, an economic-hardship PCA index), nonprofit/advocacy-organization capacity, opposition-issue topic flags, and the four outcome variables (DVs). Run against 5 real Census ACS extracts plus an NCCS nonprofit-sector extract, joined onto 1,294 facilities into a 219-column dataset. Charts: prevalence of each opposition issue topic (grid/energy, water, noise, property values, etc.), and the four outcome rates. This is the file `predictive-models`' M1–M4 models are trained on.
- **`Historical_sites_per_county.ipynb`** — builds a historical-sites-per-county variable from the real National Register of Historic Places export. Chart: distribution across counties, top 15 by site count.
- **`Historical_sites_var_distance_to_dcs.ipynb`** — nearest-historic-site distance per facility (`BallTree`, haversine), run against the real facility list (1,277 geocoded facilities). Chart: distance distribution (median 3.0 miles).
- **`Tribal_land_distance_to_facility.ipynb`** — geospatial distance to nearest tribal land boundary (`geopandas`, CRS reprojection to EPSG:5070). Chart: distance distribution; 9 facilities sit within tribal land boundaries. This run surfaced a real data-quality issue not handled by the original script — a few rows had non-numeric garbage in the coordinate columns — fixed with explicit numeric coercion before filtering.
- **`Add_variables.ipynb`** — merges county-level regulatory variables, prior-moratorium exposure (own county and adjacent), and the distance/historic-site variables above onto the facility list. Chart: facilities by prior-moratorium exposure (223 in-county, 274 adjacent-county, out of 1,373). Found and fixed an operator-precedence bug in the moratorium-exposure filter (`a >= b | c.isna()` doesn't mean what it looks like it means in Python — `|` binds tighter than `>=`) that had silently excluded every facility with a missing operational date from ever counting as moratorium-exposed; fixing it roughly doubled both counts.

## Data note

The full project pulls from a Lexis-Nexis media crawl, online petition platforms, court records, and municipal/state regulatory filings. None of the underlying row-level data is included here — notebooks show aggregate statistics and charts only.
