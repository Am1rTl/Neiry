"""Regression tests: no invented electrode readings and no off-head brain data."""
import time
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sdk_stubs import install
install()
from fastapi.testclient import TestClient
from contact_quality import ElectrodeContact, ContactOptions
import server


ROWS = [{'channel':'O1-T3','value':240_000.,'is_inf':False},
        {'channel':'O2-T4','value':320_000.,'is_inf':False}]


def settled(tracker, now):
    for delta in (7., 3., .1):
        tracker.add(ROWS, now=now-delta)


class TestContactTracker(unittest.TestCase):
    def setUp(self):
        self.tracker=ElectrodeContact(ContactOptions())
        self.now=time.monotonic()

    def test_connected_is_not_worn(self):
        self.assertEqual(self.tracker.snapshot(connected=True,now=self.now)['state'],'waiting')
        self.assertFalse(self.tracker.snapshot(connected=True,now=self.now)['calibration_allowed'])
        self.assertFalse(self.tracker.snapshot(connected=False,now=self.now)['ready'])

    def test_bad_then_good_for_six_seconds_and_multiple_real_updates(self):
        self.tracker.add([{**ROWS[0], 'value': float('inf')},ROWS[1]],now=self.now-7.)
        self.assertEqual(self.tracker.snapshot(connected=True,now=self.now-6.5)['state'],'poor')
        self.tracker.add(ROWS,now=self.now-6.0)
        self.assertFalse(self.tracker.snapshot(connected=True,now=self.now-5.99)['ready'])
        self.tracker.add(ROWS,now=self.now-3.)
        self.tracker.add(ROWS,now=self.now)
        self.assertTrue(self.tracker.snapshot(connected=True,now=self.now)['ready'])
        self.tracker.add([ROWS[0],{**ROWS[1],'value': 1_200_000}],now=self.now+1)
        self.assertFalse(self.tracker.snapshot(connected=True,now=self.now+1)['ready'])
        self.assertEqual(self.tracker.snapshot(connected=True,now=self.now+1)['state'],'poor')

    def test_old_samples_rejected_and_stale_blocks_calibration(self):
        settled(self.tracker,self.now)
        self.assertTrue(self.tracker.snapshot(connected=True,now=self.now)['ready'])
        info=self.tracker.snapshot(connected=True,now=self.now+5)
        self.assertEqual(info['state'],'stale')
        self.assertFalse(info['calibration_allowed'])
        self.tracker.add(ROWS,now=self.now-99)
        self.assertNotEqual(self.tracker.snapshot(connected=True,now=self.now)['state'],'ready')

    def test_new_reading_after_long_gap_cannot_resurrect_old_good_contact(self):
        settled(self.tracker, self.now)
        self.assertTrue(self.tracker.snapshot(connected=True, now=self.now)['ready'])
        resumed_at = self.now + self.tracker.options.stale_seconds + 1
        self.tracker.add(ROWS, now=resumed_at)
        snapshot = self.tracker.snapshot(connected=True, now=resumed_at)
        self.assertEqual(snapshot['state'], 'settling')
        self.assertFalse(snapshot['calibration_allowed'])
        self.assertEqual(snapshot['sample_count'], 1)

    def test_empty_nan_zero_infinite_channels_not_ready(self):
        for vals in ([],[ROWS[0]],
                     [{**ROWS[0],'value':0},ROWS[1]],
                     [{**ROWS[0],'value':float('nan')},ROWS[1]],
                     [{**ROWS[0],'value':float('inf')},ROWS[1]]):
            self.tracker.add(vals,now=self.now)
            self.assertFalse(self.tracker.snapshot(connected=True,now=self.now)['ready'])
            self.now+=1.

    def test_duplicate_channel_names_are_not_independent_electrodes(self):
        duplicated = [ROWS[0], {**ROWS[1], 'channel': ROWS[0]['channel']}]
        for delta in (7., 3., .1):
            self.tracker.add(duplicated, now=self.now-delta)
        state = self.tracker.snapshot(connected=True, now=self.now)
        self.assertEqual(state['state'], 'incomplete')
        self.assertFalse(state['calibration_allowed'])

    def test_converted_actual_kohm_value_and_distinct_channels(self):
        settled(self.tracker,self.now)
        data=self.tracker.snapshot(connected=True,now=self.now)
        self.assertEqual(data['unit'],'ohm')
        self.assertEqual(data['channels'][0]['kohm'],240)
        self.assertEqual(data['channels'][1]['kohm'],320)
        self.assertEqual(data['source'],'Capsule SDK resistance callback')
        self.tracker.reset()
        self.assertEqual(self.tracker.snapshot(connected=True,now=self.now)['state'],'waiting')


class TestLiveValidity(unittest.TestCase):
    def setUp(self):
        self.before=server.S
        server.S=server.State()
        with server.STATE_LOCK:
            server.S.status='connected';server.S._device_ready=True
            server.S.channel_names=['O1-T3','O2-T4']
            server.S.eeg_sample_rate=250
            server.S.eeg.extend([[1,2]]*250)
            server.S.eeg_t.extend(range(250))
            server.S.ppg.extend([4.]*100)
            server.S.ppg_sample_rate=100
            server.S.latest_cardio={'heartRate':87.0,'skinContact':True,'metricsAvailable':True,
                                    'hasArtifacts':False,'motionArtifacts':False,'t':server.S.now()}
            server.S.latest_emotions={'attention':99.0,'relaxation':71.0,'t':server.S.now()}
            server.S.calibrated=True
            for name in ('eeg','ppg','cardio','emotions'):
                server.S.metrics_updated_at[name]=time.monotonic()

    def tearDown(self):
        server.S=self.before

    def test_bad_contact_hides_scores_even_when_connected_and_callbacks_arrive(self):
        with server.STATE_LOCK:
            status=server._validity_locked()
            self.assertFalse(status['eeg'])
            self.assertFalse(status['cardio'])
            self.assertFalse(status['emotions'])
        snap=server._build_tick_snapshot()
        self.assertIsNone(snap['cardio'])
        self.assertIsNone(snap['emotions'])
        self.assertFalse(snap['contact']['ready'])
        with TestClient(server.app) as client:
            result=client.get('/api/device/status').json()
            self.assertFalse(result['contact']['ready'])
            self.assertFalse(result['calibration_available'])
            self.assertEqual(client.post('/api/device/calibration/start').status_code,409)

    def test_real_sdk_contact_enables_then_freshness_and_flag_gate(self):
        with server.STATE_LOCK:
            settled(server.S.contact_tracker,time.monotonic())
            v=server._validity_locked()
            self.assertTrue(v['eeg'])
            self.assertTrue(v['cardio'])
            self.assertTrue(v['emotions'])
            server.S.latest_cardio['skinContact']=False
            self.assertFalse(server._validity_locked()['cardio'])
            server.S.latest_cardio['skinContact']=True
            # SDK may publish psychophysiological estimates far less often than EEG.
            # A 20-second gap is normal; two minutes of silence is not a 0.1s dropout.
            server.S.metrics_updated_at['emotions']=time.monotonic()-20
            self.assertTrue(server._validity_locked()['emotions'])
            server.S.metrics_updated_at['emotions']=time.monotonic()-181
            self.assertFalse(server._validity_locked()['emotions'])

    def test_failed_contact_resets_after_disconnect(self):
        with server.STATE_LOCK:
            settled(server.S.contact_tracker,time.monotonic())
        server._clear_live_connection('disconnect','idle')
        self.assertFalse(server._device_status_payload()['contact']['ready'])
        self.assertIsNone(server._build_tick_snapshot()['emotions'])

    def test_new_live_contact_endpoint_returns_missing_not_synthetic(self):
        with TestClient(server.app) as client:
            result=client.get('/api/live/contact').json()
        self.assertIn(result['contact']['state'],('waiting','disconnected'))
        self.assertEqual(result['contact']['channels'],[])
        self.assertFalse(result['validity']['eeg'])


if __name__=='__main__':unittest.main()
