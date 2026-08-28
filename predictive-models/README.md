# Data Center Opposition — Predictive Models

Calibrated logistic-regression models predicting whether a proposed data center will face community opposition, and whether that opposition succeeds — four related models (M1–M4). Part of the LocalQ Labs data center opposition project.

No trained model weights or underlying data are included (the `.pkl` artifacts are derived from proprietary data) — this is the modeling/methodology code, not a runnable end-to-end pipeline.

## The currently deployed system

**`build_scenario_models.ipynb`** and **`predict_location.ipynb`** are executed notebooks — real training run, real scored examples. Building these inputs is a multi-step pipelie: `build_county_features.py` joining `master_county_regulatory.csv` + `facilities_outcomes.csv`, then `build_facility_variables.py` joining onto the facility list by FIPS. 

**`build_scenario_models.ipynb`** fits the model variants — `FULL`, `NO_ZONING`, `COUNTY_ONLY` per model — each under a strict 40% train / 20% calibrate / 40% test split. During the construction of the models, real problems and their solutions included:
- **A year-extrapolation hazard, found and capped.** `project_year` enters the models with a strong positive coefficient (opposition rose from 2.6% in 2018 to 66.7% in 2026), so an uncapped query dated 2035 evaluates that trend line nine years past any data and returns 95%+ for nearly every county. Fixed by capping the *scored input* — not the model — at the largest year with ≥10 training observations, verified to cost ≤0.0002 AUC.
- **The deployed risk tiers were redesigned mid-project.** The original rule (LOW/HIGH at 0.7×/1.5× the historical base rate) broke down once the real 2026 opposition rate passed 50% — "1.5× the rate" stops meaning "unusually likely" once the rate is already the majority outcome. A naive fix (re-anchoring the multiplier to the current rate) was tested and **rejected**: it pushed the HIGH threshold above the model's own achievable score range, collapsing sensitivity to 0.16. The shipped fix was implementing tercile cuts of the model's own calibrated-score distribution, which self-adjusts as the population's rate keeps moving instead of going stale.
- **A cohort-drift check that undercut the model's own headline numbers, published anyway.** Sliced by `project_year`, the pooled AUC turns out to be carried by facilities that already have an outcome — the population queries scored meaningfully lower. This is documented in [`docs/PREDICTION_STACK.md`](docs/PREDICTION_STACK.md) with the instruction to quote the honest number, not the pooled one.

On real data (1,294 facilities, county predictors matched by FIPS for 1,100 of them): **M1 (faces opposition) test AUC 0.886, M4 (successful opposition, primary deployable) test AUC 0.790**, both 8-split means, `FULL` variant. Chart: test AUC by model/variant, and standardized logistic-regression coefficients for M1 — which variables move the needle most, and in which direction.

**M2 (opposition succeeds, conditional on opposition happening at all). Original test AUC was 0.681** — noticeably better than chance, but well behind M1/M4. Two likely reasons include (1) the fact that M2 is fit on only the facilities where opposition occurred at all (n=372, roughly a quarter of the full set), and (2) the fact that conditioning on `M1=1` removes exactly the variables that made M1 predictive in the first place (site/county risk factors), leaving less signal for "does the opposition that occurs succeed." This is consistent with why `predict_location.py`'s default output is `M1` + `M4` only — M2 isn't part of the primary deployed answer.

**Originally, the standard 40/20/40 split (with a separate calibration fold) gave AUC 0.605** The 40/20/40 rerun (`build_scenario_models.ipynb`, "Bug found" section) isolated the cause of the lower AUC with the 40/20/40 split: `fit_one()`'s Platt calibration step is fit unregularized (`C=1e10`) on M2's calibration fold, which at ~74 rows is a third the size of M1's or M4's (M2 fits only on the opposition-occurred subset). On 1 of 8 seeds, that fold's sampling noise was enough to flip the fitted slope negative, which **inverts the score ranking** — test AUC on that one fold alone collapsed from a normal 0.696 to 0.304 (exactly `1 − 0.696`, the signature of a sign flip), and the same seed's sensitivity and precision at the deployed 0.5 cutoff cratered from 0.73/0.57 to 0.07/0.14. That single degenerate fold, averaged into the 8-seed mean, is what produced the ~0.60 figure. Fixed with a guard: if the Platt fit's slope isn't positive, fall back to passing the raw probability through unchanged rather than trust an inverted calibrator. **Corrected M2 FULL test AUC: 0.681 (8-seed mean, sd 0.017)**

**`model_comparison.ipynb`** answers the obvious next question — why logistic regression, not a tree-based model. Same M1 predictor set, same 40/20/40 discipline, same 8-split averaging, four model classes head to head:

| Model | Test AUC | Sensitivity @ 0.5 | Precision @ 0.5 | Brier |
|---|---|---|---|---|
| **Logistic Regression** | **0.908** | **0.839** | 0.667 | 0.125 |
| Random Forest | 0.907 | 0.811 | 0.667 | 0.125 |
| Bagging (trees) | 0.902 | 0.666 | **0.766** | **0.115** |
| Decision Tree | 0.883 | 0.830 | 0.627 | 0.136 |

Logistic regression comes out on top on the metrics that were actually the deployment criteria, namely best AUC and best sensitivity, with every coefficient staying directly interpretable. Bagging wins on precision, but its sensitivity is far lower (0.67 vs. 0.84), which is the wrong tradeoff given this project's stated cost asymmetry (`TIER_RULE_v3`: a missed opposition risk costs more than an unnecessary review). The margin over Random Forest is thin on AUC, but a linear model with ~500-row folds and 16 predictors is far less prone to the overfitting. A tree ensemble is more exposed to overfitting at this sample size.

**`predict_location.ipynb`** scores real counties with the models above: a full-information example (Loudoun County, VA) and a side-by-side comparison of the same county (Culpeper, VA) scored with and without site-level details, showing that missing information narrows the estimate's precision rather than just shifting it — the entire point of scoring with a variant fit for what's known, instead of imputing.

**`predict_location.py`** and **`build_scenario_models.py`** are also included as plain, importable modules alongside their notebooks (the notebooks are copies of these executed for real output — `pytest` can't import a `.ipynb`), with real tests in `tests/` (`pytest tests/`, 22 tests): `choose_variant()`'s full input matrix — including that `by_right=0` is a real answer and must not be treated as missing, and that partial zoning info (e.g. one of three fields known) routes the same as none, since a variant exists per input *set*, not per field — and the `M4 <= min(M1, M3)` clamp inside `predict()`, exercised with stub models (fixed `predict_proba` outputs) so no trained artifact is needed, including the documented-but-easy-to-break behavior that M4 always triggers M1/M3 internally to police the clamp even when the caller only asked for M4 back. `year_cap()`'s support-threshold boundary is tested the same way in `build_scenario_models.py`.

**`api_predict_snippet.py`** is the FastAPI integration layer — two endpoints (single-site and batch, up to 500 sites with per-row error isolation) wired to `predict_location.py`, the corrected, tercile-tiered path (see below for why, not `scoring_function.py`). Kept as code (not a notebook) since it's a snippet meant to be pasted into an existing FastAPI app and can't run standalone.

Full write-up, including the complete variant-performance table: [`docs/PREDICTION_STACK.md`](docs/PREDICTION_STACK.md).

## Earlier design: the v2 calibration pipeline

Before the system above, a simpler design was built and documented — a single scorer (no variant selection) with Platt-calibrated probabilities and fixed-multiple risk tiers. It's kept here because the methodology write-up is instructive, not because it's what's currently deployed — `scoring_function.py` carries an explicit internal note that it's superseded and not to be wired into production.

| | Predicts | n | Test AUC | Brier |
|---|---|---|---|---|
| M1 | Faces any opposition | 1,237 | 0.851 | 0.136 |
| M2 | Opposition succeeds (conditional on M1) | 328 | 0.680 | 0.224 |
| M3 | Any adverse outcome (any cause) | 1,243 | 0.795 | 0.126 |
| M4 ★ | Successful opposition | 1,243 | 0.845 | 0.091 |

★ primary deployable at the time. `n` is the number of facilities with a known outcome for that model. Metrics are on a genuinely held-out 40% test split — no model is fit on more than 40% of the data (enforced at load time, see `validate_artifacts.py`).

- **`build_mcr_variables.py`** — builds the seven county-level regulatory variables used as model predictors (legal appeal standing, ballot-initiative availability, zoning authority, environmental review requirements, water permitting, groundwater trend) from the county regulatory table. Still used by the current system too.
- **`build_calibration_v2.py`**, **`calib_eval.py`** — the calibration pipeline: stratified 40% train / 20% calibrate / 40% test split, Platt scaling fit correctly in logit space (fixing a bug where fitting on raw probabilities forced an impossible calibrated floor — see `docs/CALIBRATION_diagnosis_and_fix.md`), plus a hierarchical clamp enforcing `M4 ≤ min(M1, M3)`.
- **`scoring_function.py`** — the v2 scorer: loads the calibrated models, applies the hierarchical clamp, and refuses to load any model artifact whose recorded train fraction exceeds 40% (a provenance guard against accidentally deploying a model trained on its own test set). Superseded by `predict_location.py` above.
- **`validate_artifacts.py`** — consistency checker across the v2 deployed artifacts (calibration file, config, web-app model views); exits non-zero on drift.
- **`calibration_diagnosis.py`** — reproduces the bug and its fix directly from the real held-out data: the actual deployed (buggy) calibrator against a properly refit logit-space one, scored on the same 498 held-out facilities. Reproduces the write-up's numbers essentially exactly (floors 0.0250/0.0418, violation rate 22.5% buggy vs 11.4% fixed). Needs the proprietary training pickles to rerun; `calibration_diagnosis.png` is the real output.
- **`docs/`** — written methodology notes, including the calibration fix, the original (now-retired) risk-tier rationale, a signal test on a rejected candidate variable, and the full current-system write-up (`PREDICTION_STACK.md`).
