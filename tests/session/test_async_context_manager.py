"""Tests for ``async with session:`` and ``session.wait_closed()``.

Peripheral-dx-onboarding.md calls for httpx-style async context
manager support so users who already own an asyncio loop don't have
to touch signal handling or shutdown order.
"""

from __future__ import annotations

import asyncio

import pytest

from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from tests.session.test_session import (
    FakeAgent,
    FakeSTT,
    FakeTransport,
    FakeTTS,
    FakeVAD,
)


def _config(**overrides) -> SessionConfig:
    defaults = dict(
        transport=FakeTransport(),
        vad=FakeVAD(),
        stt=FakeSTT(),
        agent=FakeAgent(),
        tts=FakeTTS(),
        noise_reducer=PassthroughNoiseReducer(),
        enable_noise_reduction=False,
    )
    defaults.update(overrides)
    return SessionConfig(**defaults)


@pytest.mark.asyncio
async def test_async_context_manager_starts_and_shuts_down():
    session = Session(_config())

    async with session:
        # ``__aenter__`` leaves the session in the running state.
        assert session.is_running is True
    # ``__aexit__`` runs shutdown(), which flips _closed.
    assert session._closed is True


@pytest.mark.asyncio
async def test_wait_closed_returns_immediately_when_already_closed():
    session = Session(_config())
    async with session:
        pass

    await asyncio.wait_for(session.wait_closed(), timeout=0.5)


@pytest.mark.asyncio
async def test_wait_closed_wakes_when_session_shuts_down():
    session = Session(_config())
    await session.start()

    waiter = asyncio.create_task(session.wait_closed())
    # Let the waiter register before we shut down.
    await asyncio.sleep(0)

    await session.shutdown()
    await asyncio.wait_for(waiter, timeout=0.5)
    assert waiter.done()


@pytest.mark.asyncio
async def test_aenter_is_noop_when_session_already_running():
    """Entering the context twice must not call ``start()`` a second time."""
    session = Session(_config())
    await session.start()
    assert session.is_running is True

    start_calls = 0
    original_start = session.start

    async def _counting_start() -> None:
        nonlocal start_calls
        start_calls += 1
        await original_start()

    session.start = _counting_start  # type: ignore[method-assign]
    try:
        async with session:
            assert session.is_running is True
    finally:
        # Restore before the test tears down so shutdown() uses the real path.
        session.start = original_start  # type: ignore[method-assign]

    assert start_calls == 0
    # __aexit__ always tears down, so the session should now be closed.
    assert session._closed is True


@pytest.mark.asyncio
async def test_aenter_is_noop_when_session_already_closed():
    """A session that has been shut down must not try to restart on re-entry."""
    session = Session(_config())
    await session.start()
    await session.shutdown()
    assert session._closed is True

    start_calls = 0

    async def _counting_start() -> None:
        nonlocal start_calls
        start_calls += 1

    session.start = _counting_start  # type: ignore[method-assign]

    async with session:
        pass

    assert start_calls == 0
