"""Contract tests for centralized lifecycle-budget defaults."""

from easycat.teardown_budgets import (
    AGENT_POST_DONE_STREAM_DRAIN_TIMEOUT_S,
    LLAMA_POST_CANCEL_AWAIT_TIMEOUT_S,
    REMOTE_RESPONSES_COMPLETED_STREAM_DRAIN_TIMEOUT_S,
)


def test_agent_lifecycle_budget_values_are_preserved() -> None:
    assert AGENT_POST_DONE_STREAM_DRAIN_TIMEOUT_S == 0.01
    assert LLAMA_POST_CANCEL_AWAIT_TIMEOUT_S == 2.0
    assert REMOTE_RESPONSES_COMPLETED_STREAM_DRAIN_TIMEOUT_S == 0.05
