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

from easycat._numeric import is_finite_number
from easycat.errors import EasyCatError
from easycat.events import TTSEventType

logger = logging.getLogger(__name__)


class _TTSFirstByteDeadlineExceeded(TimeoutError):
    """Private marker for expiry of this wrapper's first-byte deadline."""


def _finite_positive_timeout(value: Any, *, name: str) -> float:
    """Validate one public timeout boundary without accepting booleans."""
    if not is_finite_number(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


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
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            name = None
        if name and name != "unknown":
            return name
    return fallback


# ── Timeout error types ────────────────────────────────────────────


class STTTimeoutError(EasyCatError):
    """STT provider did not produce a transcript within the timeout.

    Carries the stable ``code`` ``EASYCAT_E301`` so journal ``Error``
    records are machine-correlatable with ``easycat explain``.
    """

    code = "EASYCAT_E301"

    def __init__(self, provider_name: str, timeout: float) -> None:
        self.provider_name = provider_name
        self.timeout = timeout
        super().__init__(
            self.code,
            f"STT provider '{provider_name}' timed out after {timeout:.1f}s",
            provider=provider_name,
            timeout=timeout,
        )


class AgentTimeoutError(EasyCatError):
    """Agent did not respond within the timeout.

    Carries the stable ``code`` ``EASYCAT_E302`` so journal ``Error``
    records are machine-correlatable with ``easycat explain``.
    """

    code = "EASYCAT_E302"

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(self.code, f"Agent timed out after {timeout:.1f}s", timeout=timeout)


class TTSTimeoutError(EasyCatError):
    """TTS provider did not produce audio within the timeout.

    Carries the stable ``code`` ``EASYCAT_E303`` so journal ``Error``
    records are machine-correlatable with ``easycat explain``.
    """

    code = "EASYCAT_E303"

    def __init__(self, provider_name: str, timeout: float) -> None:
        self.provider_name = provider_name
        self.timeout = timeout
        super().__init__(
            self.code,
            f"TTS provider '{provider_name}' timed out after {timeout:.1f}s",
            provider=provider_name,
            timeout=timeout,
        )


# ── Timeout configuration ─────────────────────────────────────────


@dataclass
class TimeoutConfig:
    """Configurable timeout values for pipeline stages."""

    stt_timeout: float = 10.0  # seconds
    agent_timeout: float = 30.0  # seconds
    tts_first_byte_timeout: float = 5.0  # seconds

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Revalidate mutable timeout policy at a runtime build boundary."""
        self.stt_timeout = _finite_positive_timeout(self.stt_timeout, name="stt_timeout")
        self.agent_timeout = _finite_positive_timeout(self.agent_timeout, name="agent_timeout")
        self.tts_first_byte_timeout = _finite_positive_timeout(
            self.tts_first_byte_timeout,
            name="tts_first_byte_timeout",
        )


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
    timeout = _finite_positive_timeout(timeout, name="timeout")
    timeout_context = asyncio.timeout(timeout)
    try:
        async with timeout_context:
            return await coro
    except TimeoutError:
        # A provider can raise its own ``TimeoutError`` before this wrapper's
        # deadline. Preserve that failure rather than reporting a false
        # EasyCat agent-deadline breach (and emitting a misleading Error).
        if not timeout_context.expired():
            raise
        err = AgentTimeoutError(timeout)
        logger.warning(str(err))
        if event_bus is not None:
            from easycat.events import Error, ErrorStage

            await event_bus.emit(Error(exception=err, stage=ErrorStage.AGENT))
        raise err from None


async def _next_before_deadline(events_iter: AsyncIterator[Any], deadline: float) -> Any:
    """Read one event without extending an absolute event-loop deadline."""
    if deadline <= asyncio.get_running_loop().time():
        raise _TTSFirstByteDeadlineExceeded
    timeout_context = asyncio.timeout_at(deadline)
    try:
        async with timeout_context:
            return await events_iter.__anext__()
    except TimeoutError:
        # Do not translate a provider-owned TimeoutError into a first-byte
        # timeout. ``asyncio.Timeout.expired`` tells us whether this scope
        # actually delivered the cancellation that became TimeoutError.
        if timeout_context.expired():
            raise _TTSFirstByteDeadlineExceeded from None
        raise


async def with_tts_timeout(
    events_iter: AsyncIterator[Any],
    *,
    timeout: float,
    provider_name: str = "tts",
    event_bus: Any | None = None,
) -> AsyncIterator[Any]:
    """Wrap a TTS events iterator with a first-byte timeout.

    The timeout applies until the first non-empty audio event. Marker and
    empty-audio events are still yielded, but they do not disarm or reset the
    original deadline. Generic iterators that do not yield ``TTSEvent``-like
    values retain the historical behavior where their first item satisfies
    the timeout.
    """
    timeout = _finite_positive_timeout(timeout, name="timeout")
    first_audio_received = False
    timed_out = False
    first_audio_deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            try:
                event = await (
                    events_iter.__anext__()
                    if first_audio_received
                    else _next_before_deadline(events_iter, first_audio_deadline)
                )
                first_audio_received = first_audio_received or _is_tts_first_byte(event)
                yield event
            except StopAsyncIteration:
                return
            except _TTSFirstByteDeadlineExceeded:
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


def _is_tts_first_byte(event: Any) -> bool:
    """Whether *event* satisfies a TTS first-audio-byte deadline."""
    event_type = getattr(event, "type", None)
    if event_type is TTSEventType.MARKERS:
        return False
    if event_type is TTSEventType.AUDIO:
        audio = getattr(event, "audio", None)
        return bool(getattr(audio, "data", b""))
    # ``with_tts_timeout`` predates typed TTSEvents and remains useful for
    # generic async iterators in callers/tests. Preserve that first-item
    # behavior for values outside the provider event contract.
    return True
