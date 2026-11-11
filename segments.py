"""Bounded EEG window extraction; never sorts by legacy host timestamps."""
from __future__ import annotations
import csv
import json
import math
from itertools import islice
from pathlib import Path
from typing import Any
import numpy as np
from scipy.signal import welch

BANDS={'delta':(1,4),'theta':(4,8),'alpha':(8,13),'beta':(13,30),'30-45 Hz':(30,45)}


def segment(path: Path, start_s: float, duration_s: float, max_points: int = 1400) -> dict[str, Any]:
    if not math.isfinite(start_s) or start_s < 0 or not math.isfinite(duration_s) or not 2 <= duration_s <= 120:
        raise ValueError('Допустимы отрезки длительностью 2–120 секунд и начало >= 0')
    if not 100<=max_points<=4000:raise ValueError('От 100 до 4000 точек')
    meta=json.loads((path/'meta.json').read_text(encoding='utf-8'))
    sr=float(meta.get('sample_rates',{}).get('eeg',250))
    if not 0 < sr <= 4096:raise ValueError('Некорректная частота дискретизации')
    first,last=int(round(start_s*sr)),int(round((start_s+duration_s)*sr))
    eeg=path/'eeg.csv'
    if not eeg.is_file():return {'available':False,'reason':'Нет eeg.csv'}
    with eeg.open(newline='',encoding='utf-8-sig') as handle:
        reader=csv.DictReader(handle)
        fields=[c for c in (reader.fieldnames or []) if c not in ('ts_unix','ts_iso','sample_index')]
        if not fields:return {'available':False,'reason':'В eeg.csv нет каналов'}
        values=[]
        for row in islice(reader,first,last):
            try:values.append([float(row.get(name,'')) for name in fields])
            except (ValueError,TypeError):values.append([float('nan')]*len(fields))
    if not values:return {'available':False,'reason':'В этом диапазоне нет отсчётов'}
    a=np.asarray(values,dtype=float)
    # Extract min/max indices of every channel per time bucket, preserving order.
    bucket=max(1,int(math.ceil(len(a)*max(2,2*len(fields))/max_points)))
    indices=set([0,len(a)-1])
    for begin in range(0,len(a),bucket):
        block=a[begin:begin+bucket]
        for col in range(a.shape[1]):
            x=block[:,col]; valid=np.where(np.isfinite(x))[0]
            if valid.size:
                indices.add(begin+int(valid[np.argmin(x[valid])]))
                indices.add(begin+int(valid[np.argmax(x[valid])]))
    keep=sorted(indices)
    chart=[{'t':round((first+i)/sr,4),
            'values':[float(v) if math.isfinite(v) else None for v in a[i]]}
           for i in keep]
    powers=[]
    for col in range(a.shape[1]):
        v=a[:,col]
        if len(v)<2*sr or np.isfinite(v).mean()<.99:
            powers.append({'channel':fields[col],'bands':None,'reason':'Мало данных или пропуски'})
            continue
        freq,psd=welch(v-v.mean(),fs=sr,nperseg=min(len(v),int(sr*4)),detrend='linear')
        mask=(freq>=1)&(freq<45)
        total=float(np.trapezoid(psd[mask],freq[mask]))
        band={}
        for name,(lo,hi) in BANDS.items():
            inside=(freq>=lo)&(freq<hi)
            band[name]=round(float(np.trapezoid(psd[inside],freq[inside])/total)*100,2) if total>0 and np.count_nonzero(inside)>=2 else None
        powers.append({'channel':fields[col],'bands':band,
                       'reason':'Исследовательская относительная спектральная мощность; без проверки артефактов'})
    return {'available':True,'start_s':start_s,'duration_s':len(a)/sr,
            'sample_count':len(a),'sample_rate_hz':sr,'channels':fields,
            'points':chart,'bands':powers,
            'notice':'Ось построена по порядку отсчётов. Старые ts_unix могут идти назад; межпоточная синхронизация приблизительна.'}
