"""Reproducible, local-only exploratory analysis of Capsule recordings.

No diagnostic, emotion, intelligence, brain-region or source-localisation claims.
Rows stay in acquisition order: legacy ts_unix is reconstructed from callback
arrival times and must NOT be used to sort/resample the EEG waveform.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal

ANALYSIS_VERSION = "2.1.0"
SCHEMA_VERSION = "1.0"
BANDS = {"delta": (1., 4.), "theta": (4., 8.), "alpha": (8., 13.),
         "beta": (13., 30.), "gamma": (30., 45.)}
STREAMS = ("eeg", "ppg", "mems", "resistances")
ENVELOPE = {"ts_unix", "ts_iso", "stream", "t", "timestampMilli"}

@dataclass(frozen=True)
class AnalysisConfig:
    epoch_s: float = 4.
    hop_s: float = 2.
    robust_z: float = 6.
    minimum_power_ratio_outlier: float = 8.
    motion_robust_z: float = 6.
    line_dominance_fraction: float = .50
    minimum_spectral_windows: int = 15
    ppg_window_s: float = 30.
    minimum_prv_s: float = 60.
    impedance_warn_ohm: float = 500_000.
    impedance_bad_ohm: float = 1_000_000.


def sanitize(value: Any) -> Any:
    """Strict JSON, including legacy JSONL NaN/Infinity values."""
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [sanitize(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(float(value)) else None
    return value


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _number(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def stats(values: Any) -> dict:
    a = np.asarray(values, dtype=float).ravel()
    x = a[np.isfinite(a)]
    out = {"count": int(a.size), "finite_count": int(x.size),
           "nonfinite_count": int(a.size-x.size)}
    if x.size:
        out.update(dict(zip(("min", "p01", "p05", "median", "p95", "p99", "max"),
                            np.percentile(x, [0, 1, 5, 50, 95, 99, 100]))))
        out.update(mean=float(np.mean(x)), std=float(np.std(x)))
    else:
        out.update({k: None for k in ("min", "p01", "p05", "median", "p95", "p99", "max", "mean", "std")})
    return sanitize(out)


def _robust_upper(x: np.ndarray, multiplier: float = 6., min_ratio: float = 1.) -> float:
    x = np.asarray(x, float); x = x[np.isfinite(x)]
    if not len(x):
        return float("inf")
    med = float(np.median(x)); mad = float(np.median(np.abs(x-med)))
    return max(med + multiplier*1.4826*mad, med*min_ratio, np.finfo(float).tiny)


def read_stream(path: Path, rate: float = 0.) -> dict:
    empty = {"columns": [], "values": np.empty((0, 0)), "ts": np.empty(0),
             "index": np.empty(0), "malformed_rows": 0, "rate": rate,
             "timestamp_source": "missing"}
    try:
        if not path.exists() or not path.stat().st_size:
            return empty
        with path.open(newline="", encoding="utf-8-sig") as h:
            r = csv.DictReader(h)
            names = r.fieldnames or []
            cols = [k for k in names if k not in {"ts_unix", "ts_iso", "sample_index"}]
            rows, timestamps, indices = [], [], []
            bad = 0
            for i, row in enumerate(r):
                if None in row or any(row.get(k) is None for k in names):
                    bad += 1
                ts = _number(row.get("ts_unix"))
                if not np.isfinite(ts):
                    try:
                        ts = datetime.fromisoformat(row.get("ts_iso") or "").timestamp()
                    except ValueError:
                        pass
                timestamps.append(ts)
                indices.append(_number(row.get("sample_index", i)))
                rows.append([_number(row.get(k)) for k in cols])
        values = np.asarray(rows, dtype=float).reshape(-1, len(cols)) if cols else np.empty((len(rows), 0))
        return {"columns": cols, "values": values, "ts": np.asarray(timestamps),
                "index": np.asarray(indices), "malformed_rows": bad, "rate": rate,
                "timestamp_source": "ts_unix" if "ts_unix" in names else "ts_iso",
                "index_source": "sample_index" if "sample_index" in names else "row_order"}
    except OSError:
        return empty


def audit_stream(s: dict) -> dict:
    n = len(s["ts"]); fs = s["rate"]
    t = s["ts"]; ft = t[np.isfinite(t)]
    dt = np.diff(t); finite_dt = dt[np.isfinite(dt)]
    indices = s["index"]; idiff = np.diff(indices)
    wall = float(ft[-1]-ft[0]) if len(ft)>1 else 0.
    nominal = n/fs if fs>0 else None
    return sanitize({
        "sample_count": n, "sample_rate_hz": fs or None,
        "nominal_duration_s": nominal, "wall_span_s": wall,
        "first_timestamp": float(ft[0]) if len(ft) else None,
        "last_timestamp": float(ft[-1]) if len(ft) else None,
        "timestamp_nonfinite": int(n-len(ft)),
        "nonmonotonic_steps": int(np.sum(finite_dt <= 0)),
        "gaps_over_100ms": int(np.sum(finite_dt > .1)),
        "largest_forward_step_s": float(np.max(finite_dt)) if len(finite_dt) else None,
        "largest_backward_step_s": float(np.min(finite_dt)) if len(finite_dt) else None,
        "median_step_s": float(np.median(finite_dt)) if len(finite_dt) else None,
        "nominal_minus_wall_s": nominal-wall if nominal is not None else None,
        "index_discontinuities": int(np.sum(~np.isfinite(idiff) | (idiff != 1))),
        "index_source": s.get("index_source", "missing"),
        "malformed_rows": s["malformed_rows"], "columns": s["columns"],
        "note": "Частота и длительность по индексу; обратные шаги времени callback не являются доказательством потери отсчётов." if fs else "Нерегулярный поток событий; проценты ниже считаются по строкам, не по времени."})


def integrate_band(freq: np.ndarray, psd: np.ndarray, lo: float, hi: float) -> float:
    """Trapezoidal integration with interpolated edges; adjacent bands add up."""
    f = np.asarray(freq); p = np.asarray(psd)
    if not len(f) or lo < f[0] or hi > f[-1] or hi <= lo:
        return float("nan")
    inner = (f > lo) & (f < hi)
    xx = np.r_[lo, f[inner], hi]
    yy = np.r_[np.interp(lo, f, p), p[inner], np.interp(hi, f, p)]
    return float(np.trapezoid(yy, xx))


def band_metrics(freq: np.ndarray, power: np.ndarray) -> tuple[dict, dict]:
    absolute = {key: integrate_band(freq, power, *limits) for key, limits in BANDS.items()}
    total = sum(absolute.values())
    relative = {key: val/total if total>0 else None for key, val in absolute.items()}
    return sanitize(absolute), sanitize(relative)


def alpha_candidate(freq: np.ndarray, power: np.ndarray, usable_windows: int) -> dict:
    out = {"candidate_hz": None, "prominence_db": None, "status": "not_established",
           "description": "Устойчивый локальный альфа-пик не подтверждён; это не диагноз."}
    f=np.asarray(freq); p=np.asarray(power)
    if len(f)<5 or not np.all(np.isfinite(p)) or np.max(p)<=0:
        return out
    db=10*np.log10(np.maximum(p, np.finfo(float).tiny))
    inds, props=signal.find_peaks(db, prominence=3.)
    allowed=[(int(i), float(prom)) for i,prom in zip(inds, props["prominences"]) if 8.<f[i]<13.]
    if not allowed or usable_windows < 15:
        return out
    idx,prom=max(allowed,key=lambda pair:p[pair[0]])
    out.update(candidate_hz=float(f[idx]), prominence_db=prom, status="candidate",
               description="Локальный максимум 8–13 Гц в прошедших скрининг окнах; кандидат, не подтверждённая индивидуальная альфа-частота. Нужны повторяемость и проба глаза открыты/закрыты; апериодический фон отдельно не моделировался.")
    return out


def spectral_entropy(freq: np.ndarray, power: np.ndarray) -> float | None:
    mask=(freq>=1)&(freq<=45); p=power[mask]
    if len(p)<2 or np.sum(p)<=0 or not np.all(np.isfinite(p)):
        return None
    prob=p/np.sum(p); nz=prob>0
    return float(-np.sum(prob[nz]*np.log(prob[nz]))/np.log(len(p)))


def _excerpt(values: np.ndarray, fs: float, start: int = 0, seconds: float = 4.) -> dict:
    n=min(len(values)-start, max(1, int(fs*seconds)))
    return {"time_s": (np.arange(n)/fs+start/fs).tolist(),
            "values": sanitize(values[start:start+n]), "start_s": start/fs,
            "selection": "Первое прошедшее скрининг окно; если таких окон нет — начало записи. Исходные значения без сглаживания."}


def analyze_motion(s: dict, config: AnalysisConfig) -> dict:
    if len(s["ts"])<2 or s["values"].shape[1]<6 or s["rate"]<=0:
        return {"available": False, "summary": {}, "timeline": []}
    x=s["values"]; fs=s["rate"]; a=np.linalg.norm(x[:,:3],axis=1); g=np.linalg.norm(x[:,3:6],axis=1)
    n=max(1, int(config.epoch_s*fs)); hop=max(1, int(config.hop_s*fs))
    timeline=[]
    for i in range(0,len(x)-n+1,hop):
        aa=a[i:i+n]; gg=g[i:i+n]
        timeline.append({"time_s": (i+n/2)/fs,
                         "acc_rms": float(np.sqrt(np.mean((aa-np.nanmedian(aa))**2))),
                         "gyro_rms": float(np.sqrt(np.mean(gg**2)))})
    threshold=_robust_upper(np.array([p["gyro_rms"] for p in timeline]),config.motion_robust_z,3.)
    for p in timeline:
        p["moving"]=bool(p["gyro_rms"]>threshold)
    return sanitize({"available":True, "summary": {
        "acc_magnitude": stats(a), "gyro_magnitude": stats(g),
        "movement_threshold": threshold,
        "movement_windows_pct": 100*np.mean([p["moving"] for p in timeline]) if timeline else None,
        "units_note": "Единицы MEMS отсутствуют в meta.json; значения сохранены в шкале SDK. Порог движения — внутрисессионная эвристика, не физиологическая норма. 0% означает отсутствие относительных выбросов, а не неподвижность; постоянное движение может не выделяться."}, "timeline":timeline})


def _motion_at(times: np.ndarray, motion: dict, offset: float = 0.) -> np.ndarray:
    line=motion.get("timeline",[])
    if not line:
        return np.zeros(len(times))
    t=np.array([p["time_s"] for p in line]); v=np.array([p["gyro_rms"] for p in line])
    return np.interp(times+offset,t,v,left=np.nan,right=np.nan)


def analyze_eeg(s: dict, motion: dict, motion_offset: float, config: AnalysisConfig, unit_label: str | None = None) -> dict:
    fs=s["rate"]; data=s["values"]
    units=unit_label or "SDK (физические единицы не указаны)"
    if not len(data) or fs<=0:
        return {"available":False,"sample_rate_hz":fs or None,"units":units,"channels":[]}
    nper=int(round(config.epoch_s*fs)); hop=int(round(config.hop_s*fs))
    if nper<8 or hop<1:
        raise ValueError("Invalid sampling rate / epoch configuration")
    starts=np.arange(0,max(0,len(data)-nper+1),hop,dtype=int)
    times=(starts+nper/2)/fs
    mscore=_motion_at(times,motion,motion_offset)
    mthreshold=motion.get("summary",{}).get("movement_threshold",float("inf"))
    channels=[]
    for ch,name in enumerate(s["columns"]):
        x=data[:,ch]
        spectral=[]; rms=[]; diff_rms=[]; unique_levels=[]; base_reasons=[]
        for i in starts:
            a=x[i:i+nper]; reasons=[]
            index_slice=s["index"][max(0,int(i)-1):int(i)+nper]
            if np.any(~np.isfinite(index_slice)) or np.any(np.diff(index_slice)!=1):
                reasons.append("sample_index_discontinuity")
            unique_levels.append(int(len(np.unique(a[np.isfinite(a)]))))
            if not np.all(np.isfinite(a)):
                reasons.append("nonfinite")
                spectral.append(None);rms.append(float("nan"));diff_rms.append(float("nan"));base_reasons.append(reasons)
                continue
            b=signal.detrend(a,type="linear")
            rms.append(float(np.sqrt(np.mean(b*b))))
            diff_rms.append(float(np.sqrt(np.mean(np.diff(a)**2))))
            if np.ptp(a)==0:
                reasons.append("flatline")
            if unit_label is None and np.mean(np.isclose(np.abs(a),.8,rtol=0,atol=1e-10))>.001:
                reasons.append("repeated_rail_0p8")
            f, p=signal.periodogram(a,fs=fs,window="hann",detrend="linear",scaling="density",return_onesided=True)
            if fs/2>=100:
                power_1_100=integrate_band(f,p,1,100)
                power_48_52=integrate_band(f,p,48,52)
                if power_1_100>0 and power_48_52/power_1_100>config.line_dominance_fraction:
                    reasons.append("line_component_dominant")
            spectral.append(p);base_reasons.append(reasons)
        rr=np.asarray(rms); dd=np.asarray(diff_rms)
        rms_thresh=_robust_upper(rr,config.robust_z,config.minimum_power_ratio_outlier)
        diff_thresh=_robust_upper(dd,config.robust_z,config.minimum_power_ratio_outlier)
        accepted=[]; timeline=[]; reasons_count=Counter()
        for j,i in enumerate(starts):
            reasons=base_reasons[j][:]
            if rr[j]>rms_thresh:reasons.append("amplitude_outlier")
            if dd[j]>diff_thresh:reasons.append("derivative_outlier")
            if np.isfinite(mscore[j]) and mscore[j]>mthreshold:reasons.append("movement")
            usable=not reasons
            accepted.append(usable);reasons_count.update(reasons)
            p=spectral[j]
            bp,rel=band_metrics(f,p) if p is not None else ({},{})
            ap=alpha_candidate(f,p,15) if p is not None else {"candidate_hz":None}
            timeline.append({"time_s":float(times[j]),"usable":usable,"reasons":reasons,
                             "bands":rel,"alpha_peak_hz":ap["candidate_hz"],"rms":rr[j],"motion_score":mscore[j]})
        finite_spectra=[p for p in spectral if p is not None]
        screened=[p for p,ok in zip(spectral,accepted) if ok and p is not None]
        n_good=len(screened); total=len(starts)
        freq=np.fft.rfftfreq(nper,1/fs)
        raw=np.mean(finite_spectra,axis=0) if finite_spectra else np.full(len(freq),np.nan)
        clean=np.mean(screened,axis=0) if n_good>=config.minimum_spectral_windows else np.full(len(freq),np.nan)
        absraw,relraw=band_metrics(freq,raw)
        absclean,relclean=band_metrics(freq,clean)
        peak=alpha_candidate(freq,clean,n_good)
        adjacent_repeat_pct=100*float(np.mean(np.diff(x)==0)) if len(x)>1 else 0.
        median_levels=float(np.median(unique_levels)) if unique_levels else 0.
        coarse=(adjacent_repeat_pct>50. or median_levels<20.)
        reported_peak=peak["candidate_hz"]
        if coarse and reported_peak is not None:
            peak.update(status="unreliable_quantization", description="В спектре есть локальный максимум, но округление и малое число локальных амплитудных уровней делают физиологическую интерпретацию альфа-пика ненадёжной. Кандидат не публикуется как индивидуальная частота.")
            reported_peak=None
        if coarse:
            for point in timeline:
                point["candidate_alpha_peak_hz"]=point["alpha_peak_hz"]
                point["alpha_peak_hz"]=None
        freqmask=freq<=min(60,fs/2)
        rail_count=int(np.sum(np.isclose(np.abs(x),.8,rtol=0,atol=1e-10)))
        good_indices=[j for j,ok in enumerate(accepted) if ok]
        good_starts=[int(starts[j]) for j in good_indices]
        typical_start=int(starts[min(good_indices,key=lambda j:abs(rr[j]-np.median(rr[good_indices])))]) if good_indices else 0
        power_total=integrate_band(freq,clean,1,45) if n_good else None
        linepower=integrate_band(freq,raw,48,52) if fs/2>=52 else None
        clean_line=integrate_band(freq,clean,48,52) if fs/2>=52 and n_good>=config.minimum_spectral_windows else None
        clean_wide=integrate_band(freq,clean,1,min(100,fs/2)) if fs/2>1 and n_good>=config.minimum_spectral_windows else None
        widepower=integrate_band(freq,raw,1,min(100,fs/2)) if fs/2>1 else None
        severe_line=bool(widepower and widepower>0 and linepower is not None and linepower/widepower>.5)
        if severe_line:
            if peak["candidate_hz"] is not None:
                peak.update(status="unreliable_line_contamination",description="Локальный максимум зависит от отбора немногих окон на фоне доминирующей 50-Гц компоненты. Он сохранён только как свойство цифрового спектра, не как индивидуальная альфа-частота.")
            reported_peak=None
            for point in timeline:
                point["candidate_alpha_peak_hz"]=point.get("candidate_alpha_peak_hz",point["alpha_peak_hz"])
                point["alpha_peak_hz"]=None
        from advanced_metrics import summarize_advanced
        advanced=summarize_advanced(x,fs,starts,accepted,freq,clean,coarse,
                                   config.minimum_spectral_windows)
        chstat=stats(x)
        chstat.update(quantization_step_observed=_quantization(x), adjacent_repeat_pct=adjacent_repeat_pct,
                      unique_levels=int(len(np.unique(x[np.isfinite(x)]))), median_window_unique_levels=median_levels,
                      quantization_concern=coarse, rail_0p8_samples=rail_count,
                      rail_0p8_pct=100*rail_count/len(x),total_power_1_45=power_total)
        channels.append({"name":name,"sample_count":len(x),"stats":chstat,
             "quality":{"total_windows":total,"usable_windows":n_good,
                        "usable_pct":100*n_good/total if total else 0,
                        "rejected_reasons":dict(reasons_count),"status":"limited" if n_good>=config.minimum_spectral_windows else "insufficient",
                        "rms_threshold":rms_thresh,"derivative_threshold":diff_thresh,
                        "note":"Процент перекрывающихся окон, прошедших эвристики, не доля достоверной мозговой активности и не оценка здоровья."},
             "band_power":absclean,"relative_power":relclean,
             "band_power_raw":absraw,"relative_power_raw":relraw,
             "alpha_peak_hz":reported_peak,"alpha_peak":peak,
             "entropy":spectral_entropy(freq,clean),
             "advanced":advanced,
             "line_noise_fraction_48_52_of_1_100":linepower/widepower if widepower and widepower>0 else None,
             "line_noise_fraction_screened_48_52_of_1_100":clean_line/clean_wide if clean_wide and clean_wide>0 else None,
             "psd":{"freq_hz":freq[freqmask].tolist(),"power":clean[freqmask].tolist(),
                     "raw_power":raw[freqmask].tolist(),"units":(unit_label or "SDK")+"²/Hz"},
             "timeline":timeline,
             "spectral_interpretability":"severely_limited" if (coarse or (linepower/widepower if widepower and widepower>0 else 0)>.5 or not n_good) else "limited",
             "excerpt":dict(_excerpt(x,fs,typical_start,config.epoch_s), selection="Окно с наиболее близким к медиане RMS среди прошедших скрининг. Если таких нет — начало записи. Исходные значения без сглаживания.")})
    return sanitize({"available":True,"sample_rate_hz":fs,"units":units,"unit_source":"meta.units.eeg (declared, not independently calibrated)" if unit_label else "unverified", "band_power_units":(unit_label or "SDK")+"²", "channels":channels,
                     "band_definitions_hz":BANDS,"spectral_denominator":"1–45 Гц",
                     "spectral_interpretability":"severely_limited" if any(c.get("spectral_interpretability")=="severely_limited" for c in channels) else "limited",
                     "method":"Среднее односторонних Hann-периодограмм: 4 с, шаг 2 с, линейный detrend, density; Welch с 50% перекрытием после явного скрининга окон."})


def _quantization(x: np.ndarray) -> float | None:
    a=np.unique(x[np.isfinite(x)])
    d=np.diff(a); d=d[d>1e-12]
    return float(np.min(d)) if len(d) else None


def _interval_problems(intervals: np.ndarray) -> np.ndarray:
    """Flag suspicious IBI changes without deleting or repairing any intervals."""
    a=np.asarray(intervals,float)
    bad=(~np.isfinite(a)) | (a<=0)
    for i,value in enumerate(a):
        nearby=a[max(0,i-2):min(len(a),i+3)]
        median=float(np.median(nearby)) if len(nearby) else 0.
        if median<=0 or abs(value-median)>.20*median:bad[i]=True
    if len(a)>1:
        jumps=np.abs(np.diff(a))>.20*np.minimum(a[:-1],a[1:])
        bad[:-1] |= jumps;bad[1:] |= jumps
    return bad


def analyze_ppg(s: dict, motion: dict, motion_offset: float, config: AnalysisConfig) -> dict:
    null_prv={"rmssd_ms":None,"sdnn_ms":None,"pnn50_pct":None,"status":"unavailable",
              "duration_s":0.,"n_intervals":0,
              "description":"Нет непрерывного участка ≥60 с, прошедшего проверки пульсовой формы, интервалов и движения. PRV из PPG не равна ECG-HRV."}
    empty={"available":False,"pulse_rate_bpm":None,"prv":null_prv,
           "quality":{"status":"unavailable","accepted_windows":0,"total_windows":0,"usable_pct":0},
           "timeline":[],"excerpt":{"time_s":[],"values":[]}}
    if not len(s["values"]) or s["rate"]<=0:
        return empty
    x=s["values"][:,0]; fs=s["rate"]
    if fs<15:
        empty.update(available=True);empty["quality"]["status"]="sampling_rate_too_low";return empty
    n=int(config.ppg_window_s*fs); starts=np.arange(0,max(0,len(x)-n+1),n,dtype=int)
    filt=signal.butter(3,[.5,5],btype="bandpass",fs=fs,output="sos")
    timeline=[]; accepted_peaks=[]; accepted_starts=[]; prv_eligible=[]; interval_problem_fractions=[]
    mt=motion.get("summary",{}).get("movement_threshold",float("inf"))
    for start in starts:
        segment=x[start:start+n]; reasons=[]; hr=None; cv=None; periodicity=None; pk=np.empty(0,dtype=int)
        index_slice=s["index"][max(0,int(start)-1):int(start)+n]
        if np.any(~np.isfinite(index_slice)) or np.any(np.diff(index_slice)!=1):
            reasons.append("sample_index_discontinuity")
        problem_fraction=None; interval_problem_count=0
        if not np.all(np.isfinite(segment)):
            reasons.append("nonfinite")
        elif np.ptp(segment)==0:
            reasons.append("flatline")
        else:
            y=signal.sosfiltfilt(filt,segment)
            spread=float(np.std(y))
            if spread<=np.finfo(float).eps:
                reasons.append("flatline")
            else:
                best=None
                for sign in (1,-1):
                    peaks,_=signal.find_peaks(sign*y,distance=max(1,int(.33*fs)),prominence=.6*spread)
                    peaks=peaks[(peaks>=int(.5*fs))&(peaks<n-int(.5*fs))]
                    intervals=np.diff(peaks)/fs
                    valid=(intervals>=.333)&(intervals<=1.5)
                    frac=float(np.mean(valid)) if len(valid) else 0.
                    c=float(np.std(intervals)/np.mean(intervals)) if len(intervals)>2 and np.mean(intervals)>0 else 999.
                    score=frac-c
                    if best is None or score>best[0]:best=(score,peaks,intervals,frac,c)
                _,pk,ibi,frac,cv=best
                interval_problems=_interval_problems(ibi)
                interval_problem_count=int(np.sum(interval_problems))
                problem_fraction=float(np.mean(interval_problems)) if len(interval_problems) else None
                hr=60/float(np.median(ibi)) if len(ibi) else None
                if len(ibi)<15:reasons.append("too_few_pulses")
                if frac<.95:reasons.append("implausible_intervals")
                if cv>.20:reasons.append("irregular_or_noisy_intervals")
                # Normalised autocorrelation at the detected median IBI.
                yy=y-y.mean(); corr=signal.correlate(yy,yy,mode="full",method="fft")[len(yy)-1:]
                lag=int(round(np.median(ibi)*fs)) if len(ibi) else 0
                periodicity=float(corr[lag]/corr[0]) if lag and lag<len(corr) and corr[0]>0 else 0.
                if periodicity<.5:reasons.append("low_periodicity")
                # Big isolated transients can create apparently plausible pulses.
                if np.max(np.abs(y))>8*spread:reasons.append("large_transient")
        movement=_motion_at(np.linspace(start/fs,(start+n)/fs,15),motion,motion_offset)
        moving_fraction=float(np.mean(movement>mt)) if len(movement) else 0.
        if moving_fraction>.05:reasons.append("movement")
        good=not reasons
        eligible=good and problem_fraction is not None and problem_fraction==0.
        prv_eligible.append(eligible)
        interval_problem_fractions.append(problem_fraction)
        if good:
            accepted_peaks.append(pk+start);accepted_starts.append(int(start))
        else:
            accepted_peaks.append(None);accepted_starts.append(None)
        timeline.append({"time_s":float((start+n/2)/fs),"pulse_rate_bpm":hr if good else None,
                         "candidate_pulse_rate_bpm":hr,"usable":good,"reasons":reasons,
                         "regularity":periodicity,"interval_cv":cv,"movement_fraction":moving_fraction,
                         "prv_eligible":eligible,"prv_problem_intervals_pct":100*problem_fraction if problem_fraction is not None else None,
                         "prv_problem_intervals_count":interval_problem_count})
    accepted=[p for p in timeline if p["usable"]]
    # PRV uses only the longest uninterrupted run of fully accepted blocks;
    # inter-block gaps near filter edges are excluded, never interpolated.
    runs=[]; current=[]
    for j,p in enumerate(accepted_peaks):
        if p is not None and prv_eligible[j]: current.append(j)
        elif current: runs.append(current);current=[]
    if current:runs.append(current)
    longest=max(runs,key=len) if runs else []
    prv=dict(null_prv)
    duration=len(longest)*config.ppg_window_s
    if duration>=config.minimum_prv_s:
        intervals=[]; differences=[]
        for j in longest:
            ibi=np.diff(accepted_peaks[j])/fs
            intervals.extend(ibi)
            differences.extend(np.diff(ibi))
        if len(intervals)>=40 and len(differences):
            a=np.array(intervals);dif=np.array(differences)
            prv={"rmssd_ms":float(np.sqrt(np.mean(dif**2))*1000),
                 "sdnn_ms":float(np.std(a,ddof=1)*1000),"pnn50_pct":float(np.mean(np.abs(dif)>.05)*100),
                 "status":"exploratory","duration_s":duration,"n_intervals":len(a),
                 "block_start_s":float(longest[0]*config.ppg_window_s),"block_end_s":float((longest[-1]+1)*config.ppg_window_s),
                 "valid_interval_duration_s":float(np.sum(a)),"n_differences":len(dif),
                 "description":"Исследовательская PRV по самому длинному непрерывному блоку окон без выявленных проблем IBI. Интервалы через границы окон исключены; интервалы не исправлялись и не склеивались после удаления. Эктопия по PPG не верифицирована. Не клиническая ECG-HRV и не показатель стресса."}
    prv["quality_gate"]="Каждый IBI отличается от локальной медианы (окрестность ±2 интервала) и соседнего IBI не более чем на 20%; ни одного проблемного интервала в принятых окнах. Целые окна исключаются, не отдельные удары; ≥60 с подряд. Эвристика консервативна и может отвергать реальную физиологическую вариабельность."
    prv["selection_bias_note"]="Отбор по регулярности может предпочитать менее вариабельные участки. Эти значения не характеризуют всю запись и не служат физиологическим сравнением сессий."
    prv.setdefault("block_start_s",None);prv.setdefault("block_end_s",None)
    prv.setdefault("valid_interval_duration_s",0.);prv.setdefault("n_differences",0)
    prv["eligible_windows"]=int(sum(prv_eligible))
    prv["problem_intervals_pct_by_window"]=[None if q is None else 100*q for q in interval_problem_fractions]
    rate=float(np.median([p["pulse_rate_bpm"] for p in accepted])) if accepted else None
    first=next((i for i in accepted_starts if i is not None),0)
    return sanitize({"available":True,"sample_rate_hz":fs,"sample_count":len(x),
        "pulse_rate_bpm":rate,"pulse_rate_range_bpm":stats([p["pulse_rate_bpm"] for p in accepted]),
        "prv":prv,"quality":{"status":"exploratory" if accepted else "insufficient",
             "accepted_windows":len(accepted),"total_windows":len(timeline),
             "usable_pct":100*len(accepted)/len(timeline) if timeline else 0,
             "note":"Невалидированные эвристики; отсутствие флага не доказывает отсутствие артефакта."},
        "timeline":timeline,"excerpt":_excerpt(x,fs,first,8.),
        "method":"Пульс: медиана по принятым окнам величины 60/median(IBI). Bandpass 0.5–5 Гц; 30-секундные неперекрывающиеся окна; пики обеих полярностей, интервал 0.333–1.5 с, CV≤0.20, autocorrelation≥0.5, фильтр движения. PRV дополнительно исключает целые окна с >20% локальным/соседним изменением хотя бы одного IBI и требует ≥60 с подряд."})


def analyze_impedance(s: dict, origin: float, config: AnalysisConfig) -> dict:
    if not len(s["ts"]):return {"available":False,"channels":[]}
    channels=[]; inds=np.linspace(0,len(s["ts"])-1,min(240,len(s["ts"])),dtype=int)
    for i,name in enumerate(s["columns"]):
        x=s["values"][:,i]; finite=x[np.isfinite(x)]; st=stats(x)
        channels.append({"name":name,"median_ohm":st["median"],"p95_ohm":st["p95"],"max_ohm":st["max"],
                         "above_500k_pct":float(100*np.mean(finite>config.impedance_warn_ohm)) if len(finite) else None,
                         "above_1m_pct":float(100*np.mean(finite>config.impedance_bad_ohm)) if len(finite) else None,
                         "missing_pct":100*(len(x)-len(finite))/len(x),
                         "timeline":[{"time_s":float(s["ts"][j]-origin),"value_ohm":float(x[j])} for j in inds]})
    return sanitize({"available":True,"sample_count":len(s["ts"]),"channels":channels,
        "threshold_note":"500 кОм / 1 МОм взяты из исходной конфигурации приложения. Это проектные пороги для контакта, а не универсальные нормы EEG; проценты по конечным строкам измерений, не по длительности."})


def analyze_vendor(path: Path, events: list, origin: float) -> dict:
    streams=defaultdict(lambda:{"count":0,"keys":set(),"prev":None,"consecutive_duplicates":0,
        "fields":defaultdict(list),"flags":defaultdict(list),"timeline":[],"valid_heart_rates":[]})
    malformed=0
    try:
        with path.open(encoding="utf-8") as h:
            for line in h:
                try:r=json.loads(line)
                except (ValueError,TypeError):malformed+=1;continue
                if not isinstance(r,dict):malformed+=1;continue
                name=str(r.get("stream","unknown")); item=streams[name];item["count"]+=1
                payload={k:v for k,v in r.items() if k not in ENVELOPE}
                key=json.dumps(payload,sort_keys=True,allow_nan=True)
                item["keys"].add(key);item["consecutive_duplicates"]+=int(key==item["prev"]);item["prev"]=key
                numeric={}
                for k,v in payload.items():
                    if isinstance(v,bool):item["flags"][k].append(v)
                    elif isinstance(v,(int,float)):
                        item["fields"][k].append(v);numeric[k]=v
                if name=="cardio" and r.get("metricsAvailable") is True and not r.get("hasArtifacts") and not r.get("motionArtifacts") and r.get("skinContact") is True and _number(r.get("heartRate"))>0:
                    item["valid_heart_rates"].append(r["heartRate"])
                item["timeline"].append({"time_s":_number(r.get("ts_unix"))-origin,"values":numeric})
    except OSError:pass
    result=[]
    for name,item in streams.items():
        n=item["count"];line=item["timeline"]
        inds=np.linspace(0,len(line)-1,min(200,len(line)),dtype=int) if line else []
        result.append({"name":name,"count":n,"unique_payloads":len(item["keys"]),
            "duplicate_pct":100*(n-len(item["keys"]))/n if n else 0,
            "consecutive_duplicates":item["consecutive_duplicates"],
            "fields":{k:stats(v) for k,v in item["fields"].items()},
            "flags":{k:{"true_pct":100*float(np.mean(v)),"count":len(v)} for k,v in item["flags"].items()},
            "heart_rate_sdk_valid":stats(item["valid_heart_rates"]) if name=="cardio" else None,
            "timeline":[line[i] for i in inds]})
    calibration=[e for e in events if e.get("type")=="calibration"]
    return sanitize({"streams":result,"calibration":calibration,"malformed_rows":malformed,
         "note":"Показатели SDK — непрозрачные оценки алгоритмов, не измеренные эмоции или свойства личности. Повторения могут быть штатным переизданием состояний. Проценты и статистика по строкам, не независимым наблюдениям; экстремальные значения не переводятся в проценты."})


def _read_events(path: Path) -> list:
    out=[]
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:value=json.loads(line)
            except ValueError:continue
            if isinstance(value,dict):out.append(value)
    except OSError:pass
    return out


def _provenance(path: Path) -> dict:
    files=[]
    for p in sorted(path.iterdir()):
        if not p.is_file() or p.suffix.lower() not in {".csv",".json",".jsonl"}:continue
        digest=hashlib.sha256()
        with p.open("rb") as h:
            for block in iter(lambda:h.read(1024*1024),b""):digest.update(block)
        files.append({"name":p.name,"size_bytes":p.stat().st_size,"sha256":digest.hexdigest()})
    return {"files":files,"source_unchanged":True,
            "time_axis":"sample_index / nominal_sample_rate; no sorting, interpolation or timestamp-based resampling",
            "unit_policy":"Unknown units are never silently converted to volts or microvolts."}


def analyze_session(path: str | Path, config: AnalysisConfig | None = None) -> dict:
    """Read one local session, return strict-JSON-compatible evidence and caveats."""
    cfg=config or AnalysisConfig();p=Path(path)
    if not p.is_dir():raise FileNotFoundError(str(p))
    meta=_json(p/"meta.json");rates=meta.get("sample_rates") or {}
    # Defaults are deliberately NOT invented if metadata omits a rate.
    loaded={name:read_stream(p/f"{name}.csv", _number(rates.get(name,0))) for name in STREAMS}
    for name,s in loaded.items():
        if not np.isfinite(s["rate"]) or s["rate"]<0:s["rate"]=0.
    audit={name:audit_stream(s) for name,s in loaded.items()}
    firsts=[a["first_timestamp"] for a in audit.values() if a["first_timestamp"] is not None]
    lasts=[a["last_timestamp"] for a in audit.values() if a["last_timestamp"] is not None]
    origin=min(firsts) if firsts else 0.
    motion=analyze_motion(loaded["mems"],cfg)
    mems_start=audit["mems"]["first_timestamp"] or origin
    eeg_start=audit["eeg"]["first_timestamp"] or origin
    ppg_start=audit["ppg"]["first_timestamp"] or origin
    meta_units=meta.get("units") if isinstance(meta.get("units"),dict) else {}
    declared=meta_units.get("eeg")
    known_units={"V":"V","mV":"mV","uV":"µV","µV":"µV","μV":"µV"}
    unit_label=known_units.get(declared) if isinstance(declared,str) else None
    eeg=analyze_eeg(loaded["eeg"],motion,eeg_start-mems_start,cfg,unit_label)
    ppg=analyze_ppg(loaded["ppg"],motion,ppg_start-mems_start,cfg)
    impedance=analyze_impedance(loaded["resistances"],origin,cfg)
    events=_read_events(p/"events.jsonl")
    vendor=analyze_vendor(p/"metrics.jsonl",events,origin)
    flags=[];observations=[]
    def flag(code,severity,title,detail):flags.append({"code":code,"severity":severity,"title":title,"detail":detail})
    def obs(title,detail,confidence="descriptive",category="data"):observations.append({"title":title,"detail":detail,"confidence":confidence,"category":category})
    if not eeg["available"]:
        flag("no_eeg","critical","ЭЭГ отсутствует","eeg.csv пуст или не содержит доступного сигнала; выводы о ритмах мозга для этой сессии недоступны.")
        obs("В этой сессии нет записи ЭЭГ","Доступны только присутствующие периферические потоки; пульс и движение не заменяют измерение ЭЭГ.","high")
    else:
        if not unit_label:
            flag("unknown_eeg_units","warning","Физические единицы ЭЭГ не подтверждены","В meta.json отсутствуют распознаваемые физические единицы EEG. Интегральная мощность в исходных единицах²; плотность PSD в исходных единицах²/Гц. Перевода в мкВ нет.")
        else:
            obs("Единицы взяты из метаданных",f"{unit_label}: объявлено в meta.json, независимо не калибровано. Числа не пересчитывались в другие единицы.","metadata","data")
        is_legacy=("recording_format_version" not in meta and "format_version" not in meta)
        if is_legacy or meta.get("eeg_rounding_decimals")==4:
            flag("quantized_legacy","warning","Точность исходной записи ограничена","Legacy recording.py округлял значения ЭЭГ до четырёх знаков после запятой. Утраченные малые колебания восстановить нельзя.")
        count=audit["eeg"]["nonmonotonic_steps"]
        if count:
            flag("arrival_timestamp_jitter","warning","Время пакетов не монотонно",f"{count} обратных/нулевых шагов ts_unix. Использован порядок sample_index и заданная частота; пропуски/длительности пакетов по callback-времени не доказываются.")
        for c in eeg["channels"]:
            q=c["quality"]
            raw_line=c.get("line_noise_fraction_48_52_of_1_100")
            screened_line=c.get("line_noise_fraction_screened_48_52_of_1_100")
            if max(raw_line or 0,screened_line or 0)>.20:
                flag("line_component_"+c["name"],"warning",f"Сильная компонента около 50 Гц: {c['name']}",f"Доля мощности 48–52 Гц относительно 1–100 Гц: исходный спектр {100*(raw_line or 0):.1f}%, после скрининга {f'{100*screened_line:.1f}%' if screened_line is not None else 'недоступно (недостаточно окон)'}. Вероятна сетевая помеха; её происхождение спектр сам по себе не доказывает. Полосы 1–45 Гц исключают эту область, но это не подтверждает чистоту оставшегося сигнала.")
            if c["stats"].get("quantization_concern"):
                flag("coarse_quantization_"+c["name"],"warning",f"Мало локальных амплитудных уровней: {c['name']}",f"Одинаковые соседние отсчёты: {c['stats']['adjacent_repeat_pct']:.1f}%; медиана уникальных уровней в 4-секундном окне: {c['stats']['median_window_unique_levels']:.0f}. Спектр описывает сохранённую округлённую волну; точный индивидуальный альфа-пик не подтверждается.")
            obs(f"{c['name']}: скрининг сигнала",f"Прошли {q['usable_windows']} из {q['total_windows']} перекрывающихся окон ({q['usable_pct']:.1f}%). Это отбор по эвристикам, не подтверждение физиологической чистоты.","high","quality")
            if not unit_label and c["stats"]["rail_0p8_samples"]:
                flag("rail_"+c["name"],"warning",f"Повторяющиеся крайние значения: {c['name']}",f"{c['stats']['rail_0p8_samples']} отсчётов равны ±0.8; такие окна исключены как вероятное ограничение диапазона/артефакт.")
            if c["alpha_peak_hz"] is not None:
                obs(f"{c['name']}: кандидат альфа-пика",f"{c['alpha_peak_hz']:.2f} Гц в среднем спектре прошедших скрининг окон. Не индивидуальная норма и не оценка интеллекта; для подтверждения нужен размеченный протокол.","exploratory","spectral")
    for name in ("eeg","ppg","mems"):
        if audit[name]["index_discontinuities"]:
            flag("index_"+name,"warning",f"Разрыв индекса {name}",f"Непоследовательных переходов индекса: {audit[name]['index_discontinuities']}; предположение равномерной сетки требует проверки.")
    markers=[e for e in events if e.get("type") in {"marker","task","condition"}]
    if not markers:
        flag("no_condition_markers","info","Нет разметки условий","Нет отметок глаза открыты/закрыты, заданий или событий поведения; связь ритмов с состояниями и причинные эффекты не оцениваются.")
    if not (p/"summary.json").exists():
        flag("missing_session_summary","info","Нет итогового файла закрытия","summary.json отсутствует: окончание сессии определяется по доступным строкам, корректное закрытие записи не подтверждено.")
    if vendor["calibration"]:
        c=vendor["calibration"][-1]
        obs("Калибровка SDK сохранена в событиях",f"individualFrequency={c.get('individualFrequency')}, individualPeakFrequency={c.get('individualPeakFrequency')}. Это разные поля SDK; их нельзя автоматически приравнивать к пику нашего спектра.","high","vendor")
    for stream in vendor["streams"]:
        nonfinite=sum(v["nonfinite_count"] for v in stream["fields"].values())
        if nonfinite:
            flag("vendor_nonfinite_"+stream["name"],"warning",f"Недоступные значения SDK: {stream['name']}",f"{nonfinite} NaN/Infinity в числовых полях; сохранены как null, не заменены нулём.")
    if ppg["pulse_rate_bpm"] is not None:
        obs("Оценка пульса из PPG",f"Медиана принятых 30-секундных окон: {ppg['pulse_rate_bpm']:.1f} уд/мин; приняты {ppg['quality']['accepted_windows']} из {ppg['quality']['total_windows']}. Это пульсовая оценка, не ЭКГ и не диагноз.","exploratory","ppg")
    if ppg["prv"]["status"]=="unavailable":
        flag("prv_unavailable","info","PRV не рассчитана","Недостаточно подряд окон подходящего качества; приложение не выдаёт численную вариабельность из ненадёжных пиков.")
    quality_pct=float(np.mean([c["quality"]["usable_pct"] for c in eeg["channels"]])) if eeg["channels"] else None
    duration=audit["eeg"]["nominal_duration_s"] if eeg["available"] else max([a["nominal_duration_s"] or 0 for a in audit.values()],default=0)
    methods={
       "eeg_spectrum":"Односторонняя PSD scipy.signal.periodogram, periodic Hann, linear detrend; окна 4 с, шаг 2 с, среднее. Для агрегатного screened-спектра требуется ≥15 окон; иначе он и полосовые метрики null, без подстановки raw. SciPy правильно удваивает положительные частоты, кроме DC/Nyquist. Частотный шаг 0.25 Гц при 250 Гц.",
       "band_power":"Интеграл PSD с интерполяцией границ: δ 1–4, θ 4–8, α 8–13, β 13–30, low-γ 30–45 Гц. Относительная мощность нормирована на сумму этих полос (1–45 Гц); пределы условны.",
       "quality":"Отбор окон по NaN/Infinity, flatline, повторяющимся ±0.8 (legacy observed rail), экстремальному RMS/производной (max(median+6×1.4826×MAD,8×median)), выраженному движению (gyro RMS > max(median+6×1.4826×MAD,3×median)) и доминирующей компоненте 48–52 Гц (>50% мощности 1–100 Гц при Nyquist≥100 Гц). Последний порог — инженерная эвристика, не опубликованная клиническая норма. Пороговые эвристики не валидированы клинически; прошедшие окна всё ещё могут содержать артефакты.",
       "alpha":"Локальный максимум строго внутри 8–13 Гц, log-PSD prominence≥3 dB и ≥15 прошедших окон. Если >50% соседних отсчётов одинаковы или медиана уровней в окне <20, alpha_peak_hz скрыт, численный кандидат только в alpha_peak. Это эвристика ограничения квантования. Отдельное моделирование апериодического фона и реактивность не выполнены.",
       "entropy":"Shannon entropy распределения мощности 1–45 Гц, нормированная на log(число частотных отсчётов); зависит от параметров PSD, шума и фильтрации, не мера интеллекта.",
       "advanced":"Исследовательские Hjorth и линейный log-PSD наклон по отобранным окнам без сильного квантования; это НЕ параметрическая модель specparam и не оценка возбуждения/торможения мозга.",
       "ppg":ppg.get("method","Недоступно"),
       "timestamps":"Сигналы анализируются по порядку строк и номинальной частоте. Legacy callback ts_unix имеет обратные скачки. Начальные метки только грубо совмещают MEMS и EEG/PPG; аппаратная синхронизация неизвестна.",
       "comparisons":"Описательное сравнение записей; перекрывающиеся окна и строки SDK зависимы. P-values, популяционные нормы, диагнозы и причинные выводы не вычисляются.",
       "source_ids":["S03","S04","S05","S06","S08","S11","S14","S15","S16","S22"]}
    result={"schema_version":SCHEMA_VERSION,"analysis_version":ANALYSIS_VERSION,
       "generated_at":datetime.now(timezone.utc).isoformat(),
       "session":{"id":p.name,"metadata_session_id":meta.get("session_id"),"started_at":meta.get("started_at"),
           "device":meta.get("device","Unknown"),"channels":meta.get("channels",loaded["eeg"]["columns"]),
           "duration_s":duration,"wall_duration_s":max(lasts)-min(firsts) if firsts and lasts else 0,
           "streams":audit,"sample_rates":rates,"markers_count":len(markers),"events":sanitize(events)},
       "quality":{"status":"limited" if eeg["available"] else "no_eeg","spectral_interpretability":eeg.get("spectral_interpretability","unavailable"),"flags":flags,"usable_eeg_pct":quality_pct,
                  "usable_eeg_denominator":"Среднее по каналам процентов перекрывающихся 4-секундных окон с шагом 2 с. Это не процент чистого времени, не независимые наблюдения и не оценка здоровья."},
       "eeg":eeg,"ppg":ppg,"motion":motion,"impedance":impedance,"vendor":vendor,
       "observations":observations,
       "limitations":[
          ("Два биполярных отведения O1–T3 и O2–T4 смешивают вклад нескольких источников. Отдельно локализовать затылочную или височную кору, глубокие структуры и работу всех отделов мозга нельзя." if set(loaded["eeg"]["columns"])=={"O1-T3","O2-T4"} else "Пространственная локализация по этому анализу не выполняется: нужны проверенный монтаж, референсы, достаточное число каналов и модель проводимости."),
          ("Нет подтверждённых физических единиц EEG; " if not unit_label else "Физические единицы объявлены в metadata, независимо не калиброваны; ")+"внутренняя фильтрация, аппаратная синхронизация и непрерывные счётчики устройства требуют отдельной проверки.",
          "Протокол, состояние глаз, возраст, лекарства, сон, кофеин и выполняемые задания не заданы; стабильные особенности человека не отделяются от состояния и условий записи.",
          "Дельта/тета могут содержать дрейф и движения глаз, beta/gamma — мышечную активность. Автоматический скрининг не доказывает корковое происхождение оставшегося сигнала.",
          "Показатели внимания, релаксации, нагрузки, стресса, усталости и продуктивности SDK — модельные индексы без предоставленной независимой индивидуальной валидации.",
          "Нет оснований для диагноза, оценки IQ, личности, доминирования полушария, чтения мыслей, определения нейромедиаторов или лечения по этим записям.",
          "Сравнения между сессиями описательные: нет стандартизованных условий, повторных контрольных проб и независимой проверки качества."],
       "recommendations":[
          "Сохранять EEG без округления; добавить единицы, масштаб, монтаж, референсы, внутренние фильтры и версию SDK в meta.json.",
          "Сохранять device sample counter / hardware timestamp и arrival timestamp раздельно; не перестраивать ось времени по времени callback.",
          "Записывать 2–3 повторения: 2 минуты глаза открыты, 2 минуты закрыты, с маркерами и минимальным движением. Эффект закрывания глаз проверять внутри одного человека, без обещаний обязательного alpha-усиления.",
          "Добавить спокойный 5-минутный PPG-блок при стабильном контакте; для клинической HRV использовать валидированную ЭКГ и корректный протокол.",
          "Для связи EEG с задачей сохранять события, ответы, точность/время реакции и дневник сна/кофеина; сравнивать одинаковые условия.",
          "Для пространственных/клинических вопросов нужны многоканальная ЭЭГ, проверенный монтаж и специалист; нынешние два отведения не заменяют обследование."],
       "methods":methods,"configuration":asdict(cfg),"provenance":_provenance(p)}
    return sanitize(result)


def summary(report: dict) -> dict:
    return {"id":report["session"]["id"],"started_at":report["session"]["started_at"],
            "duration_s":report["session"]["duration_s"],"quality":report["quality"]["status"],
            "usable_eeg_pct":report["quality"]["usable_eeg_pct"],
            "eeg_available":report["eeg"]["available"],"ppg_available":report["ppg"]["available"],
            "pulse_rate_bpm":report["ppg"]["pulse_rate_bpm"],
            "channels":[{"name":c["name"],"alpha_peak_hz":c["alpha_peak_hz"],
                         "relative_power":c["relative_power"],"quality":c["quality"]} for c in report["eeg"]["channels"]],
            "analysis_version":report["analysis_version"]}


def main() -> int:
    parser=argparse.ArgumentParser(description="Локальный воспроизводимый анализ записей EEG/PPG/MEMS")
    parser.add_argument("session",nargs="?",help="Каталог одной сессии")
    parser.add_argument("--all",action="store_true",help="Проанализировать все session_* каталоги")
    parser.add_argument("--records",default=str(Path(__file__).parent/"records"))
    parser.add_argument("--output",default=str(Path(__file__).parent/"reports"))
    args=parser.parse_args();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    sessions=sorted(Path(args.records).glob("session_*")) if args.all else ([Path(args.session)] if args.session else [])
    if not sessions:parser.error("Укажите session или --all")
    summaries=[]
    for session in sessions:
        sid=session.resolve().name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",sid):
            parser.error("Недопустимое имя каталога сессии для файла отчёта")
        report=analyze_session(session)
        (out/f"{sid}.json").write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding="utf-8")
        summaries.append(summary(report));print(json.dumps(summaries[-1],ensure_ascii=False,allow_nan=False))
    if args.all:
        (out/"index.json").write_text(json.dumps({"analysis_version":ANALYSIS_VERSION,"sessions":summaries},ensure_ascii=False,allow_nan=False,indent=2),encoding="utf-8")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
