"""Retry strategy for failed outbound calls."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class RetryDecision(Enum):
    RETRY = "retry"
    NO_RETRY = "no_retry"
    SMS_FALLBACK = "sms_fallback"


@dataclass(frozen=True)
class SMSFallbackSuggested:
    """Emitted when SMS fallback is suggested after failed call attempts."""

    number: str
    attempts: int


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
        default_factory=lambda: frozenset(
            {
                "declined",
                "blocked_unwanted",
                "blocked_rejected",
            }
        )
    )
    shorter_delay_reasons: frozenset[str] = field(default_factory=lambda: frozenset({"busy"}))
    shorter_delay_s: float = 30.0


class RetryStrategy:
    """Determines whether and when to retry a failed outbound call."""

    def __init__(self, config: RetryStrategyConfig | None = None) -> None:
        self._config = config or RetryStrategyConfig()
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
        """Calculate the delay before retrying a call to this number."""
        state = self.get_state(number)
        reason = state.last_reason

        if reason in self._config.shorter_delay_reasons:
            return self._config.shorter_delay_s

        delay = self._config.base_delay_s * (self._config.backoff_factor ** (state.attempts - 1))
        return min(delay, self._config.max_delay_s)

    def reset(self, number: str) -> None:
        """Reset retry state for a number (e.g., after successful call)."""
        if number in self._states:
            del self._states[number]
