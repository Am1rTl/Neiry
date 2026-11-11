"""Regressions discovered in the supplied archive; no device is required."""
import csv
import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np
from analytics import compute_psd
from json_utils import finite_json
from recording import SessionRecorder


class RecordingIntegrity(unittest.TestCase):
    def test_preserves_micro_amplitude_details(self):
        with tempfile.TemporaryDirectory() as folder:
            r = SessionRecorder(folder, channel_names=["O1-T3", "O2-T4"], sample_rates={"eeg": 250})
            directory = r.start()
            values = [[0.0100001234567, -0.000003456789]]
            r.record_eeg(values, [1700000000.0])
            r.close()
            with (directory / "eeg.csv").open() as f:
                row = next(csv.DictReader(f))
            self.assertEqual(float(row["O1-T3"]), values[0][0])
            self.assertEqual(float(row["O2-T4"]), values[0][1])
            self.assertIsNone(json.loads((directory / "meta.json").read_text())["eeg_rounding_decimals"])

    def test_missing_channel_is_not_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            r = SessionRecorder(folder, channel_names=["left", "right"])
            d = r.start(); r.record_eeg([[1.0]], [1700000000.0]); r.close()
            with (d / "eeg.csv").open() as f:
                row = next(csv.DictReader(f))
            self.assertTrue(math.isnan(float(row["right"])))

    def test_sdk_nan_becomes_null_not_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            r = SessionRecorder(folder); d = r.start()
            r.record_metric("productivity", {"currentValue": float("nan"), "score": 105})
            r.close()
            row = json.loads((d / "metrics.jsonl").read_text())
            self.assertIsNone(row["currentValue"])
            self.assertEqual(row["score"], 105)

    def test_sanitizer_does_not_mutate_live_state(self):
        original = {"nested": [float("nan"), float("inf"), 4.0]}
        safe = finite_json(original)
        self.assertEqual(safe["nested"], [None, None, 4.0])
        self.assertTrue(math.isnan(original["nested"][0]))
        json.dumps(safe, allow_nan=False)


class SpectralNormalization(unittest.TestCase):
    def test_one_sided_psd_integrates_to_variance_even_and_odd(self):
        for n in (1000, 1001):
            x = np.sin(2*np.pi*10*np.arange(n)/250.0) + .3*np.cos(2*np.pi*31*np.arange(n)/250.0)
            data = compute_psd(x[:, None], 250, 125)
            power = np.sum(data["psd_per_channel"][0]) * 250/n
            self.assertAlmostEqual(power, float(np.var(x)), places=10)

    def test_nan_rejected_instead_of_bad_json(self):
        with self.assertRaises(ValueError):
            compute_psd([[1.0], [float("nan")]], 250)


if __name__ == "__main__":
    unittest.main()
