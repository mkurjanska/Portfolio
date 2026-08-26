"""
api_predict_snippet.py — endpoints to paste into api.py for the site-prediction feature.

Depends on:  models/predict_location.py, models/scenario_models.pkl,
             models/county_prediction_features.csv

⚠ Deliberately imports predict_location.py, not scoring_function.py. scoring_function.py
wraps calibration_v3.pkl, which predates the DV corrections (M3's base rate has moved
0.1759 -> 0.1979 since that pkl was fitted) and still carries the retired v3 tier rule.
predict_location.py / scenario_models.pkl are the corrected, tercile-tiered (v4) path -- see
PREDICTION_STACK.md. Don't repoint this at scoring_function.py without first refitting
calibration_v3.pkl to match.

Add `sys.path.insert(0, str(Path(__file__).parent / 'models'))` near the top of api.py,
or install the models directory as a package.

Both endpoints reuse the EXISTING geocoding stack (_geocode_address, geocode_one,
load_index) — nothing about the front half changes.

DEFAULT OUTPUT is M1 (any opposition) + M4 (opposition-caused adverse outcome). M3 ("any
adverse outcome, any cause") is left out of the default response: it's near-chance on the
population actually being queried (AUC 0.55 on the 2026 cohort), since ~40% of its positives
are non-opposition failures the model's predictors can't explain.
Pass `"models": ["M1", "M3", "M4"]` in the request body to include it anyway.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent / 'models'))

from fastapi import Depends, HTTPException, Body           # noqa: E402
from predict_location import predict, SITE                 # noqa: E402

_ALLOWED_MODELS = {'M1', 'M2', 'M3', 'M4'}


@app.post("/api/predict-site")                                          # noqa: F821
def api_predict_site(payload: dict = Body(...),
                     _user: str = Depends(verify_credentials)):         # noqa: F821
    """Score one site.

    Body accepts EITHER a resolved county:
        {"fips": "51047", "by_right": 0, "industrial_zoned": 1,
         "build_converted": 0, "project_year": 2026}
    OR a free-text address, which is geocoded first:
        {"address": "1234 Example Rd, Culpeper VA", "project_year": 2026}

    Site inputs are all optional. Omitting by_right / industrial_zoned /
    build_converted routes to a variant FIT WITHOUT them rather than imputing --
    the estimate is weaker, not biased, and the response says so under `qualifications`.

    Optional `"models": [...]` (subset of M1/M2/M3/M4) overrides the default M1+M4 output.
    """
    site = {k: payload.get(k) for k in SITE}
    fips = payload.get('fips')
    geo = None

    models = payload.get('models')
    if models is not None:
        bad = set(models) - _ALLOWED_MODELS
        if bad:
            raise HTTPException(400, f"Unknown model(s) {sorted(bad)}; allowed: M1-M4")
        models = tuple(models)

    if not fips:
        addr = (payload.get('address') or '').strip()
        if not addr:
            raise HTTPException(400, "Provide either 'fips' or 'address'")
        ll, src = _geocode_address(addr)                                 # noqa: F821
        if ll is None:
            raise HTTPException(422, f"Could not geocode: {addr}")
        lat, lng = ll
        g = geocode_one(lat, lng, load_index())                          # noqa: F821
        fips = g.county_geoid
        geo = {'lat': lat, 'lng': lng, 'source': src,
               'place_name': g.place_name, 'incorporation': g.incorporation}
        if not fips:
            raise HTTPException(422, f"Geocoded but no county resolved: {addr}")

    try:
        kwargs = dict(site)
        if models is not None:
            kwargs['models'] = models
        result = predict(fips=fips, **kwargs)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except TypeError as e:
        raise HTTPException(400, str(e))

    if geo:
        result['geocode'] = geo
    return result


@app.post("/api/predict-sites")                                          # noqa: F821
def api_predict_sites(payload: dict = Body(...),
                      _user: str = Depends(verify_credentials)):         # noqa: F821
    """Batch version. Body: {"sites": [ {...}, {...} ]}. Per-row errors are returned
    inline rather than failing the whole request."""
    sites = payload.get('sites') or []
    if not isinstance(sites, list) or not sites:
        raise HTTPException(400, "Body must include a non-empty 'sites' list")
    if len(sites) > 500:
        raise HTTPException(413, "Maximum 500 sites per request")

    out = []
    for i, s in enumerate(sites):
        try:
            out.append({'index': i, **api_predict_site(s, _user=_user)})
        except HTTPException as e:
            out.append({'index': i, 'error': e.detail})
        except Exception as e:
            out.append({'index': i, 'error': str(e)})
    return {'results': out, 'n': len(out)}
