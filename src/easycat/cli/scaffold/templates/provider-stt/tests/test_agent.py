"""Offline regression tests for this project's turn pipeline."""

import asyncio

from easycat.debug.testing import (
    assert_latency,
    assert_no_error,
    assert_turn_completed,
    run_text_turn,
)


class StubAgent:
    async def run(self, text: str) -> str:
        return f"You said: {text}"


def test_turn_completes_cleanly() -> None:
    result = asyncio.run(run_text_turn(StubAgent(), "hello"))

    assert result.response == "You said: hello"
    assert_turn_completed(result, result.turn_id)
    assert_no_error(result, turn_id=result.turn_id)
    assert_latency(result, max_ms=5000.0, percentile="p95")
