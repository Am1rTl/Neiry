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


def experiment_bounds(path: Path, run_id: str) -> dict:
    """Return a finished run as a half-open range of CSV row positions."""
    begin = end = None
    events = path / 'events.jsonl'
    if events.is_file():
        with events.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # A live session may have a partial trailing line.
                if not isinstance(event, dict) or event.get('run_id') != run_id:
                    continue
                if event.get('type') in ('experiment_lab_start', 'protocol_start'):
                    begin = event
                elif begin and event.get('type') in ('experiment_lab_finish', 'protocol_complete', 'protocol_aborted'):
                    end = event
                    break
    if not begin or not end:
        raise ValueError('В записи нет пары меток начала и завершения этого эксперимента.')
    first, last = begin.get('eeg_row'), end.get('eeg_row')
    approximate = first is None or last is None
    if approximate:
        # Historical files have host timestamps only. Keep acquisition order.
        first = last = None
        with (path / 'eeg.csv').open(newline='', encoding='utf-8-sig') as handle:
            count = 0
            for idx, row in enumerate(csv.DictReader(handle)):
                count = idx + 1
                try:
                    stamp = float(row['ts_unix'])
                except (TypeError, KeyError, ValueError):
                    continue
                if first is None and stamp >= begin['ts_unix']:
                    first = idx
                if first is not None and stamp >= end['ts_unix']:
                    last = idx
                    break
            last = count if last is None else last
    if first is None or last <= first:
        raise ValueError('За время эксперимента не записано ни одного отсчёта ЭЭГ.')
    sr = sample_rate(path)
    return {'start_s': first / sr, 'end_s': last / sr, 'approximate': approximate,
            'name': begin.get('protocol_id') or begin.get('kind') or run_id}


def sample_rate(path: Path) -> float:
    meta = json.loads((path / 'meta.json').read_text(encoding='utf-8'))
    rate = float(meta.get('sample_rates', {}).get('eeg', 250))
    if not 0 < rate <= 4096:
        raise ValueError('Некорректная частота дискретизации')
    return rate


def segment(path: Path, start_s: float, duration_s: float, max_points: int = 1400) -> dict[str, Any]:
    if not math.isfinite(start_s) or start_s < 0 or not math.isfinite(duration_s) or not 0 < duration_s <= 86400:
        raise ValueError('Укажите положительную длительность до 24 часов и начало >= 0')
    if not 100 <= max_points <= 4000:
        raise ValueError('От 100 до 4000 точек')
    sr = sample_rate(path)
    first, last = round(start_s * sr), round((start_s + duration_s) * sr)
    eeg = path / 'eeg.csv'
    if not eeg.is_file():
        return {'available': False, 'reason': 'Нет eeg.csv'}
    chart, chunks = [], []
    count = 0
    with eeg.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        cols = [i for i, c in enumerate(header) if c not in ('ts_unix', 'ts_iso', 'sample_index')]
        fields = [header[i] for i in cols]
        if not fields:
            return {'available': False, 'reason': 'В eeg.csv нет каналов'}
        rows = islice(reader, first, last)
        bucket = max(1, math.ceil((last-first) * 2 * len(cols) / (max_points-2)))
        while batch := list(islice(rows, bucket)):
            values = []
            for row in batch:
                try:
                    values.append([float(row[i]) for i in cols])
                except (ValueError, IndexError):
                    values.append([math.nan] * len(cols))
            block = np.asarray(values)
            if duration_s <= 120:
                chunks.append(block)
            keep = set()
            if count == 0:
                keep.add(0)
            for col in range(len(cols)):
                valid = np.flatnonzero(np.isfinite(block[:, col]))
                if valid.size:
                    keep.update((valid[np.argmin(block[valid, col])], valid[np.argmax(block[valid, col])]))
            if not keep:
                keep.add(0)
            tail = {'t': (first+count+len(block)-1)/sr,
                    'values': [float(v) if math.isfinite(v) else None for v in block[-1]]}
            for idx in sorted(keep):
                chart.append({'t': (first+count+idx)/sr,
                              'values': [float(v) if math.isfinite(v) else None for v in block[idx]]})
            count += len(block)
    if not count:
        return {'available': False, 'reason': 'В этом диапазоне нет отсчётов'}
    if chart[-1]['t'] != tail['t']:
        chart.append(tail)
    a = np.concatenate(chunks) if chunks else np.empty((0, len(cols)))
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
    return {'available':True,'start_s':first/sr,'duration_s':count/sr,
            'sample_count':count,'sample_rate_hz':sr,'channels':fields,
            'points':chart,'bands':powers,
            'notice':'Ось построена по порядку отсчётов. Старые ts_unix могут идти назад; межпоточная синхронизация приблизительна.'}
