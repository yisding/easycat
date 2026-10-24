"""Backoff delay bounds for ReconnectingWebSocket."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket


async def test_jittered_backoff_delay_never_exceeds_max_delay() -> None:
    """``max_delay`` should bound the wait between attempts, jitter included."""
    config = ReconnectConfig(
        max_retries=3,
        base_delay=10.0,
        max_delay=10.0,
        backoff_factor=2.0,
        jitter_factor=0.5,
    )

    async def failing_connect(*_args: object, **_kwargs: object) -> object:
        raise OSError("refused")

    ws = ReconnectingWebSocket("ws://example.invalid", config, connect_fn=failing_connect)
    delays: list[float] = []

    async def record_backoff(delay: float) -> None:
        delays.append(delay)

    # random() == 1.0 yields the maximum jitter multiplier (1 + jitter_factor).
    with (
        patch("easycat.reconnecting_ws.random.random", return_value=1.0),
        patch.object(ws, "_backoff_or_close", record_backoff),
        pytest.raises(ConnectionError),
    ):
        await ws.connect()

    assert delays, "expected at least one backoff wait"
    assert max(delays) <= config.max_delay
