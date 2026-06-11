"""Offline regression tests for this project's turn pipeline.

No API keys or network needed: a deterministic stub agent stands in
for the LLM while EasyCat's real turn machinery (the send_text path,
journal, and latency metrics) runs end to end. Swap in your real agent
when you want live evals — see AGENTS.md for the full eval ladder.

Run with: uv run pytest
"""

import asyncio

from easycat.debug.testing import (
    assert_latency,
    assert_no_error,
    assert_turn_completed,
    run_text_turn,
)


class StubAgent:
    """Deterministic stand-in for the LLM-backed agent."""

    async def run(self, text: str) -> str:
        return f"You said: {text}"


def test_turn_completes_cleanly() -> None:
    result = asyncio.run(run_text_turn(StubAgent(), "hello"))

    assert result.response == "You said: hello"
    assert_turn_completed(result, result.turn_id)
    assert_no_error(result, turn_id=result.turn_id)


def test_turn_latency_within_budget() -> None:
    result = asyncio.run(run_text_turn(StubAgent(), "ping"))

    # The stub answers instantly; 5 s catches pipeline hangs without
    # flaking on slow CI machines.
    assert_latency(result, max_ms=5000.0, percentile="p95")
