"""Consecutive-failure circuit breaker (one per worker process, shared by its claim loops)."""

import time
from collections.abc import Callable
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self, failure_threshold: int, open_seconds: float, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self._monotonic = monotonic
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._monotonic() - self._opened_at >= self.open_seconds:
            self._state = CircuitState.HALF_OPEN
            self._trial_in_flight = False
        return self._state

    def allow(self) -> bool:
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.HALF_OPEN and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._trial_in_flight = False

    def record_failure(self) -> bool:
        """Returns True when this failure opened the circuit."""
        if self._state is CircuitState.HALF_OPEN:
            self._open()
            return True
        self._failures += 1
        if self._state is CircuitState.CLOSED and self._failures >= self.failure_threshold:
            self._open()
            return True
        return False

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._monotonic()
        self._trial_in_flight = False
