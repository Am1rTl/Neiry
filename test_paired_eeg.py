"""Numerical guards for two-lead sensor coupling and honest unavailable states."""
from __future__ import annotations
import unittest
from unittest.mock import patch
import numpy as np
from fastapi.testclient import TestClient
from _sdk_stubs import install
install()
from paired_eeg import analyze_pair
import server

FS = 250

def pair(seconds=44,phase=0.,seed=17):
    rng = np.random.default_rng(seed)
    t = np.arange(int(FS*seconds))/FS
    left = .05*np.sin(2*np.pi*10*t) + .006*rng.normal(size=t.size)
    right = .06*np.sin(2*np.pi*10*t+phase) + .006*rng.normal(size=t.size)
    return np.column_stack([left,right])

class TestPairedScientificMetrics(unittest.TestCase):
    def test_synchronized_alpha_pair_measurable_without_brain_claim(self):
        r = analyze_pair(pair(44,phase=np.pi/3),FS)
        self.assertTrue(r['available'],r.get('reason'))
        self.assertEqual(r['quality']['accepted_epochs'],11)
        self.assertGreater(r['bands']['alpha']['coherence_msc'],.60)
        self.assertGreater(r['bands']['alpha']['left_relative_power'],.5)
        self.assertGreater(r['bands']['alpha']['right_relative_power'],.5)
        self.assertIn('не доказательство',r['notice'])
        self.assertNotIn('dominant_hemisphere',r)
        self.assertNotIn('source_location',r)

    def test_flatline_never_yields_perfect_connectivity(self):
        x=np.zeros((FS*44,2))
        r=analyze_pair(x,FS)
        self.assertFalse(r['available'])
        self.assertEqual(r['quality']['accepted_epochs'],0)
        self.assertEqual(r['quality']['rejected_by']['flatline'],11)

    def test_coarse_recording_is_suppressed(self):
        r=analyze_pair(np.round(pair(44),2),FS)
        self.assertFalse(r['available'])
        self.assertTrue(r['quality']['rejected_by'].get('quantized'))

    def test_single_channel_and_short_recording_unavailable(self):
        self.assertFalse(analyze_pair(pair(44)[:,:1],FS)['available'])
        self.assertFalse(analyze_pair(pair(8),FS)['available'])

    def test_quality_mask_requires_pairwise_accepted_windows(self):
        m=[True]*3+[False]*8
        r=analyze_pair(pair(44),FS,usable_window=m)
        self.assertFalse(r['available'])
        self.assertEqual(r['quality']['accepted_epochs'],3)

    def test_swapped_montage_is_reordered_not_mislabeled(self):
        a=pair(44)
        baseline=analyze_pair(a,FS)
        swapped=analyze_pair(a[:,[1,0]],FS,names=['O2-T4','O1-T3'])
        self.assertTrue(swapped['available'])
        self.assertAlmostEqual(swapped['bands']['alpha']['relative_log_right_left'],
                               baseline['bands']['alpha']['relative_log_right_left'], places=5)
        self.assertFalse(analyze_pair(a,FS,names=['Fp1-Fp2','C3-C4'])['available'])

    def test_large_phase_lag_is_not_anatomical_direction(self):
        r=analyze_pair(pair(50,phase=np.pi/2),FS)
        self.assertTrue(r['available'])
        self.assertGreater(r['bands']['alpha']['wpli'],.25)
        self.assertGreaterEqual(r['bands']['alpha']['imaginary_coherency_magnitude'],0)
        self.assertLessEqual(r['bands']['alpha']['wpli'],1)

class TestLivePairedAPI(unittest.TestCase):
    def setUp(self):
        self.old=server.S
        server.S=server.State()

    def tearDown(self):
        server.S=self.old

    def test_no_device_not_a_synthetic_brain_map(self):
        with patch.object(server,'REPLAY',None):
            response=TestClient(server.app).get('/api/live/paired')
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json()['available'])
        self.assertFalse(response.json()['bands'])

    def test_connected_without_wearing_does_not_show_pair(self):
        with server.STATE_LOCK:
            server.S._device_ready=True
            server.S.status='connected'
            server.S.eeg_sample_rate=FS
            server.S.channel_names=['O1-T3','O2-T4']
            server.S.eeg.extend(pair(30).tolist())
        with patch.object(server,'REPLAY',None):
            response=TestClient(server.app).get('/api/live/paired')
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json()['available'])
        self.assertEqual(response.json()['status'],'contact_required')

if __name__=='__main__':unittest.main()
