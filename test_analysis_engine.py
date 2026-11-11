"""Numerical and failure-mode checks; no device or external service required."""
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy import signal

from analysis_engine import (AnalysisConfig, analyze_eeg, analyze_motion,
                             analyze_ppg, analyze_session, audit_stream,
                             band_metrics, integrate_band, sanitize)


def stream(x, fs, names=None):
    x=np.asarray(x,float)
    if x.ndim==1:x=x[:,None]
    n=len(x)
    return {"columns":names or ["O1-T3"]*x.shape[1],"values":x,
            "ts":np.arange(n)/fs,"index":np.arange(n),"rate":fs,
            "malformed_rows":0,"index_source":"sample_index"}


class AnalysisNumerics(unittest.TestCase):
    def test_one_sided_power_and_band_partition(self):
        fs=250; t=np.arange(fs*40)/fs
        y=2*np.sin(2*np.pi*10*t)
        f,p=signal.welch(y,fs=fs,window='hann',nperseg=1000,noverlap=500,detrend='linear')
        self.assertAlmostEqual(integrate_band(f,p,1,45),2.,places=4)
        absolute,relative=band_metrics(f,p)
        self.assertAlmostEqual(sum(relative.values()),1.)
        self.assertGreater(relative['alpha'],.999)
        self.assertAlmostEqual(sum(absolute.values()),integrate_band(f,p,1,45),places=12)

    def test_engine_matches_welch_for_clean_signal(self):
        fs=250; t=np.arange(fs*40)/fs
        y=np.sin(2*np.pi*10*t)
        result=analyze_eeg(stream(y,fs),{},0,AnalysisConfig())['channels'][0]
        self.assertEqual(result['quality']['usable_windows'],19)
        self.assertAlmostEqual(result['alpha_peak_hz'],10.)
        self.assertGreater(result['relative_power']['alpha'],.999)
        f,p=signal.welch(y,fs=fs,window='hann',nperseg=1000,noverlap=500,detrend='linear')
        self.assertTrue(np.allclose(result['psd']['power'],p[f<=60],atol=1e-12))

    def test_dominant_50hz_never_falls_back_to_raw_as_clean(self):
        fs=250;t=np.arange(fs*40)/fs
        y=10*np.sin(2*np.pi*50*t)+.1*np.sin(2*np.pi*10*t)
        r=analyze_eeg(stream(y,fs),{},0,AnalysisConfig())['channels'][0]
        self.assertEqual(r['quality']['usable_windows'],0)
        self.assertGreater(r['line_noise_fraction_48_52_of_1_100'],.95)
        self.assertTrue(all(v is None for v in r['psd']['power']))
        self.assertTrue(any(v is not None for v in r['psd']['raw_power']))
        self.assertIsNone(r['alpha_peak_hz'])
        self.assertTrue(all(v is None for v in r['relative_power'].values()))

    def test_nonfinite_windows_rejected_not_interpolated(self):
        fs=250;t=np.arange(fs*20)/fs;y=np.sin(2*np.pi*10*t);y[1000:1500]=np.nan
        result=analyze_eeg(stream(y,fs),{},0,AnalysisConfig())['channels'][0]
        self.assertGreater(result['quality']['rejected_reasons']['nonfinite'],0)
        self.assertLess(result['quality']['usable_windows'],result['quality']['total_windows'])
        json.dumps(result,allow_nan=False)

    def test_flatline_is_not_healthy_brain_signal(self):
        result=analyze_eeg(stream(np.zeros(10000),250),{},0,AnalysisConfig())['channels'][0]
        self.assertEqual(result['quality']['usable_windows'],0)
        self.assertIsNone(result['alpha_peak_hz'])
        self.assertTrue(all(v is None for v in result['relative_power'].values()))

    def test_too_short_has_no_spectral_conclusion(self):
        result=analyze_eeg(stream(np.arange(100)/100,250),{},0,AnalysisConfig())['channels'][0]
        self.assertEqual(result['quality']['total_windows'],0)
        self.assertIsNone(result['alpha_peak_hz'])
        json.dumps(result,allow_nan=False)

    def test_motion_rejects_contaminated_windows(self):
        fs=250;t=np.arange(fs*40)/fs;y=np.sin(2*np.pi*10*t)
        motion={'summary':{'movement_threshold':5},'timeline':[
            {'time_s':0,'gyro_rms':0},{'time_s':10,'gyro_rms':0},
            {'time_s':12,'gyro_rms':100},{'time_s':20,'gyro_rms':100},
            {'time_s':22,'gyro_rms':0},{'time_s':40,'gyro_rms':0}]}
        result=analyze_eeg(stream(y,fs),motion,0,AnalysisConfig())['channels'][0]
        self.assertGreater(result['quality']['rejected_reasons']['movement'],0)
        self.assertLess(result['quality']['usable_windows'],19)

    def test_timestamps_not_sorted_or_claimed_as_loss(self):
        s=stream(np.arange(100),250);s['ts'][50:]-=.1
        audit=audit_stream(s)
        self.assertEqual(audit['nonmonotonic_steps'],1)
        self.assertEqual(audit['index_discontinuities'],0)
        self.assertEqual(audit['nominal_duration_s'],.4)

    def test_constant_ppg_no_hr_or_prv(self):
        r=analyze_ppg(stream(np.ones(9000),100,['value']),{},0,AnalysisConfig())
        self.assertIsNone(r['pulse_rate_bpm']);self.assertIsNone(r['prv']['rmssd_ms'])
        self.assertEqual(r['quality']['accepted_windows'],0)

    def test_clean_periodic_ppg_pulse_and_prv(self):
        fs=100;t=np.arange(fs*120)/fs
        p=np.sin(2*np.pi*1.2*t)+.15*np.sin(2*np.pi*2.4*t)
        r=analyze_ppg(stream(p,fs,['value']),{},0,AnalysisConfig())
        self.assertAlmostEqual(r['pulse_rate_bpm'],72,delta=1)
        self.assertEqual(r['prv']['status'],'exploratory')
        self.assertLess(r['prv']['rmssd_ms'],15)

    def test_known_nonzero_prv_and_corrupted_pulses(self):
        fs=100;t=np.arange(fs*120)/fs
        intervals=.8+.04*np.sin(2*np.pi*np.arange(180)/10)
        beats=np.cumsum(intervals);beats=beats[(beats>.5)&(beats<119.5)]
        waveform=lambda b:np.sum(np.exp(-((t[:,None]-b[None,:])/.06)**2),axis=1)
        clean=analyze_ppg(stream(waveform(beats),fs,['value']),{},0,AnalysisConfig())
        expected=float(np.sqrt(np.mean(np.diff(intervals)**2))*1000)
        self.assertEqual(clean['prv']['status'],'exploratory')
        self.assertAlmostEqual(clean['prv']['rmssd_ms'],expected,delta=2.)
        missing=np.delete(beats,np.arange(8,len(beats),12))
        extra=np.sort(np.r_[beats,beats[np.arange(8,len(beats),12)]+.4])
        for damaged in (missing,extra):
            bad=analyze_ppg(stream(waveform(damaged),fs,['value']),{},0,AnalysisConfig())
            self.assertEqual(bad['prv']['status'],'unavailable')
            self.assertIsNone(bad['prv']['rmssd_ms'])

    def test_index_gap_rejects_crossing_eeg_and_ppg_windows(self):
        fs=250;t=np.arange(fs*40)/fs;y=np.sin(2*np.pi*10*t)
        s=stream(y,fs);s['index'][2000:]+=100
        r=analyze_eeg(s,{},0,AnalysisConfig())['channels'][0]
        self.assertGreater(r['quality']['rejected_reasons']['sample_index_discontinuity'],0)
        fs=100;t=np.arange(fs*90)/fs;s=stream(np.sin(2*np.pi*1.2*t),fs,['value'])
        s['index'][3000:]+=100
        p=analyze_ppg(s,{},0,AnalysisConfig())
        self.assertIn('sample_index_discontinuity',p['timeline'][1]['reasons'])
        self.assertEqual(p['prv']['status'],'unavailable')

    def test_all_nan_eeg_safe(self):
        r=analyze_eeg(stream(np.full(10000,np.nan),250),{},0,AnalysisConfig())
        self.assertEqual(r['channels'][0]['quality']['usable_windows'],0)
        json.dumps(r,allow_nan=False)

    def test_new_metadata_not_falsely_flagged_legacy_rounding(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'meta.json').write_text(json.dumps({'session_id':'new','recording_format_version':2,
                'eeg_rounding_decimals':None,'sample_rates':{'eeg':250}}))
            with (p/'eeg.csv').open('w',newline='') as h:
                writer=csv.writer(h);writer.writerow(['ts_unix','sample_index','O1-T3'])
                for i in range(2000):writer.writerow([i/250,i,math.sin(2*math.pi*10*i/250)])
            report=analyze_session(p)
            codes=[q['code'] for q in report['quality']['flags']]
            self.assertNotIn('quantized_legacy',codes)

    def test_missing_eeg_and_bad_vendor_numbers_safe(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'meta.json').write_text(json.dumps({'session_id':'empty','sample_rates':{'eeg':250,'ppg':100,'mems':250}}))
            (p/'eeg.csv').write_text('')
            (p/'metrics.jsonl').write_text('{"stream":"productivity","currentValue":NaN,"ts_unix":1}\n')
            result=analyze_session(p)
            self.assertFalse(result['eeg']['available']);self.assertEqual(result['quality']['status'],'no_eeg')
            self.assertIsNone(result['vendor']['streams'][0]['fields']['currentValue']['mean'])
            json.dumps(result,allow_nan=False)
            self.assertEqual(len(result['provenance']['files']),3)

    def test_sanitize_nonfinite(self):
        self.assertEqual(sanitize({'a':float('nan'),'b':np.array([1,np.inf])}),{'a':None,'b':[1.,None]})

if __name__=='__main__':unittest.main()
