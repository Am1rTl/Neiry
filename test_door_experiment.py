"""Door protocol regression tests using the local API and a synthetic recording."""
from collections import Counter
import io
import json
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient

from experiments import DOOR_PHASES, DOOR_PROTOCOL, DOOR_TRIALS, ExperimentManager
from recording import SessionRecorder
import server


class TestDoorExperiment(unittest.TestCase):
    def setUp(self):
        workspace = self.enterContext(tempfile.TemporaryDirectory())
        self.recorder = SessionRecorder(workspace, channel_names=['L', 'R'],
                                        sample_rates={'eeg': 250})
        self.recorder.start()
        self.addCleanup(self.recorder.close)
        self.signal_ready = True
        # Move the clock explicitly in the test; use the real API and recorder.
        self.manager = ExperimentManager(lambda: server.RECORDER, tick_s=3600,
                                         door_signal_ready=lambda: self.signal_ready)
        self.enterContext(patch.multiple(server, RECORDS_DIR=workspace, RECORDER=self.recorder,
                                         EXPERIMENTS=self.manager, LAB_STORE=None))
        self.enterContext(patch.object(server, '_live_connected', return_value=True))
        self.addCleanup(self.manager.stop)
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def start(self):
        return self.client.post('/api/experiments/start', json={'protocol_id': DOOR_PROTOCOL})

    def present(self, status, **fields):
        body = {'run_id': status['run_id'], 'phase_index': status['phase_index'],
                'client_unix_ms': time.time() * 1000}
        return self.client.post('/api/experiments/door/presented', json=body | fields)

    def test_completed_run_exports_raw_eeg_and_balanced_labels(self):
        result = self.start()
        self.assertEqual(result.status_code, 200)
        first = result.json()
        self.assertEqual(first['total_trials'], DOOR_TRIALS)
        self.assertEqual(self.present(first, run_id='other').status_code, 409)
        self.assertEqual(self.present(first, client_unix_ms='invalid').status_code, 400)

        for index in range(DOOR_TRIALS * len(DOOR_PHASES)):
            status = self.manager.status()
            self.assertEqual(status['phase_index'], index)
            self.assertEqual(self.present(status).status_code, 200)
            if index == 0:
                self.assertEqual(self.present(status).status_code, 200)
            self.recorder.record_eeg([[index, -index]], [time.time()])
            with self.manager._lock:
                self.manager._next_phase()
        self.assertEqual(self.manager.status()['state'], 'completed')

        self.assertEqual(self.client.post('/api/recording/stop').status_code, 200)
        session = first['session_id']
        download = self.client.get(f'/api/records/{session}/download')
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            events = [json.loads(line) for line in archive.read(f'{session}/events.jsonl').splitlines()]
            self.assertEqual(len(archive.read(f'{session}/eeg.csv').splitlines()),
                             DOOR_TRIALS * len(DOOR_PHASES) + 1)
        markers = [event for event in events if event.get('run_id') == first['run_id']]
        self.assertEqual(markers[0]['eye_state'], 'open')
        self.assertEqual(markers[-1]['type'], 'protocol_complete')
        for start, shown, end in zip(markers[1:-1:3], markers[2:-1:3], markers[3:-1:3], strict=True):
            self.assertEqual([start['type'], shown['type'], end['type']],
                             ['protocol_phase_start', 'door_phase_presented', 'protocol_phase_end'])
            self.assertEqual(start['index'], shown['index'])
            self.assertEqual(start['label'], end['label'])
            self.assertIn('client_unix_ms', shown)
        tasks = [event for event in markers if event['type'] == 'door_phase_presented'
                 and event['label'] == 'task']
        self.assertEqual(Counter(event['condition'] for event in tasks), {'imagery': 10, 'rest': 10})
        self.assertEqual([event['trial'] for event in tasks], list(range(1, DOOR_TRIALS + 1)))

    def test_requires_signal_and_flushes_abort_on_recording_stop(self):
        self.signal_ready = False
        self.assertEqual(self.start().status_code, 409)
        self.signal_ready = True
        status = self.start().json()
        self.assertEqual(self.client.post('/api/experiments/stop?run_id=old').json()['state'],
                         'running')
        self.assertEqual(self.client.post('/api/recording/stop').status_code, 200)
        events = [json.loads(line) for line in
                  (self.recorder.session_dir / 'events.jsonl').read_text().splitlines()]
        aborted = events[-2]
        self.assertEqual(aborted['type'], 'protocol_aborted')
        self.assertEqual(aborted['reason'], 'recording_stopped')
        self.assertEqual(aborted['run_id'], status['run_id'])

    def test_stops_after_presentation_timeout_or_signal_loss(self):
        for reason in ('stimulus_missing', 'signal_lost'):
            with self.subTest(reason=reason):
                manager = ExperimentManager(lambda: self.recorder, tick_s=.005,
                                            door_signal_ready=lambda: self.signal_ready)
                self.addCleanup(manager.stop)
                self.signal_ready = True
                manager.start(DOOR_PROTOCOL)
                with manager._lock:
                    if reason == 'stimulus_missing':
                        manager._phase_started -= 2
                    else:
                        self.signal_ready = False
                self.assertTrue(manager._stop.wait(1))
                self.assertEqual(manager.status()['state'], 'aborted')
                self.assertEqual(manager.status()['reason'], reason)
