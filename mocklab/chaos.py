"""Failure injection for the mock lab (seeded, so runs are reproducible)."""

import random
from dataclasses import dataclass
from enum import StrEnum


class Fault(StrEnum):
    NONE = "none"
    ERROR_BEFORE = "error_before_effect"  # 500, nothing written
    HANG_BEFORE = "hang_before_effect"  # sleep past client timeout, nothing written
    LOST_AFTER = "response_lost_after_effect"  # effect committed, then 500 or hang


# Shares of the total failure rate: 8% / 6% / 6% of all calls at the default 20%.
FAULT_SHARES = ((Fault.ERROR_BEFORE, 0.40), (Fault.HANG_BEFORE, 0.30), (Fault.LOST_AFTER, 0.30))


@dataclass
class Chaos:
    failure_rate: float = 0.2
    latency_min_ms: int = 300
    latency_max_ms: int = 1500
    hang_seconds: float = 5.0
    seed: int = 42

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def reseed(self) -> None:
        self.rng = random.Random(self.seed)

    def latency_s(self) -> float:
        return self.rng.uniform(self.latency_min_ms, self.latency_max_ms) / 1000

    def draw(self) -> Fault:
        r = self.rng.random()
        if r >= self.failure_rate:
            return Fault.NONE
        point = r / self.failure_rate if self.failure_rate else 1.0
        acc = 0.0
        for fault, share in FAULT_SHARES:
            acc += share
            if point < acc:
                return fault
        return FAULT_SHARES[-1][0]

    def lost_response_hangs(self) -> bool:
        """For LOST_AFTER: half the time the response never arrives, half the time a 500."""
        return self.rng.random() < 0.5
