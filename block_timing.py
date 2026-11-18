"""Verify contiguous EEG sample-index intervals using Capsule packet timestamps.

An interval establishes cadence at the first/last SDK timestamp of each packet.
It cannot synchronize the SDK clock to a stimulus or verify individual samples
inside a packet. The caller must check CSV sample indices and host-phase bounds.
"""
from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
import json
import math
import operator
from pathlib import Path

_MAX_I64 = 2**63 - 1
_MAX_SPANS = 65536
_MAX_LINE_BYTES = 4 * 1024 * 1024


class PacketClock:
    def __init__(self):
        self._starts = array("q")
        self._ends = array("q")
        self._first_two_end = array("q")

    def __len__(self):
        return len(self._starts)

    def supports_window(self, first: int, last: int) -> bool:
        """Both CSV sample indices (inclusive) must fall within one proven span."""
        if isinstance(first, bool) or isinstance(last, bool):
            return False
        try:
            first, last = operator.index(first), operator.index(last)
        except TypeError:
            return False
        pos = bisect_right(self._starts, first) - 1
        return pos >= 0 and first <= last <= self._ends[pos]

    def _discard_from(self, first: int) -> None:
        # Replayed indices make the entire suffix ambiguous until the former
        # high-water mark is passed. Do not retain an earlier overlapping span.
        pos = bisect_left(self._ends, first)
        if pos < len(self) and self._first_two_end[pos] < first:
            self._ends[pos] = first - 1
            pos += 1
        del self._starts[pos:]
        del self._ends[pos:]
        del self._first_two_end[pos:]


def _on_cadence(elapsed_ms: int, samples: int, period_ms: float) -> bool:
    # SDK times are integer milliseconds. Two rounded endpoints can differ
    # from their expected interval by strictly less than one millisecond.
    return abs(elapsed_ms - samples * period_ms) < 1


def read_packet_clock(path: Path, sample_rate: float) -> PacketClock:
    """Read metrics.jsonl once; store disjoint checked spans, never the EEG.

    Missing timing, oversized input, or exceeding the span budget disables the
    fallback. Each span needs at least two successive, nonoverlapping packets.
    """
    clock = PacketClock()
    if (not isinstance(sample_rate, (int, float)) or isinstance(sample_rate, bool)
            or not 0 < sample_rate <= 1000):
        return clock
    period = 1000 / sample_rate
    if not math.isfinite(period):
        return clock
    highest = -1
    previous_ms = None
    origin_index = origin_ms = 0
    extended = False
    try:
        with Path(path).open("rb") as handle:
            while line := handle.readline(_MAX_LINE_BYTES + 1):
                if len(line) > _MAX_LINE_BYTES:
                    return PacketClock()
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    previous_ms = None
                    continue
                if not isinstance(row, dict):
                    previous_ms = None
                    continue
                if row.get("stream") != "eeg_batch_timing":
                    continue

                first, count = row.get("first_sample_index"), row.get("sample_count")
                if type(first) is not int or type(count) is not int or first < 0 or count <= 0:
                    previous_ms = None
                    continue
                end = first + count - 1
                if end > _MAX_I64:
                    return PacketClock()
                prior_high, highest = highest, max(highest, end)
                if first <= prior_high:
                    clock._discard_from(first)
                    previous_ms = None
                    continue
                begin_ms, end_ms = row.get("sdk_first_ms"), row.get("sdk_last_ms")
                if (type(begin_ms) is not int or type(end_ms) is not int
                        or not 0 < begin_ms <= end_ms <= _MAX_I64
                        or not _on_cadence(end_ms - begin_ms, count - 1, period)):
                    previous_ms = None
                    continue
                follows = (previous_ms is not None and first == prior_high + 1 and begin_ms > previous_ms
                           and _on_cadence(begin_ms - previous_ms, 1, period)
                           and _on_cadence(begin_ms - origin_ms, first - origin_index, period)
                           and _on_cadence(end_ms - origin_ms, end - origin_index, period))
                if follows:
                    if not extended:
                        if len(clock) >= _MAX_SPANS:
                            return PacketClock()
                        clock._starts.append(origin_index)
                        clock._ends.append(end)
                        clock._first_two_end.append(end)
                        extended = True
                    else:
                        clock._ends[-1] = end
                else:
                    origin_index, origin_ms = first, begin_ms
                    extended = False
                previous_ms = end_ms
    except OSError:
        return PacketClock()
    return clock
