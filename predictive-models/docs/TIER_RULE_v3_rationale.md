# Risk Tier Boundaries — Cost-Asymmetric Rule

**Date:** July 31 2026 · LocalQ Labs · DC Opposition Models

## The rule

| Tier | Boundary | Meaning |
|---|---|---|
| LOW | < 0.7 × base rate | Below the population baseline for this outcome |
| MEDIUM | 0.7 – 1.5 × base rate | Around to moderately above baseline |
| HIGH | ≥ 1.5 × base rate | Materially above baseline |

Applied uniformly to all four models. Previously LOW < 1.0× and HIGH ≥ 2.0×.

| | Base rate | LOW < | HIGH ≥ | (previous LOW / HIGH) |
|---|---|---|---|---|
| M1 | 26.5% | 0.185 | 0.398 | 0.265 / 0.530 |
| M2 | 48.2% | 0.337 | 0.723 | 0.482 / 0.964 |
| M3 | 17.9% | 0.125 | 0.269 | 0.179 / 0.358 |
| M4 | 12.7% | 0.089 | 0.191 | 0.127 / 0.254 |

## Why this is defensible

**Every threshold already encodes a cost assumption.** For a calibrated probability, flagging at threshold *t* is the expected-cost-minimising decision when a false negative costs `K = (1−t)/t` times a false positive. That relationship holds whether or not anyone chose it deliberately.

The previous boundaries were not cost-neutral — they simply embedded cost ratios nobody had examined:

| Boundary | Threshold | Implied K | Reads as |
|---|---|---|---|
| M1 HIGH, old | 0.530 | **0.89** | a missed opposition costs *less* than an unnecessary review |
| M1 HIGH, new | 0.398 | 1.51 | a missed opposition costs ~1.5× an unnecessary review |
| M4 LOW, old | 0.127 | 6.87 | a missed campaign costs ~7× a false alarm |
| M4 LOW, new | 0.089 | 10.2 | a missed campaign costs ~10× a false alarm |

M1's old HIGH boundary implied that missing a community-opposition case was *cheaper* than reviewing a site unnecessarily. That is not a position anyone would defend on the merits — it was an artifact of picking a round multiple. **The change replaces an accidental cost assumption with a stated one.**

**Cross-model comparability survives.** Because the boundaries are still multiples of each model's base rate, a tier label still means the same thing in every model: HIGH is "meaningfully above the typical rate for this outcome," whether that outcome runs at 26.5% or 12.7% at baseline. Only the multiple changed, and it changed identically everywhere. This is the property that would have been lost by hard-coding 0.40 into M1 alone.

## What it costs and buys

Validated across 30 independent 40/20/40 splits:

| | Positives missed at LOW (old → new) | Improved in | HIGH precision (old → new) |
|---|---|---|---|
| M1 | 23.9% → **15.3%** | **100%** of splits | 0.654 → 0.604 |
| M3 | 24.6% → **14.0%** | **100%** of splits | 0.465 → 0.413 |
| M4 | 22.6% → **13.4%** | **100%** of splits | 0.384 → 0.344 |

Roughly a third fewer missed positives, for 4–5 points of HIGH-tier precision. Tier coverage shifts accordingly — M4's LOW tier goes from 66% to 56% of facilities, HIGH from 14.8% to 22.3%.

The improvement is consistent, not a single-split artifact. The 0.7 and 1.5 multiples are deliberately round: they were chosen for interpretability and confirmed stable, not tuned to the decimal against held-out data.

## Observed outcome rates by tier

Pooled over the 30 splits, so the reported rates describe the new boundaries rather than the old ones:

| | LOW | MEDIUM | HIGH |
|---|---|---|---|
| M1 — any opposition | 8.1% | 28.4% | 60.1% |
| M2 — opposition succeeds | 29.3% | 50.2% | 68.3% |
| M3 — any adverse outcome | 5.1% | 19.8% | 41.0% |
| M4 — successful opposition | 3.0% | 15.5% | 34.1% |

The scoring narrative has been updated to quote these figures. It previously cited rates computed under the old boundaries.

## Caveat text for the report and the web app

> **On tier boundaries.** Risk tiers are decision thresholds, not statistical facts. They are set at 0.7× and 1.5× each model's base rate, which deliberately favours catching genuine risk over avoiding unnecessary review: a site whose risk is missed carries a larger cost — sunk acquisition and design spend, schedule loss, reputational exposure — than a site reviewed more closely than it turned out to need. Concretely, the boundaries imply that a missed case is worth roughly 1.5 to 10 unnecessary reviews, depending on the model and the boundary. Users who face a different cost balance should read the calibrated probability rather than the tier label; the probability is unchanged by this choice, and the tier is only a summary of it.

Shorter version for a UI tooltip:

> Tiers lean toward flagging risk. We would rather review a site unnecessarily than miss one. The probability shown is unaffected — only the label thresholds are.

## Limits worth stating

**This is a policy choice, not an empirical finding.** No data can tell you what a missed opposition costs relative to an unnecessary review. The boundaries encode a judgement; the honest presentation is to name it rather than to bury it in a round number.

**It depends on the probabilities being calibrated.** The `K = (1−t)/t` reading is only meaningful for calibrated probabilities. Under the previous calibration — which carried a floor artifact of 0.042 on M4 — the implied-cost interpretation would have been meaningless. This framing is available only because the v2 calibration work fixed that.

**HIGH is now less precise.** About 4–5 points, consistent across models. M4's HIGH tier runs at 34.1% observed rate rather than the old boundary's higher concentration, and covers half again as many facilities. If HIGH triggers an expensive fixed process, that cost scales with the wider net.

**Revisit if the cost picture changes.** If the product moves from site screening to something where false alarms are expensive — automated rejection, public-facing risk labels — the asymmetry should be re-argued rather than inherited.
