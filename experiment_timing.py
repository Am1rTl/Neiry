"""Continuity evidence for long EEG blocks recorded from batched callbacks."""

import json
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from analysis_engine import _packet_timing


# One quarter of the shortest (1 s) phase margin. This is a QC tolerance,
# not an estimate of stimulus latency or a clock synchronization bound.
MAX_HOST_SPREAD_S = 0.25


@dataclass
class PacketCoverage:
    present: bool = False
    starts: list[int] = field(default_factory=list)
    ends: list[int] = field(default_factory=list)  # inclusive source row indices

    def covers(self, first: float, last: float) -> bool:
        i = bisect_right(self.starts, first) - 1
        return i >= 0 and last <= self.ends[i]


def read_packet_coverage(metrics_path: Path, rate: float) -> PacketCoverage:
    """Join packets only when their indices and SDK time bounds agree."""
    coverage = PacketCoverage()
    period = 1000 / rate
    tolerance = max(1., period / 2)
    previous_end = -1
    previous_ms = None
    try:
        with metrics_path.open(encoding="utf-8") as stream:
            for line in stream:
                if 'eeg_batch_timing' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("stream") != "eeg_batch_timing":
                    continue
                coverage.present = True
                packet = _packet_timing(row)
                if packet is None:
                    previous_ms = None
                    continue
                index, count, first_ms, last_ms = packet
                if index <= previous_end:
                    # A duplicate or reversed row makes the mapping ambiguous.
                    return PacketCoverage(present=True)
                end = index + count - 1
                duration_ok = abs((last_ms - first_ms) - (count - 1) * period) <= tolerance
                if duration_ok:
                    contiguous = (index == previous_end + 1 and previous_ms is not None
                                  and first_ms > previous_ms
                                  and abs(first_ms - previous_ms - period) <= tolerance)
                    if contiguous:
                        coverage.ends[-1] = end
                    else:
                        coverage.starts.append(index)
                        coverage.ends.append(end)
                previous_end = end
                previous_ms = last_ms if duration_ok else None
    except OSError:
        # Partial metadata after a read failure cannot attest continuity.
        return PacketCoverage(present=coverage.present)
    return coverage


def window_timing_reasons(times, indices, phase_start, phase_end, rate, gap_limit, packets):
    """Screen in acquisition order; keep host and SDK in their own clock domains."""
    reasons = []
    indices_ok = (np.all(np.isfinite(indices)) and np.all(indices == np.floor(indices))
                  and np.all(np.diff(indices) == 1))
    if not indices_ok:
        reasons.append("sample_index_gap")
    if packets.present and indices_ok and not packets.covers(indices[0], indices[-1]):
        reasons.append("sdk_packet_gap")
    if not np.all(np.isfinite(times)):
        return [*reasons, "host_timestamp_gap"]
    if np.min(times) < phase_start or np.max(times) > phase_end:
        reasons.append("phase_boundary")
    if packets.present and indices_ok:
        # A regular SDK sequence can arrive in bursts. Bound local host
        # variation relative to nominal acquisition instead of dating the
        # stimulus with the SDK epoch. Long backlog or clock steps still fail.
        offsets = (times - times[0]) - (indices - indices[0]) / rate
        if np.ptp(offsets) > MAX_HOST_SPREAD_S:
            reasons.append("host_timestamp_gap")
    else:
        dt = np.diff(times)
        if np.any(dt <= 0) or np.any(dt > gap_limit):
            reasons.append("host_timestamp_gap")
    return reasons
