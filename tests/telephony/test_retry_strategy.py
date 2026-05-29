"""Tests for retry strategy."""

from __future__ import annotations

from easycat.telephony.retry import (
    RetryDecision,
    RetryStrategy,
    RetryStrategyConfig,
)


class TestRetryStrategy:
    def test_retry_on_no_answer(self) -> None:
        strategy = RetryStrategy()
        decision = strategy.record_attempt("+1555", "no-answer")
        assert decision == RetryDecision.RETRY

    def test_retry_on_busy(self) -> None:
        strategy = RetryStrategy()
        decision = strategy.record_attempt("+1555", "busy")
        assert decision == RetryDecision.RETRY

    def test_no_retry_on_declined(self) -> None:
        strategy = RetryStrategy()
        decision = strategy.record_attempt("+1555", "declined")
        assert decision == RetryDecision.NO_RETRY

    def test_no_retry_on_blocked(self) -> None:
        strategy = RetryStrategy()
        decision = strategy.record_attempt("+1555", "blocked_unwanted")
        assert decision == RetryDecision.NO_RETRY
        state = strategy.get_state("+1555")
        assert state.exhausted

    def test_max_retries_enforced(self) -> None:
        cfg = RetryStrategyConfig(max_retries=3, sms_fallback_after=5)
        strategy = RetryStrategy(cfg)
        strategy.record_attempt("+1555", "no-answer")
        strategy.record_attempt("+1555", "no-answer")
        decision = strategy.record_attempt("+1555", "no-answer")
        assert decision == RetryDecision.NO_RETRY
        state = strategy.get_state("+1555")
        assert state.exhausted

    def test_different_time_retry(self) -> None:
        strategy = RetryStrategy()
        strategy.record_attempt("+1555", "no-answer")
        delay = strategy.get_delay("+1555")
        assert delay > 0

    def test_busy_shorter_delay(self) -> None:
        # Disable jitter so the shorter delay is exact.
        strategy = RetryStrategy(RetryStrategyConfig(shorter_delay_s=15.0, jitter_fraction=0.0))
        strategy.record_attempt("+1555", "busy")
        delay = strategy.get_delay("+1555")
        assert delay == 15.0

    def test_busy_shorter_delay_jittered(self) -> None:
        strategy = RetryStrategy(RetryStrategyConfig(shorter_delay_s=20.0, jitter_fraction=0.5))
        strategy.record_attempt("+1555", "busy")
        for _ in range(50):
            delay = strategy.get_delay("+1555")
            assert 10.0 <= delay <= 20.0

    def test_sms_fallback_option(self) -> None:
        cfg = RetryStrategyConfig(max_retries=5, sms_fallback_after=2)
        strategy = RetryStrategy(cfg)
        strategy.record_attempt("+1555", "no-answer")
        decision = strategy.record_attempt("+1555", "no-answer")
        assert decision == RetryDecision.SMS_FALLBACK

    def test_retry_state_persisted(self) -> None:
        strategy = RetryStrategy()
        strategy.record_attempt("+1555", "no-answer")
        state = strategy.get_state("+1555")
        assert state.attempts == 1
        assert state.last_reason == "no-answer"
        strategy.record_attempt("+1555", "busy")
        assert state.attempts == 2
        assert state.last_reason == "busy"

    def test_reset_clears_state(self) -> None:
        strategy = RetryStrategy()
        strategy.record_attempt("+1555", "no-answer")
        strategy.reset("+1555")
        state = strategy.get_state("+1555")
        assert state.attempts == 0

    def test_exponential_backoff(self) -> None:
        # Disable jitter so the deterministic backoff growth is exact.
        cfg = RetryStrategyConfig(
            base_delay_s=10.0, backoff_factor=2.0, max_delay_s=1000.0, jitter_fraction=0.0
        )
        strategy = RetryStrategy(cfg)
        strategy.record_attempt("+1555", "no-answer")
        d1 = strategy.get_delay("+1555")
        strategy.record_attempt("+1555", "no-answer")
        d2 = strategy.get_delay("+1555")
        assert d1 == 10.0
        assert d2 == 20.0

    def test_backoff_jitter_within_bounds(self) -> None:
        cfg = RetryStrategyConfig(
            base_delay_s=100.0, backoff_factor=2.0, max_delay_s=10_000.0, jitter_fraction=0.5
        )
        strategy = RetryStrategy(cfg)
        strategy.record_attempt("+1555", "no-answer")
        delays = {strategy.get_delay("+1555") for _ in range(50)}
        # Equal jitter keeps the delay in [base/2, base] of the deterministic value.
        for delay in delays:
            assert 50.0 <= delay <= 100.0
        # Jitter must actually randomize (not always the same value).
        assert len(delays) > 1
