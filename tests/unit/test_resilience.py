import random

from app.lab.circuit import CircuitBreaker, CircuitState
from app.lab.retry import backoff_delay
from mocklab.chaos import Chaos, Fault


def test_backoff_within_bounds_and_grows() -> None:
    rng = random.Random(1)
    for attempt in range(1, 10):
        cap_for_attempt = min(3.0, 0.2 * 2 ** (attempt - 1))
        for _ in range(200):
            assert 0 <= backoff_delay(attempt, 0.2, 3.0, rng) <= cap_for_attempt
    assert max(backoff_delay(8, 0.2, 3.0, rng) for _ in range(500)) > 2.5  # capped, not unbounded


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_circuit_opens_after_threshold_then_half_opens() -> None:
    now = FakeTime()
    cb = CircuitBreaker(failure_threshold=3, open_seconds=10, monotonic=now)
    assert [cb.record_failure() for _ in range(3)] == [False, False, True]
    assert cb.state is CircuitState.OPEN and not cb.allow()
    now.t = 10
    assert cb.state is CircuitState.HALF_OPEN
    assert cb.allow()  # one trial
    assert not cb.allow()  # only one in flight
    cb.record_success()
    assert cb.state is CircuitState.CLOSED and cb.allow()


def test_failed_trial_reopens() -> None:
    now = FakeTime()
    cb = CircuitBreaker(failure_threshold=1, open_seconds=5, monotonic=now)
    cb.record_failure()
    now.t = 5
    assert cb.allow()
    assert cb.record_failure() is True
    assert cb.state is CircuitState.OPEN


def test_success_resets_consecutive_count() -> None:
    cb = CircuitBreaker(failure_threshold=3, open_seconds=5)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED


def test_mock_lab_fault_mix_matches_spec() -> None:
    chaos = Chaos(failure_rate=0.2, seed=7)
    draws = [chaos.draw() for _ in range(100_000)]
    share = {f: draws.count(f) / len(draws) for f in Fault}
    assert abs(share[Fault.NONE] - 0.80) < 0.01
    assert abs(share[Fault.ERROR_BEFORE] - 0.08) < 0.01
    assert abs(share[Fault.HANG_BEFORE] - 0.06) < 0.01
    assert abs(share[Fault.LOST_AFTER] - 0.06) < 0.01
