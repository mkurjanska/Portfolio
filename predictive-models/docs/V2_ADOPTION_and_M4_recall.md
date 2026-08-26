# v2 Calibration Adopted · M4 Recall Investigated

**Date:** July 31 2026 · LocalQ Labs · DC Opposition Models

---

## Part 1 — v2 is now the deployed path

`scoring_function.py` and `scoring_config.json` now hold the v2 logic. v1 is preserved under `archive_v1/`.

**No deployed model is fit on more than 40% of the data.** Recorded train fractions: M1 39.8%, M3 40.0%, M4 40.0%, M2 10.5% (M2 is conditional on opposition, so its 40% share of a 328-row subset is a smaller slice of the full file).

A load-time guard enforces this. `scoring_function.py` refuses to load any model whose `train_fraction` exceeds 0.40 or is unrecorded:

```
CalibrationProvenanceError: Refusing to load models that violate the train/holdout split.
  M4: fit on 100.0% of rows
```

Verified by feeding it a deliberately corrupted artifact — it raises rather than scoring.

**Web app models were the same problem.** `WA_M1`, `WA_M3` and `WA_M4` turned out to be byte-identical to core M1/M3/M4 — same predictors, same fitted coefficients, same Platt, all fit on 100% of rows. `webapp_models_v2.pkl` is now a compatibility view onto the same v2 objects, so the two families cannot drift apart.

Held-out check on all 498 test facilities: **zero** M4 > M1 violations, **zero** M4 > M3.

| File | Role |
|---|---|
| `scoring_function.py` | canonical scorer — logit Platt, hierarchical clamp, provenance guard |
| `calibration_v2.pkl` | deployed models, 40% train / 20% calibrate |
| `scoring_config.json` | config with calibrated operating points |
| `webapp_models_v2.pkl` | WA view onto the same objects |
| `archive_v1/` | v1 artifacts, retained for reference, not loadable by the scorer |

---

## Part 2 — M4 does not have a false-negative problem

**The 0.178 was a measurement artifact.** The stored `cv_rec` is recall at p = 0.5. For the three balanced models that cutoff sits inside their probability mass and the figure is meaningful. M4 is the one unweighted model on a 12.7% base rate — its probabilities almost never reach 0.5, so recall at that cutoff collapses to 0.178 while telling you nothing about the model.

At M4's actual operating threshold, recall is **0.90** (precision 0.29).

### What the tiers actually do

Held-out test set, 498 facilities, 63 with successful opposition:

| M4 tier | n | successful opposition | observed rate |
|---|---|---|---|
| LOW | 321 | 10 | 3.1% |
| MEDIUM | 92 | 17 | 18.5% |
| HIGH | 85 | 36 | 42.4% |

LOW absorbs 64.5% of facilities at a 3.1% outcome rate — a quarter of the 12.7% base rate. HIGH runs at 3.3× base. **84.1% of true positives are captured at MEDIUM or above.**

### The composite tier — what the app surfaces — misses almost nothing

| Composite tier | n | successful opposition | observed rate |
|---|---|---|---|
| LOW | 222 | 2 | **0.9%** |
| MEDIUM | 135 | 15 | 11.1% |
| HIGH | 141 | 46 | 32.6% |

| Screen | LOW coverage | positives missed |
|---|---|---|
| M4 alone | 64.5% | 10 of 63 (15.9%) |
| **Composite M1/M3/M4** | 44.6% | **2 of 63 (3.2%)** |

All 10 facilities M4 alone put in LOW had both `DV_opposition = 1` and `DV_adverse_full = 1` — and **8 of the 10 are rescued by M1 or M3**. Only two survive as genuine composite misses, and one of those (Frederick VA) scored 0.1261 against a 0.127 boundary — missed by nine ten-thousandths. So there is really one substantive miss in 63.

### Where the misses concentrate

Facilities with complete data are never missed: of positives with zero missing predictors, **100%** are captured at MEDIUM or above. The missed positives average 2.60 missing predictors against 2.09 for true negatives, and **none** carried FULL confidence. Eight of the ten have `by_right = 1` or unknown — the strongest protective predictor. This is the conservative-estimate warning doing exactly what it documents: missing data imputes protective values and understates risk.

**The lever here is data completeness, not model recall.**

### If you still want M4-alone to miss less

Lowering M4's LOW/MEDIUM boundary trades coverage for capture:

| Boundary | Positives missed | Facilities in LOW | LOW observed rate |
|---|---|---|---|
| 0.127 (current, = base rate) | 10 (16%) | 321 (64%) | 3.1% |
| 0.110 | 6 (10%) | 304 (61%) | 2.0% |
| **0.090** | **3 (5%)** | **274 (55%)** | **1.1%** |
| 0.080 | 2 (3%) | 257 (52%) | 0.8% |

Moving to 0.09 catches 7 of the 10 while shifting only 47 facilities out of LOW. I am **not** recommending it: the composite already achieves 3.2% miss, and the "boundary = base rate" rule is what makes the tiers interpretable. Take this only if M4 must stand alone in some surface.

### Corrected operating points

Recomputed on calibrated probabilities and written into `scoring_config.json`:

| | Base rate | Threshold | Recall | Precision | F1 |
|---|---|---|---|---|---|
| M1 | 26.5% | 0.378 | 0.748 | 0.605 | 0.669 |
| M2 | 48.2% | 0.356 | 0.937 | 0.557 | 0.698 |
| M3 | 17.9% | 0.238 | 0.696 | 0.405 | 0.512 |
| M4 | 12.7% | 0.243 | 0.651 | 0.390 | 0.488 |

These are directly comparable across models; the old `cv_rec` figures were not.

## Recommendations

1. **Retire `cv_rec` from the model report.** It compares a fixed 0.5 cutoff across models with different class weighting and will mislead again. Report recall at each model's operating threshold.
2. **Lead the web app with the composite tier, not M4 alone.** M4 alone misses 16% of positives; the composite misses 3.2%. If the product surfaces a single number, it should be the composite.
3. **Show the probability next to the tier.** One miss was a facility 0.0009 below a boundary. A bare tier label hides that; the number makes it legible.
4. **Treat data completeness as the real recall lever.** Every positive with complete data was captured. Filling `by_right`, `industrial_zoned` and `project_year` for new sites will do more for recall than any threshold change.
