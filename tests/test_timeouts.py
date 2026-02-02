"""Tests for timeout wrappers (WS8 Tasks 8.3–8.5)."""

from __future__ import annotations

import asyncio

import pytest

from easycat.events import Error, EventBus
from easycat.timeouts import (
    AgentTimeoutError,
    STTTimeoutError,
    TimeoutConfig,
    TTSTimeoutError,
    with_agent_timeout,
    with_stt_timeout,
    with_tts_timeout,
)

# ── Helpers ────────────────────────────────────────────────────────


async def _slow_iter(items, delay=0.0, first_delay=0.0):
    """Async iterator that yields items with optional delays."""
    for i, item in enumerate(items):
        d = first_delay if i == 0 else delay
        if d:
            await asyncio.sleep(d)
        yield item


async def _collect(ait):
    """Collect all items from an async iterator."""
    result = []
    async for item in ait:
        result.append(item)
    return result


# ── TimeoutConfig ──────────────────────────────────────────────────


class TestTimeoutConfig:
    def test_defaults(self):
        cfg = TimeoutConfig()
        assert cfg.stt_timeout == 10.0
        assert cfg.agent_timeout == 30.0
        assert cfg.tts_first_byte_timeout == 5.0

    def test_custom(self):
        cfg = TimeoutConfig(stt_timeout=5.0, agent_timeout=15.0, tts_first_byte_timeout=3.0)
        assert cfg.stt_timeout == 5.0


# ── STT timeout (Task 8.3) ────────────────────────────────────────


class TestSTTTimeout:
    async def test_no_timeout_when_events_arrive(self):
        events = _slow_iter(["partial", "final"], delay=0.0)
        result = await _collect(with_stt_timeout(events, timeout=1.0))
        assert result == ["partial", "final"]

    async def test_timeout_fires_when_stt_stalls(self):
        async def stalling_iter():
            yield "partial"
            await asyncio.sleep(10)  # stall
            yield "final"

        with pytest.raises(STTTimeoutError) as exc_info:
            await _collect(with_stt_timeout(stalling_iter(), timeout=0.05))

        assert "stt" in str(exc_info.value).lower()
        assert exc_info.value.timeout == 0.05

    async def test_timeout_emits_error_event(self):
        event_bus = EventBus()
        errors = []

        async def handler(event):
            errors.append(event)

        event_bus.subscribe(Error, handler)

        async def stalling():
            await asyncio.sleep(10)
            yield "never"

        with pytest.raises(STTTimeoutError):
            await _collect(
                with_stt_timeout(
                    stalling(),
                    timeout=0.05,
                    provider_name="deepgram",
                    event_bus=event_bus,
                )
            )

        assert len(errors) == 1
        assert "stt_timeout" in errors[0].context
        assert isinstance(errors[0].exception, STTTimeoutError)

    async def test_provider_name_in_error(self):
        async def stalling():
            await asyncio.sleep(10)
            yield "never"

        with pytest.raises(STTTimeoutError) as exc_info:
            await _collect(
                with_stt_timeout(stalling(), timeout=0.05, provider_name="elevenlabs")
            )

        assert exc_info.value.provider_name == "elevenlabs"


# ── Agent timeout (Task 8.4) ──────────────────────────────────────


class TestAgentTimeout:
    async def test_no_timeout_when_agent_responds(self):
        async def fast_agent():
            return "response"

        result = await with_agent_timeout(fast_agent(), timeout=1.0)
        assert result == "response"

    async def test_timeout_fires_when_agent_hangs(self):
        async def hanging_agent():
            await asyncio.sleep(10)
            return "never"

        with pytest.raises(AgentTimeoutError) as exc_info:
            await with_agent_timeout(hanging_agent(), timeout=0.05)

        assert exc_info.value.timeout == 0.05

    async def test_timeout_emits_error_event(self):
        event_bus = EventBus()
        errors = []

        async def handler(event):
            errors.append(event)

        event_bus.subscribe(Error, handler)

        async def hanging():
            await asyncio.sleep(10)
            return "never"

        with pytest.raises(AgentTimeoutError):
            await with_agent_timeout(
                hanging(), timeout=0.05, event_bus=event_bus
            )

        assert len(errors) == 1
        assert "agent_timeout" in errors[0].context


# ── TTS first-byte timeout (Task 8.5) ─────────────────────────────


class TestTTSTimeout:
    async def test_no_timeout_when_audio_arrives(self):
        events = _slow_iter([b"chunk1", b"chunk2"], delay=0.0)
        result = await _collect(with_tts_timeout(events, timeout=1.0))
        assert result == [b"chunk1", b"chunk2"]

    async def test_timeout_fires_when_first_byte_delayed(self):
        async def slow_first_byte():
            await asyncio.sleep(10)
            yield b"chunk1"

        with pytest.raises(TTSTimeoutError) as exc_info:
            await _collect(with_tts_timeout(slow_first_byte(), timeout=0.05))

        assert exc_info.value.timeout == 0.05

    async def test_no_timeout_after_first_byte(self):
        """After the first byte arrives, subsequent delays should not trigger timeout."""

        async def slow_subsequent():
            yield b"chunk1"
            await asyncio.sleep(0.15)
            yield b"chunk2"

        # First-byte timeout is 0.1s, but second chunk is 0.15s later — should not fail
        result = await _collect(with_tts_timeout(slow_subsequent(), timeout=0.1))
        assert result == [b"chunk1", b"chunk2"]

    async def test_timeout_emits_error_event(self):
        event_bus = EventBus()
        errors = []

        async def handler(event):
            errors.append(event)

        event_bus.subscribe(Error, handler)

        async def stalling():
            await asyncio.sleep(10)
            yield b"never"

        with pytest.raises(TTSTimeoutError):
            await _collect(
                with_tts_timeout(
                    stalling(),
                    timeout=0.05,
                    provider_name="openai",
                    event_bus=event_bus,
                )
            )

        assert len(errors) == 1
        assert "tts_timeout" in errors[0].context
