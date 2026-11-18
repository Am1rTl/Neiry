"""Controls for the SDK packet evidence used during long, labelled EEG blocks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from block_timing import read_packet_clock


def packet(first, begin_ms=None, count=25):
    begin_ms = 1000 + 4 * first if begin_ms is None else begin_ms
    return {"stream": "eeg_batch_timing", "first_sample_index": first, "sample_count": count,
            "sdk_first_ms": begin_ms, "sdk_last_ms": begin_ms + 4 * (count - 1)}


class BlockTimingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "metrics.jsonl"

    def clock(self, records, fs=250):
        self.path.write_bytes(b"\n".join(x if isinstance(x, bytes) else json.dumps(x).encode() for x in records))
        return read_packet_clock(self.path, fs)

    def test_sdk_endpoints_and_consecutive_batches_cover_only_inclusive_sample_range(self):
        self.assertFalse(read_packet_clock(self.path, 250))
        self.assertFalse(self.clock([packet(0)]))
        clock = self.clock([packet(15), {"stream": "cardio"}, packet(40), packet(65)])
        self.assertEqual(len(clock), 1)
        self.assertTrue(clock.supports_window(15, 89))
        for bounds in [(14, 70), (50, 90), (18, 17), (False, 18), (15.1, 60)]:
            self.assertFalse(clock.supports_window(*bounds))

    def test_time_and_index_gaps_reset_without_bridging(self):
        for barrier in [b'{"stream":"eeg_batch_timing",', {"stream": "eeg_batch_timing"},
                        {**packet(50), "sdk_first_ms": -1}, {**packet(50), "sdk_last_ms": 9000}]:
            with self.subTest(barrier=barrier):
                clock = self.clock([packet(0), packet(25), barrier, packet(75), packet(100)])
                self.assertTrue(clock.supports_window(0, 49))
                self.assertTrue(clock.supports_window(75, 124))
                self.assertFalse(clock.supports_window(20, 80))
        for rows in [[packet(0), packet(25), packet(50, 1208), packet(75, 1308)],
                     [packet(0), packet(25), packet(75), packet(100)]]:
            clock = self.clock(rows)
            self.assertEqual(len(clock), 2)
            self.assertFalse(clock.supports_window(25, 124))

    def test_duplicate_or_reorder_invalidates_ambiguous_earlier_coverage(self):
        clock = self.clock([packet(0), packet(25), packet(50), packet(25), packet(75), packet(100)])
        self.assertFalse(clock.supports_window(0, 24))  # only one unambiguous packet
        self.assertFalse(clock.supports_window(24, 25))
        self.assertFalse(clock.supports_window(50, 74))
        self.assertTrue(clock.supports_window(75, 124))

        clock = self.clock([packet(i) for i in (0, 25, 50, 75, 50, 100, 125)])
        self.assertTrue(clock.supports_window(0, 49))
        self.assertFalse(clock.supports_window(49, 50))

    def test_invalid_types_ranges_and_limits_fail_closed(self):
        for key, value in [("first_sample_index", True), ("sample_count", 25.0), ("sample_count", -1),
                           ("sdk_last_ms", 2**63), ("sdk_first_ms", True), ("sdk_last_ms", 1000)]:
            self.assertFalse(self.clock([{**packet(0), key: value}, packet(25)]), key)
        rows = [packet(0), packet(25)]
        for fs in [0, float("nan"), -10, 1001, None, True]:
            self.assertFalse(self.clock(rows, fs))
        with patch("block_timing._MAX_LINE_BYTES", 20):
            self.assertFalse(self.clock(rows))
        with patch("block_timing._MAX_SPANS", 1):
            self.assertFalse(self.clock([*rows, packet(100), packet(125)]))

    def test_fractional_period_millisecond_rounding_does_not_allow_drift(self):
        fs = 256
        def rounded(i):
            return {**packet(i, count=5), "sdk_first_ms": round(1000 + i * 1000 / fs),
                    "sdk_last_ms": round(1000 + (i + 4) * 1000 / fs)}
        clock = self.clock([rounded(i) for i in range(0, 50, 5)], fs)
        self.assertTrue(clock.supports_window(0, 49))
        clock = self.clock([packet(i, count=5) for i in range(0, 50, 5)], fs)
        self.assertFalse(clock.supports_window(0, 49))
