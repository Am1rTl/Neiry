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
from playback import iter_session_events, PlaybackController


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

    def test_large_generator_is_lazy(self):
        generator=iter_session_events(self.p)
        self.assertFalse(isinstance(generator,list))
        self.assertEqual(len(list(generator)),8)


if __name__=='__main__':unittest.main()
