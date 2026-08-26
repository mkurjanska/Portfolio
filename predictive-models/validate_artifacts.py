"""
validate_artifacts.py — consistency check across all deployed artifacts.
Run after any change to calibration, tiers, or config. Exits non-zero on failure.
"""
import pickle, json, sys
from pathlib import Path
D=Path(__file__).parent
fail=[]
cal=pickle.load(open(D/'calibration_v2.pkl','rb'))
cfg=json.load(open(D/'scoring_config.json'))
wa=pickle.load(open(D/'webapp_models_v2.pkl','rb'))
models=[k for k in cal if not k.startswith('_')]

# 1. no model fit on more than 40%
for m in models:
    f=cal[m].get('train_fraction')
    if f is None: fail.append(f'{m}: train_fraction missing')
    elif f>0.4+1e-6: fail.append(f'{m}: train_fraction {f:.1%} exceeds 40%')

# 2. calibration pkl and config agree
for m in models:
    for k in ['base_rate','tier_low','tier_high']:
        if k not in cfg.get(m,{}): fail.append(f'{m}.{k} missing from config'); continue
        if abs(cal[m][k]-cfg[m][k])>1e-9: fail.append(f'{m}.{k}: pkl {cal[m][k]} != config {cfg[m][k]}')

# 3. webapp view matches core
for wk,ck in [('WA_M1','M1'),('WA_M3','M3'),('WA_M4','M4')]:
    for k in ['tier_low','tier_high','base_rate','preds']:
        if wa[wk][k]!=cal[ck][k]: fail.append(f'{wk}.{k} has drifted from {ck}')

# 4. tier rule applied uniformly
for m in models:
    b=cal[m]['base_rate']
    if abs(cal[m]['tier_low']/b-0.7)>0.005: fail.append(f'{m}: tier_low is {cal[m]["tier_low"]/b:.2f}x base, expected 0.70x')
    if abs(cal[m]['tier_high']/b-1.5)>0.005: fail.append(f'{m}: tier_high is {cal[m]["tier_high"]/b:.2f}x base, expected 1.50x')
    if cal[m]['tier_low']>=cal[m]['tier_high']: fail.append(f'{m}: tier_low >= tier_high')

# 5. config schema is a superset of v1
v1p=D/'archive_v1'/'scoring_config_v1.json'
if v1p.exists():
    v1=json.load(open(v1p))
    for m in [k for k in v1 if not k.startswith('_')]:
        miss=set(v1[m])-set(cfg.get(m,{}))
        if miss: fail.append(f'{m}: config lost v1 keys {sorted(miss)}')

# 6. scorer loads and respects nesting bounds
sys.path.insert(0,str(D))
import scoring_function as sf
sf._load()
if set(sf._models)!=set(models): fail.append('scoring_function loaded a different model set')

print('\n'.join('FAIL: '+f for f in fail) if fail else 'ALL ARTIFACT CHECKS PASS')
sys.exit(1 if fail else 0)
