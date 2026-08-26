# Prediction Stack — files, datasets, and how they fit together

**LocalQ Labs · model version v4 (tercile tiers)**

How the deployed system's pieces connect: the facility dataset, the builder that constructs
every model variable, the county-level datasets needed to score a new location, and the
dispatcher that picks a model variant based on what the user actually supplied.

---

## 1. `facilities_outcomes_MODEL.csv`

Every core and constructed variable used by M1–M4, on the generated facility extract. Built by
`build_facility_variables.py` from `facilities_outcomes.csv` + `county_prediction_features.csv`.

| Group | Contents |
|---|---|
| Core (from the master workbook) | `Facility_number`, `State`, `County`, dates, `Direct_Opposition_Merged`, `Status_Normalized`, `Site_Type_Normalized`, `Build_Type_Normalized`, coordinates |
| Cleaned identity | `County_clean`, `County_fips`, `County_resolution`, `County_original` |
| Constructed here | `project_year`, `project_year_imp`, `build_converted`, `industrial_zoned` (+ `_source`), `DV_opposition` |
| Adjudicated (sourced) | `DV_oppsuccess`, `DV_adverse_full`, `DV_oppcaused_adverse`, each with a `_source` column |
| County-level (joined on FIPS) | the `_v` regulatory family, demographics, population, opposition-history context |

## 2. `build_facility_variables.py`

Reads the core columns **as named in `facilities_outcomes.csv`** and constructs the rest.

**What it derives, and what it refuses to.** `DV_opposition` is mechanically derivable from
`Direct_Opposition_Merged` and was verified at 100% agreement with the adjudicated values, so it
is derived. The three adverse-outcome DVs (`DV_adverse_full`, `DV_oppsuccess`,
`DV_oppcaused_adverse`) are **research columns, not constructed ones** — resolved through a
layered adjudication process (web-verified rulings, QC corrections, and a documented fallback
rule for the handful of rows nothing else covers) and stored directly in `facilities_outcomes.csv`.
This script *reads* them; it does not re-derive or override anything. A legacy derivation path
exists for a facilities file that predates those columns, but it raises rather than running
silently, because falling back to it would discard every adjudicated ruling.

**Two honest gaps, left as gaps rather than guessed:**

- `industrial_zoned` is only informative where `Site_Type_Normalized` reads `"Industrial"` — the
  other site types are simply silent on zoning, so they stay `NaN` rather than being coded 0.
- `by_right` is not recoverable from this file at all. It must come from the municipal zoning
  data (see [municipality-zoning-scraper](../../municipality-zoning-scraper/)) or from the user
  at scoring time.

## 3. Datasets required to score a new location

| File | Supplies |
|---|---|
| **`county_prediction_features.csv`** | every county-level predictor, precomputed — the only file the scorer needs at runtime |
| `master_county_regulatory.csv` | source for the `_v` regulatory family, demographics, politics |
| `facilities_outcomes.csv` | prior-DC counts and county opposition history |

**What the user must supply.** Only four site-level inputs, all optional: `by_right`,
`industrial_zoned`, `build_converted`, `project_year`. Everything else resolves from the county.

## 4. `predict_location.py` — input check → variant selection → score → qualified output

```python
from predict_location import print_prediction
print_prediction(fips='51047', by_right=0, industrial_zoned=1,
                 build_converted=0, project_year=2026)
print_prediction(county='Culpeper', state='VA')      # county-only screen
```

### Why variants rather than imputation

`by_right`, `industrial_zoned` and `build_converted` all carry negative coefficients and
protective training medians. Median-imputing a missing value therefore assumes the protective
case and biases the estimate **downward** — the site looks safer than the evidence supports.
`build_scenario_models.py` fits variants **without** those predictors, so the model matches the
information actually available.

| Supplied | Variant |
|---|---|
| all four site inputs | `FULL` |
| `project_year` only | `NO_ZONING` |
| neither | `COUNTY_ONLY` |

Partial zoning input still routes to `NO_ZONING` — variants exist per input *set*, and using
`FULL` with one of three missing reintroduces the same bias.

### Variant performance (held-out test AUC, 8-split mean)

| | FULL | NO_ZONING | COUNTY_ONLY |
|---|---|---|---|
| M1 (faces opposition) | **0.886** | 0.881 | 0.833 |
| M2 (opposition succeeds)† | 0.681 | 0.648 | 0.648 |
| M4 (successful opposition, primary deployable) | **0.790** | 0.791 | 0.724 |

† M2's number reflects the calibration-guard fix described in the main README — see the
`build_scenario_models.ipynb` "Bug found" section for the full diagnosis. M3 is not fit here; the
engineered feature it needs (`events_per_10k_pop_log`) isn't reconstructable from the inputs
available (see the main README).

The cost of missing zoning inputs is real on M1 and M4 — a few points of AUC each. Worth telling
users so they supply the fields.

### What the output carries

Per model: calibrated probability, tier, lift vs. base rate, which predictors were imputed, and
the variant's expected AUC. Plus a composite tier and an explicit qualifications list.
`M4 ≤ min(M1, M3)` is enforced, since successful opposition is a subset of both.

---

## Why `project_year` is capped at scoring time

`project_year` enters M1/M3/M4 linearly with a strong positive coefficient — opposition has been
rising steeply year over year. Left uncapped, a query dated years into the future evaluates that
trend line far past any real data and returns an implausibly high risk for nearly every county.

The fix caps the *scored input*, not the model: `project_year` is clamped at the largest year
with at least 10 training observations before scoring (currently 2026), so a future-dated query
returns "risk under current conditions" rather than an extrapolated forecast. This was tested,
not assumed — capping only a polynomial term and leaving the linear trend free doesn't work (the
extrapolation barely moves), and the ≥10-observation rule was chosen because the raw maximum year
flips between adjacent years depending on the train/test split while the observation-count rule
doesn't. Measured cost of the cap: ≤0.0002 AUC across M1/M3/M4, since it binds on a single
training row — it exists for scoring, not for fitting.

## Tiers: terciles of the model's own score distribution (v4)

The original tier rule (LOW below 0.7× base rate, HIGH at or above 1.5×) broke down once a
model's real base rate rose past 50% — "1.5× the rate" stops meaning "unusually likely" once the
rate is already the typical outcome, which is exactly where M1 sits. The naive fix — re-anchoring
the multiplier to the current rate — was tested and rejected: it pushed the HIGH threshold above
the model's own achievable score range and collapsed sensitivity to 0.16.

The shipped rule instead takes the 33rd/67th percentile of each variant's own calibrated-score
distribution across the full facility universe, scored as if every site were proposed in the
current year. This self-adjusts as the population's true rate keeps moving, rather than going
stale against a fixed historical average. Current cuts, FULL variant:

| | LOW below | HIGH at/above |
|---|---|---|
| M1 | 0.190 | 0.472 |
| M2 | 0.181 | 0.371 |
| M4 | 0.139 | 0.307 |

## The cohort-drift finding

Sliced by `project_year`, the pooled test AUC turns out to be carried by facilities that already
have a recorded outcome — largely older, already-resolved projects. The population an operator
actually queries (a live, currently-dated project) scores meaningfully lower than the pooled
number suggests. M3 in particular is the clearest case: its pooled AUC looks reasonable, but on
the population that actually gets queried it drops to roughly chance (AUC ~0.55) — one of the
reasons M3 is dropped from `predict_location.py`'s default output rather than quoted alongside
M1/M4 without qualification. The instruction that came out of this: quote the honest,
cohort-appropriate number, not the pooled one that flatters the model.

## Known limits

- **Connecticut**: 8 legacy counties have no current FIPS code (CT abolished counties in 2022).
  Those locations cannot be scored until a planning-region crosswalk is decided.
- **`nwis_trend`** is missing for a genuine subset of counties (no monitoring well present, not a
  data bug) and shows up in the scoring qualifications on many scores as a result.
- **`opp_prop_log_cond`** is `NaN` by design for counties with no prior opposition history — that
  is the intended encoding, not a gap.
- The county table carries opposition history as of build time; rebuild it when the facilities
  list changes.

## File manifest

| File | Role |
|---|---|
| `facilities_outcomes_MODEL.csv` | complete facility dataset |
| `build_facility_variables.py` | constructs facility-level variables |
| `county_prediction_features.csv` | county predictors for any location |
| `build_scenario_models.py` / `scenario_models.pkl` | model variants per input set |
| `predict_location.py` | dispatcher |
