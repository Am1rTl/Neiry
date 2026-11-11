"""Strict JSON at public boundaries; missing numbers are never zero."""
from __future__ import annotations

import math
from typing import Any


def finite_json(value: Any) -> Any:
    """Return a new tree, preserving live SDK state and replacing NaN/Inf by None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(v) for v in value]
    if hasattr(value, "item"):
        return finite_json(value.item())
    return value
