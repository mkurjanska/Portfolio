import pandas as pd, numpy as np, pickle, warnings
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, brier_score_loss, log_loss
warnings.filterwarnings('ignore')
SEED=42; EPS=1e-6
R=pickle.load(open('model_results_FINAL.pkl','rb'))
M=pd.read_pickle('md_model_ready.pkl')
DV={'M1':'DV_opposition','M2':'DV_oppsuccess','M3':'DV_adverse_full','M4':'DV_oppcaused_adverse'}

# ---- COMMON 40/20/40 split over all rows, stratified on the joint outcome pattern
strat=(M['DV_opposition'].fillna(-1).astype(int).astype(str)+'_'+M['DV_oppcaused_adverse'].fillna(-1).astype(int).astype(str))
vc=strat.value_counts(); strat=strat.where(strat.map(vc)>=3,'rare')
idx=M.index
tr,tmp=train_test_split(idx,train_size=0.4,stratify=strat.loc[idx],random_state=SEED)
ca,te=train_test_split(tmp,train_size=1/3,stratify=strat.loc[tmp],random_state=SEED)
SPLIT=dict(tr=tr,ca=ca,te=te)

def mkpipe(cw,C): return Pipeline([('imp',SimpleImputer(strategy='median')),('sc',StandardScaler()),
                                   ('lr',LogisticRegression(C=C,class_weight=cw,max_iter=3000))])
def logit(p): p=np.clip(p,EPS,1-EPS); return np.log(p/(1-p))

def fit_common(mm):
    mod=R[mm]; dv=DV[mm]; P=mod['preds']
    d=M[P+[dv]]
    f={}
    for k in ['tr','ca','te']:
        s=d.loc[SPLIT[k]].dropna(subset=[dv])
        f['X'+k]=s[P]; f['y'+k]=s[dv].astype(int)
    pipe=mkpipe(mod['class_weight'],mod['C']).fit(f['Xtr'],f['ytr'])
    for k in ['ca','te']:
        f['raw'+k]=pipe.predict_proba(f['X'+k])[:,1]
    f['pipe']=pipe; f['mod']=mod
    return f

def calibrators(f):
    """Return dict of name -> function(raw)->cal, each fit ONLY on the 20% calibration set."""
    rc,yc=f['rawca'],f['yca']
    out={}
    pl=LogisticRegression(C=1e10,max_iter=1000).fit(rc.reshape(-1,1),yc)
    out['platt_prob']=lambda r,pl=pl: pl.predict_proba(r.reshape(-1,1))[:,1]
    pl2=LogisticRegression(C=1e10,max_iter=1000).fit(logit(rc).reshape(-1,1),yc)
    out['platt_logit']=lambda r,pl=pl2: pl.predict_proba(logit(r).reshape(-1,1))[:,1]
    iso=IsotonicRegression(out_of_bounds='clip',y_min=0,y_max=1).fit(rc,yc)
    out['isotonic']=lambda r,iso=iso: iso.predict(r)
    return out

def ece(y,p,bins=10):
    e=0.0; n=len(y)
    q=np.quantile(p,np.linspace(0,1,bins+1)); q[0]-=1e-9
    for i in range(bins):
        s=(p>q[i])&(p<=q[i+1])
        if s.sum(): e+=s.sum()/n*abs(y[s].mean()-p[s].mean())
    return e

def score(y,p):
    return dict(auc=roc_auc_score(y,p), brier=brier_score_loss(y,p),
                logloss=log_loss(y,np.clip(p,EPS,1-EPS)), ece=ece(np.asarray(y),np.asarray(p)))
