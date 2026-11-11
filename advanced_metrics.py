"""Exploratory metrics from SCREENED EEG windows (not cognition/diagnosis)."""
from __future__ import annotations
import numpy as np


def summarize_advanced(x: np.ndarray, fs: float, starts: np.ndarray,
                       accepted: list[bool], freq: np.ndarray, psd: np.ndarray,
                       coarse: bool, min_windows: int = 15) -> dict:
    good=[int(i) for i,a in zip(starts,accepted) if a]
    note='Окна перекрываются и не являются независимыми. Это характеристики отобранного цифрового сигнала, не показания интеллекта/внимания.'
    if coarse or len(good)<min_windows:
        return {'available':False,'reason':'Недостаточно пригодных окон или грубое квантование.',
                'window_count':len(good),'notice':note}
    p=np.asarray(psd,dtype=float)
    valid=np.isfinite(p)&(p>0)&(((freq>=3)&(freq<7))|((freq>=14)&(freq<=40)))
    if valid.sum()<12:
        return {'available':False,'reason':'Не хватает частотных бинов для оценки наклона',
                'window_count':len(good),'notice':note}
    xx=np.log10(freq[valid]);yy=np.log10(p[valid]);coeff=np.polyfit(xx,yy,1)
    predicted=np.polyval(coeff,xx)
    ss_res=float(np.sum((yy-predicted)**2));ss_tot=float(np.sum((yy-yy.mean())**2))
    r2=max(0,float(1-ss_res/ss_tot)) if ss_tot>0 else None
    # Hjorth depends on actual units and sampling frequency; report raw scale.
    size=max(16,int(round(fs*4)))
    values=[]
    for start in good:
        w=np.asarray(x[start:start+size],dtype=float)
        if len(w)<3 or not np.isfinite(w).all():continue
        d=np.diff(w);dd=np.diff(d)
        var0=np.var(w);var1=np.var(d);var2=np.var(dd)
        if min(var0,var1,var2)<=1e-20:continue
        activity=float(var0)
        mobility=float(np.sqrt(var1/var0))
        complexity=float(np.sqrt(var2/var1)/mobility)
        values.append((activity,mobility,complexity))
    hjorth={}
    if len(values)>=min_windows:
        arr=np.asarray(values)
        hjorth={name:round(float(np.median(arr[:,col])),7) for col,name in enumerate(('activity_sdk_units_squared','mobility','complexity'))}
    return {'available':True,'window_count':len(good),'hjorth_median':hjorth or None,
            'log_spectral_slope':round(float(coeff[0]),4),
            'log_spectral_r2':round(r2,4) if r2 is not None else None,
            'slope_method':'Линейная регрессия log10 PSD ~ log10 f на 3–7 и 14–40 Гц. НЕ разделение периодических и апериодических компонент specparam.',
            'notice':note}
