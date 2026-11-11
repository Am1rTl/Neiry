"""Low-latency *engineering* EEG checks, not medical quality certification.

Input data remain in SDK-native units (physical scale is not verified).
An inability to detect an artifact is never proof that the signal is clean.
"""
from __future__ import annotations

import math
from typing import Any
import numpy as np
from scipy.signal import welch, spectrogram


def _fraction(freq: np.ndarray, psd: np.ndarray, lo: float, hi: float,
              denom: tuple[float, float] = (1., 100.)) -> float | None:
    inside = (freq >= lo) & (freq < hi)
    total = (freq >= denom[0]) & (freq < denom[1])
    if np.count_nonzero(inside) < 2 or np.count_nonzero(total) < 2:
        return None
    power = np.trapezoid(psd[inside], freq[inside])
    all_power = np.trapezoid(psd[total], freq[total])
    return float(power / all_power) if all_power > 0 else None


def assess_eeg(samples: list[list[float]], sr: float, names: list[str] | None = None,
               max_s: float = 4.) -> dict[str, Any]:
    """Window-level line interference, flatline and clipping-like warnings.

    No numerical 'brain quality score'. Channel checks deliberately don't
    replace technician review or dry-electrode device specs.
    """
    if not 0 < sr <= 4096:
        raise ValueError('Invalid sample rate')
    required = max(64, int(sr * 2))
    if len(samples) < required:
        return {'available': False, 'reason': f'Меньше 2 секунд отсчётов ({len(samples)}/{required})',
                'channels': [], 'sample_rate_hz': sr}
    a = np.asarray(samples[-int(max_s*sr):], dtype=float)
    if a.ndim != 2 or a.shape[1] == 0:
        return {'available': False, 'reason': 'Нет каналов', 'channels': [], 'sample_rate_hz': sr}
    results=[]
    for i in range(min(a.shape[1], 16)):
        x=a[:,i]
        finite=x[np.isfinite(x)]
        title=names[i] if names and len(names)>i else f'Канал {i+1}'
        if len(finite) < int(len(x)*.95):
            results.append({'name':title, 'status':'unusable','reasons':['Большая доля нечисловых отсчётов']})
            continue
        if len(finite)<64:
            results.append({'name':title, 'status':'insufficient','reasons':['Недостаточно отсчётов']})
            continue
        signal=np.nan_to_num(x-np.nanmedian(x))
        std=float(np.std(signal))
        unique_count=int(len(np.unique(x)))
        reasons=[]
        if std <= 1e-12:
            reasons.append('Почти постоянный сигнал — проверьте контакт/поток')
        if unique_count <= 12:
            reasons.append(f'Всего {unique_count} различных уровней: возможна потеря точности')
        psd_freq, psd=welch(signal,fs=sr,nperseg=min(len(signal),int(sr*2)),detrend='linear')
        # Same relative denominator for 50Hz and 60Hz; don't apply if Nyquist < 80 Hz.
        interference = None
        if sr >= 200:
            values=[v for v in (_fraction(psd_freq,psd,48,52), _fraction(psd_freq,psd,58,62)) if v is not None]
            interference=max(values) if values else None
            if interference is not None and interference > .30:
                reasons.append(f'Сильная узкая помеха около 50/60 Гц ({interference:.0%} мощности 1–100 Гц)')
        # Percent of extrema is only a clipping-like *clue*; no ADC range is assumed.
        lo,hi=float(np.min(finite)),float(np.max(finite))
        edge=float(np.count_nonzero((x==lo)|(x==hi)) / len(x))
        if unique_count>2 and edge>.30:
            reasons.append('Много повторений крайних значений — проверьте ограничение диапазона')
        status = 'warning' if reasons else 'screened'
        results.append({'name':title,'status':status,'reasons':reasons,
                        'rms_sdk_units':round(float(np.sqrt(np.mean(signal**2))),7),
                        'unique_levels':unique_count,
                        'line_power_fraction':round(interference,4) if interference is not None else None,
                        'extrema_fraction':round(edge,4)})
    return {'available':True,'sample_rate_hz':sr,'window_seconds':len(a)/sr,
            'channels':results,'notice':'Инженерные эвристики; отсутствие предупреждения не подтверждает физиологическую чистоту.'}


def live_spectrogram(samples: list[list[float]], sr: float, names: list[str] | None = None,
                     seconds: float = 20., max_columns: int = 64) -> dict[str, Any]:
    """Log10 power tiles, 2–45 Hz; designed for a light canvas heatmap."""
    if not 0 < sr <= 4096:
        raise ValueError('Invalid sample rate')
    x=np.asarray(samples[-int(sr*seconds):],dtype=float)
    if x.ndim!=2 or x.shape[0]<max(128,int(sr*3)):
        return {'available':False,'reason':'Для спектрограммы нужно не менее трёх секунд ЭЭГ'}
    output=[]
    for i in range(min(x.shape[1],2)):
        row=x[:,i]
        if not np.isfinite(row).all():
            output.append({'name': names[i] if names and i<len(names) else f'Канал {i+1}', 'available':False})
            continue
        window=min(len(row),max(64,int(sr*2)))
        freqs,t,power=spectrogram(row,fs=sr,nperseg=window,noverlap=int(window*.6),
                                  detrend='linear',scaling='density')
        mask=(freqs>=2)&(freqs<=45)
        arr=np.log10(np.maximum(power[mask],1e-18))
        if arr.shape[1]>max_columns:
            # Max-over-time keeps brief high energy bursts visible at low resolution.
            edge=np.linspace(0,arr.shape[1],max_columns+1,dtype=int)
            arr=np.stack([np.max(arr[:,edge[j]:max(edge[j]+1,edge[j+1])],axis=1)
                          for j in range(max_columns)],axis=1)
            t=np.array([t[min(len(t)-1, edge[j])] for j in range(max_columns)])
        output.append({'name':names[i] if names and i<len(names) else f'Канал {i+1}',
                       'available':True,'freq_hz':freqs[mask].round(2).tolist(),
                       'time_s':t.round(2).tolist(),
                       'log10_psd':np.round(arr,3).tolist(),
                       'notice':'Логарифм мощности в неизвестных физических единицах; уровень сравним только внутри сигнала.'})
    return {'available':True,'channels':output}
