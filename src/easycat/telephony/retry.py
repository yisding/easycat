"""Retry strategy for failed outbound calls."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum

from easycat._numeric import is_finite_number
from easycat.telephony.outbound import BLOCK_REASONS


class RetryDecision(Enum):
    RETRY = "retry"
    NO_RETRY = "no_retry"
    SMS_FALLBACK = "sms_fallback"


@dataclass
class RetryState:
    """Tracks retry state for a single destination number."""

    number: str
    attempts: int = 0
    last_attempt_time: float = 0.0
    last_reason: str = ""
    exhausted: bool = False


@dataclass
class RetryStrategyConfig:
    """Configuration for retry strategy."""

    max_retries: int = 3
    base_delay_s: float = 60.0
    max_delay_s: float = 3600.0
    backoff_factor: float = 2.0
    sms_fallback_after: int = 2
    no_retry_reasons: frozenset[str] = field(
        default_factory=lambda: BLOCK_REASONS | frozenset({"declined"})
    )
    shorter_delay_reasons: frozenset[str] = field(default_factory=lambda: frozenset({"busy"}))
    shorter_delay_s: float = 30.0
    # Fraction of the computed delay that is randomized to avoid synchronized
    # retry storms across many numbers (thundering herd). 0.0 disables jitter.
    # Equal jitter: the returned delay falls in
    # [delay * (1 - jitter_fraction), delay].
    jitter_fraction: float = 0.5

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Revalidate mutable retry policy before it becomes runtime state."""
        _require_positive_integer("max_retries", self.max_retries)
        _require_positive_integer("sms_fallback_after", self.sms_fallback_after)
        _require_non_negative_number("base_delay_s", self.base_delay_s)
        _require_non_negative_number("max_delay_s", self.max_delay_s)
        _require_positive_number("backoff_factor", self.backoff_factor)
        _require_non_negative_number("shorter_delay_s", self.shorter_delay_s)
        if not is_finite_number(self.jitter_fraction) or not 0 <= self.jitter_fraction <= 1:
            raise ValueError("jitter_fraction must be a finite number between 0 and 1")
        _require_reason_set("no_retry_reasons", self.no_retry_reasons)
        _require_reason_set("shorter_delay_reasons", self.shorter_delay_reasons)


class RetryStrategy:
    """Determines whether and when to retry a failed outbound call."""

    def __init__(self, config: RetryStrategyConfig | None = None) -> None:
        self._config = config or RetryStrategyConfig()
        self._config.validate()
        self._states: dict[str, RetryState] = {}

    def get_state(self, number: str) -> RetryState:
        if number not in self._states:
            self._states[number] = RetryState(number=number)
        return self._states[number]

    def record_attempt(self, number: str, reason: str) -> RetryDecision:
        """Record a failed call attempt and return the retry decision.

        Returns:
            ``NO_RETRY`` for permanently non-retryable reasons.
            ``SMS_FALLBACK`` once ``sms_fallback_after`` attempts are reached
            (check ``state.exhausted`` to distinguish "retry with SMS" from
            "exhausted, send SMS instead").
            ``RETRY`` when a retry is allowed.
        """
        state = self.get_state(number)
        state.attempts += 1
        state.last_attempt_time = time.monotonic()
        state.last_reason = reason

        # No retry for certain reasons.
        if reason in self._config.no_retry_reasons:
            state.exhausted = True
            return RetryDecision.NO_RETRY

        # Check max retries — mark exhausted before SMS fallback check.
        if state.attempts >= self._config.max_retries:
            state.exhausted = True
            if self._config.sms_fallback_after <= self._config.max_retries:
                return RetryDecision.SMS_FALLBACK
            return RetryDecision.NO_RETRY

        # Suggest SMS fallback after threshold (still retryable).
        if state.attempts >= self._config.sms_fallback_after:
            return RetryDecision.SMS_FALLBACK

        return RetryDecision.RETRY

    def get_delay(self, number: str) -> float:
        """Calculate the delay before retrying a call to this number.

        Randomized jitter (see ``RetryStrategyConfig.jitter_fraction``) is
        applied so that a batch of numbers failing simultaneously does not
        retry in lockstep and recreate the load spike that caused the failures.
        """
        state = self.get_state(number)
        reason = state.last_reason

        if reason in self._config.shorter_delay_reasons:
            return self._apply_jitter(self._config.shorter_delay_s)

        try:
            delay = self._config.base_delay_s * (
                self._config.backoff_factor ** (state.attempts - 1)
            )
        except OverflowError:
            delay = self._config.max_delay_s
        if not math.isfinite(delay):
            delay = self._config.max_delay_s
        delay = min(delay, self._config.max_delay_s)
        return self._apply_jitter(delay)

    def _apply_jitter(self, delay: float) -> float:
        """Apply equal jitter to a computed delay.

        Returns a value in ``[delay * (1 - jitter_fraction), delay]``. With the
        default ``jitter_fraction`` of 0.5 this is the classic "equal jitter"
        strategy (``delay/2 + random.uniform(0, delay/2)``).
        """
        fraction = self._config.jitter_fraction
        if fraction <= 0.0 or delay <= 0.0:
            return delay
        fraction = min(fraction, 1.0)
        jitter_range = delay * fraction
        return delay - jitter_range + random.uniform(0.0, jitter_range)

    def reset(self, number: str) -> None:
        """Reset retry state for a number (e.g., after successful call)."""
        if number in self._states:
            del self._states[number]


def _require_positive_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _require_non_negative_number(name: str, value: object) -> None:
    if not is_finite_number(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0")


def _require_positive_number(name: str, value: object) -> None:
    if not is_finite_number(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")


def _require_reason_set(name: str, value: object) -> None:
    if not isinstance(value, frozenset) or not all(
        isinstance(item, str) and bool(item) for item in value
    ):
        raise ValueError(f"{name} must be a frozenset of non-empty strings")
