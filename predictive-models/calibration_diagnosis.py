"""
calibration_diagnosis.py — reproduces the diagnostic behind the July 2026 calibration fix
(see docs/CALIBRATION_diagnosis_and_fix.md): Platt scaling fit on the raw probability
(bounded below at 0) forces a calibrated floor of sigmoid(intercept) that differs per model.
M4's floor came out higher than M1's (0.0418 vs 0.0250), so despite M4 being a strict subset
of M1 by construction (successful opposition implies opposition happened), the deployed
scorer sometimes rated successful-opposition risk higher than any-opposition risk on the same
facility. Fitting Platt in logit space instead removes the floor artifact.

Uses the actual deployed artifacts (pipe_deploy/platt_deploy in model_results_FINAL.pkl) for
the buggy side, so the reproduced numbers match the ones in the write-up exactly, and refits
the fix (Platt in logit space, calibrator held out on the 20% split, per build_calibration_v2.py)
for the corrected side.

Needs md_model_ready.pkl and model_results_FINAL.pkl alongside this script -- not committed,
proprietary training data (see README). Produces calibration_diagnosis.png.
"""
from pathlib import Path
import pickle, warnings
import numpy as np, pandas as pd
import matplotlib.pyplot as plt
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split

warnings.filterwarnings('ignore')

HERE = Path(__file__).parent
SEED, EPS = 42, 1e-6


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def mkpipe(cw, C):
    return Pipeline([('imp', SimpleImputer(strategy='median')), ('sc', StandardScaler()),
                     ('lr', LogisticRegression(C=C, class_weight=cw, max_iter=3000))])


R = pickle.load(open(HERE / 'model_results_FINAL.pkl', 'rb'))
M = pd.read_pickle(HERE / 'md_model_ready.pkl')
DV = {'M1': 'DV_opposition', 'M3': 'DV_adverse_full', 'M4': 'DV_oppcaused_adverse'}

# Same 40% train / 20% calibrate / 40% test split as build_calibration_v2.py, stratified on
# the joint (M1, M4) outcome pattern so both DVs are represented in every fold.
strat = (M['DV_opposition'].fillna(-1).astype(int).astype(str) + '_' +
         M['DV_oppcaused_adverse'].fillna(-1).astype(int).astype(str))
vc = strat.value_counts()
strat = strat.where(strat.map(vc) >= 3, 'rare')
tr, tmp = train_test_split(M.index, train_size=0.4, stratify=strat, random_state=SEED)
ca, te = train_test_split(tmp, train_size=1 / 3, stratify=strat.loc[tmp], random_state=SEED)

# The held-out test population: every facility with a coded M4 outcome in the test split --
# same population size the write-up scores (n=498).
test_pop = M.loc[te].dropna(subset=[DV['M4']]).index
print(f'held-out test population: n={len(test_pop)}')

buggy, fixed = {}, {}
for m in ['M1', 'M4']:
    mod = R[m]
    P = mod['preds']
    X = M.loc[test_pop, P]

    # --- Buggy side: the ACTUAL deployed artifacts (fit on all data, Platt on raw probability).
    raw_deploy = mod['pipe_deploy'].predict_proba(X)[:, 1]
    buggy[m] = mod['platt_deploy'].predict_proba(raw_deploy.reshape(-1, 1))[:, 1]
    floor_buggy = float(mod['platt_deploy'].predict_proba([[0.0]])[0, 1])

    # --- Fixed side: refit properly -- pipe on the 40% train split, Platt in logit space on
    # the held-out 20% calibration split (build_calibration_v2.py's actual fix).
    dv = DV[m]
    Str = M.loc[tr, P + [dv]].dropna(subset=[dv])
    Sca = M.loc[ca, P + [dv]].dropna(subset=[dv])
    pipe = mkpipe(mod['class_weight'], mod['C']).fit(Str[P], Str[dv].astype(int))
    raw_ca = pipe.predict_proba(Sca[P])[:, 1]
    platt_logit = LogisticRegression(C=1e10, max_iter=1000).fit(logit(raw_ca).reshape(-1, 1), Sca[dv].astype(int))
    raw_te = pipe.predict_proba(X)[:, 1]
    fixed[m] = platt_logit.predict_proba(logit(raw_te).reshape(-1, 1))[:, 1]
    floor_fixed = float(platt_logit.predict_proba([[logit(np.array([1e-12]))[0]]])[0, 1])

    print(f'{m}: floor buggy (probability-space, deployed) = {floor_buggy:.4f}   '
          f'floor fixed (logit-space, held-out calibrate) = {floor_fixed:.6f}')

viol_buggy = float((buggy['M4'] > buggy['M1']).mean())
viol_fixed = float((fixed['M4'] > fixed['M1']).mean())
print(f'\nM4 > M1 violation rate -- buggy (deployed): {viol_buggy:.1%}   fixed: {viol_fixed:.1%}')

# --- Figure: transfer curve (left) + M4-vs-M1 scatter (right) ---
fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))

raw_grid = np.linspace(1e-4, 1 - 1e-4, 400)
colors = {'M1': '#2a78d6', 'M4': '#eb6834'}
for m in ['M1', 'M4']:
    cal_buggy = R[m]['platt_deploy'].predict_proba(raw_grid.reshape(-1, 1))[:, 1]
    axes[0].plot(raw_grid, cal_buggy, '--', color=colors[m], alpha=0.85,
                 label=f'{m} (probability-space, deployed)')
axes[0].set_prop_cycle(None)
for m in ['M1', 'M4']:
    # Recompute the fixed transfer curve on the same pipe/platt fit above, refit once more on the
    # full train+calibrate split for a stable curve (matches what was fit above per model).
    mod = R[m]
    P = mod['preds']
    dv = DV[m]
    Str = M.loc[tr, P + [dv]].dropna(subset=[dv])
    Sca = M.loc[ca, P + [dv]].dropna(subset=[dv])
    pipe = mkpipe(mod['class_weight'], mod['C']).fit(Str[P], Str[dv].astype(int))
    raw_ca = pipe.predict_proba(Sca[P])[:, 1]
    platt_logit = LogisticRegression(C=1e10, max_iter=1000).fit(logit(raw_ca).reshape(-1, 1), Sca[dv].astype(int))
    cal_fixed = platt_logit.predict_proba(logit(raw_grid).reshape(-1, 1))[:, 1]
    axes[0].plot(raw_grid, cal_fixed, '-', color=colors[m], label=f'{m} (logit-space, fixed)')

axes[0].set_xlabel('raw model output')
axes[0].set_ylabel('calibrated probability')
axes[0].set_title('Calibration transfer curve\ndashed = current (probability space), solid = corrected (logit space)',
                   fontsize=10)
axes[0].legend(fontsize=8, loc='upper left')
axes[0].set_xlim(0, 1)
axes[0].set_ylim(0, 1)

axes[1].scatter(buggy['M1'], buggy['M4'], s=14, color='#c0392b', alpha=0.35, label='probability-space (deployed)')
axes[1].scatter(fixed['M1'], fixed['M4'], s=14, color='#2a9d5c', alpha=0.35, label='logit-space (fixed)')
lim = max(buggy['M1'].max(), buggy['M4'].max(), fixed['M1'].max(), fixed['M4'].max()) * 1.05
axes[1].plot([0, lim], [0, lim], 'k--', linewidth=1, label='M4 = M1 (M4 > M1 is impossible)')
axes[1].set_xlabel('M1 calibrated probability (any opposition)')
axes[1].set_ylabel('M4 calibrated probability (successful opposition)')
axes[1].set_title(f'Every held-out facility, M4 vs M1 (n={len(test_pop)})\n'
                   f'above the diagonal is impossible: {viol_buggy:.1%} (deployed) vs {viol_fixed:.1%} (fixed)',
                   fontsize=10)
axes[1].legend(fontsize=8, loc='upper left')
axes[1].set_xlim(0, lim)
axes[1].set_ylim(0, lim)

fig.suptitle('Why M4 exceeded M1: a calibration floor artifact, found and fixed', fontsize=13, y=1.02)
plt.tight_layout()
plt.savefig(HERE / 'calibration_diagnosis.png', dpi=150, bbox_inches='tight')
print(f'\n✓ saved {HERE / "calibration_diagnosis.png"}')
