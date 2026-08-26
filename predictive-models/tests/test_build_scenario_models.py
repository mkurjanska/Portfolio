"""
Real unit tests for year_cap() and _IdentityCalibrator in build_scenario_models.py -- pure
functions/classes, no training data or pickled artifacts required.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import math

import numpy as np
import pytest

from build_scenario_models import year_cap, MIN_YEAR_SUPPORT, _IdentityCalibrator, logit


def test_empty_input_returns_nan():
    assert math.isnan(year_cap([]))


def test_all_non_numeric_returns_nan():
    assert math.isnan(year_cap(['not', 'a', 'year']))


def test_single_recent_outlier_does_not_move_the_cap():
    # The documented case: one facility dated 2027 among many at 2026 must not flip the
    # cap to 2027 -- that single row doesn't meet MIN_YEAR_SUPPORT.
    years = [2026] * 15 + [2027]
    assert year_cap(years) == 2026.0


def test_year_qualifies_once_it_hits_the_support_threshold():
    years = [2026] * 5 + [2027] * MIN_YEAR_SUPPORT
    assert year_cap(years) == 2027.0


def test_boundary_one_below_threshold_does_not_qualify():
    years = [2026] * MIN_YEAR_SUPPORT + [2027] * (MIN_YEAR_SUPPORT - 1)
    assert year_cap(years) == 2026.0


def test_no_year_meets_threshold_falls_back_to_raw_max():
    # With no year reaching MIN_YEAR_SUPPORT observations, the function falls back to the
    # plain maximum rather than returning nothing.
    years = [2020, 2021, 2022, 2023, 2024]
    assert year_cap(years) == 2024.0


def test_nan_and_non_numeric_values_are_dropped_not_counted():
    years = [2026] * MIN_YEAR_SUPPORT + [None, float('nan'), 'bad'] * 5
    assert year_cap(years) == 2026.0


# ---------------------------------------------------------------------------
# _IdentityCalibrator -- the fallback for a Platt fit that inverted ranking on a thin
# calibration fold -- M2's ~74-row calibration fold is a third the size of M1/M4's, thin
# enough that a negative-slope fit flipped test AUC from 0.696 to 0.304 on one seed).
# ---------------------------------------------------------------------------

def test_identity_calibrator_round_trips_through_logit():
    # predict_proba takes logit-space input, same interface as the real Platt model it
    # replaces -- feeding it logit(p) must hand back p unchanged (a true no-op).
    p_in = np.array([0.1, 0.3, 0.5, 0.7, 0.95])
    p_out = _IdentityCalibrator().predict_proba(logit(p_in).reshape(-1, 1))[:, 1]
    assert p_out == pytest.approx(p_in, abs=1e-6)


def test_identity_calibrator_preserves_ranking():
    # The entire point of the fallback: unlike an inverted Platt fit, it never reorders
    # scores -- higher raw probability must stay higher after the "calibration" step.
    p_in = np.array([0.05, 0.4, 0.6, 0.9])
    p_out = _IdentityCalibrator().predict_proba(logit(p_in).reshape(-1, 1))[:, 1]
    assert list(p_out) == sorted(p_out)


def test_identity_calibrator_rows_sum_to_one():
    p_in = np.array([0.2, 0.8])
    proba = _IdentityCalibrator().predict_proba(logit(p_in).reshape(-1, 1))
    assert proba.sum(axis=1) == pytest.approx(1.0)
