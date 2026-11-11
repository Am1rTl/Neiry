"""Construct monotonic, receipt-time-based sample timestamps for streamed SDK batches.

Capsule callbacks provide batches rather than a reliable device timestamp here. This
software timeline is approximate; it preserves order and never claims hardware sync.
"""
from __future__ import annotations

import math


def batch_times(previous: float | None, received_at: float, count: int, sample_rate: int) -> list[float]:
    if count <= 0:
        return []
    if not math.isfinite(received_at) or sample_rate <= 0:
        raise ValueError("A finite receipt time and a positive sample rate are required")
    step = 1.0 / sample_rate
    start = received_at - (count - 1) * step
    if previous is not None and math.isfinite(previous):
        start = max(start, previous + step)
    return [start + i * step for i in range(count)]
