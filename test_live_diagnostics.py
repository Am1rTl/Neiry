"""Regression coverage for demand-driven diagnostics and shared result caching."""

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class LiveDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.state = server.State()
        self.state._device_ready = True
        self.state.eeg_sample_rate = 250
        self.state.channel_names = ["O1-T3"]
        self.state.eeg.extend([[1.0]] * 1500)
        self.state.eeg_t.extend(i / 250 for i in range(1500))
        self.state.stream_counts["eeg"] = 1500
        self.enterContext(patch.multiple(server, S=self.state, REPLAY=None, _DIAGNOSTICS_CACHE={}))
        self.quality = self.enterContext(patch("live_quality.assess_eeg", return_value={"available": True, "channels": []}))
        self.spectrogram = self.enterContext(patch("live_quality.live_spectrogram", return_value={"available": True, "channels": []}))

    def test_monitor_reconnect_receives_requested_history_then_only_new_samples(self):
        default = server._WSDelta(lean=True)
        returning = server._WSDelta(lean=True, history_s=15)
        snap = server._build_tick_snapshot((default, returning))
        first = server._build_client_tick(snap, default)["eeg_stream"]
        restored = server._build_client_tick(snap, returning)["eeg_stream"]
        self.assertEqual((first["n"], restored["n"]), (1250, 1500))
        self.assertEqual(restored["sample_index"], 0)
        self.state.eeg.append([2.0])
        self.state.eeg_t.append(6.0)
        self.state.stream_counts["eeg"] += 1
        tick = server._build_client_tick(server._build_tick_snapshot((returning,)), returning)
        self.assertEqual(tick["eeg_stream"]["samples"], [2.0])

    def test_combined_endpoint_only_computes_requested_work(self):
        client = TestClient(server.app)
        result = client.get("/api/live/diagnostics?quality=false&spectrogram=false").json()
        self.assertEqual(result, {"quality": None, "spectrogram": None})
        self.quality.assert_not_called()
        self.spectrogram.assert_not_called()

        quality_only = client.get("/api/live/diagnostics?quality=true&spectrogram=false").json()
        self.assertIsNone(quality_only["spectrogram"])
        self.assertTrue(quality_only["quality"]["available"])
        self.assertEqual(len(self.quality.call_args.args[0]), 1000)
        self.spectrogram.assert_not_called()

        result = client.get("/api/live/diagnostics").json()
        self.assertEqual(result["quality"], quality_only["quality"])
        self.assertEqual(len(self.spectrogram.call_args.args[0]), 1500)
        self.assertEqual(client.get("/api/live/quality").json(), result["quality"])
        self.assertEqual(client.get("/api/live/spectrogram").json(), result["spectrogram"])
        self.quality.assert_called_once()
        self.spectrogram.assert_called_once()

    def test_cache_keeps_paused_data_and_limits_live_recalculation(self):
        server._live_diagnostics(True, True, 20.0)
        for entry in server._DIAGNOSTICS_CACHE.values():
            entry.computed_at -= 10.0
        server._live_diagnostics(True, True, 20.0)
        self.quality.assert_called_once()
        self.spectrogram.assert_called_once()

        self.state.eeg.append([2.0])
        self.state.eeg_t.append(6.1)
        self.state.stream_counts["eeg"] += 1
        server._live_diagnostics(True, True, 20.0)
        self.assertEqual(self.quality.call_count, 2)
        self.assertEqual(self.spectrogram.call_count, 2)

        self.state.eeg.append([3.0])
        self.state.eeg_t.append(6.2)
        self.state.stream_counts["eeg"] += 1
        server._live_diagnostics(True, True, 20.0)
        self.assertEqual(self.quality.call_count, 2)
        self.assertEqual(self.spectrogram.call_count, 2)

    def test_two_tabs_share_the_same_inflight_computation(self):
        ready = threading.Barrier(2)

        def slow_result(*args):
            time.sleep(0.03)
            return {"available": True, "channels": []}

        def request():
            ready.wait()
            return server._live_diagnostics(True, False, 20.0)["quality"]

        self.quality.side_effect = slow_result
        with ThreadPoolExecutor(max_workers=2) as tabs:
            first = tabs.submit(request)
            second = tabs.submit(request)
            self.assertEqual(first.result(timeout=2), second.result(timeout=2))
        self.quality.assert_called_once()

    def test_cache_invalidates_after_replay_seek_even_with_same_window_shape(self):
        self.state._device_ready = False
        playback = SimpleNamespace(generation=1)
        with patch.object(server, "REPLAY", playback):
            server._live_diagnostics(True, True, 20.0)
            playback.generation += 1
            server._live_diagnostics(True, True, 20.0)
        self.assertEqual(self.quality.call_count, 2)
        self.assertEqual(self.spectrogram.call_count, 2)
