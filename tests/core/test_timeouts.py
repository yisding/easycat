"""Tests for timeout wrappers."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import Error, ErrorStage, EventBus, TTSEvent, TTSEventType
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


async def _wait_forever() -> None:
    """Wait until cancelled by the timeout under test."""
    await asyncio.Event().wait()


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
        "value",
        [
            pytest.param(True, id="true"),
            pytest.param(False, id="false"),
            pytest.param("1", id="string"),
            pytest.param(None, id="none"),
            pytest.param(object(), id="object"),
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="positive-infinity"),
            pytest.param(float("-inf"), id="negative-infinity"),
            pytest.param(10**1000, id="integer-too-large-for-float"),
            pytest.param(0.0, id="zero"),
            pytest.param(-1.0, id="negative"),
        ],
    )
    @pytest.mark.parametrize(
        "field",
        ["stt_timeout", "agent_timeout", "tts_first_byte_timeout"],
    )
    def test_invalid_timeout_rejected(self, field, value):
        with pytest.raises(ValueError, match=rf"{field} must be a finite positive number"):
            TimeoutConfig(**{field: value})

    @pytest.mark.parametrize(
        "field",
        ["stt_timeout", "agent_timeout", "tts_first_byte_timeout"],
    )
    def test_validate_rejects_post_construction_mutation(self, field: str) -> None:
        cfg = TimeoutConfig()
        setattr(cfg, field, 0.0)

        with pytest.raises(ValueError, match=rf"{field} must be a finite positive number"):
            cfg.validate()


# ── Agent timeout (Task 8.4) ──────────────────────────────────────


class TestAgentTimeout:
    @pytest.mark.parametrize(
        "timeout",
        [True, "1", None, float("nan"), float("inf"), 10**1000, 0.0, -1.0],
    )
    async def test_invalid_timeout_rejected_at_public_boundary(self, timeout):
        with pytest.raises(ValueError, match="timeout must be a finite positive number"):
            await with_agent_timeout(object(), timeout=timeout)

    async def test_no_timeout_when_agent_responds(self):
        async def fast_agent():
            return "response"

        result = await with_agent_timeout(fast_agent(), timeout=1.0)
        assert result == "response"

    async def test_agent_runs_in_caller_task(self):
        caller_task = asyncio.current_task()
        agent_task = None

        async def fast_agent():
            nonlocal agent_task
            agent_task = asyncio.current_task()
            return "response"

        await with_agent_timeout(fast_agent(), timeout=1.0)

        assert agent_task is caller_task

    async def test_timeout_fires_when_agent_hangs(self):
        async def hanging_agent():
            await _wait_forever()
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
            await _wait_forever()
            return "never"

        with pytest.raises(AgentTimeoutError):
            await with_agent_timeout(hanging(), timeout=0.05, event_bus=event_bus)

        assert len(errors) == 1
        assert errors[0].stage == ErrorStage.AGENT

    async def test_provider_timeout_error_is_not_relabelled_as_agent_deadline(self):
        """Only this wrapper's elapsed deadline becomes ``AgentTimeoutError``."""
        provider_error = TimeoutError("provider request deadline")
        event_bus = EventBus()
        errors: list[Error] = []
        event_bus.subscribe(Error, errors.append)

        async def provider() -> None:
            raise provider_error

        with pytest.raises(TimeoutError) as exc_info:
            await with_agent_timeout(provider(), timeout=1.0, event_bus=event_bus)

        assert exc_info.value is provider_error
        assert errors == []


# ── TTS first-byte timeout (Task 8.5) ─────────────────────────────


class TestTTSTimeout:
    @pytest.mark.parametrize(
        "timeout",
        [True, "1", None, float("nan"), float("inf"), 0.0, -1.0],
    )
    async def test_invalid_timeout_rejected_at_public_boundary(self, timeout):
        with pytest.raises(ValueError, match="timeout must be a finite positive number"):
            await _collect(with_tts_timeout(_slow_iter([]), timeout=timeout))

    async def test_no_timeout_when_audio_arrives(self):
        events = _slow_iter([b"chunk1", b"chunk2"], delay=0.0)
        result = await _collect(with_tts_timeout(events, timeout=1.0))
        assert result == [b"chunk1", b"chunk2"]

    async def test_timeout_fires_when_first_byte_delayed(self):
        async def slow_first_byte():
            await _wait_forever()
            yield b"chunk1"

        with pytest.raises(TTSTimeoutError) as exc_info:
            await _collect(with_tts_timeout(slow_first_byte(), timeout=0.05))

        assert exc_info.value.timeout == 0.05

    async def test_no_timeout_after_first_byte(self):
        """After the first byte arrives, a slow later event remains eligible."""

        async def source():
            yield b"chunk1"
            await asyncio.sleep(0.02)
            yield b"chunk2"

        result = await _collect(with_tts_timeout(source(), timeout=0.005))

        assert result == [b"chunk1", b"chunk2"]

    async def test_marker_does_not_satisfy_first_audio_timeout(self):
        async def marker_then_stall():
            yield TTSEvent(type=TTSEventType.MARKERS, markers=[{"word": "hello"}])
            await _wait_forever()

        with pytest.raises(TTSTimeoutError):
            await _collect(with_tts_timeout(marker_then_stall(), timeout=0.01))

    async def test_pre_audio_events_do_not_reset_first_audio_deadline(self):
        async def pre_audio_events_forever():
            while True:
                await asyncio.sleep(0.005)
                yield TTSEvent(type=TTSEventType.MARKERS, markers=[{"word": "pending"}])
                yield TTSEvent(
                    type=TTSEventType.AUDIO,
                    audio=AudioChunk(data=b"", format=PCM16_MONO_16K),
                )

        async with asyncio.timeout(0.1):
            with pytest.raises(TTSTimeoutError):
                await _collect(with_tts_timeout(pre_audio_events_forever(), timeout=0.02))

    async def test_first_byte_iteration_runs_in_caller_task(self):
        caller_task = asyncio.current_task()
        source_task = None

        async def source():
            nonlocal source_task
            source_task = asyncio.current_task()
            yield b"chunk1"

        await _collect(with_tts_timeout(source(), timeout=1.0))

        assert source_task is caller_task

    async def test_timeout_emits_error_event(self):
        event_bus = EventBus()
        errors = []

        async def handler(event):
            errors.append(event)

        event_bus.subscribe(Error, handler)

        async def stalling():
            await _wait_forever()
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
            await _wait_forever()
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

    async def test_provider_timeout_error_is_not_relabelled_as_tts_deadline(self):
        """A source failure must not emit a false first-byte timeout event."""
        provider_error = TimeoutError("provider request deadline")
        event_bus = EventBus()
        errors: list[Error] = []
        event_bus.subscribe(Error, errors.append)

        async def provider_events():
            raise provider_error
            yield b"unreachable"

        with pytest.raises(TimeoutError) as exc_info:
            await _collect(
                with_tts_timeout(
                    provider_events(),
                    timeout=1.0,
                    event_bus=event_bus,
                )
            )

        assert exc_info.value is provider_error
        assert errors == []


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
