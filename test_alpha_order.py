import json
from pathlib import Path
import random
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from experiment_lab import ExperimentLab, _alpha_settings
from experiment_lab_analysis import _alpha_summary
from recording import SessionRecorder


class TestAlphaOrder(unittest.TestCase):
    def test_randomized_balanced_pairs_across_all_supported_counts(self):
        with patch("experiment_lab.random", random.Random(1234)):
            for count in range(1, 9):
                orders = []
                for _ in range(16):
                    settings = _alpha_settings({"alpha_order": "counterbalanced", "repeats": count}, "live")
                    order = settings["alpha_pair_first"]
                    self.assertEqual(len(order), count)
                    self.assertLessEqual(abs(order.count("eyes_open") - order.count("eyes_closed")), 1)
                    orders.append(tuple(order))
                self.assertGreater(len(set(orders)), 1)

    def test_fixed_defaults_and_legacy_requests(self):
        self.assertEqual(_alpha_settings({}, "live")["alpha_pair_first"], ["eyes_open"] * 3)
        self.assertEqual(_alpha_settings({}, "demo")["alpha_pair_first"], ["eyes_open"] * 2)
        self.assertEqual(_alpha_settings({"repeats": 2.5}, "live")["repeats"], 3)
        for invalid in [{"alpha_order": "typo"}, {"alpha_order": [], "repeats": 2},
                        {"alpha_order": "counterbalanced", "repeats": True},
                        {"alpha_order": "counterbalanced", "repeats": 9}]:
            with self.assertRaises(HTTPException):
                _alpha_settings(invalid, "demo")

    def test_live_and_demo_persist_the_same_plan_across_pause_and_export(self):
        for mode in ("live", "demo"):
            with TemporaryDirectory() as temp:
                base = Path(temp)
                rec = SessionRecorder(base, channel_names=["O1-T3"], sample_rates={"eeg": 250})
                rec.start()
                device = {"ready": True, "occipital_channels": ["O1"], "channels": ["O1-T3"], "sample_rate": 250}
                lab = ExperimentLab(lambda: base, lambda: device, lambda: rec,
                                    lambda: (rec, False), lambda _: None, dict)
                with patch("experiment_lab.random", random.Random(11)):
                    run = lab.start({"kind": "alpha", "stage": "baseline", "mode": mode,
                                     "context": {"participant_code": "fixture"},
                                     "settings": {"alpha_order": "counterbalanced", "repeats": 4,
                                                  "alpha_pair_first": ["ignore"]}})
                plan = run["settings"]["alpha_pair_first"]
                self.assertEqual(sorted(plan), ["eyes_closed"] * 2 + ["eyes_open"] * 2)
                at = time.time() * 1000
                wrong = "eyes_closed" if plan[0] == "eyes_open" else "eyes_open"
                with self.assertRaises(HTTPException):
                    lab.add_events(run["id"], {"events": [
                        {"type": "phase_start", "index": 0, "label": wrong, "at_ms": at}]})
                self.assertEqual(lab.detail(run["id"])["events"], [])
                for kind in ("pause", "resume"):
                    lab.add_events(run["id"], {"events": [{"type": kind, "at_ms": at}]})
                self.assertEqual(lab.detail(run["id"])["settings"]["alpha_pair_first"], plan)
                lab.add_events(run["id"], {"events": [
                    {"type": "phase_start", "index": 0, "label": plan[0], "at_ms": at},
                    {"type": "phase_end", "index": 0, "label": plan[0], "at_ms": at + 1},
                    {"type": "phase_start", "index": 1, "label": wrong, "at_ms": at + 1},
                ]})
                self.assertEqual(lab.finish(run["id"], {"status": "aborted"})["settings"]["alpha_pair_first"], plan)
                rec.close()
                events = [json.loads(line) for line in (rec.session_dir / "events.jsonl").read_text().splitlines()]
                starts = [e for e in events if e["type"] == "experiment_lab_start"]
                if mode == "live":
                    self.assertEqual(starts[0]["settings"]["alpha_pair_first"], plan)
                else:
                    self.assertEqual(starts, [])

    def test_count_reversed_pairs_and_do_not_shift_across_missing_phases(self):
        def block(index, label, t):
            return {"index": index, "label": label, "start_s": t, "end_s": t + 30,
                    "channels": [{"name": "O1-T3", "alpha_power": 2 if label == "eyes_closed" else 1}]}
        phases = [block(1, "eyes_closed", 30), block(2, "eyes_open", 60),
                  block(4, "eyes_closed", 120), block(5, "eyes_open", 150)]
        result = _alpha_summary(phases, ["O1-T3"])
        self.assertEqual(result["channels"][0], {"name": "O1-T3", "closed_open_ratio": 2, "paired_blocks": 1})
        self.assertEqual(result["pairs"][0]["closed_index"], 4)
        self.assertEqual(result["pairs"][0]["open_index"], 5)
