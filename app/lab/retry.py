"""Exponential backoff with full jitter (AWS-style): sleep ~ U(0, min(cap, base * 2**attempt))."""

import random


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    """attempt is 1-based: the delay before attempt `attempt + 1`."""
    r = rng or random
    return r.uniform(0, min(cap, base * (2 ** max(0, attempt - 1))))
