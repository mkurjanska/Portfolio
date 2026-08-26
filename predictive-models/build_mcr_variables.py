"""
build_mcr_variables.py
Build the seven MCR regulatory variables used in champion model specs.

Source: master_county_regulatory.csv (3,068 county rows)
Join key: county (lower, stripped) + '|' + state (lower, stripped) = cty_key

Variables built:
  appeal_v      — legal appeal standing (0–3 ordinal)
  ballot_v      — local ballot initiative possible (0/1 binary)
  bond_v        — bond required to appeal (0/1 binary)
  has_cz        — county has zoning authority (0/1 binary)
  env_review_v  — state environmental review applies (0/1/2 ordinal)
  water_v       — water withdrawal permit required (0/1/2 ordinal)
  nwis_trend    — groundwater level trend ft/yr (continuous, negative = falling)

Usage:
    from build_mcr_variables import build_mcr_vars
    md = build_mcr_vars(md, mcr_path='master_county_regulatory.csv')
"""

import pandas as pd
import numpy as np


def parse_yn(s):
    s = str(s).strip().lower()
    if s in ('yes','1','1.0','true'):  return 1.0
    if s in ('no','0','0.0','false'): return 0.0
    return np.nan


def parse_appeal_standing(s):
    """
    Ordinal: 0=no standing, 1=adjacent/party only, 2=aggrieved person, 3=any person
    """
    s = str(s).strip().lower()
    if 'any_person' in s:                        return 3.0
    if 'aggrieved' in s:                         return 2.0
    if 'adjacent' in s or 'adjacent_party' in s: return 1.0
    if 'no_standing' in s or s in ('no','nan',''):return 0.0
    return np.nan


def parse_env_review(s):
    """
    Ordinal: 0=no review, 1=threshold/state-agency/partial, 2=full state review
    """
    s = str(s).strip().lower()
    if s == 'yes':                                       return 2.0
    if 'state_agency' in s or 'threshold' in s:         return 1.0
    if s in ('no','nan',''):                             return 0.0
    return np.nan


def parse_water_permit(s):
    """
    Ordinal: 0=no permit required, 1=designated areas/partial, 2=statewide permit required
    """
    s = str(s).strip().lower()
    if s in ('yes','yes_statewide'):                     return 2.0
    if 'designated' in s or 'area' in s or 'partial' in s: return 1.0
    if s in ('no','no_permit','nan',''):                 return 0.0
    return np.nan


def build_mcr_vars(md: pd.DataFrame,
                   mcr_path: str = 'master_county_regulatory.csv') -> pd.DataFrame:
    """
    Load MCR file, build regulatory _v variables, merge onto model dataframe.
    md must have 'County' and 'State' columns.
    Returns md with seven new columns added.
    """
    mcr = pd.read_csv(mcr_path, dtype=str, low_memory=False)

    # Build join key on MCR side
    mcr['cty_key'] = (mcr['county'].str.strip().str.lower() + '|' +
                      mcr['state'].str.strip().str.lower())

    # Build _v variables
    mcr['appeal_v']     = mcr['zoning_appeal_standing'].apply(parse_appeal_standing)
    mcr['ballot_v']     = mcr['local_ballot_initiative'].apply(parse_yn)
    mcr['bond_v']       = mcr['bond_required_to_appeal'].apply(parse_yn)
    mcr['has_cz']       = mcr['zoning_county_has_zoning'].apply(parse_yn)
    mcr['env_review_v'] = mcr['state_env_review_applies'].apply(parse_env_review)
    mcr['water_v']      = mcr['water_withdrawal_permit'].apply(parse_water_permit)
    mcr['nwis_trend']   = pd.to_numeric(mcr['nwis_wt_trend_ft_yr'], errors='coerce')

    MCR_VARS = ['appeal_v','ballot_v','bond_v','has_cz',
                'env_review_v','water_v','nwis_trend']

    mcr_merge = mcr[['cty_key'] + MCR_VARS].drop_duplicates('cty_key')

    # Build join key on model data side
    md = md.copy()
    md['cty_key'] = (md['County'].str.strip().str.lower() + '|' +
                     md['State'].str.strip().str.lower())

    # Drop any existing _v columns before merge to avoid _x/_y suffixes
    for v in MCR_VARS:
        if v in md.columns:
            md = md.drop(columns=[v])

    md = md.merge(mcr_merge, on='cty_key', how='left')

    # Coverage report
    print("MCR variable coverage after merge:")
    for v in MCR_VARS:
        nn = pd.to_numeric(md[v], errors='coerce').notna().sum()
        print(f"  {v:<15} {nn:4d}/{len(md)} ({nn/len(md)*100:.0f}%)")

    return md


# ── Encoding reference (for documentation) ────────────────────────────────────
ENCODING_REFERENCE = {
    'appeal_v': {
        'type': 'ordinal 0–3',
        'source_col': 'zoning_appeal_standing',
        0: 'no standing / no challenge mechanism',
        1: 'adjacent party or named party only',
        2: 'aggrieved person standard',
        3: 'any person can appeal (broadest)',
    },
    'ballot_v': {
        'type': 'binary 0/1',
        'source_col': 'local_ballot_initiative',
        0: 'ballot initiatives not available',
        1: 'local ballot initiative possible',
    },
    'bond_v': {
        'type': 'binary 0/1',
        'source_col': 'bond_required_to_appeal',
        0: 'no bond required to appeal',
        1: 'bond required to file zoning appeal',
    },
    'has_cz': {
        'type': 'binary 0/1',
        'source_col': 'zoning_county_has_zoning',
        0: 'county has no zoning authority (or municipal only)',
        1: 'county has its own zoning authority',
    },
    'env_review_v': {
        'type': 'ordinal 0–2',
        'source_col': 'state_env_review_applies',
        0: 'no state environmental review',
        1: 'partial / threshold / state agency review',
        2: 'full state environmental review applies',
    },
    'water_v': {
        'type': 'ordinal 0–2',
        'source_col': 'water_withdrawal_permit',
        0: 'no water withdrawal permit required',
        1: 'permit required in designated/stressed areas only',
        2: 'statewide water withdrawal permit required',
    },
    'nwis_trend': {
        'type': 'continuous (ft/yr)',
        'source_col': 'nwis_wt_trend_ft_yr',
        'note': 'Groundwater level trend. Negative = falling water table. From USGS NWIS via MCR.',
    },
}


if __name__ == '__main__':
    import sys
    md = pd.read_csv('dc_opposition_model_data_FINAL.csv',
                     dtype={'Facility_number': str}, low_memory=False)
    print(f"Loaded model data: {md.shape}")
    md = build_mcr_vars(md, mcr_path='master_county_regulatory.csv')
    print(f"\nAfter merge: {md.shape}")
    md.to_csv('dc_opposition_model_data_with_mcr_vars.csv', index=False)
    print("Saved: dc_opposition_model_data_with_mcr_vars.csv")
