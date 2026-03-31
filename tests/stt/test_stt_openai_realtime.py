"""Tests for the OpenAI Realtime streaming STT provider."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.events import STTEventType
from easycat.providers import STTProvider
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig
from tests.stt.helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks

# ── Mock WebSocket ──────────────────────────────────────────────


class _MockWSConnection:
    """Simulates a websockets ClientConnection for testing.

    Yields pre-scripted messages to the receive loop and records all
    sent messages for assertion.
    """

    def __init__(self, messages: list[str] | None = None) -> None:
        self._messages = messages or []
        self.sent: list[str | bytes] = []
        self.close_code: int | None = None
        self._closed = False

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data)
        # Yield to the event loop so the concurrent receive task can run.
        await asyncio.sleep(0)

    async def recv(self) -> str:
        if not self._messages:
            # Block until close is called (simulates a long-lived connection).
            while not self._closed:
                await asyncio.sleep(0.01)
            raise Exception("connection closed")
        return self._messages.pop(0)

    async def close(self) -> None:
        self._closed = True
        self.close_code = 1000

    def __aiter__(self) -> AsyncIterator[str]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[str]:
        while self._messages:
            yield self._messages.pop(0)
        # Block until close, like a real long-lived WebSocket connection.
        while not self._closed:
            await asyncio.sleep(0.01)


class _MockWSFactory:
    """Factory that returns a mock connection and records the URL/headers."""

    def __init__(self, messages: list[str] | None = None) -> None:
        self.connection = _MockWSConnection(messages)
        self.call_url: str | None = None
        self.call_headers: dict[str, str] | None = None

    async def __call__(self, url: str, **kwargs: Any) -> _MockWSConnection:
        self.call_url = url
        self.call_headers = kwargs.get("additional_headers", {})
        return self.connection


def _make_transcription_completed(transcript: str) -> str:
    return json.dumps(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": transcript,
        }
    )


def _make_transcription_delta(delta: str) -> str:
    return json.dumps(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": delta,
        }
    )


def _make_session_created() -> str:
    return json.dumps({"type": "session.created", "session": {}})


def _make_session_updated() -> str:
    return json.dumps({"type": "session.updated", "session": {}})


# ── Protocol conformance ────────────────────────────────────────


def test_openai_realtime_stt_conforms_to_protocol():
    provider = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="test-key"))
    assert isinstance(provider, STTProvider)


# ── Session setup ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_realtime_sends_session_update_on_start():
    factory = _MockWSFactory(
        [
            _make_session_created(),
            _make_session_updated(),
            _make_transcription_completed("hello"),
        ]
    )
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, [])

    # The first sent message should be the session.update config.
    assert len(factory.connection.sent) >= 1
    session_msg = json.loads(factory.connection.sent[0])
    assert session_msg["type"] == "session.update"
    assert session_msg["session"]["turn_detection"] is None
    assert "input_audio_transcription" in session_msg["session"]
    assert session_msg["session"]["input_audio_transcription"]["model"] == "gpt-4o-transcribe"


@pytest.mark.asyncio
async def test_openai_realtime_sends_language_in_session_update():
    factory = _MockWSFactory(
        [
            _make_transcription_completed("hola"),
        ]
    )
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", language="es", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, [])

    session_msg = json.loads(factory.connection.sent[0])
    assert session_msg["session"]["input_audio_transcription"]["language"] == "es"


@pytest.mark.asyncio
async def test_openai_realtime_auth_headers():
    factory = _MockWSFactory([_make_transcription_completed("hi")])
    config = OpenAIRealtimeSTTConfig(api_key="sk-secret-123", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, [])

    assert factory.call_headers is not None
    assert factory.call_headers["Authorization"] == "Bearer sk-secret-123"
    assert factory.call_headers["OpenAI-Beta"] == "realtime=v1"


@pytest.mark.asyncio
async def test_openai_realtime_url_includes_model():
    factory = _MockWSFactory([_make_transcription_completed("hi")])
    config = OpenAIRealtimeSTTConfig(
        api_key="sk-test", model="gpt-4o-mini-transcribe", ws_connect=factory
    )
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, [])

    assert factory.call_url is not None
    assert "model=gpt-4o-mini-transcribe" in factory.call_url


# ── Audio sending ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_realtime_sends_audio_as_base64():
    factory = _MockWSFactory([_make_transcription_completed("test")])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)
    await collect_stt_events(stt, chunks)

    # Find audio append messages (skip session.update and commit).
    audio_msgs = [
        json.loads(m)
        for m in factory.connection.sent
        if isinstance(m, str) and "input_audio_buffer.append" in m
    ]
    assert len(audio_msgs) >= 1
    for msg in audio_msgs:
        assert msg["type"] == "input_audio_buffer.append"
        # Verify the audio field is valid base64.
        decoded = base64.b64decode(msg["audio"])
        assert len(decoded) > 0


@pytest.mark.asyncio
async def test_openai_realtime_sends_commit_on_end():
    factory = _MockWSFactory([_make_transcription_completed("done")])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    # The last protocol message before close should be the commit.
    commit_msgs = [
        json.loads(m)
        for m in factory.connection.sent
        if isinstance(m, str) and "input_audio_buffer.commit" in m
    ]
    assert len(commit_msgs) == 1
    assert commit_msgs[0]["type"] == "input_audio_buffer.commit"


# ── Transcription events ───────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_realtime_emits_final_transcript():
    factory = _MockWSFactory([_make_transcription_completed("hello world")])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    finals = [e for e in events if e.type == STTEventType.FINAL]
    assert len(finals) == 1
    assert finals[0].text == "hello world"


@pytest.mark.asyncio
async def test_openai_realtime_emits_partial_then_final():
    factory = _MockWSFactory(
        [
            _make_transcription_delta("hel"),
            _make_transcription_delta("lo wor"),
            _make_transcription_completed("hello world"),
        ]
    )
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    partials = [e for e in events if e.type == STTEventType.PARTIAL]
    finals = [e for e in events if e.type == STTEventType.FINAL]

    # Should have two partials (accumulated).
    assert len(partials) == 2
    assert partials[0].text == "hel"
    assert partials[1].text == "hello wor"

    assert len(finals) == 1
    assert finals[0].text == "hello world"


@pytest.mark.asyncio
async def test_openai_realtime_final_from_partials_when_no_transcript():
    """If completed has no transcript, fall back to accumulated partial text."""
    factory = _MockWSFactory(
        [
            _make_transcription_delta("hello"),
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "",
                }
            ),
        ]
    )
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    finals = [e for e in events if e.type == STTEventType.FINAL]
    assert len(finals) == 1
    assert finals[0].text == "hello"


# ── Reusability ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_realtime_reusable_across_streams():
    factory1 = _MockWSFactory([_make_transcription_completed("stream one")])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory1)
    stt = OpenAIRealtimeSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    events1 = await collect_stt_events(stt, chunks)
    finals1 = [e for e in events1 if e.type == STTEventType.FINAL]
    assert len(finals1) == 1
    assert finals1[0].text == "stream one"

    # Second stream with a fresh factory.
    factory2 = _MockWSFactory([_make_transcription_completed("stream two")])
    stt._config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory2)

    events2 = await collect_stt_events(stt, chunks)
    finals2 = [e for e in events2 if e.type == STTEventType.FINAL]
    assert len(finals2) == 1
    assert finals2[0].text == "stream two"


# ── No events on empty audio ───────────────────────────────────


@pytest.mark.asyncio
async def test_openai_realtime_no_events_on_empty_audio():
    factory = _MockWSFactory([])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    events = await collect_stt_events(stt, [])
    assert len(events) == 0


# ── Factory integration ─────────────────────────────────────────


def test_factory_creates_openai_realtime():
    from easycat.stt.factory import STTProviderConfig, create_stt_provider

    provider = create_stt_provider(
        STTProviderConfig(provider="openai-realtime", api_key="sk-test")
    )
    assert isinstance(provider, OpenAIRealtimeSTT)


def test_factory_rejects_unknown_provider():
    from easycat.stt.factory import STTProviderConfig, create_stt_provider

    with pytest.raises(ValueError, match="Unknown STT provider"):
        create_stt_provider(STTProviderConfig(provider="nonexistent", api_key="k"))
