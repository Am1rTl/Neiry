"""Тесты аналитики Capsule WebApp. Запуск: .venv/bin/python -m unittest test_analytics -v"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analytics import compute_hrv, compute_psd  # noqa: E402


def _sine(freq: float, sr: float, dur_s: float, amp: float = 1.0,
          noise_amp: float = 0.0) -> list[float]:
    n = int(sr * dur_s)
    return [amp * math.sin(2 * math.pi * freq * i / sr)
            + noise_amp * math.sin(2 * math.pi * 47 * i / sr)
            for i in range(n)]


def _pulse_train(bpm: float, sr: float, dur_s: float, width_s: float = 0.08) -> list[float]:
    period = 60.0 / bpm
    n = int(sr * dur_s)
    return [math.exp(-(((i / sr) % period) / width_s) ** 2) for i in range(n)]


class TestPSD(unittest.TestCase):
    def test_finds_sine_peak(self):
        sr = 250.0
        ch0 = _sine(10.0, sr, 4.0, noise_amp=0.05)
        samples = [[v, 0.0] for v in ch0]
        spec = compute_psd(samples, sr, 50.0)
        self.assertEqual(len(spec["psd_per_channel"]), 2)
        self.assertEqual(len(spec["freqs"]), len(spec["psd_per_channel"][0]))
        peak = spec["freqs"][max(range(len(spec["freqs"])),
                                 key=lambda i: spec["psd_per_channel"][0][i])]
        self.assertAlmostEqual(peak, 10.0, delta=1.0)
        self.assertLessEqual(max(spec["freqs"]), 50.0)

    def test_empty_input_raises(self):
        with self.assertRaises(ValueError):
            compute_psd([], 250.0, 50.0)
        with self.assertRaises(ValueError):
            compute_psd([[], []], 250.0, 50.0)

    def test_bad_params_raise(self):
        with self.assertRaises(ValueError):
            compute_psd([[1.0]], 0, 50.0)
        with self.assertRaises(ValueError):
            compute_psd([[1.0]], 250.0, 0)


class TestHRV(unittest.TestCase):
    def test_pulse_60bpm(self):
        sr = 100.0
        hrv = compute_hrv(_pulse_train(60.0, sr, 12.0), sr)
        self.assertEqual(hrv["n_beats"], 12)
        assert hrv["mean_hr"] is not None
        self.assertAlmostEqual(hrv["mean_hr"], 60.0, delta=5.0)
        assert hrv["rmssd_ms"] is not None
        self.assertLess(hrv["rmssd_ms"], 50.0)  # идеально ровный пульс

    def test_empty_input(self):
        hrv = compute_hrv([], 100.0)
        self.assertEqual(hrv["n_beats"], 0)
        self.assertIsNone(hrv["mean_hr"])
        self.assertIsNone(hrv["rmssd_ms"])
        self.assertIsNone(hrv["sdnn_ms"])

    def test_flatline_has_no_beats(self):
        hrv = compute_hrv([1.0] * 1000, 100.0)
        self.assertEqual(hrv["n_beats"], 0)
        self.assertIsNone(hrv["mean_hr"])

    def test_bad_sr_raises(self):
        with self.assertRaises(ValueError):
            compute_hrv([1.0, 2.0], 0)


if __name__ == "__main__":
    unittest.main()
