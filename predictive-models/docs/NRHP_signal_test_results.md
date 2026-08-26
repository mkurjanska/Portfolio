# NRHP Signal Test — Results

**Date:** July 31 2026 · LocalQ Labs · DC Opposition Models
**Verdict: Do not retrain. NRHP historic-listing data adds no signal beyond the current champion specs.**

---

## 1. Data

| Source | Rows | Notes |
|---|---|---|
| `nationalregisterlisted_20250624.xlsx` | 100,117 | Two uploaded copies are byte-identical (same MD5) — one file |
| `nrhp_by_county.csv` | 3,183 | Pre-aggregated county totals, 2025 snapshot |

The xlsx carries `Listed Date`, `Category of Property`, `NHL Designated Date`, `Level of Significance`, and `Acreage`. That let me build **time-aware** counts — listings recorded strictly before each facility's `project_year_imp` — rather than the flat 2025 total, which would leak post-decision listings into pre-decision predictions. Listing years span 1966–2025.

**County join:** 1,226 / 1,243 = **98.6%**, after normalizing for suffixes (county/parish/borough), spacing (`dupage` vs `du page`), accents (`doña ana`), and Virginia independent cities. Of the 17 non-joins, 7 are counties genuinely absent from the register (coded 0, not missing) and 10 have malformed county strings in the model dataset (coded NaN — see §5).

## 2. Variables built

Counts prior to project year: `nrhp_prior`, `nrhp_district_prior`, `nrhp_nhl_prior`, `nrhp_national_prior`, `nrhp_acre_prior`; log transforms of each; normalized forms `nrhp_density_sqmi`, `nrhp_per_10k_pop`, `nrhp_district_share`; and `nrhp_total` (flat 2025 snapshot, for comparison).

## 3. Univariate association

Raw counts correlate **negatively** with opposition (`nrhp_prior_log` vs `DV_opposition`: r = −0.121, p < 0.001) — a urbanization confound, since listing counts scale with population and settlement age.

Population-normalized listings looked promising: `nrhp_per_10k_pop` reached **univariate AUC 0.646** on `DV_opposition`, 0.623 on `DV_oppcaused_adverse`. `nrhp_district_share` reached r = +0.101 (p < 0.001) on `DV_adverse_full`.

## 4. With/without test against champion specs

Protocol reproduced exactly from the locked models — 10-fold stratified CV, seed 42, `SimpleImputer(median)` → `StandardScaler` → `LogisticRegression` at each model's documented `class_weight` and `C`. This reproduces all four documented CV AUCs to three decimals (M1 0.828, M2 0.704, M3 0.805, M4 0.833; total deviation 0.0007). Deltas below are paired across 10 folds × 10 repeats.

| Model | Baseline AUC | Best NRHP variable | ΔAUC | Noise floor |
|---|---|---|---|---|
| M1 | 0.8278 | `nrhp_district_prior_log` | **+0.0016** | ±0.031 |
| M2 | 0.6987 | `nrhp_nhl_prior_log` | **−0.0031** | ±0.086 |
| M3 | 0.7985 | `nrhp_district_share` | **+0.0006** | ±0.044 |
| M4 | 0.8310 | `nrhp_nhl_prior_log` | **−0.0005** | ±0.039 |

Every other candidate was negative. The largest gain anywhere (+0.0016) is roughly **1/20th** of M1's fold-to-fold standard deviation. Recall and precision at each model's documented F1 threshold are flat or slightly worse (M1 recall 0.8293 → 0.8232 with the best variable).

**Robustness:** restricting to complete cases (n≈1,097, no median imputation of NRHP) gives the same answer — M1 +0.0001, M3 −0.0018, M4 −0.0008.

**Why the univariate signal disappears:** `nrhp_per_10k_pop_log` is collinear with predictors already in the specs — `jurisdiction_nonwhite` (r = −0.527), `jurisdiction_homeownership` (+0.310), `opp_prop_log_cond` (+0.257), `appeal_v` (+0.245). It is largely a restatement of settlement pattern and demographics the models already encode.

## 5. Two incidental findings

**Malformed county strings.** Ten rows carry free-text corrections in the county field rather than a county name — e.g. `"dickens (corrected from 'lubbock'). facility is in dickens county, ~40 miles east of lubbock; the dataset's lubbock county listing is incorrect."`. These break any county-level join, including the existing MCR merge. Worth cleaning into a proper county value plus a separate notes column.

**Risk-tier nesting violation.** Successful opposition (M4) is by construction a subset of any opposition (M1), so M4's probability should never exceed M1's. Scoring 400 real training facilities, **M4 > M1 in 40 cases (10.0%)** — all in the low-probability tail (e.g. M1 0.034 vs M4 0.047). This follows from calibrating each model independently; it is not caused by the NRHP work. It matters for the web application, where a facility could display LOW for "any opposition" and a higher number for "successful opposition" in the same panel. Worth an explicit monotonicity constraint before launch.

## 6. Recommendation

Do not retrain. `model_results_FINAL.pkl`, `webapp_models.pkl`, and `scoring_config.json` stay as they are — the July 2026 champion specs remain locked and unchanged.

The constructed variables are saved in `nrhp_model_variables.csv` (1,243 × 19) should you want them for descriptive work; the historic-district share in particular may be useful for reporting even though it does not predict.
