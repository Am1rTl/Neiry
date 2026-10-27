"""Тесты рекордера Capsule WebApp. Запуск: .venv/bin/python -m unittest test_recording -v"""
import csv
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recording import SessionRecorder  # noqa: E402


def make_rec(tmp, **kw):
    kw.setdefault("device_name", "Headband")
    kw.setdefault("device_serial", "CC24613B9270")
    kw.setdefault("channel_names", ["Fp1", "Fp2"])
    kw.setdefault("sample_rates", {"eeg": 250})
    return SessionRecorder(Path(tmp) / "records", **kw)


class TestRecorder(unittest.TestCase):
    def test_writes_all_streams(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = make_rec(tmp)
            d = rec.start()
            rec.record_eeg([[1, 2], [3, 4]], [1000.0, 1000.004])
            rec.record_ppg([0.5, 0.6], [1000.0, 1000.01])
            rec.record_mems([[0, 0, 1]], [[0, 0, 0]], [1000.0])
            rec.record_resistances({"Fp1": 1200.0, "Fp2": 900.0}, ts=1000.0)
            rec.record_metric("emotions", {"attention": 42.0}, ts=1000.0)
            rec.record_metric("cardio", {"heartRate": 70.0}, ts=1000.0)
            rec.set_calibration({"individualFrequency": 10.2})
            rec.event("device_connected")
            summary = rec.close()

            for name, n in (("eeg.csv", 2), ("ppg.csv", 2), ("mems.csv", 1)):
                with open(d / name) as f:
                    self.assertEqual(len(list(csv.DictReader(f))), n)
            with open(d / "resistances.csv") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(float(rows[0]["Fp1"]), 1200.0)
            with open(d / "metrics.jsonl") as f:
                metrics = [json.loads(l) for l in f]
            self.assertEqual({m["stream"] for m in metrics}, {"emotions", "cardio"})
            self.assertEqual(json.loads((d / "calibration.json").read_text())["individualFrequency"], 10.2)
            self.assertEqual(summary["counts"]["eeg_samples"], 2)
            self.assertEqual(summary["counts"]["metric_rows"], 2)
            rec.record_eeg([[9, 9]], [1001.0])  # после close — no-op, без исключений

    def test_session_closed_event_and_idempotent_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = make_rec(tmp)
            d = rec.start()
            summary = rec.close()
            self.assertIn("session_id", summary)
            with open(d / "events.jsonl") as f:
                events = [json.loads(l) for l in f]
            types = [e["type"] for e in events]
            self.assertEqual(types[0], "session_started")
            self.assertEqual(types[-1], "session_closed")
            self.assertEqual(rec.close(), {})  # повторный close — пусто, без ошибок

    def test_calibration_writethrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = make_rec(tmp)
            d = rec.start()
            self.assertFalse((d / "calibration.json").exists())
            rec.set_calibration({"individualFrequency": 9.8})
            # Калибровка на диске сразу, до close().
            self.assertEqual(
                json.loads((d / "calibration.json").read_text())["individualFrequency"], 9.8)
            rec.close()

    def test_resistances_header_from_first_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Имена SDK отличаются от EEG-каналов — заголовок берём из первого вызова.
            rec = make_rec(tmp, channel_names=["O1-T3", "O2-T4"])
            d = rec.start()
            rec.record_resistances({"R1": 100.0, "R2": 200.0}, ts=1000.0)
            rec.record_resistances({"R1": float("inf")}, ts=1001.0)  # INF + пропуск R2
            rec.close()
            with open(d / "resistances.csv") as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ["ts_unix", "ts_iso", "R1", "R2"])
                rows = list(reader)
            self.assertEqual(float(rows[0]["R1"]), 100.0)
            self.assertTrue(math.isnan(float(rows[1]["R1"])))
            self.assertTrue(math.isnan(float(rows[1]["R2"])))
            self.assertEqual(rows[1]["R1"], "NaN")

    def test_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = make_rec(tmp)
            rec.start()
            rec.record_eeg([[1, 2]], [1000.0])
            rec.record_metric("battery", {"charge_pct": 50}, ts=1000.0)
            st = rec.stats()
            self.assertEqual(st["session_id"], rec.session_id)
            self.assertGreaterEqual(st["duration_s"], 0)
            self.assertEqual(st["counts"]["eeg_samples"], 1)
            self.assertEqual(st["counts"]["metric_rows"], 1)
            self.assertEqual(st["write_errors"], 0)
            self.assertGreater(st["file_sizes"]["eeg.csv"], 0)
            self.assertTrue(st["active"])
            rec.close()
            self.assertFalse(rec.stats()["active"])

    def test_should_rotate(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = make_rec(tmp)
            rec.start()
            self.assertFalse(rec.should_rotate())
            rec.record_eeg([[1, 2]], [1000.0])
            self.assertTrue(rec.should_rotate(max_mb=0))  # любой размер >= 0
            rec._started_ts -= 7 * 3600  # состариваем сессию на 7 часов
            self.assertTrue(rec.should_rotate(max_hours=6))
            self.assertFalse(rec.should_rotate(max_hours=8, max_mb=10**9))
            rec.close()
            self.assertFalse(rec.should_rotate())  # закрытая не ротируется

    def test_start_with_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = make_rec(tmp)
            d = rec.start(session_id="session_20240101_000000_test_p2")
            self.assertEqual(rec.session_id, "session_20240101_000000_test_p2")
            self.assertEqual(d.name, "session_20240101_000000_test_p2")
            self.assertTrue((d / "eeg.csv").exists())
            rec.close()
            # Повторный старт с тем же id не затирает чужую сессию.
            d2 = rec.start(session_id="session_20240101_000000_test_p2")
            self.assertNotEqual(d2.name, d.name)
            self.assertTrue(d2.name.endswith("_2"))
            rec.close()

    def test_jsonl_bulk_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = make_rec(tmp)
            d = rec.start()
            for i in range(120):  # больше порога построчного flush
                rec.record_metric("battery", {"charge_pct": i}, ts=1000.0 + i)
            rec.close()
            with open(d / "metrics.jsonl") as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 120)


if __name__ == "__main__":
    unittest.main()
