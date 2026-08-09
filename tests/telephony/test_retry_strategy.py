"""Tests for retry strategy."""

from __future__ import annotations

import pytest

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

    def test_backoff_overflow_caps_at_max_delay(self) -> None:
        config = RetryStrategyConfig(
            max_retries=5,
            sms_fallback_after=5,
            base_delay_s=1.0,
            backoff_factor=1e308,
            max_delay_s=10.0,
            jitter_fraction=0.0,
        )
        strategy = RetryStrategy(config)
        for _ in range(3):
            strategy.record_attempt("+1555", "other")

        assert strategy.get_delay("+1555") == 10.0

    def test_zero_base_delay_before_first_attempt_returns_zero(self) -> None:
        strategy = RetryStrategy(RetryStrategyConfig(base_delay_s=0.0, jitter_fraction=0.0))

        assert strategy.get_delay("+1555") == 0.0


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("max_retries", 0, "max_retries"),
        ("max_retries", True, "max_retries"),
        ("sms_fallback_after", 0, "sms_fallback_after"),
        ("base_delay_s", float("nan"), "base_delay_s"),
        ("max_delay_s", float("inf"), "max_delay_s"),
        ("backoff_factor", 0.0, "backoff_factor"),
        ("shorter_delay_s", -1.0, "shorter_delay_s"),
        ("jitter_fraction", -0.1, "jitter_fraction"),
        ("jitter_fraction", 1.1, "jitter_fraction"),
        ("jitter_fraction", True, "jitter_fraction"),
        ("no_retry_reasons", {"declined"}, "no_retry_reasons"),
        ("no_retry_reasons", frozenset({1}), "no_retry_reasons"),
        ("shorter_delay_reasons", frozenset({""}), "shorter_delay_reasons"),
    ],
)
def test_retry_config_rejects_invalid_policy(
    field_name: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        RetryStrategyConfig(**{field_name: value})  # type: ignore[arg-type]


def test_retry_strategy_revalidates_mutated_config() -> None:
    config = RetryStrategyConfig()
    config.base_delay_s = float("nan")

    with pytest.raises(ValueError, match="base_delay_s"):
        RetryStrategy(config)
