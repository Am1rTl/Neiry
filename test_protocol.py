"""Unit-тесты чистых функций server.py без железа. Запуск: .venv/bin/python -m unittest test_protocol -v

Покрывает санитизацию JSON-пейлоадов (_sanitize), сводный статус
сенсоров (_sensor_status) и построение WS-пакетов (_build_batch).
Стабы SDK (_sdk_stubs) ставятся ДО import server, поэтому тесты идут
и без поставки CapsuleAPI.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sdk_stubs import install as _install_sdk_stubs

_install_sdk_stubs()  # стабы SDK — ДО import server, чтобы тесты шли без железа

import server
from server import _build_batch, _sanitize, _sensor_status


class TestSanitize(unittest.TestCase):
    def test_nan_and_inf_become_none(self):
        obj = {"a": float("nan"), "b": float("inf"), "c": float("-inf")}
        self.assertIsNone(_sanitize(obj))
        self.assertEqual(obj, {"a": None, "b": None, "c": None})

    def test_nested_dicts_and_lists(self):
        obj = {"d": {"x": float("nan"), "ok": 1.5},
               "l": [1.0, float("inf"), {"y": float("-inf")}, "s", None, 7]}
        _sanitize(obj)
        self.assertEqual(obj, {"d": {"x": None, "ok": 1.5},
                               "l": [1.0, None, {"y": None}, "s", None, 7]})

    def test_top_level_list_and_plain_values_untouched(self):
        obj = [0, 1.5, "eeg", True, None, {"v": 2.5}]
        _sanitize(obj)
        self.assertEqual(obj, [0, 1.5, "eeg", True, None, {"v": 2.5}])


class TestSensorStatus(unittest.TestCase):
    def _fresh(self):
        return server.State()

    def test_empty_state_is_none_everywhere(self):
        st = _sensor_status(self._fresh())
        for key in ("eeg", "ppg", "mems", "skin", "cardio",
                    "emotions", "phy", "prod", "nfb"):
            self.assertEqual(st[key]["state"], "none", key)

    def test_eeg_ok_and_boundaries(self):
        s = self._fresh()
        s.channel_names = ["Fp1", "Fp2"]
        s.eeg_sample_rate = 250
        s.eeg.extend([[0.0, 0.0]] * 99)
        self.assertEqual(_sensor_status(s)["eeg"]["state"], "none")  # ниже порога 100
        s.eeg.append([0.0, 0.0])
        eeg = _sensor_status(s)["eeg"]
        self.assertEqual(eeg["state"], "ok")
        self.assertEqual(eeg["channels"], ["Fp1", "Fp2"])
        s.eeg_sample_rate = 0
        self.assertEqual(_sensor_status(s)["eeg"]["state"], "none")  # нет sr — нет ok

    def test_ppg_and_mems_thresholds(self):
        s = self._fresh()
        s.ppg_sample_rate = 100
        s.mems_sample_rate = 250
        s.ppg.extend([0.0] * 49)
        s.mems_acc.extend([[0.0, 0.0, 1.0]] * 99)
        st = _sensor_status(s)
        self.assertEqual(st["ppg"]["state"], "none")
        self.assertEqual(st["mems"]["state"], "none")
        s.ppg.append(0.0)
        s.mems_acc.append([0.0, 0.0, 1.0])
        st = _sensor_status(s)
        self.assertEqual(st["ppg"]["state"], "ok")   # порог 50
        self.assertEqual(st["mems"]["state"], "ok")  # порог 100

    def test_skin_none_ok_warn_error(self):
        s = self._fresh()
        self.assertEqual(_sensor_status(s)["skin"]["state"], "none")

        s.latest_resistances = [
            {"channel": "Fp1", "value": 1200.0, "is_inf": False},
            {"channel": "Fp2", "value": 900.0, "is_inf": False},
        ]
        skin = _sensor_status(s)["skin"]
        self.assertEqual(skin["state"], "ok")
        self.assertEqual(skin["max_r"], 1200.0)

        s.latest_resistances = [
            {"channel": "Fp1", "value": 1200.0, "is_inf": False},
            {"channel": "Fp2", "value": None, "is_inf": True},
        ]
        skin = _sensor_status(s)["skin"]
        self.assertEqual(skin["state"], "warn")
        self.assertEqual((skin["n_good"], skin["n_bad"]), (1, 1))

        s.latest_resistances = [
            {"channel": "Fp1", "value": None, "is_inf": True},
            {"channel": "Fp2", "value": float("nan"), "is_inf": False},
        ]
        skin = _sensor_status(s)["skin"]
        self.assertEqual(skin["state"], "error")
        self.assertEqual(skin["n_bad"], 2)

    def test_cardio_branches(self):
        s = self._fresh()
        good = {"heartRate": 70.0, "skinContact": True,
                "motionArtifacts": False, "metricsAvailable": True,
                "stressIndex": 1.0, "kaplanIndex": 2.0}
        s.latest_cardio = dict(good)
        cardio = _sensor_status(s)["cardio"]
        self.assertEqual(cardio["state"], "ok")
        self.assertEqual(cardio["hr"], 70.0)

        s.latest_cardio = {**good, "skinContact": False}
        cardio = _sensor_status(s)["cardio"]
        self.assertEqual((cardio["state"], cardio["reason"]), ("warn", "no skin contact"))

        s.latest_cardio = {**good, "metricsAvailable": False, "motionArtifacts": True}
        cardio = _sensor_status(s)["cardio"]
        self.assertEqual((cardio["state"], cardio["reason"]), ("warn", "motion artifacts"))

        s.latest_cardio = {**good, "metricsAvailable": False}
        cardio = _sensor_status(s)["cardio"]
        self.assertEqual((cardio["state"], cardio["reason"]), ("warn", "signal weak"))

    def test_metric_streams_none_and_ok(self):
        s = self._fresh()
        s.latest_emotions = {"attention": 50.0, "relaxation": 60.0}
        s.latest_phy = {"fatigue": 1.0, "relaxation": 2.0,
                        "concentration": 3.0, "stress": 4.0}
        s.latest_prod = {"fatigueScore": 5.0, "productivityScore": 6.0}
        st = _sensor_status(s)
        self.assertEqual(st["emotions"]["state"], "ok")
        self.assertEqual(st["phy"]["state"], "ok")
        self.assertEqual(st["prod"]["state"], "ok")

    def test_nfb_ok_warn_none(self):
        s = self._fresh()
        s.calibration_phase = "ready"
        nfb = _sensor_status(s)["nfb"]
        self.assertEqual((nfb["state"], nfb["reason"]), ("warn", "not calibrated yet"))

        s.nfb_hist.append({"alpha": 1.5})
        nfb = _sensor_status(s)["nfb"]
        self.assertEqual(nfb["state"], "ok")
        self.assertEqual((nfb["n"], nfb["last"]), (1, 1.5))

        s2 = self._fresh()
        s2.calibrated = True
        self.assertEqual(_sensor_status(s2)["nfb"]["state"], "none")


class TestBuildBatch(unittest.TestCase):
    def test_empty_input_returns_none(self):
        payload, last_t = _build_batch([], [], 2, None, 250)
        self.assertIsNone(payload)
        self.assertIsNone(last_t)
        payload, last_t = _build_batch([1.0], [], 2, 5.0, 250)
        self.assertIsNone(payload)
        self.assertEqual(last_t, 5.0)

    def test_first_sync_short_buffer(self):
        times = [1.0, 2.0, 3.0]
        samples = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
        payload, last_t = _build_batch(times, samples, 2, None, 250)
        self.assertEqual(payload, {"t0": 1.0, "sr": 250, "n": 3,
                                   "ch1": [1.0, 3.0, 5.0],
                                   "ch2": [2.0, 4.0, 6.0]})
        self.assertEqual(last_t, 3.0)

    def test_first_sync_capped(self):
        times = [float(i) for i in range(600)]
        samples = [[float(i), -float(i)] for i in range(600)]
        payload, last_t = _build_batch(times, samples, 2, None, 250)
        self.assertEqual(payload["n"], 500)
        self.assertEqual(payload["t0"], 100.0)
        self.assertEqual(payload["ch1"][0], 100.0)
        self.assertEqual(last_t, 599.0)

    def test_first_sync_exact_cap_boundary(self):
        times = [float(i) for i in range(500)]
        samples = [[float(i), 0.0] for i in range(500)]
        payload, last_t = _build_batch(times, samples, 2, None, 250)
        self.assertEqual(payload["n"], 500)
        self.assertEqual(payload["t0"], 0.0)
        self.assertEqual(last_t, 499.0)

    def test_delta_by_time(self):
        times = [1.0, 2.0, 3.0, 4.0]
        samples = [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0], [4.0, 4.0]]
        payload, last_t = _build_batch(times, samples, 2, 2.0, 250)
        self.assertEqual(payload["n"], 2)
        self.assertEqual(payload["t0"], 3.0)
        self.assertEqual(payload["ch1"], [3.0, 4.0])
        self.assertEqual(last_t, 4.0)

    def test_delta_without_new_data(self):
        times = [1.0, 2.0]
        samples = [[1.0, 1.0], [2.0, 2.0]]
        payload, last_t = _build_batch(times, samples, 2, 2.0, 250)
        self.assertIsNone(payload)
        self.assertEqual(last_t, 2.0)

    def test_single_channel_samples(self):
        payload, last_t = _build_batch([1.0, 2.0], [0.5, 0.6], 1, None, 100)
        self.assertEqual(payload, {"t0": 1.0, "sr": 100, "n": 2,
                                   "samples": [0.5, 0.6]})
        self.assertEqual(last_t, 2.0)
        payload, _ = _build_batch([1.0, 2.0], [[0.5], [0.6]], 1, None, 100)
        self.assertEqual(payload["samples"], [0.5, 0.6])


if __name__ == "__main__":
    unittest.main()
