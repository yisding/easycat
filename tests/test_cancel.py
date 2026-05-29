"""Tests for the cooperative CancelToken."""

from __future__ import annotations

import asyncio
import threading

from easycat.cancel import CancelToken


async def test_cancel_sets_flag_and_timestamp() -> None:
    token = CancelToken()
    assert token.is_cancelled is False
    assert token.cancelled_at is None

    token.cancel()

    assert token.is_cancelled is True
    assert token.cancelled_at is not None


async def test_cancel_is_idempotent_timestamp() -> None:
    token = CancelToken()
    token.cancel()
    first = token.cancelled_at
    token.cancel()
    assert token.cancelled_at == first


async def test_wait_returns_after_cancel() -> None:
    token = CancelToken()
    asyncio.get_running_loop().call_soon(token.cancel)
    await asyncio.wait_for(token.wait(), timeout=1.0)
    assert token.is_cancelled


async def test_cancel_from_another_thread_wakes_waiter() -> None:
    """cancel() from a non-loop thread must wake an awaiting wait()."""
    token = CancelToken()

    def worker() -> None:
        token.cancel()

    async def wait_then_assert() -> None:
        waiter = asyncio.ensure_future(token.wait())
        # Yield so the waiter actually begins awaiting on the loop.
        await asyncio.sleep(0)
        threading.Thread(target=worker).start()
        await asyncio.wait_for(waiter, timeout=2.0)

    await wait_then_assert()
    assert token.is_cancelled


def test_cancel_without_running_loop() -> None:
    """Constructing and cancelling outside any loop must still work."""
    token = CancelToken()
    token.cancel()
    assert token.is_cancelled

    async def confirm() -> None:
        await asyncio.wait_for(token.wait(), timeout=1.0)

    asyncio.run(confirm())


def test_does_not_touch_event_private_loop() -> None:
    """Regression: cancel() must not depend on asyncio.Event._loop."""
    import easycat.cancel as cancel_mod

    src = cancel_mod.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "_event._loop" not in text
