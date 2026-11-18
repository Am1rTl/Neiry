import unittest
from tempfile import TemporaryDirectory

import numpy as np

from research_readiness import assess_research_readiness, sdk_supports_alpha_block


RATE = 100
TIMES = np.arange(4901) / RATE


def make_report(events):
    timeline = [{"usable": True} for _ in range(23)]
    return {
        "session": {"events": events, "streams": {"eeg": {"first_timestamp": 0, "last_timestamp": 49}}},
        "configuration": {"epoch_s": 4, "hop_s": 2, "minimum_spectral_windows": 15},
        "eeg": {"available": True, "sample_rate_hz": RATE, "channels": [
            {"name": "O1-T3", "spectral_interpretability": "limited", "timeline": timeline,
             "quality": {"usable_windows": len(timeline)}}]},
    }


def make_events(source="lab", ident="a", bounds=(1, 23, 25, 47)):
    if source == "lab":
        events = [{"type": "experiment_lab_start", "kind": "alpha", "run_id": ident,
                   "settings": {"timing": "browser_monotonic_software_clock",
                                "clock_offset_ms": 3.2, "clock_round_trip_ms": 4.7}}]
    else:
        events = [{"type": "protocol_start", "protocol_id": "eyes", "run_id": ident}]
    for i, time in enumerate(bounds):
        index = i // 2
        label = ("eyes_open", "eyes_closed")[index]
        kind = ("phase_start", "phase_end")[i % 2]
        if source == "lab":
            events.append({"type": "experiment_lab_event", "run_id": ident,
                           "event": {"type": kind, "at_ms": time * 1000, "index": index, "label": label}})
        else:
            events.append({"type": "protocol_" + kind, "protocol_id": "eyes", "run_id": ident,
                           "ts_unix": time, "index": index, "label": label})
    if source == "lab":
        events.append({"type": "experiment_lab_finish", "run_id": ident, "experiment_status": "completed"})
    else:
        events.append({"type": "protocol_complete", "run_id": ident, "protocol_id": "eyes"})
    return events


def analyze(events):
    return assess_research_readiness(make_report(events), eeg_timestamps=TIMES)


class TestResearchReadiness(unittest.TestCase):
    def test_both_recording_dialects(self):
        for source in ["protocol", "lab"]:
            result = analyze(make_events(source))
            self.assertEqual(result["alpha"]["status"], "reviewable")
            self.assertEqual(result["alpha"]["pairs_with_coverage"], 1)
            self.assertEqual(result["alpha"]["pair_coverage"][0]["channel_windows"]["O1-T3"],
                             {"eyes_open": 4, "eyes_closed": 4})
            self.assertEqual(result["decoder"]["status"], "untested")

    def test_free_text_or_a_plan_is_not_an_observed_pair(self):
        points = [{"type": "marker", "label": label, "ts_unix": t} for t, label in
                  [(1, "eyes_open"), (25, "eyes_closed")]]
        self.assertEqual(analyze(points)["alpha"]["status"], "no_protocol")
        plan = make_events("protocol")
        plan[0]["phases"] = [e["label"] for e in plan[1:-1]]
        self.assertEqual(analyze([plan[0], plan[-1]])["alpha"]["status"], "incomplete")

    def test_aborted_runs_incomplete_epochs_and_mismatched_retries(self):
        events = make_events()
        events[-1]["experiment_status"] = "aborted"
        self.assertEqual(analyze(events)["alpha"]["paired_blocks"], 0)
        events = make_events()
        events[2]["event"]["reason"] = "view_away"
        self.assertEqual(analyze(events)["alpha"]["paired_blocks"], 0)
        retry = make_events()[1:3]
        retry[0]["event"]["phase_id"] = retry[1]["event"]["phase_id"] = "new"
        self.assertEqual(analyze(events[:3] + retry + events[3:])["alpha"]["paired_blocks"], 1)
        retry[0]["event"]["phase_id"] = "old"
        self.assertEqual(analyze(events[:3] + retry + events[3:])["alpha"]["paired_blocks"], 0)

    def test_unknown_browser_clock_does_not_verify_alignment(self):
        for settings in [{}, {"timing": "browser_unsynchronized"}, {"clock_offset_ms": None}]:
            events = make_events()
            events[0]["settings"] = settings
            alpha = analyze(events)["alpha"]
            self.assertEqual(alpha["paired_blocks"], 1)
            self.assertEqual(alpha["pairs_with_coverage"], 0)
            self.assertEqual(alpha["pair_coverage"][0]["clock"], "browser_epoch_unverified")
            self.assertIn("часов", alpha["detail"])

    def test_do_not_join_different_runs_or_out_of_range_blocks(self):
        a, b = make_events(), make_events(ident="b")
        self.assertEqual(analyze(a[:3] + a[-1:] + b[:1] + b[3:])["alpha"]["paired_blocks"], 0)
        self.assertEqual(analyze(make_events(bounds=(1, 23, 25, 50)))["alpha"]["paired_blocks"], 0)
        self.assertEqual(analyze(make_events(bounds=(1, 26, 25, 47)))["alpha"]["paired_blocks"], 0)

    def test_phase_coverage_requires_actual_contiguous_timestamps_and_disjoint_windows(self):
        report = make_report(make_events())
        self.assertEqual(assess_research_readiness(report)["alpha"]["status"], "insufficient_signal")
        unstable = TIMES.copy()
        unstable[1::100] -= .4
        result = assess_research_readiness(report, eeg_timestamps=unstable)
        self.assertEqual(result["alpha"]["pairs_with_coverage"], 0)
        rejected = result["alpha"]["window_counts"]
        self.assertGreater(rejected["signal_qc_windows_with_host_time_issue"], 0)
        self.assertEqual(rejected["signal_qc_windows_with_host_time_issue"], rejected["channel_windows_passing_signal_qc"])
        self.assertIn("метки приёма", result["next_action"])
        # Overlapping windows with starts 4, 6, and 8 s (and 28, 30, 32 s).
        timeline = report["eeg"]["channels"][0]["timeline"]
        for i, w in enumerate(timeline):
            w["usable"] = i in {2, 3, 4, 14, 15, 16}
        self.assertEqual(assess_research_readiness(report, eeg_timestamps=TIMES)["alpha"]["pairs_with_coverage"], 0)

    def test_sdk_fallback_only_for_long_aligned_blocks_with_explicit_index(self):
        class CoveredClock:
            def supports_window(self, start, end):
                return 0 <= start < end <= 7100

        report = make_report(make_events(bounds=(1, 34, 36, 69)))
        report["session"]["streams"]["eeg"]["last_timestamp"] = 71
        indices = np.arange(7101)
        times = indices / RATE
        times[5::10] -= .04
        nwin = (len(times) - 4*RATE) // (2*RATE) + 1
        report["eeg"]["channels"][0]["timeline"] = [{"usable": True} for _ in range(nwin)]
        result = assess_research_readiness(report, eeg_timestamps=times, eeg_indices=indices,
                                           packet_clock=CoveredClock)["alpha"]
        self.assertEqual(result["pairs_with_coverage"], 1)
        self.assertGreater(result["window_counts"]["sdk_supported_channel_windows"], 0)
        for mode in result["pair_coverage"][0]["timing_by_channel"]["O1-T3"].values():
            self.assertEqual(mode["strict_host"], 0)
            self.assertGreaterEqual(mode["sdk_continuity_host_block"], 3)
        class InterruptedClock:
            def supports_window(self, start, end):
                # Continuous SDK packets cover the open phase only.
                return end <= 3400
        lost_clock = assess_research_readiness(report, eeg_timestamps=times, eeg_indices=indices,
                                              packet_clock=InterruptedClock())["alpha"]
        self.assertEqual(lost_clock["pairs_with_coverage"], 0)
        self.assertEqual(lost_clock["pair_coverage"][0]["channel_windows"]["O1-T3"]["eyes_closed"], 0)
        self.assertGreater(lost_clock["window_counts"]["sdk_supported_channel_windows"], 0)
        no_index = assess_research_readiness(report, eeg_timestamps=times, packet_clock=CoveredClock)["alpha"]
        self.assertEqual(no_index["pairs_with_coverage"], 0)
        for key, val in (("sdk", None), ("index", indices + .5),
                         ("index", indices + indices // 80)):
            kwargs = {"eeg_indices": indices, "packet_clock": CoveredClock()}
            kwargs["eeg_indices" if key == "index" else "packet_clock"] = val
            out = assess_research_readiness(report, eeg_timestamps=times, **kwargs)
            self.assertEqual(out["alpha"]["pairs_with_coverage"], 0)
        report["session"]["events"] = make_events(bounds=(1, 27, 36, 64))
        short = assess_research_readiness(report, eeg_timestamps=times, eeg_indices=indices,
                                          packet_clock=CoveredClock())["alpha"]
        self.assertEqual(short["pairs_with_coverage"], 0)
        # Endpoints can lie inside the trimmed phase while an interior sample exits it.
        window = np.arange(400) / 100 + 3.02
        window[1] -= .05
        self.assertFalse(sdk_supports_alpha_block(window, indices[:400], RATE, 0, 60, CoveredClock()))
        # A broad drift rejects even in-bound windows with an intact SDK interval.
        self.assertFalse(sdk_supports_alpha_block(4 + indices[:400]/100*1.2, indices[:400],
                                                  RATE, 0, 60, CoveredClock()))

    def test_both_phases_need_the_same_eligible_lead(self):
        report = make_report(make_events())
        left = report["eeg"]["channels"][0]
        right = {**left, "name": "O2-T4", "timeline": [{"usable": n >= 12} for n in range(23)]}
        left["timeline"] = [{"usable": n < 12} for n in range(23)]
        report["eeg"]["channels"].append(right)
        self.assertEqual(assess_research_readiness(report, eeg_timestamps=TIMES)["alpha"]["status"], "insufficient_signal")
        report["eeg"]["available"] = False
        report["eeg"]["channels"] = []
        self.assertEqual(assess_research_readiness(report)["alpha"]["status"], "no_eeg")

    def test_session_recorder_to_archived_report_uses_client_event_clock(self):
        from analysis_engine import analyze_session
        from recording import SessionRecorder

        with TemporaryDirectory() as directory:
            recorder = SessionRecorder(directory, channel_names=["O1-T3"], sample_rates={"eeg": RATE})
            session = recorder.start()
            signal = np.random.default_rng(204).normal(0, .01, size=(len(TIMES), 1))
            recorder.record_eeg(signal, TIMES)
            for row in make_events():
                row = dict(row)
                # Batches are received long after the instruction. Joining to
                # server receipt time here would put every phase outside EEG.
                recorder.event(row.pop("type"), timestamp=100, **row)
            recorder.close()
            report = analyze_session(session)["research_readiness"]
            self.assertEqual(report["alpha"]["status"], "reviewable")
            self.assertEqual(report["alpha"]["pair_coverage"][0]["channel_windows"]["O1-T3"],
                             {"eyes_open": 4, "eyes_closed": 4})
