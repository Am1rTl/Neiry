"""Replay tests on synthetic recordings, including invalid host time order."""
import csv
import itertools
import json
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from playback import iter_session_events, PlaybackController
import server


class TestStreamingReplay(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.p=Path(self.tmp.name)
        (self.p/'meta.json').write_text(json.dumps({'sample_rates':{'eeg':100,'ppg':50}}))
        with (self.p/'eeg.csv').open('w',newline='') as f:
            w=csv.writer(f);w.writerow(['ts_unix','ts_iso','sample_index','O1','O2'])
            for ix,t in enumerate([100.0,99.0,100.2,97.0,101.0]):w.writerow([t,'',ix,ix*10,ix*20])
        with (self.p/'ppg.csv').open('w',newline='') as f:
            w=csv.writer(f);w.writerow(['ts_unix','ts_iso','sample_index','value'])
            for ix in range(3):w.writerow([100+ix*.03,'',ix,ix])
    def tearDown(self):self.tmp.cleanup()

    def test_preserves_sample_index_despite_backwards_timestamps(self):
        samples=[(t,v[0]) for t,k,v in iter_session_events(self.p) if k=='eeg']
        self.assertEqual([v for t,v in samples],[0,10,20,30,40])
        self.assertEqual([round(t,2) for t,v in samples],[100,100.01,100.02,100.03,100.04])

    def test_streaming_and_pause_resume(self):
        class Fake:
            def __init__(self):
                for attr in ('eeg','eeg_t','ppg','ppg_t','mems_acc','mems_gyro','mems_t'):
                    setattr(self,attr,deque(maxlen=30))
            def now(self):return 0.0
        st=Fake(); r=PlaybackController(st,threading.RLock(),lambda:None)
        r.start(self.p,speed=1)
        time.sleep(.006)
        r.pause(); count=len(st.eeg)
        time.sleep(.07)
        self.assertLessEqual(len(st.eeg), count+1)
        r.resume(); time.sleep(.09)
        r.stop()
        self.assertEqual(list(st.eeg),[[0.0,0.0],[10.,20.],[20.,40.],[30.,60.],[40.,80.]])

    def test_seek_clears_metric_values_and_history_from_previous_position(self):
        state = server.State()
        controller = PlaybackController(state, threading.RLock(), lambda: None)
        with patch.object(controller, "_run"):
            controller.start(self.p)
            state.latest_cardio = {"heartRate": 80}
            state.cardio_hist.append(state.latest_cardio)
            state.latest_phy = {"concentration": 50}
            controller.seek(0.02)
        self.addCleanup(controller.stop)
        self.assertIsNone(state.latest_cardio)
        self.assertEqual(len(state.cardio_hist), 0)
        self.assertIsNone(state.latest_phy)

    def test_large_generator_is_lazy(self):
        generator=iter_session_events(self.p)
        self.assertFalse(isinstance(generator,list))
        self.assertEqual(len(list(generator)),8)


class TestArchivedCardio(unittest.TestCase):
    def setUp(self):
        self.state = server.State()
        self.report = {"t": 1.0, "heartRate": 79.5, "metricsAvailable": True, "skinContact": True,
                       "hasArtifacts": False, "motionArtifacts": False}
        self.replay = SimpleNamespace(generation=3, status=lambda: {"active": True})
        self.enterContext(patch.multiple(server, S=self.state, REPLAY=self.replay))

    def test_valid_saved_pulse_does_not_require_live_electrode_contact(self):
        self.state.latest_cardio = self.report
        snap = server._build_tick_snapshot()
        self.assertFalse(snap["connected"])
        self.assertTrue(snap["validity"]["cardio"])
        self.assertEqual(snap["cardio"]["heartRate"], 79.5)
        self.assertEqual(snap["sensor_status"]["cardio"]["state"], "ok")

    def test_rejected_saved_pulse_is_omitted_and_explained_to_live_and_legacy_clients(self):
        rejected = {**self.report, "skinContact": False, "t": 1.5}
        self.state.cardio_hist.extend((self.report, rejected))
        self.state.latest_cardio = rejected
        snap = server._build_tick_snapshot()
        self.assertFalse(snap["validity"]["cardio"])
        self.assertIsNone(snap["cardio"])
        self.assertIn("контакт", snap["sensor_status"]["cardio"]["reason"])
        self.assertEqual(snap["sensor_status"]["cardio"]["t"], 1.5)
        self.assertEqual(snap["cardio_history"], [self.report])

    def test_missing_quality_flags_do_not_confirm_archived_pulse(self):
        self.state.latest_cardio = {"t": 1.0, "heartRate": 82}
        snap = server._build_tick_snapshot()
        self.assertFalse(snap["validity"]["cardio"])
        self.assertIsNone(snap["cardio"])
        self.assertIn("флагов", snap["sensor_status"]["cardio"]["reason"])


if __name__=='__main__':unittest.main()
