"""Packet delivery, true discontinuities and interrupted alpha-run diagnostics."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from experiment_lab_analysis import _alpha_summary, summarize_run
from experiment_timing import PacketCoverage, read_packet_coverage, window_timing_reasons
from recording import SessionRecorder
from test_experiment_lab_analysis import phase, RATE, UNIX_TIME


class ExperimentTimingTests(unittest.TestCase):
    def test_window_requires_bounded_delivery_phase_membership_and_sdk_continuity(self):
        index = np.arange(1000)
        time = index / RATE
        # Alternate batches arrive 40 ms apart from the nominal grid.
        time[25::50] -= .04
        complete = PacketCoverage(True, [0], [999])
        def reasons(t, i=index, coverage=complete):
            return window_timing_reasons(t, i, 0, 4, RATE, .025, coverage)
        self.assertEqual(reasons(time), [])
        self.assertIn("host_timestamp_gap", reasons(time, coverage=PacketCoverage()))
        self.assertIn("sdk_packet_gap", reasons(time, coverage=PacketCoverage(True,[0,501],[499,999])))
        self.assertIn("sample_index_gap", reasons(time, i=np.r_[index[:50],index[50:]+1]))
        delayed = time.copy()
        delayed[250:500] += .40
        self.assertIn("host_timestamp_gap", reasons(delayed))
        escaped = time.copy()
        escaped[3] = -.01
        self.assertIn("phase_boundary", reasons(escaped))

    def test_blocks_from_different_planned_pairs_are_not_paired(self):
        blocks = [{"index": i, "label": "eyes_closed" if i % 2 else "eyes_open",
                   "channels": [{"name": "O1", "alpha_power": 1.0}]} for i in (1, 2, 3)]
        pairs = _alpha_summary(blocks, ["O1"])["pairs"]
        self.assertEqual([(p["open_index"], p["closed_index"]) for p in pairs], [(2, 3)])

    def test_missing_metadata_duplicate_and_clock_steps(self):
        def packet(i, first_ms, duration=96):
            return dict(stream="eeg_batch_timing", first_sample_index=i, sample_count=25,
                        sdk_first_ms=first_ms, sdk_last_ms=first_ms + duration)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.jsonl"
            self.assertFalse(read_packet_coverage(path, RATE).present)
            for broken in [packet(25, 1500), packet(25, 1100, 40), packet(26, 1104),
                           packet(0, 1100), {"stream": "eeg_batch_timing"}]:
                path.write_text('\n'.join(map(json.dumps, [packet(0, 1000),broken,packet(50, 1200)])))
                ranges = read_packet_coverage(path, RATE)
                self.assertTrue(ranges.present)
                self.assertFalse(ranges.covers(0, 74))

    def test_realistic_delivery_and_interrupted_attempt_never_enter_primary_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = SessionRecorder(Path(tmp), channel_names=["O1-T3","O2-T4"],
                                       sample_rates={"eeg": RATE})
            directory = recorder.start()
            for i in range(0, 90 * RATE, 25):
                indices = i + np.arange(25)
                t = indices / RATE
                if i < 30 * RATE:
                    signal = np.sin(2*np.pi*84*t)
                else:
                    signal = (1 if i < 60*RATE else 2) * np.sin(2*np.pi*10*t)
                arrival = UNIX_TIME + t + (.035 if i % 50 else 0.)
                # Arbitrary epoch difference. Only SDK intervals are informative.
                ms = 50_000 + i * 4
                recorder.record_eeg(np.c_[signal,signal], arrival, sdk_bounds=(ms, ms+96))
            recorder.close()
            interrupted = phase("eyes_open", 0, 0, 30)
            interrupted[-1]["reason"] = "view_away"
            run = {"kind":"alpha", "mode":"live", "events":interrupted}
            result = summarize_run(run, directory)
            self.assertEqual(result["alpha"]["pairs"], [])
            first = result["alpha"]["interrupted_phases"][0]["channels"][0]
            self.assertNotIn("alpha_power", first)
            self.assertGreater(first["rejected"]["dominant_high_frequency_line"], 0)
            run["events"] += phase("eyes_open", 0, 30, 60) + phase("eyes_closed", 1, 60, 90)
            result = summarize_run(run, directory)
            self.assertGreater(result["quality"]["timestamp_gaps"], 0)
            for channel in result["alpha"]["channels"]:
                self.assertEqual(channel["paired_blocks"], 1)
                self.assertAlmostEqual(channel["closed_open_ratio"], 4, places=2)
