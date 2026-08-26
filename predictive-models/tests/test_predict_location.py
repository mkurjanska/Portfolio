"""
Real unit tests for predict_location.py — no trained model or data files needed.

choose_variant() is pure and tested directly. The M4 <= min(M1, M3) clamp lives inside
predict(), which normally needs scenario_models.pkl + county_prediction_features.csv;
here it's tested by monkeypatching the module's _scen/_feat globals with small fakes and
stub model objects whose predict_proba() returns a fixed, chosen probability regardless
of input -- so the test controls exactly what M1/M3/M4 "predict" without any real model.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

import predict_location as pl


# ---------------------------------------------------------------------------
# choose_variant()
# ---------------------------------------------------------------------------

def test_all_site_inputs_present_gives_full():
    variant, missing = pl.choose_variant(
        dict(by_right=1, industrial_zoned=0, build_converted=0, project_year=2026))
    assert variant == 'FULL'
    assert missing == []


def test_zero_is_a_valid_value_not_missing():
    # by_right=0 is a real, meaningful answer ("not by-right") -- must not be treated
    # the same as "unknown". This is the case a naive `if not site.get(k)` check gets wrong.
    variant, missing = pl.choose_variant(
        dict(by_right=0, industrial_zoned=0, build_converted=0, project_year=2026))
    assert variant == 'FULL'
    assert missing == []


def test_nothing_supplied_gives_county_only():
    variant, missing = pl.choose_variant({})
    assert variant == 'COUNTY_ONLY'
    assert sorted(missing) == sorted(pl.SITE)


def test_year_only_gives_no_zoning():
    variant, missing = pl.choose_variant(dict(project_year=2026))
    assert variant == 'NO_ZONING'
    assert sorted(missing) == sorted(pl.ZONING)


def test_partial_zoning_still_routes_to_no_zoning():
    # One of three zoning fields known is not "closer to FULL" -- a variant exists per
    # input SET, not per field, so this must route the same as zero zoning fields known.
    variant, missing = pl.choose_variant(
        dict(industrial_zoned=1, project_year=2026))
    assert variant == 'NO_ZONING'
    assert sorted(missing) == sorted(['by_right', 'build_converted'])


def test_full_zoning_but_no_year_gives_county_only():
    # have_year gates FULL and NO_ZONING both -- complete zoning info with no project_year
    # falls all the way back to COUNTY_ONLY, not NO_ZONING's mirror image.
    variant, missing = pl.choose_variant(
        dict(by_right=1, industrial_zoned=1, build_converted=0))
    assert variant == 'COUNTY_ONLY'
    assert missing == ['project_year']


def test_nan_value_counts_as_missing():
    variant, missing = pl.choose_variant(
        dict(by_right=float('nan'), industrial_zoned=1, build_converted=0, project_year=2026))
    assert variant == 'NO_ZONING'
    assert missing == ['by_right']


# ---------------------------------------------------------------------------
# predict(): the M4 <= min(M1, M3) clamp
# ---------------------------------------------------------------------------

class _FixedProb:
    """Stub sklearn-style model: predict_proba() ignores its input and always returns
    the same [1-p, p] row, so a test can dictate exactly what a model "predicts"."""
    def __init__(self, p):
        self.p = p

    def predict_proba(self, X):
        n = len(X)
        return np.tile([1 - self.p, self.p], (n, 1))


def _entry(cal_prob, tier_low=0.3, tier_high=0.7, base_rate=0.3):
    return dict(preds=['a'], pipe=_FixedProb(0.5), platt_logit=_FixedProb(cal_prob),
                tier_low=tier_low, tier_high=tier_high, base_rate=base_rate,
                description='test model', test_auc_mean8=0.8, year_cap=None)


def _fake_feat():
    return pd.DataFrame({'county': ['Test County'], 'state': ['VA'], 'a': [1.0]},
                         index=pd.Index(['51001'], name='fips'))


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch):
    # predict_location caches _scen/_feat at module level after first _load(); reset
    # around every test so one test's fakes can't leak into the next.
    monkeypatch.setattr(pl, '_scen', None)
    monkeypatch.setattr(pl, '_feat', None)
    yield


def _site():
    return dict(by_right=1, industrial_zoned=1, build_converted=1, project_year=2026)


def test_m4_is_clamped_down_to_the_tighter_of_m1_m3(monkeypatch):
    scen = {'M1': {'FULL': _entry(0.5)}, 'M3': {'FULL': _entry(0.2)}, 'M4': {'FULL': _entry(0.9)}}
    monkeypatch.setattr(pl, '_scen', scen)
    monkeypatch.setattr(pl, '_feat', _fake_feat())

    r = pl.predict(fips='51001', models=('M1', 'M3', 'M4'), **_site())

    assert r['scores']['M4']['cal_prob'] == pytest.approx(0.2)
    assert r['scores']['M4']['clamped'] is True
    assert r['scores']['M4']['clamped_by'] == 'M3'
    assert r['scores']['M4']['cal_prob_unclamped'] == pytest.approx(0.9)


def test_m4_below_both_bounds_is_left_unclamped(monkeypatch):
    scen = {'M1': {'FULL': _entry(0.5)}, 'M3': {'FULL': _entry(0.2)}, 'M4': {'FULL': _entry(0.1)}}
    monkeypatch.setattr(pl, '_scen', scen)
    monkeypatch.setattr(pl, '_feat', _fake_feat())

    r = pl.predict(fips='51001', models=('M1', 'M3', 'M4'), **_site())

    assert r['scores']['M4']['cal_prob'] == pytest.approx(0.1)
    assert 'clamped' not in r['scores']['M4']


def test_m4_exactly_equal_to_the_bound_is_not_clamped(monkeypatch):
    # the clamp is a strict `>`, so touching the bound exactly must not fire it or flip a
    # `clamped` flag onto an otherwise-untouched score.
    scen = {'M1': {'FULL': _entry(0.5)}, 'M3': {'FULL': _entry(0.2)}, 'M4': {'FULL': _entry(0.2)}}
    monkeypatch.setattr(pl, '_scen', scen)
    monkeypatch.setattr(pl, '_feat', _fake_feat())

    r = pl.predict(fips='51001', models=('M1', 'M3', 'M4'), **_site())

    assert r['scores']['M4']['cal_prob'] == pytest.approx(0.2)
    assert 'clamped' not in r['scores']['M4']


def test_m1_m3_computed_internally_but_only_returned_if_requested(monkeypatch):
    # Documented behavior: requesting M4 alone still fits M1 and M3 in the background to
    # police the clamp, but only the caller's requested models come back in `scores`.
    scen = {'M1': {'FULL': _entry(0.5)}, 'M3': {'FULL': _entry(0.2)}, 'M4': {'FULL': _entry(0.9)}}
    monkeypatch.setattr(pl, '_scen', scen)
    monkeypatch.setattr(pl, '_feat', _fake_feat())

    r = pl.predict(fips='51001', models=('M4',), **_site())

    assert set(r['scores']) == {'M4'}
    # the clamp still used M3's 0.2 even though M3 never appears in the output
    assert r['scores']['M4']['cal_prob'] == pytest.approx(0.2)


def test_m1_alone_does_not_trigger_m3(monkeypatch):
    # M3 must only ever be computed as a side effect of an M4 request, never on its own.
    scen = {'M1': {'FULL': _entry(0.5)}, 'M3': {'FULL': _entry(0.2)}}
    monkeypatch.setattr(pl, '_scen', scen)
    monkeypatch.setattr(pl, '_feat', _fake_feat())

    r = pl.predict(fips='51001', models=('M1',), **_site())

    assert set(r['scores']) == {'M1'}
