"""
DC Opposition Risk Scoring Function — v2 (corrected calibration)
LocalQ Labs — July 2026

Changes vs v1:
  * Platt scaling applied in LOGIT space (v1 fit the logistic on the raw probability,
    which forced a calibrated floor of sigmoid(intercept) > 0 — 0.0418 for M4 against
    0.0250 for M1, producing impossible M4 > M1 scores in the low-probability tail).
  * Pipeline fit on the 40% train split, calibrator fit on the held-out 20% split.
    v1 deploy artifacts fit both on 100% of the data.
  * Hierarchical clamp: M4 <= min(M1, M3), matching the strict subset relationship
    between the outcomes. Applied bounds are reported in each score dict.

Model specs (predictors, C, class_weight) are unchanged.

Loads fitted models, scores a new facility, returns:
  - calibrated probability per model
  - risk tier (LOW / MEDIUM / HIGH) per model
  - composite risk narrative leading with lift vs baseline

Usage:
    from scoring_function import score_facility, print_score
    result = score_facility(facility_dict)
    print_score(facility_dict)
"""

import numpy as np
import pandas as pd
import pickle
import json
from pathlib import Path

# ── Load models ───────────────────────────────────────────────────────────────
_MODEL_PATH  = Path(__file__).parent / 'calibration_v2.pkl'
_CONFIG_PATH = Path(__file__).parent / 'scoring_config.json'
_models  = None
_config  = None

MAX_TRAIN_FRACTION = 0.40   # no deployed model may be fit on more than this share of the data


class CalibrationProvenanceError(RuntimeError):
    """Raised when a deployed model was fit on more data than the split allows."""


def _load():
    global _models, _config
    if _models is None:
        with open(_MODEL_PATH, 'rb') as f:
            raw = pickle.load(f)
        _models = {k: v for k, v in raw.items() if not k.startswith('_')}

        # Guard: refuse to score with any model fit on more than the training split.
        # The v1 artifacts fit both pipeline and calibrator on 100% of rows, which is
        # what produced the nesting defect this version corrects.
        offenders = []
        for mk, m in _models.items():
            frac = m.get('train_fraction')
            if frac is None:
                offenders.append(f"{mk}: no train_fraction recorded")
            elif frac > MAX_TRAIN_FRACTION + 1e-6:
                offenders.append(f"{mk}: fit on {frac:.1%} of rows")
        if offenders:
            raise CalibrationProvenanceError(
                "Refusing to load models that violate the train/holdout split.\n  "
                + "\n  ".join(offenders))
        with open(_CONFIG_PATH) as f:
            _config = json.load(f)

# ── Tier context: what observed rates look like in each tier ──────────────────
# Derived from test-set tier_stats in fitted models
_TIER_CONTEXT = {
    'M1': {
        'LOW':    'At this level, roughly 1 in 12 comparable facilities faced opposition '
                  '— below the typical rate.',
        'MEDIUM': 'At this level, roughly 1 in 4 comparable facilities faced opposition '
                  '— above the typical rate.',
        'HIGH':   'At this level, roughly 1 in 2 comparable facilities faced opposition '
                  '— a strong signal of elevated risk.',
    },
    'M3': {
        'LOW':    'At this level, roughly 1 in 20 comparable facilities experienced an adverse outcome '
                  '— below the typical rate.',
        'MEDIUM': 'At this level, roughly 1 in 5 comparable facilities experienced an adverse outcome '
                  '— above the typical rate.',
        'HIGH':   'At this level, roughly 1 in 2 comparable facilities experienced an adverse outcome '
                  '— a strong signal of elevated risk.',
    },
    'M4': {
        'LOW':    'At this level, roughly 1 in 33 comparable facilities faced a successful opposition campaign '
                  '— below the typical rate.',
        'MEDIUM': 'At this level, roughly 1 in 6 comparable facilities faced a successful opposition campaign '
                  '— above the typical rate.',
        'HIGH':   'At this level, roughly 1 in 3 comparable facilities faced a successful opposition campaign '
                  '— a strong signal of elevated risk.',
    },
}

# ── Derived variable construction ─────────────────────────────────────────────
def _build_derived(fac: dict) -> dict:
    d = dict(fac)

    # mine_log: log1p(mine_total_prop_2024 * 1000)
    mine = d.get('mine_total_prop_2024', np.nan)
    d['mine_log'] = np.log1p(mine * 1000) if pd.notna(mine) else np.nan

    # opp_comp_A / opp_comp_B: log1p of raw event counts
    ev  = d.get('ev_half_clean', np.nan)
    odc = d.get('prior_opp_dcs_county', np.nan)
    d['opp_comp_A'] = np.log1p(ev)  if pd.notna(ev)  else np.nan
    d['opp_comp_B'] = np.log1p(odc) if pd.notna(odc) else np.nan

    # opp_county_status: 0=first mover, 1=has DCs no opp, 2=has prior opp
    prior_dcs = d.get('prior_dcs_imp', np.nan)
    opp_prop  = d.get('opp_proportion_imp', np.nan)
    if pd.notna(prior_dcs):
        if prior_dcs == 0:
            d['opp_county_status'] = 0.0
        elif pd.notna(opp_prop) and opp_prop == 0:
            d['opp_county_status'] = 1.0
        else:
            d['opp_county_status'] = 2.0
    else:
        d['opp_county_status'] = np.nan

    # opp_prop_log_cond: log1p(proportion) only where proportion > 0
    d['opp_prop_log_cond'] = (np.log1p(opp_prop)
                               if pd.notna(opp_prop) and opp_prop > 0
                               else np.nan)

    # prior_dcs_log_imp
    d['prior_dcs_log_imp'] = (np.log1p(prior_dcs)
                               if pd.notna(prior_dcs) else np.nan)
    return d


def _assign_tier(cal_prob, tier_low, tier_high):
    if cal_prob < tier_low:  return 'LOW'
    if cal_prob < tier_high: return 'MEDIUM'
    return 'HIGH'


def _score_one_model(mk, facility):
    model = _models[mk]
    preds = model['preds']
    pipe  = model['pipe']
    platt = model['platt_logit']

    X = np.array([[facility.get(p, np.nan) for p in preds]], dtype=float)
    raw_prob = float(pipe.predict_proba(X)[0, 1])
    _e = 1e-6
    _r = min(max(raw_prob, _e), 1 - _e)
    raw_logit = np.log(_r / (1 - _r))
    cal_prob = float(platt.predict_proba(np.array([[raw_logit]]))[0, 1])
    tier     = _assign_tier(cal_prob, model['tier_low'], model['tier_high'])
    missing  = [p for p in preds if pd.isna(facility.get(p, np.nan))]
    lift     = cal_prob / model['base_rate']

    return {
        'model':         mk,
        'description':   model['description'],
        'raw_prob':      round(raw_prob, 4),
        'cal_prob':      round(cal_prob, 4),
        'cal_prob_pct':  f"{cal_prob*100:.1f}%",
        'tier':          tier,
        'tier_low':      model['tier_low'],
        'tier_high':     model['tier_high'],
        'base_rate':     model['base_rate'],
        'base_rate_pct': f"{model['base_rate']*100:.1f}%",
        'lift':          round(lift, 2),
        'missing_preds': missing,
        'confidence':    ('FULL'    if not missing else
                          'REDUCED' if len(missing) <= 3 else 'LOW'),
    }


def _build_narrative(scores, composite_tier):
    """
    Build a plain-English narrative leading with lift vs baseline,
    followed by tier context grounded in observed rates.
    """
    lines = []
    tier_icons = {'HIGH': '⚠', 'MEDIUM': '△', 'LOW': '✓'}

    # Per-model lines — lead with lift comparison
    ORDER = ['M4', 'M1', 'M3', 'M2']
    LABELS = {
        'M4': 'Successful community opposition',
        'M1': 'Any community opposition',
        'M3': 'Adverse outcome (any cause)',
        'M2': 'Opposition success (if opposition occurs)',
    }

    for mk in ORDER:
        s = scores.get(mk)
        if not s:
            continue
        icon = tier_icons[s['tier']]
        lift = s['lift']
        if lift >= 2:
            lift_str = f"{lift:.1f}× more likely than a typical US data center"
        elif lift >= 1.1:
            lift_str = f"{lift:.1f}× more likely than a typical US data center"
        elif lift >= 0.9:
            lift_str = "about as likely as a typical US data center"
        else:
            lift_str = f"{lift:.1f}× as likely as a typical US data center — below baseline"

        lines.append(
            f"  {icon} {LABELS[mk]}: {s['cal_prob_pct']}  [{s['tier']}]\n"
            f"    This site is {lift_str}\n"
            f"    ({s['cal_prob_pct']} vs {s['base_rate_pct']} baseline).\n"
            f"    {_TIER_CONTEXT[mk][s['tier']]}"
        )

    # Composite action line
    action = {
        'HIGH':   ("⚠  COMPOSITE RISK: HIGH\n"
                   "    Active diligence required. Commission a full community "
                   "engagement assessment before proceeding. Consider early "
                   "outreach to local stakeholders and review of regulatory "
                   "exposure."),
        'MEDIUM': ("△  COMPOSITE RISK: MEDIUM\n"
                   "    Elevated risk warrants monitoring. A targeted community "
                   "landscape review is recommended. Identify key opposition "
                   "vectors and build a proactive engagement plan."),
        'LOW':    ("✓  COMPOSITE RISK: LOW\n"
                   "    No significant opposition signal detected for this site. "
                   "Standard due diligence applies. Continue to monitor as "
                   "project details become public."),
    }
    lines.append(action[composite_tier])

    # Confidence caveat if needed
    all_missing = set()
    for s in scores.values():
        all_missing.update(s.get('missing_preds', []))
    key_missing = [p for p in ['by_right','industrial_zoned','build_converted']
                   if p in all_missing]
    if key_missing:
        # Direction of bias: all three vars have negative coefficients,
        # and training medians are by_right=1.0, industrial_zoned=1.0,
        # build_converted=0.0. Imputing these medians applies protective
        # effects the site may not actually have → scores UNDERSTATE risk.
        friendly = {
            'by_right':        'by-right approval status',
            'industrial_zoned':'industrial zoning',
            'build_converted': 'build type (new vs conversion)',
        }
        missing_labels = ', '.join(friendly.get(p, p) for p in key_missing)
        lines.append(
            f"  ℹ  Note: {missing_labels} not provided for this site. "
            f"The model has assumed the most common values from comparable "
            f"facilities — which tend to be lower-risk site characteristics. "
            f"The scores above are therefore a conservative estimate: "
            f"actual risk may be higher if this site lacks these protective "
            f"characteristics. Providing these inputs will give a more "
            f"accurate picture."
        )

    return '\n\n'.join(lines)


def score_facility(facility: dict, models: list = None) -> dict:
    """
    Score a facility against one or more models.

    Parameters
    ----------
    facility : dict
        Raw facility variables. Missing values imputed to training medians.
    models : list of str, optional
        Default: ['M1', 'M3', 'M4']. Options: 'M1','M2','M3','M4'.

    Returns
    -------
    dict:
        scores          per-model score dicts
        composite_tier  overall LOW / MEDIUM / HIGH
        narrative       plain-English risk narrative
    """
    _load()
    if models is None:
        models = ['M1', 'M3', 'M4']

    fac    = _build_derived(facility)

    # M4 (successful opposition) is a strict subset of both M1 (any opposition) and
    # M3 (any adverse outcome), so its probability can never exceed either. Score the
    # bounding models even if the caller did not request them, then clamp.
    needed = set(models) | ({'M1', 'M3'} if 'M4' in models else set())
    scored = {mk: _score_one_model(mk, fac) for mk in needed if mk in _models}

    if 'M4' in scored:
        bounds = {mk: scored[mk]['cal_prob'] for mk in ('M1', 'M3') if mk in scored}
        if bounds:
            cap = min(bounds.values())
            if scored['M4']['cal_prob'] > cap:
                scored['M4']['cal_prob_unclamped'] = scored['M4']['cal_prob']
                scored['M4']['cal_prob']     = round(cap, 4)
                scored['M4']['cal_prob_pct'] = f"{cap*100:.1f}%"
                scored['M4']['tier']         = _assign_tier(
                    cap, _models['M4']['tier_low'], _models['M4']['tier_high'])
                scored['M4']['lift']         = round(cap / _models['M4']['base_rate'], 2)
                scored['M4']['clamped_by']   = min(bounds, key=bounds.get)

    scores = {mk: scored[mk] for mk in models if mk in scored}

    tiers = [s['tier'] for s in scores.values()]
    composite_tier = ('HIGH'   if 'HIGH'   in tiers else
                      'MEDIUM' if 'MEDIUM' in tiers else 'LOW')

    return {
        'scores':         scores,
        'composite_tier': composite_tier,
        'primary_model':  'M4',
        'narrative':      _build_narrative(scores, composite_tier),
    }


def print_score(facility: dict, **kwargs):
    """Score and pretty-print to stdout."""
    result = score_facility(facility, **kwargs)
    print("\n" + "="*65)
    print("  DC OPPOSITION RISK SCORE  |  LocalQ Labs")
    print("="*65)
    print()
    print(result['narrative'])
    print()
    print("─"*65)
    print("  Raw scores (internal):")
    for mk, s in result['scores'].items():
        print(f"    {mk}  raw={s['raw_prob']:.3f}  "
              f"calibrated={s['cal_prob']:.3f}  "
              f"confidence={s['confidence']}")
    print("="*65)
    return result


# ── Example ───────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # High-risk: Greenfield, Loudoun County VA
    loudoun = {
        'industrial_zoned': 0, 'build_converted': 0, 'by_right': 0,
        'project_year': 2026,
        'jurisdiction_homeownership': 0.72, 'jurisdiction_education': 0.58,
        'jurisdiction_hardship': -0.3,      'jurisdiction_nonwhite': 0.31,
        'mine_total_prop_2024': 0.001,      'mfg_pp_2010_2024': -2.1,
        'mfg_decline': 1,                   'gop_governor': 0,
        'gop_trifecta': 0,                  'appeal_v': 3,
        'ballot_v': 1,    'env_review_v': 1, 'bond_v': 0,
        'has_cz': 1,      'water_v': 2,     'nwis_trend': -0.8,
        'county_opp_context_clean': 1.2,
        'ev_half_clean': 8,  'prior_opp_dcs_county': 5,
        'opp_rank_A': 1.4,   'opp_rank_B': 1.1,
        'prior_dcs_imp': 12, 'opp_proportion_imp': 0.42,
        'env_org_capacity': 2.8,
    }

    print("\n--- EXAMPLE 1: High-risk greenfield (Loudoun County VA) ---")
    print_score(loudoun)

    # Low-risk: industrial site, rural GOP county, no prior opposition
    low_risk = {
        'industrial_zoned': 1, 'build_converted': 1, 'by_right': 1,
        'project_year': 2024,
        'jurisdiction_homeownership': 0.61, 'jurisdiction_education': 0.18,
        'jurisdiction_hardship': 0.8,       'jurisdiction_nonwhite': 0.09,
        'mine_total_prop_2024': 0.04,       'mfg_pp_2010_2024': -1.2,
        'mfg_decline': 1,                   'gop_governor': 1,
        'gop_trifecta': 1,                  'appeal_v': 1,
        'ballot_v': 0,    'env_review_v': 0, 'bond_v': 1,
        'has_cz': 0,      'water_v': 0,     'nwis_trend': 0.1,
        'county_opp_context_clean': -0.54,
        'ev_half_clean': 0,  'prior_opp_dcs_county': 0,
        'opp_rank_A': -1.2,  'opp_rank_B': -0.8,
        'prior_dcs_imp': 0,  'opp_proportion_imp': 0.0,
        'env_org_capacity': 0.5,
    }

    print("\n--- EXAMPLE 2: Low-risk industrial conversion (rural GOP county) ---")
    print_score(low_risk)

    # Missing key inputs (partial)
    partial = {
        'project_year': 2026,
        'jurisdiction_homeownership': 0.65, 'jurisdiction_education': 0.38,
        'jurisdiction_hardship': 0.1,       'jurisdiction_nonwhite': 0.22,
        'mine_total_prop_2024': 0.005,      'mfg_pp_2010_2024': -1.8,
        'gop_governor': 0, 'gop_trifecta': 0,
        'appeal_v': 2, 'ballot_v': 1, 'env_review_v': 1,
        'county_opp_context_clean': 0.3,
        'ev_half_clean': 2, 'prior_opp_dcs_county': 1,
        'prior_dcs_imp': 5, 'opp_proportion_imp': 0.20,
        'env_org_capacity': 1.8,
        # by_right, industrial_zoned, build_converted NOT provided
    }

    print("\n--- EXAMPLE 3: Partial inputs (site vars unknown) ---")
    print_score(partial)
