"""Timeout wrappers for pipeline stages (WS8 Tasks 8.3–8.5).

Provides configurable timeouts for STT response, agent run, and TTS
first-byte latency. Each timeout emits a typed error event and allows
session recovery.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── Timeout error types ────────────────────────────────────────────


class STTTimeoutError(Exception):
    """STT provider did not produce a transcript within the timeout."""

    def __init__(self, provider_name: str, timeout: float) -> None:
        self.provider_name = provider_name
        self.timeout = timeout
        super().__init__(
            f"STT provider '{provider_name}' timed out after {timeout:.1f}s"
        )


class AgentTimeoutError(Exception):
    """Agent did not respond within the timeout."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"Agent timed out after {timeout:.1f}s")


class TTSTimeoutError(Exception):
    """TTS provider did not produce audio within the timeout."""

    def __init__(self, provider_name: str, timeout: float) -> None:
        self.provider_name = provider_name
        self.timeout = timeout
        super().__init__(
            f"TTS provider '{provider_name}' timed out after {timeout:.1f}s"
        )


# ── Timeout configuration ─────────────────────────────────────────


@dataclass
class TimeoutConfig:
    """Configurable timeout values for pipeline stages."""

    stt_timeout: float = 10.0  # seconds
    agent_timeout: float = 30.0  # seconds
    tts_first_byte_timeout: float = 5.0  # seconds


# ── Timeout-guarded functions ──────────────────────────────────────


async def with_stt_timeout(
    events_iter: AsyncIterator[Any],
    *,
    timeout: float,
    provider_name: str = "stt",
    event_bus: Any | None = None,
) -> AsyncIterator[Any]:
    """Wrap an STT events iterator with a timeout.

    If no event is received within `timeout` seconds, emits an error
    event and raises STTTimeoutError.
    """
    timed_out = False
    try:
        while True:
            try:
                event = await asyncio.wait_for(events_iter.__anext__(), timeout=timeout)
                yield event
            except StopAsyncIteration:
                return
            except TimeoutError:
                timed_out = True
                break
    finally:
        if timed_out:
            err = STTTimeoutError(provider_name, timeout)
            logger.warning(str(err))
            if event_bus is not None:
                from easycat.events import Error

                await event_bus.emit(
                    Error(exception=err, context=f"stt_timeout:{provider_name}")
                )
            raise err


async def with_agent_timeout(
    coro: Any,
    *,
    timeout: float,
    event_bus: Any | None = None,
) -> Any:
    """Run an agent coroutine with a timeout.

    If the agent doesn't respond within `timeout` seconds, emits an
    error event and raises AgentTimeoutError.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        err = AgentTimeoutError(timeout)
        logger.warning(str(err))
        if event_bus is not None:
            from easycat.events import Error

            await event_bus.emit(Error(exception=err, context="agent_timeout"))
        raise err from None


async def with_tts_timeout(
    events_iter: AsyncIterator[Any],
    *,
    timeout: float,
    provider_name: str = "tts",
    event_bus: Any | None = None,
) -> AsyncIterator[Any]:
    """Wrap a TTS events iterator with a first-byte timeout.

    The timeout only applies to the first event. After the first event
    is received, subsequent events are yielded without a timeout.
    """
    first_received = False
    timed_out = False
    try:
        while True:
            try:
                wait_time = timeout if not first_received else None
                if wait_time is not None:
                    event = await asyncio.wait_for(
                        events_iter.__anext__(), timeout=wait_time
                    )
                else:
                    event = await events_iter.__anext__()
                first_received = True
                yield event
            except StopAsyncIteration:
                return
            except TimeoutError:
                timed_out = True
                break
    finally:
        if timed_out:
            err = TTSTimeoutError(provider_name, timeout)
            logger.warning(str(err))
            if event_bus is not None:
                from easycat.events import Error

                await event_bus.emit(
                    Error(exception=err, context=f"tts_timeout:{provider_name}")
                )
            raise err
