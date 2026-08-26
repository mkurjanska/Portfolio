# Risk-Tier Nesting Violation — Diagnosis and Calibration Fix

**Date:** July 31 2026 · LocalQ Labs · DC Opposition Models
**Split discipline:** 40% train / 20% calibrate / 40% test throughout. Nothing is fit on more than 40% of the data.

---

## 1. The defect

M4 (successful community opposition) is by construction a subset of both M1 (any opposition) and M3 (any adverse outcome). I confirmed the nesting holds strictly in the data — **zero** rows have `DV_oppcaused_adverse = 1` with either `DV_opposition = 0` or `DV_adverse_full = 0`.

The scores violate it anyway. On the held-out test set:

| | M4 > M1 | M4 > M3 |
|---|---|---|
| Current deployed scorer | **22.1%** | **43.6%** |

Averaged over 30 independent 40/20/40 splits: 24.1% and 37.9%. The M3 violation is the larger problem and was not in view before.

## 2. Root cause

**The violation is created by the calibration step, not by the models.** On the same test rows, the raw pipeline outputs violate the ordering only **1.0%** of the time. Calibration takes that to 22.1%.

The mechanism is a floor artifact. `platt_deploy` is a logistic fit on the **raw probability**:

```
cal = sigmoid(a · raw + b)
```

Because `raw` is bounded below at 0, the calibrated output can never fall below `sigmoid(b)`. Each model gets its own `b`, so each gets its own floor:

| Model | a | b | **floor at raw = 0** | base rate |
|---|---|---|---|---|
| M1 | 5.265 | −3.663 | **0.0250** | 26.5% |
| M3 | 4.822 | −3.959 | 0.0187 | 17.9% |
| M4 | 6.591 | −3.133 | **0.0418** | 12.7% |

**M4's floor is 1.7× M1's, despite M4's base rate being less than half of M1's.** Any facility both models score as very unlikely lands on M4's higher floor and produces the impossible result. This is exactly where the violations sit: in the M1 < 0.05 band, **100%** of rows violate.

Why M4 specifically? M4 is the one model using `class_weight=None`. Balanced weighting stretches M1's raw output across the full range (test p05 = 0.067, median = 0.410); unweighted M4's raw output is compressed near its base rate (p05 = 0.006, median = 0.061). The Platt fit for M4 therefore has narrow support and extrapolates badly toward raw = 0, landing on a high intercept.

**Platt scaling is defined on the decision function — the log-odds — not on the probability.** Fitting it on the probability is the bug.

## 3. A second finding: the deploy path trains on 100% of the data

While tracing this I recovered the split seed (42, confirmed by reproducing all four documented test AUCs and Briers to three decimals) and checked what the deployed artifacts were actually fit on:

- `pipe_deploy` — fit on **all 1,243 rows**, not the 40% train split.
- `platt_deploy` — fit on **all 1,243 rows**, using predictions from that same all-data pipeline.

So the deployed calibrator never saw a held-out prediction, and the deployed scorer is not the one the reported metrics describe. The `pipe_test` / `platt_test` pair is properly held out and those published numbers are honest — but they describe a different object than the one scoring facilities.

The practical damage turns out to be modest: in simulation, the extra training data roughly offsets the in-sample calibration bias. It is still worth fixing, because it breaks the 40/60 rule and because the reported metrics should describe the deployed artifact. **Training on 40% instead of 60% costs almost nothing** — Brier moves by less than 0.002 on every model, so the discipline is nearly free.

## 4. Options tested

All calibrators fit on the held-out 20% split; all figures from the untouched 40% test set; deltas paired across 2,000 bootstrap resamples and confirmed across 30 independent splits.

| Scheme | M4 AUC | Brier | ECE | M4>M1 | M4>M3 |
|---|---|---|---|---|---|
| A — current, Platt on probability | 0.8458 | 0.0942 | 0.0742 | 22.1% | 43.6% |
| B — Platt on logit | 0.8458 | 0.0907 | 0.0552 | 11.5% | 18.6% |
| **C — Platt on logit + clamp** | **0.8548** | **0.0877** | **0.0398** | **0%** | **0%** |
| D — factorized M1 × M2 | 0.8612 | 0.0874 | 0.0260 | 0% | 20.2% |
| E — factorized + clamp to M3 | 0.8759 | 0.0857 | 0.0393 | 0% | 0% |

Paired bootstrap: **C beats A** on AUC (+0.0089, P = 0.99) and Brier (P = 0.998). E beats A too, but **E does not reliably beat C** (ΔAUC CI spans zero; E wins Brier in only 37% of splits and ECE in 27%).

**Recommendation: C.** E buys a little discrimination at the cost of calibration, and it makes the primary deployable model depend on M2 — the weakest model in the family (CV AUC 0.704, n = 328, trained only on opposed facilities and therefore applied out of domain). That is a poor dependency for the model the web app leads with.

Isotonic regression was also tested and rejected: it degrades AUC (ties from the step function) and log-loss on every model.

Switching M1, M2 and M3 to logit-space Platt is **neutral** — differences appear in the fourth decimal, win rates near 50%. Only M4 benefits materially, which is consistent with the diagnosis. Making the change family-wide is for consistency, not gain.

## 5. What I built

| File | Contents |
|---|---|
| `calibration_v2.pkl` | Pipelines fit on the 40% train split, logit-space Platt fit on the held-out 20%, tier thresholds carried over unchanged |
| `scoring_function_v2.py` | Drop-in replacement. Logit-space calibration + hierarchical clamp `M4 ≤ min(M1, M3)` |
| `scoring_config_v2.json` | Lightweight config mirroring v1's structure, plus Platt coefficients and honest held-out metrics |
| `calibration_diagnosis.py` / `.png` | The figure (script + rendered output) |

The clamp scores M1 and M3 even when the caller requests only M4, and records `clamped_by` and `cal_prob_unclamped` on any score it binds, so nothing is silently altered.

**Model specs are untouched.** Predictors, `C`, and `class_weight` are exactly as locked in July 2026. This is a calibration-layer change only.

## 6. Validation

Scoring all 498 held-out test facilities through both versions:

- Violations: 22.1% / 43.6% → **0% / 0%**
- M4 calibrated floor: 0.0418 → 0.0002
- The clamp binds on 25.9% of facilities
- **82.1% of M4 tier assignments are unchanged.** Of those that move, most are LOW → MEDIUM (53 facilities), which is the floor artifact unwinding
- Held-out M4 AUC 0.8535, Brier 0.0876

v1 appears to score AUC 0.8815 on these rows, but its pipeline was trained on them — that number is optimistic and not comparable.

## 7. Two things to decide before launch

**The conservative-estimate warning interacts with the clamp.** Missing `by_right` / `industrial_zoned` / `build_converted` already biases scores downward, and the clamp can only lower M4 further. When the bounding model is itself poorly informed, the clamp may compound the understatement. Consider suppressing the clamp when M1 or M3 is flagged REDUCED confidence, and surfacing the unclamped value alongside.

**Deploying v2 means retraining the point models on 40% rather than 100%.** The measured cost is under 0.002 Brier. If you would rather keep the all-data point models and change only the calibrator, that is a one-line variant — but the calibrator then has no clean held-out set to fit on, which is the situation that produced this defect.
