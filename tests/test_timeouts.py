"""Tests for timeout wrappers."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from easycat.events import Error, ErrorStage, EventBus
from easycat.timeouts import (
    AgentTimeoutError,
    TimeoutConfig,
    TTSTimeoutError,
    resolve_provider_name,
    with_agent_timeout,
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

    @pytest.mark.parametrize(
        "field, value",
        [
            ("stt_timeout", 0.0),
            ("stt_timeout", -1.0),
            ("agent_timeout", 0.0),
            ("agent_timeout", -1.0),
            ("tts_first_byte_timeout", 0.0),
            ("tts_first_byte_timeout", -1.0),
        ],
    )
    def test_non_positive_timeout_rejected(self, field, value):
        with pytest.raises(ValueError, match=field):
            TimeoutConfig(**{field: value})


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
            await with_agent_timeout(hanging(), timeout=0.05, event_bus=event_bus)

        assert len(errors) == 1
        assert errors[0].stage == ErrorStage.AGENT


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
        assert errors[0].stage == ErrorStage.TTS

    async def test_breaking_out_closes_source_iterator(self):
        """Breaking out of the wrapper deterministically aclose()s the source."""
        finalized = asyncio.Event()

        async def source():
            try:
                yield b"chunk1"
                yield b"chunk2"
            finally:
                finalized.set()

        wrapper = with_tts_timeout(source(), timeout=1.0)
        # Consume under aclosing() — the deterministic contract production uses
        # (_tts_synthesizer.synthesize). Breaking the loop then closes the
        # wrapper, whose finally aclose()s the source rather than leaving it to
        # GC.
        async with contextlib.aclosing(wrapper):
            async for _ in wrapper:
                break

        assert finalized.is_set()

    async def test_error_event_carries_provider_name(self):
        event_bus = EventBus()
        errors: list[Error] = []
        event_bus.subscribe(Error, lambda e: errors.append(e))

        async def stalling():
            await asyncio.sleep(10)
            yield b"never"

        with pytest.raises(TTSTimeoutError):
            await _collect(
                with_tts_timeout(
                    stalling(),
                    timeout=0.05,
                    provider_name="elevenlabs",
                    event_bus=event_bus,
                )
            )

        assert errors[0].provider == "elevenlabs"


# ── resolve_provider_name ─────────────────────────────────────────


class TestResolveProviderName:
    def test_uses_version_info_provider_key(self):
        class Provider:
            def version_info(self):
                return {"provider": "deepgram"}

        assert resolve_provider_name(Provider(), "tts") == "deepgram"

    def test_falls_back_on_unknown(self):
        class Provider:
            def version_info(self):
                return {"provider": "unknown"}

        assert resolve_provider_name(Provider(), "tts") == "tts"

    def test_falls_back_on_missing_key(self):
        class Provider:
            def version_info(self):
                return {}

        assert resolve_provider_name(Provider(), "stt") == "stt"

    def test_falls_back_when_no_version_info(self):
        assert resolve_provider_name(object(), "tts") == "tts"

    def test_falls_back_when_version_info_raises(self):
        class Provider:
            def version_info(self):
                raise RuntimeError("boom")

        assert resolve_provider_name(Provider(), "stt") == "stt"
