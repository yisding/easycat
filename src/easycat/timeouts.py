"""Timeout wrappers for pipeline stages.

Provides configurable timeouts for STT response, agent run, and TTS
first-byte latency. Each timeout emits a typed error event and allows
session recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def resolve_provider_name(provider: Any, fallback: str) -> str:
    """Derive a provider's reported name from ``version_info()``.

    Every provider implements ``version_info()`` returning a ``provider``
    key. The base-class default is ``"unknown"``; treat that (and a
    missing key) as no signal and fall back to the generic stage label so
    timeout diagnostics name the real backend when it is known.
    """
    version_info = getattr(provider, "version_info", None)
    if callable(version_info):
        try:
            name = version_info().get("provider")
        except Exception:
            name = None
        if name and name != "unknown":
            return name
    return fallback


# ── Timeout error types ────────────────────────────────────────────


class STTTimeoutError(Exception):
    """STT provider did not produce a transcript within the timeout.

    Carries the stable ``code`` ``EASYCAT_E301`` so journal ``Error``
    records are machine-correlatable with ``easycat explain``.
    """

    code = "EASYCAT_E301"

    def __init__(self, provider_name: str, timeout: float) -> None:
        self.provider_name = provider_name
        self.timeout = timeout
        super().__init__(f"STT provider '{provider_name}' timed out after {timeout:.1f}s")


class AgentTimeoutError(Exception):
    """Agent did not respond within the timeout.

    Carries the stable ``code`` ``EASYCAT_E302`` so journal ``Error``
    records are machine-correlatable with ``easycat explain``.
    """

    code = "EASYCAT_E302"

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"Agent timed out after {timeout:.1f}s")


class TTSTimeoutError(Exception):
    """TTS provider did not produce audio within the timeout.

    Carries the stable ``code`` ``EASYCAT_E303`` so journal ``Error``
    records are machine-correlatable with ``easycat explain``.
    """

    code = "EASYCAT_E303"

    def __init__(self, provider_name: str, timeout: float) -> None:
        self.provider_name = provider_name
        self.timeout = timeout
        super().__init__(f"TTS provider '{provider_name}' timed out after {timeout:.1f}s")


# ── Timeout configuration ─────────────────────────────────────────


@dataclass
class TimeoutConfig:
    """Configurable timeout values for pipeline stages."""

    stt_timeout: float = 10.0  # seconds
    agent_timeout: float = 30.0  # seconds
    tts_first_byte_timeout: float = 5.0  # seconds

    def __post_init__(self) -> None:
        if self.stt_timeout <= 0:
            raise ValueError("stt_timeout must be positive")
        if self.agent_timeout <= 0:
            raise ValueError("agent_timeout must be positive")
        if self.tts_first_byte_timeout <= 0:
            raise ValueError("tts_first_byte_timeout must be positive")


# ── Timeout-guarded functions ──────────────────────────────────────


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
            from easycat.events import Error, ErrorStage

            await event_bus.emit(Error(exception=err, stage=ErrorStage.AGENT))
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
                    event = await asyncio.wait_for(events_iter.__anext__(), timeout=wait_time)
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
        # Deterministically finalize the wrapped source on consumer break
        # (barge-in / cancellation) or timeout, instead of leaving the
        # provider's underlying stream generator (WS/HTTP) to GC.
        aclose = getattr(events_iter, "aclose", None)
        if callable(aclose):
            with contextlib.suppress(Exception):
                await aclose()
        if timed_out:
            err = TTSTimeoutError(provider_name, timeout)
            logger.warning(str(err))
            if event_bus is not None:
                from easycat.events import Error, ErrorStage

                await event_bus.emit(
                    Error(exception=err, stage=ErrorStage.TTS, provider=provider_name)
                )
    if timed_out:
        raise TTSTimeoutError(provider_name, timeout)
