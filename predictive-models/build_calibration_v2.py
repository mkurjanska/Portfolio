"""
build_calibration_v2.py — corrected calibration layer for the DC opposition models.

Changes vs the July 2026 deploy path:
  1. Platt scaling is fit in LOGIT space, not probability space. Fitting a logistic on a
     raw probability bounded in [0,1] forces a floor of sigmoid(intercept) > 0. That floor
     was 0.0418 for M4 but only 0.0250 for M1 — so any facility both models scored as very
     unlikely came out with M4 > M1, which is impossible (M4 is a subset of M1).
  2. The pipeline is fit on the 40% train split and the calibrator on the held-out 20%
     calibration split. The previous deploy artifacts fit BOTH on 100% of the data.
  3. A hierarchical clamp enforces  M4 <= min(M1, M3)  at scoring time.

Model specs (predictors, C, class_weight) are UNCHANGED — the champion specs stay locked.
"""
import pandas as pd, numpy as np, pickle, warnings
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, brier_score_loss, log_loss
warnings.filterwarnings('ignore')

SEED=42; EPS=1e-6
OUT='.'
R=pickle.load(open(f'{OUT}/model_results_FINAL.pkl','rb'))
M=pd.read_pickle(f'{OUT}/md_model_ready.pkl')
DV={'M1':'DV_opposition','M2':'DV_oppsuccess','M3':'DV_adverse_full','M4':'DV_oppcaused_adverse'}

def logit(p): p=np.clip(p,EPS,1-EPS); return np.log(p/(1-p))
def mkpipe(cw,C): return Pipeline([('imp',SimpleImputer(strategy='median')),('sc',StandardScaler()),
                                   ('lr',LogisticRegression(C=C,class_weight=cw,max_iter=3000))])

# --- common 40/20/40 split, stratified on the joint outcome pattern
strat=(M['DV_opposition'].fillna(-1).astype(int).astype(str)+'_'+
       M['DV_oppcaused_adverse'].fillna(-1).astype(int).astype(str))
vc=strat.value_counts(); strat=strat.where(strat.map(vc)>=3,'rare')
tr,tmp=train_test_split(M.index,train_size=0.4,stratify=strat,random_state=SEED)
ca,te=train_test_split(tmp,train_size=1/3,stratify=strat.loc[tmp],random_state=SEED)
SPLIT=dict(train=tr,calibrate=ca,test=te)

cal_v2={}
print(f'split: train={len(tr)} ({len(tr)/len(M):.1%})  calibrate={len(ca)} ({len(ca)/len(M):.1%})  test={len(te)} ({len(te)/len(M):.1%})\n')
print(f"{'model':6}{'test AUC':>10}{'Brier':>9}{'LogLoss':>10}{'floor':>9}{'n_tr':>7}{'n_ca':>7}{'n_te':>7}")
for mm in ['M1','M2','M3','M4']:
    mod=R[mm]; dv=DV[mm]; P=mod['preds']; d=M[P+[dv]]
    S={k:d.loc[v].dropna(subset=[dv]) for k,v in SPLIT.items()}
    Xtr,ytr=S['train'][P],S['train'][dv].astype(int)
    Xca,yca=S['calibrate'][P],S['calibrate'][dv].astype(int)
    Xte,yte=S['test'][P],S['test'][dv].astype(int)
    pipe=mkpipe(mod['class_weight'],mod['C']).fit(Xtr,ytr)
    platt=LogisticRegression(C=1e10,max_iter=1000).fit(logit(pipe.predict_proba(Xca)[:,1]).reshape(-1,1),yca)
    pte=platt.predict_proba(logit(pipe.predict_proba(Xte)[:,1]).reshape(-1,1))[:,1]
    floor=platt.predict_proba(np.array([[logit(np.array([1e-12]))[0]]]))[0,1]
    cal_v2[mm]=dict(preds=P, pipe=pipe, platt_logit=platt, space='logit',
                    class_weight=mod['class_weight'], C=mod['C'],
                    base_rate=mod['base_rate'], tier_low=mod['tier_low'], tier_high=mod['tier_high'],
                    description=mod['description'],
                    test_auc=roc_auc_score(yte,pte), test_brier=brier_score_loss(yte,pte),
                    test_logloss=log_loss(yte,np.clip(pte,EPS,1-EPS)),
                    n_train=len(ytr), n_cal=len(yca), n_test=len(yte))
    print(f"{mm:6}{cal_v2[mm]['test_auc']:10.4f}{cal_v2[mm]['test_brier']:9.4f}{cal_v2[mm]['test_logloss']:10.4f}{floor:9.6f}{len(ytr):7d}{len(yca):7d}{len(yte):7d}")
cal_v2['_meta']=dict(seed=SEED, split_fractions=(0.4,0.2,0.4), split_index={k:list(v) for k,v in SPLIT.items()},
                     clamp='M4 <= min(M1, M3)', calibration='Platt in logit space, fit on held-out 20%')
pickle.dump(cal_v2, open(f'{OUT}/calibration_v2.pkl','wb'))
print(f'\nsaved {OUT}/calibration_v2.pkl')
