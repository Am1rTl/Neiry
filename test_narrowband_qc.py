"""Synthetic frequency controls for the archive measurement-chain diagnostic."""
import unittest

import numpy as np

from analysis_engine import AnalysisConfig, analyze_eeg
from test_analysis_engine import stream


class NarrowbandQC(unittest.TestCase):
    @staticmethod
    def measure(x, fs=250):
        return analyze_eeg(stream(x, fs), {}, 0, AnalysisConfig())["channels"][0]

    def test_sampling_third_is_visible_even_when_50_hz_rule_passes(self):
        fs = 250
        t = np.arange(fs * 40) / fs
        y = 4 * np.sin(2 * np.pi * (fs / 3) * t) + np.cos(2 * np.pi * 10 * t)
        ch = self.measure(y)
        qc = ch["narrowband_qc"]
        self.assertEqual(qc["eligible_windows"], 19)
        self.assertEqual(qc["screened_target_dominant_windows"], 19)
        self.assertGreater(qc["median_target_fraction"], 0.9)
        self.assertGreater(ch["relative_power"]["alpha"], 0.99)

    def test_two_lines_can_dominate_jointly(self):
        t = np.arange(10000) / 250
        y = sum(np.sin(2 * np.pi * f * t) for f in (10, 50, 250 / 3))
        qc = self.measure(y)["narrowband_qc"]
        self.assertEqual(qc["target_dominant_windows"], 0)
        self.assertEqual(qc["screened_combined_dominant_windows"], 19)

    def test_missing_or_insufficient_measurements_do_not_mean_zero_interference(self):
        t = np.arange(10000) / 250
        y = np.sin(2 * np.pi * 10 * t)
        y[1000:1500] = np.nan
        qc = self.measure(y)["narrowband_qc"]
        self.assertLess(qc["eligible_windows"], 19)
        self.assertEqual(qc["target_dominant_windows"], 0)
        self.assertIsNone(self.measure(np.full(1000, 0.37))["narrowband_qc"]["median_target_fraction"])
        self.assertFalse(self.measure(y, 100)["narrowband_qc"]["supported"])

    def test_low_rate_line_still_limits_interpretation(self):
        t = np.arange(128 * 40) / 128
        y = 8 * np.sin(2 * np.pi * 50 * t) + np.sin(2 * np.pi * 10 * t)
        ch = self.measure(y, 128)
        self.assertEqual(ch["spectral_interpretability"], "severely_limited")
        self.assertIsNone(ch["alpha_peak_hz"])
        self.assertIsNone(ch["line_noise_fraction_48_52_of_1_100"])
