"""Tests for VoicePipeline — the primary entry point for voice agents.

Validates:
- Auto-detection of OpenAI Agents SDK and PydanticAI agents
- Internal wrapping with the correct adapter
- Pass-through for already-wrapped adapters and plain agents
- Full lifecycle (start/stop/run) with stub providers
- Framework-specific kwargs are forwarded correctly
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.agents.base import BaseAgentAdapter
from easycat.agents.openai_agents import OpenAIAgentsAdapter
from easycat.agents.pydantic_ai import PydanticAIAdapter
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    AgentFinal,
    EventBus,
    STTEvent,
    STTEventType,
    STTFinal,
    TTSEvent,
    TTSEventType,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.session import TurnState
from easycat.turn_manager import TurnManagerConfig
from easycat.voice_pipeline import (
    VoicePipeline,
    _is_openai_agents_sdk,
    _is_pydantic_ai,
    _wrap_agent,
)

# ── Mock agents mimicking real framework objects ─────────────────────


class FakeOpenAIAgent:
    """Mimics agents.Agent from the OpenAI Agents SDK."""

    __module__ = "agents.agent"

    def __init__(self, name: str = "test", instructions: str = "") -> None:
        self.name = name
        self.instructions = instructions
        self.tools: list[Any] = []
        self.model = "gpt-4o"


class FakePydanticAIAgent:
    """Mimics pydantic_ai.Agent."""

    __module__ = "pydantic_ai.agent"

    def __init__(self) -> None:
        self.model = "openai:gpt-4o"

    async def run(self, text: str, **kwargs: Any) -> Any:
        return None

    async def run_stream(self, text: str, **kwargs: Any) -> Any:
        return None

    async def iter(self, text: str, **kwargs: Any) -> Any:
        return None


class PlainAgent:
    """A plain object with a run() method — no framework."""

    async def run(self, text: str) -> str:
        return f"Echo: {text}"


class NotAnAgent:
    """An object that doesn't look like any agent."""

    pass


# ── Detection tests ──────────────────────────────────────────────────


class TestFrameworkDetection:
    def test_detects_openai_agents_sdk(self):
        agent = FakeOpenAIAgent()
        assert _is_openai_agents_sdk(agent)
        assert not _is_pydantic_ai(agent)

    def test_detects_pydantic_ai(self):
        agent = FakePydanticAIAgent()
        assert _is_pydantic_ai(agent)
        assert not _is_openai_agents_sdk(agent)

    def test_plain_agent_is_neither(self):
        agent = PlainAgent()
        assert not _is_openai_agents_sdk(agent)
        assert not _is_pydantic_ai(agent)


# ── Wrapping tests ───────────────────────────────────────────────────


class TestWrapAgent:
    def test_wraps_openai_agent(self):
        agent = FakeOpenAIAgent()
        wrapped = _wrap_agent(agent)
        assert isinstance(wrapped, OpenAIAgentsAdapter)

    def test_wraps_pydantic_ai_agent(self):
        agent = FakePydanticAIAgent()
        wrapped = _wrap_agent(agent)
        assert isinstance(wrapped, PydanticAIAdapter)

    def test_passes_through_existing_adapter(self):
        inner = FakeOpenAIAgent()
        adapter = OpenAIAgentsAdapter(inner)
        wrapped = _wrap_agent(adapter)
        assert wrapped is adapter

    def test_passes_through_plain_agent(self):
        agent = PlainAgent()
        wrapped = _wrap_agent(agent)
        assert wrapped is agent

    def test_raises_for_invalid_object(self):
        with pytest.raises(TypeError, match="Cannot detect agent framework"):
            _wrap_agent(NotAnAgent())

    def test_forwards_openai_run_config(self):
        agent = FakeOpenAIAgent()
        config = {"model": "gpt-4o-mini"}
        wrapped = _wrap_agent(agent, run_config=config)
        assert isinstance(wrapped, OpenAIAgentsAdapter)
        assert wrapped._run_config == config

    def test_forwards_openai_context(self):
        agent = FakeOpenAIAgent()
        ctx = {"user_id": "123"}
        wrapped = _wrap_agent(agent, context=ctx)
        assert isinstance(wrapped, OpenAIAgentsAdapter)
        assert wrapped._context == ctx

    def test_forwards_pydantic_deps(self):
        agent = FakePydanticAIAgent()
        deps = {"db": "connection"}
        wrapped = _wrap_agent(agent, deps=deps)
        assert isinstance(wrapped, PydanticAIAdapter)
        assert wrapped._deps == deps

    def test_forwards_pydantic_model_settings(self):
        agent = FakePydanticAIAgent()
        settings = {"temperature": 0.5}
        wrapped = _wrap_agent(agent, model_settings=settings)
        assert isinstance(wrapped, PydanticAIAdapter)
        assert wrapped._model_settings == settings


# ── VoicePipeline construction tests ─────────────────────────────────


class TestVoicePipelineConstruction:
    def test_accepts_openai_agent(self):
        agent = FakeOpenAIAgent(name="Bot")
        pipeline = VoicePipeline(agent)
        assert pipeline.session is not None
        assert isinstance(pipeline.session.agent, OpenAIAgentsAdapter)

    def test_accepts_pydantic_ai_agent(self):
        agent = FakePydanticAIAgent()
        pipeline = VoicePipeline(agent)
        assert pipeline.session is not None
        assert isinstance(pipeline.session.agent, PydanticAIAdapter)

    def test_accepts_plain_agent(self):
        agent = PlainAgent()
        pipeline = VoicePipeline(agent)
        assert pipeline.session.agent is agent

    def test_accepts_existing_adapter(self):
        adapter = OpenAIAgentsAdapter(FakeOpenAIAgent())
        pipeline = VoicePipeline(adapter)
        assert pipeline.session.agent is adapter

    def test_rejects_invalid_agent(self):
        with pytest.raises(TypeError):
            VoicePipeline(NotAnAgent())

    def test_event_bus_shortcut(self):
        pipeline = VoicePipeline(PlainAgent())
        assert pipeline.event_bus is pipeline.session.event_bus

    def test_turn_state_shortcut(self):
        pipeline = VoicePipeline(PlainAgent())
        assert pipeline.turn_state == TurnState.IDLE

    def test_is_running_before_start(self):
        pipeline = VoicePipeline(PlainAgent())
        assert not pipeline.is_running

    def test_custom_event_bus(self):
        bus = EventBus()
        pipeline = VoicePipeline(PlainAgent(), event_bus=bus)
        assert pipeline.event_bus is bus

    def test_forwards_openai_kwargs(self):
        agent = FakeOpenAIAgent()
        config = {"model": "gpt-4o-mini"}
        ctx = {"user_id": "42"}
        pipeline = VoicePipeline(agent, run_config=config, context=ctx)
        adapter = pipeline.session.agent
        assert isinstance(adapter, OpenAIAgentsAdapter)
        assert adapter._run_config == config
        assert adapter._context == ctx

    def test_forwards_pydantic_kwargs(self):
        agent = FakePydanticAIAgent()
        pipeline = VoicePipeline(
            agent, deps={"db": "conn"}, model_settings={"temperature": 0.7}
        )
        adapter = pipeline.session.agent
        assert isinstance(adapter, PydanticAIAdapter)
        assert adapter._deps == {"db": "conn"}
        assert adapter._model_settings == {"temperature": 0.7}


# ── Lifecycle tests (using stubs) ────────────────────────────────────


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class StubTransport:
    """Yields a few audio chunks then stops."""

    def __init__(self, chunks: int = 3) -> None:
        self._chunks = chunks
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for _ in range(self._chunks):
            yield _chunk()

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)


class StubVAD:
    def __init__(self) -> None:
        self._n = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Any]:
        self._n += 1
        if self._n == 1:
            yield VADStartSpeaking()
        elif self._n == 3:
            yield VADStopSpeaking()

    def configure(self, **kwargs: object) -> None:
        pass


class StubSTT:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        await self._queue.put(STTEvent(type=STTEventType.FINAL, text="hello"))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class StubTTS:
    async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class StubNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


@pytest.mark.asyncio
async def test_full_lifecycle_with_openai_agent():
    """VoicePipeline runs a full turn with a fake OpenAI Agents SDK agent."""
    agent = PlainAgent()  # Simple stand-in (auto-detection not the point here)
    transport = StubTransport()
    pipeline = VoicePipeline(
        agent,
        transport=transport,
        vad=StubVAD(),
        stt=StubSTT(),
        tts=StubTTS(),
        noise_reducer=StubNoiseReducer(),
        turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
    )

    events_seen: list[str] = []
    pipeline.event_bus.subscribe(TurnStarted, lambda e: events_seen.append("TurnStarted"))
    pipeline.event_bus.subscribe(STTFinal, lambda e: events_seen.append("STTFinal"))
    pipeline.event_bus.subscribe(AgentFinal, lambda e: events_seen.append("AgentFinal"))
    pipeline.event_bus.subscribe(TurnEnded, lambda e: events_seen.append("TurnEnded"))

    await pipeline.start()
    await asyncio.sleep(0.3)
    await pipeline.stop()

    assert not pipeline.is_running
    assert "TurnStarted" in events_seen
    assert "STTFinal" in events_seen
    assert "AgentFinal" in events_seen
    assert len(transport.sent) > 0


@pytest.mark.asyncio
async def test_start_stop():
    """Pipeline can be started and stopped."""
    pipeline = VoicePipeline(PlainAgent())
    await pipeline.start()
    assert pipeline.is_running
    await pipeline.stop()
    assert not pipeline.is_running


@pytest.mark.asyncio
async def test_run_blocks_until_transport_closes():
    """pipeline.run() blocks until the transport's receive_audio() ends."""
    transport = StubTransport(chunks=1)
    pipeline = VoicePipeline(
        PlainAgent(),
        transport=transport,
        enable_vad=False,
        enable_noise_reduction=False,
    )

    # run() should return once the transport is done and session shuts down
    task = asyncio.create_task(pipeline.run())
    await asyncio.sleep(0.3)

    # Pipeline should still be "running" in its poll loop but the transport
    # already finished. Force stop to unblock.
    await pipeline.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert not pipeline.is_running
