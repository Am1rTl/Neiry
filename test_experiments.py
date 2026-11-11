import threading
import time
import unittest
from experiments import ExperimentManager,validate_phases

class FakeRecorder:
    session_id='session-test'
    active=True
    def __init__(self):self.events=[]
    def event(self, kind,**k):self.events.append((kind,k))

class TestExperiments(unittest.TestCase):
    def test_markers_and_completion(self):
        rec=FakeRecorder();m=ExperimentManager(lambda:rec,tick_s=.005)
        s=m.start('custom',[{'label':'eyes_open','duration_s':.01}]) if False else None
        # duration validation is strict for production, accelerate the clock in fixture
        m.start('eyes',[{'label':'eyes_open','duration_s':2},{'label':'eyes_closed','duration_s':2}])
        with m._lock:m._phase_started-=3
        time.sleep(.03)
        self.assertEqual(m.status()['phase']['label'],'eyes_closed')
        with m._lock:m._phase_started-=3
        time.sleep(.03)
        self.assertEqual(m.status()['state'],'completed')
        names=[name for name,_ in rec.events]
        self.assertIn('protocol_complete',names)
        self.assertEqual(names.count('protocol_phase_start'),2)
        self.assertEqual(names.count('protocol_phase_end'),2)

    def test_requires_recording_and_bounded_phases(self):
        rec=FakeRecorder();rec.active=False
        m=ExperimentManager(lambda:rec)
        with self.assertRaises(RuntimeError):m.start('eyes')
        for value in ([],[{'label':'x','duration_s':float('nan')}],[{'label':'a','duration_s':-5}]):
            with self.assertRaises(ValueError):validate_phases(value)

    def test_stops_on_recording_close(self):
        rec=FakeRecorder();m=ExperimentManager(lambda:rec,tick_s=.005)
        m.start('rest',[{'label':'rest','duration_s':2}]);rec.active=False
        time.sleep(.03)
        self.assertEqual(m.status()['state'],'aborted')

if __name__=='__main__':unittest.main()
