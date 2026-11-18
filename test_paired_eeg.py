"""Numerical guards for two-lead sensor coupling and honest unavailable states."""
from __future__ import annotations
import time
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from fastapi.testclient import TestClient
from _sdk_stubs import install
install()
from analysis_engine import analyze_session
from paired_eeg import analyze_pair
from recording import SessionRecorder
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

    def test_power_remains_available_when_too_few_epochs_for_connectivity(self):
        result = analyze_pair(pair(12), FS)
        self.assertFalse(result['available'])
        self.assertFalse(result['bands'])
        power = result['spectral_power']
        self.assertTrue(power['available'])
        self.assertEqual(power['status'], 'limited')
        self.assertGreater(power['bands']['alpha']['left_relative_power'], .9)
        self.assertEqual(power['channels'][0]['accepted_epochs'], 3)
        self.assertNotIn('coherence_msc', power['bands']['alpha'])

    def test_noisy_channel_does_not_hide_usable_other_channel(self):
        data = pair(40)
        t = np.arange(len(data)) / FS
        data[:, 0] += .5 * np.sin(2 * np.pi * 50 * t)
        result = analyze_pair(data, FS)
        self.assertFalse(result['available'])
        power = result['spectral_power']
        self.assertTrue(power['available'])
        self.assertIsNone(power['bands']['alpha']['left_relative_power'])
        self.assertGreater(power['bands']['alpha']['right_relative_power'], .9)
        self.assertEqual(power['channels'][0]['rejected_by']['line_noise'], 10)
        self.assertEqual(power['channels'][1]['accepted_epochs'], 10)

    def test_three_clean_windows_among_noise_can_show_limited_power(self):
        data = pair(40)
        t = np.arange(FS * 28) / FS
        data[:FS * 28] += .5 * np.sin(2 * np.pi * 50 * t)[:, None]
        result = analyze_pair(data, FS)
        self.assertFalse(result['available'])
        self.assertEqual(result['quality']['accepted_epochs'], 3)
        self.assertTrue(result['spectral_power']['available'])
        self.assertEqual(result['spectral_power']['status'], 'limited')
        self.assertGreater(result['spectral_power']['bands']['alpha']['left_relative_power'], .9)

    def test_raw_power_displays_contaminated_signal_without_publishing_connectivity(self):
        data = pair(40)
        t = np.arange(len(data)) / FS
        data += .5 * np.sin(2 * np.pi * 50 * t)[:, None]
        result = analyze_pair(data, FS)
        self.assertFalse(result['available'])
        self.assertFalse(result['spectral_power']['available'])
        raw = result['raw_spectral_power']
        self.assertTrue(raw['available'])
        self.assertEqual(raw['status'], 'raw')
        self.assertEqual(raw['channels'][0]['accepted_epochs'], 10)
        self.assertGreater(raw['bands']['alpha']['left_relative_power'], .9)
        self.assertNotIn('coherence_msc', raw['bands']['alpha'])
        flat = analyze_pair(np.zeros((FS * 40, 2)), FS)
        self.assertFalse(flat['raw_spectral_power']['available'])

    def test_invalid_windows_never_supply_descriptive_power(self):
        for data in [np.zeros((FS * 40, 2)), np.full((FS * 40, 2), np.nan), np.round(pair(40), 2)]:
            result = analyze_pair(data, FS)
            self.assertFalse(result['spectral_power']['available'])
            self.assertIsNone(result['spectral_power']['bands']['alpha']['left_relative_power'])
        result = analyze_pair(pair(40), FS, usable_window=[True] * 2 + [False] * 8)
        self.assertFalse(result['spectral_power']['available'])
        self.assertEqual(result['spectral_power']['channels'][0]['accepted_epochs'], 2)

    def test_swapped_montage_is_reordered_not_mislabeled(self):
        a=pair(44)
        baseline=analyze_pair(a,FS)
        swapped=analyze_pair(a[:,[1,0]],FS,names=['O2-T4','O1-T3'])
        self.assertTrue(swapped['available'])
        self.assertAlmostEqual(swapped['bands']['alpha']['relative_log_right_left'],
                               baseline['bands']['alpha']['relative_log_right_left'], places=5)
        self.assertFalse(analyze_pair(a,FS,names=['Fp1-Fp2','C3-C4'])['available'])

    def test_archive_report_screens_pair_windows_and_preserves_events(self):
        with TemporaryDirectory() as directory:
            recorder = SessionRecorder(directory, channel_names=["O1-T3", "O2-T4"],
                                       sample_rates={"eeg": FS})
            session = recorder.start()
            values = pair(44, phase=np.pi/3)
            recorder.record_eeg(values, time.time() + np.arange(len(values)) / FS)
            recorder.event("marker", checkpoint_id="screened_pair", label=None)
            recorder.event("checkpoint_label", checkpoint_id="screened_pair", label="Глаза закрыты")
            recorder.close()
            report = analyze_session(session)
        paired = report["paired_eeg"]
        self.assertTrue(paired["available"], paired.get("reason"))
        self.assertEqual(paired["quality"]["accepted_epochs"], 11)
        self.assertEqual(report["session"]["markers_count"], 1)
        self.assertIn("Глаза закрыты", [event.get("label") for event in report["session"]["events"]])

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

    def test_live_pair_uses_only_continuous_stable_contact_data(self):
        with server.STATE_LOCK:
            state = server.S
            state._device_ready = True
            state.status = 'connected'
            state.eeg_sample_rate = FS
            state.channel_names = ['O1-T3', 'O2-T4']
            state.eeg.extend(pair(40).tolist())
            state.eeg_t.extend(np.arange(40 * FS) / FS)
            state.metrics_updated_at['eeg'] = time.monotonic()
        contact = {'ready': True, 'stable_seconds': 40.0}
        client = TestClient(server.app)
        self.addCleanup(client.close)
        with (patch.object(server, 'REPLAY', None),
              patch.object(server, '_contact_locked', return_value=contact)):
            live = client.get('/api/live/paired').json()
            self.assertTrue(live['available'], live.get('reason'))
            self.assertEqual(live['source_mode'], 'live')
            self.assertEqual(live['quality']['accepted_epochs'], 10)
            contact['stable_seconds'] = 8.0
            settling = client.get('/api/live/paired').json()
            self.assertFalse(settling['available'])
            self.assertEqual(settling['status'], 'insufficient')

    def test_capsule_partial_state_packet_uses_available_aliases(self):
        server.on_emotions_states(None, SimpleNamespace(focus=float('nan'), attention=0.55, chill=0.42))
        self.assertEqual(server.S.latest_emotions['attention'], 0.55)
        self.assertEqual(server.S.latest_emotions['relaxation'], 0.42)
        self.assertNotIn('cognitiveLoad', server.S.latest_emotions)


if __name__=='__main__':unittest.main()
