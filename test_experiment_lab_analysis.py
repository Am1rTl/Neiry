"""Protocol-level regressions using deterministic data instead of a headset."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from experiment_lab_analysis import summarize_run
from recording import SessionRecorder


UNIX_TIME = 1_700_000_000
RATE = 250


def phase(label, index, start_s, end_s):
    return [
        {"type": event, "label": label, "index": index, "at_ms": (UNIX_TIME + seconds) * 1000}
        for event, seconds in (("phase_start", start_s), ("phase_end", end_s))
    ]


class ExperimentAnalysisTests(unittest.TestCase):
    def test_alpha_pairs_real_channel_names_and_rejects_gap_and_contact(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = SessionRecorder(Path(temporary), channel_names=["O1-T3", "O2-T4", "T3"],
                                       sample_rates={"eeg": RATE})
            recording = recorder.start()
            seconds = np.arange(60 * RATE) / RATE
            is_closed = seconds >= 30
            amplitude = np.where(is_closed, 2.0, 1.0)
            input_ = amplitude * np.cos(2 * np.pi * 10 * seconds)
            # A stream interruption intersects an otherwise usable open-eye window.
            keep = np.abs(seconds - 10) > 0.04
            recorder.record_eeg(np.c_[input_[keep], 0.5 * input_[keep], input_[keep]], UNIX_TIME + seconds[keep])
            # SDK resistance recorded once during a window.
            recorder.record_resistances({"O1-T3": 1_100_000, "O2-T4": 500_000}, UNIX_TIME + 22)
            recorder.close()

            analysis = summarize_run({
                "kind": "alpha", "mode": "live", "status": "completed",
                "events": phase("eyes_open", 0, 0, 30) + phase("eyes_closed", 1, 30, 60),
            }, recording)
            self.assertEqual(analysis["quality"]["channels"], ["O1-T3", "O2-T4"])
            self.assertEqual(analysis["quality"]["bad_contact_checks"], 1)
            # SDK-facing sample_index is assigned by recording, so loss cannot
            # be inferred merely by a missing arrival at the input boundary.
            self.assertEqual(analysis["quality"]["sample_index_gaps"], 0)
            self.assertEqual(analysis["quality"]["timestamp_gaps"], 1)
            open_lead = analysis["alpha"]["phases"][0]["channels"][0]
            self.assertGreaterEqual(open_lead["rejected"]["poor_contact"], 1)
            self.assertEqual(open_lead["rejected"]["host_timestamp_gap"], 1)
            for lead in analysis["alpha"]["channels"]:
                self.assertEqual(lead["paired_blocks"], 1)
                self.assertAlmostEqual(lead["closed_open_ratio"], 4, delta=.02)
            json.dumps(analysis, allow_nan=False)

    def test_visual_trial_validity_and_no_unobserved_threshold(self):
        events = []
        for angle, responses in ((1, [True, False, True, False]), (3, [True, True, True, True])):
            events.extend({
                "type": "trial", "condition": "trained", "angle_deg": angle, "valid": True,
                "correct": correct, "rt_ms": 450 if correct else 1300,
            } for correct in responses)
        events.extend({"type": "trial", "condition": "control", "angle_deg": angle,
                       "valid": True, "correct": True, "rt_ms": 300} for angle in (1, 3) for _ in range(4))
        events.append({"type": "trial", "condition": "trained", "angle_deg": 1,
                       "valid": False, "correct": True, "rt_ms": 10})
        result = summarize_run({"kind": "visual", "mode": "demo", "stage": "baseline", "events": events}, None)
        trained = result["visual"]["conditions"]["trained"]
        self.assertEqual(trained["trials"], 9)
        self.assertEqual(trained["valid_trials"], 8)
        self.assertEqual(trained["median_correct_rt_ms"], 450)
        self.assertEqual(trained["threshold_75_deg"], 2)
        self.assertIsNone(result["visual"]["conditions"]["control"]["threshold_75_deg"])
        self.assertEqual(result["quality"]["source"], "demo")

    def test_live_feedback_uses_event_clock_for_eeg_window(self):
        import server

        now = time.time()
        state = server.State()
        state.channel_names = ["O1-T3", "T3"]
        state.eeg_sample_rate = RATE
        state.now = lambda: 10.0
        state.eeg.extend([[1.0, 2.0]] * (2 * RATE))
        state.eeg_t.extend(9.9 - offset / RATE for offset in reversed(range(2 * RATE)))

        with (patch.object(server, "S", state),
              patch.object(server, "_validity_locked", return_value={"eeg": True}),
              patch.object(server.time, "time", return_value=now)):
            window = server._experiment_eeg_window()

        self.assertTrue(window["ready"])
        self.assertEqual(window["channels"], ["O1-T3"])
        self.assertAlmostEqual(window["window_end_ms"], (now - 0.1) * 1000)

    def test_feedback_compares_measured_alpha_and_same_band_only(self):
        data = [
            {"alpha": 2, "alpha_fixed": 8, "band": [8, 13], "alpha_valid": True,
             "value": .9, "valid": True, "baseline_alpha": 1.5, "sample_at_ms": 1000},
            {"alpha": 4, "band": [8, 13], "alpha_valid": True, "value": None, "valid": False},
            {"alpha": 100, "alpha_fixed": 16, "band": [9, 12], "alpha_valid": True, "value": .2, "valid": True},
            {"alpha": 999, "band": [8, 13], "alpha_valid": False, "value": .1, "valid": True},
            {"alpha": 999, "band": [8, 13], "alpha_valid": True, "label": "calibration"},
            {"alpha": 999, "alpha_fixed": 999, "band": [8, 13], "alpha_valid": True, "sample_at_ms": 1000},
        ]
        result = summarize_run({
            "kind": "feedback", "mode": "live", "stage": "control",
            "events": [{"type": "feedback", **e} for e in data],
        }, None)
        feedback = result["feedback"]
        self.assertEqual(feedback["valid_samples"], 2)
        self.assertEqual(feedback["displayed_samples"], 3)
        self.assertEqual(feedback["median_alpha"], 3)
        self.assertEqual(feedback["median_alpha_fixed"], 12)
        self.assertEqual(feedback["fixed_valid_samples"], 2)
        self.assertEqual(feedback["baseline_ratio"], 2)


if __name__ == "__main__":
    unittest.main()
