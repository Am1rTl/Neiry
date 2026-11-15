"""Regression checks for staged calibration and the Capsule recording streams."""
import ctypes
import json
import time
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import server
from capsule_calibration import bind_calibration_result, bind_staged_calibration
from recording import SessionRecorder


def nfb_result(reason=0):
    return SimpleNamespace(
        failReason=ctypes.c_int(reason), timestampMilli=1_700_000_000_000,
        individualFrequency=10, individualPeakFrequency=10.25,
        individualPeakFrequencyPower=3, individualPeakFrequencySuppression=2,
        individualBandwidth=6, individualNormalizedPower=0.5, lowerFrequency=7, upperFrequency=13,
    )


class GuidedCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.state = server.State()
        self.state._device_ready = True
        self.state.status = 'connected'
        self.state.calibration_phase = 'ready'
        self.state._calibration_stage_start = Mock()
        self.cal = Mock()
        self.cal.has_calibration_failed.return_value = False
        self.state._calibrator = self.cal
        self.contact = {'calibration_allowed': True, 'ready': True}
        directory = self.enterContext(TemporaryDirectory())
        self.rec = SessionRecorder(directory, channel_names=['O1-T3', 'O2-T4'])
        self.path = self.rec.start()
        self.addCleanup(self.rec.close)
        self.enterContext(patch.multiple(server, S=self.state, RECORDER=self.rec))
        self.enterContext(patch.object(server, '_contact_locked', return_value=self.contact))
        self.client = TestClient(server.app)

    def begin(self, mode):
        response = self.client.post('/api/device/calibration/start', json={'mode': mode})
        self.assertEqual(response.status_code, 200)
        return self.state._calibration_run

    def advance_past_cue(self, run):
        run.prepare_until = time.monotonic() - 1
        server._update_calibration([])

    def test_full_uses_sdk_completion_and_exports_eye_states(self):
        run = self.begin('full')
        self.assertEqual(self.client.post('/api/device/calibration/start').status_code, 409)
        self.assertEqual(self.client.post('/api/experiments/start', json={'protocol_id': 'door'}).status_code, 409)
        for i, eyes in enumerate(('closed', 'open', 'closed', 'open')):
            with self.subTest(stage=i):
                d = self.client.get('/api/device/status').json()['calibration_run']
                self.assertEqual((d['stage'], d['eye_state'], d['preparing']), (i+1, eyes, True))
                self.advance_past_cue(run)
                self.state._calibration_stage_start.assert_called_with(i)
                if i == 0:  # An early result must not complete a four-stage run.
                    server._on_calibration(self.cal, nfb_result())
                    self.assertFalse(self.state.calibrated)
                server._on_calibration_stage_finished(self.cal)
                server._update_calibration([])
        self.assertFalse(self.state.calibrated)  # Last stage event still needs a result.
        server._on_calibration(self.cal, nfb_result())
        self.assertTrue(self.state.calibrated)
        baseline = Mock()
        server._update_calibration([baseline])
        baseline.calibrate_baselines.assert_not_called()
        run.baselines_at = time.monotonic() - 1
        server._update_calibration([baseline])
        baseline.calibrate_baselines.assert_called_once()
        self.rec.close()
        events = [json.loads(line) for line in (self.path / 'events.jsonl').read_text().splitlines()]
        starts = [row for row in events if row['type'] == 'calibration_stage_start']
        self.assertEqual([row['eye_state'] for row in starts], ['closed', 'open', 'closed', 'open'])
        self.assertEqual({row['run_id'] for row in starts}, {run.run_id})
        self.assertEqual(sum(row['type'] == 'calibration_stage_end' for row in events), 4)
        result = json.loads((self.path / 'calibration.json').read_text())
        self.assertEqual((result['mode'], result['individualPeakFrequency']), ('full', 10.25))

    def test_quick_ignores_stale_result_and_surfaces_sdk_reason(self):
        run = self.begin('quick')
        self.advance_past_cue(run)
        self.cal.calibrate_quick.assert_called_once()
        server._on_calibration(object(), nfb_result())
        self.assertFalse(self.state.calibrated)
        server._on_calibration(self.cal, nfb_result(reason=1))
        d = self.client.get('/api/device/status').json()
        self.assertEqual(d['calibration_phase'], 'failed')
        self.assertIn('артефактов', d['message'])
        self.assertIsNone(d['calibration_result'])
        self.assertTrue(d['calibration_available'])

    def test_sdk_failure_already_set_does_not_abort_retry(self):
        self.cal.has_calibration_failed.return_value = True
        run = self.begin('quick')
        self.advance_past_cue(run)
        server._update_calibration([])
        self.assertEqual(self.state.calibration_phase, 'running')
        server._on_calibration(self.cal, nfb_result())
        self.assertTrue(self.state.calibrated)

    def test_contact_loss_discards_late_result_from_active_stage(self):
        run = self.begin('full')
        self.advance_past_cue(run)
        self.contact['calibration_allowed'] = False
        server._update_calibration([])
        self.contact['calibration_allowed'] = True
        server._on_calibration(self.cal, nfb_result())
        self.assertFalse(self.state.calibrated)
        self.assertTrue(self.state._calibration_reconnect)
        self.assertEqual(self.client.post('/api/device/calibration/start').status_code, 409)

    def test_raw_capture_records_sdk_batch_bounds_and_artifacts(self):
        eeg = Mock()
        eeg.get_samples_count.return_value = 2
        eeg.get_channels_count.return_value = 2
        eeg.get_raw_value.side_effect = lambda channel, sample: 10 * channel + sample
        eeg.get_timestamp.side_effect = [1_700_000_000_000, 1_700_000_000_004]
        server.on_eeg(None, eeg)

        class Error(ctypes.Structure):
            _fields_ = [('code', ctypes.c_int)]

        pointer = ctypes.pointer(ctypes.c_int(1))
        quality = Mock(return_value=0.92)
        lib = SimpleNamespace(
            clCDevice_SetOnEEGArtifactsEvent=Mock(),
            clCEEGArtifacts_GetChannelsCount=Mock(return_value=1),
            clCEEGArtifacts_GetTimestampMilli=Mock(return_value=1_700_000_000_004),
            clCEEGArtifacts_GetArtifactByChannel=Mock(return_value=4),
            clCEEGArtifacts_GetEEGQuality=quality,
        )
        device = SimpleNamespace(get_c_pointer=lambda: pointer)
        with patch.multiple(server, Error=Error, Error_Code=SimpleNamespace(OK=0)):
            callback = server._subscribe_eeg_artifacts(device, lib)
            lib.clCDevice_SetOnEEGArtifactsEvent.assert_called_once_with(pointer, callback)
            callback(pointer, pointer)
        self.assertIs(quality.restype, ctypes.c_float)
        self.rec.close()
        records = [json.loads(line) for line in (self.path / 'metrics.jsonl').read_text().splitlines()]
        batch, artifact = records
        self.assertEqual((batch['stream'], batch['sample_count'], batch['first_sample_index']),
                         ('eeg_batch_timing', 2, 0))
        self.assertEqual(batch['sdk_last_ms'] - batch['sdk_first_ms'], 4)
        self.assertEqual(artifact['channels'], [{'index': 0, 'flags': 4, 'quality': 0.92}])


class NativeCalibrationBindingTests(unittest.TestCase):
    def test_staged_binding_uses_three_argument_symbol(self):
        lib = SimpleNamespace(clCNFBCalibrator_CalibrateIndividualNFB=Mock(),
                              clCNFBCalibrator_SetOnCalibrationStageFinishedEvent=Mock())
        cal = Mock()
        cal.get_c_pointer.return_value = ctypes.pointer(ctypes.c_int(1))
        finish = Mock()
        check_error = Mock()

        class Error(ctypes.Structure):
            _fields_ = [('code', ctypes.c_int)]

        start = bind_staged_calibration(lib, cal, Error, check_error, finish)
        start(3)
        args = lib.clCNFBCalibrator_CalibrateIndividualNFB.call_args.args
        self.assertEqual((len(args), args[1]), (3, 3))
        check_error.assert_called_once()
        stage_callback = lib.clCNFBCalibrator_SetOnCalibrationStageFinishedEvent.call_args.args[1]
        stage_callback(cal.get_c_pointer())
        finish.assert_called_once_with(cal)

    def test_result_callback_retains_original_device_identity(self):
        class Result(ctypes.Structure):
            _fields_ = [('frequency', ctypes.c_float)]

        lib = SimpleNamespace(clCNFBCalibrator_SetOnCalibratedEvent=Mock())
        pointer = ctypes.pointer(ctypes.c_int(1))
        cal = SimpleNamespace(get_c_pointer=lambda: pointer)
        results = []
        callback = bind_calibration_result(lib, cal, Result, lambda c, d: results.append((c, d.frequency)))
        callback(pointer, None)
        callback(pointer, ctypes.pointer(Result(10.5)))
        self.assertEqual(results, [(cal, 10.5)])
