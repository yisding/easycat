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
        scripted = list(messages or [])
        if not any('"type": "session.created"' in msg for msg in scripted):
            scripted.insert(0, _make_session_created())
        if not any(
            event in msg
            for msg in scripted
            for event in ('"type": "session.updated"', '"type": "transcription_session.updated"')
        ):
            insert_at = 1 if scripted and '"type": "session.created"' in scripted[0] else 0
            scripted.insert(insert_at, _make_transcription_session_updated())
        self.connection = _MockWSConnection(scripted)
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


def _make_transcription_session_updated() -> str:
    return json.dumps({"type": "transcription_session.updated", "session": {}})


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
    session = session_msg["session"]
    assert session["type"] == "realtime"
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["input"]["turn_detection"] is None
    assert session["audio"]["input"]["transcription"]["model"] == "gpt-4o-transcribe"


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
    assert session_msg["session"]["audio"]["input"]["transcription"]["language"] == "es"


@pytest.mark.asyncio
async def test_openai_realtime_auth_headers():
    factory = _MockWSFactory([_make_transcription_completed("hi")])
    config = OpenAIRealtimeSTTConfig(api_key="sk-secret-123", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, [])

    assert factory.call_headers is not None
    assert factory.call_headers["Authorization"] == "Bearer sk-secret-123"
    assert "OpenAI-Beta" not in factory.call_headers


@pytest.mark.asyncio
async def test_openai_realtime_model_is_set_via_session_update():
    """The connection model and transcription model are distinct."""
    factory = _MockWSFactory([_make_transcription_completed("hi")])
    config = OpenAIRealtimeSTTConfig(
        api_key="sk-test", model="gpt-4o-mini-transcribe", ws_connect=factory
    )
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, [])

    assert factory.call_url is not None
    assert "model=gpt-realtime-mini" in factory.call_url
    # The transcription model also belongs in the session.update payload.
    session_msg = json.loads(factory.connection.sent[0])
    assert (
        session_msg["session"]["audio"]["input"]["transcription"]["model"]
        == "gpt-4o-mini-transcribe"
    )


@pytest.mark.asyncio
async def test_openai_realtime_merges_explicit_connection_model_into_existing_query_string():
    factory = _MockWSFactory([_make_transcription_completed("hi")])
    config = OpenAIRealtimeSTTConfig(
        api_key="sk-test",
        ws_url="wss://api.openai.com/v1/realtime?foo=bar",
        connection_model="gpt-realtime-mini",
        ws_connect=factory,
    )
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, [])

    assert factory.call_url is not None
    assert "foo=bar" in factory.call_url
    assert "model=gpt-realtime-mini" in factory.call_url


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


@pytest.mark.asyncio
async def test_openai_realtime_skips_commit_on_short_tail():
    """OpenAI Realtime refuses commits with <100ms of buffered audio
    (1008 policy violation).  The provider must skip the send locally
    rather than surface that as a spurious warning — and must still
    clear ``_audio_pending_commit`` so ``_on_end`` doesn't re-attempt.
    """
    factory = _MockWSFactory()
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    # ~20ms at 16 kHz → well below the 100ms OpenAI Realtime minimum
    pcm = generate_pcm_sine(duration_ms=20)
    await stt.start_stream()
    for chunk in make_audio_chunks(pcm):
        await stt.send_audio(chunk)
    committed = await stt.commit_segment()
    assert committed is False, "short tail must not be sent to the server"

    commit_msgs = [
        m
        for m in factory.connection.sent
        if isinstance(m, str) and "input_audio_buffer.commit" in m
    ]
    assert commit_msgs == [], "provider should not send a commit it knows will fail"
    # State reset so end_stream below won't re-send.
    assert stt._audio_pending_commit is False
    assert stt._bytes_since_last_commit == 0
    await stt.end_stream()


@pytest.mark.asyncio
async def test_openai_realtime_emits_error_event_on_server_error_message():
    """When the server sends an ``error`` message, the provider must
    emit a journal-visible ``Error`` event on the bus with the server's
    message + buffer context, not only a logger.warning."""
    from easycat.events import Error, ErrorStage, EventBus

    class _BusCapture:
        def __init__(self) -> None:
            self.errors: list[Error] = []

        def subscribe(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def emit(self, event: Any) -> None:
            if isinstance(event, Error):
                self.errors.append(event)

    bus = EventBus()
    captured: list[Error] = []
    bus.subscribe(Error, captured.append)

    error_msg = json.dumps(
        {"type": "error", "error": {"message": "buffer too small", "code": "buffer_too_small"}}
    )
    factory = _MockWSFactory([error_msg])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory, event_bus=bus)
    stt = OpenAIRealtimeSTT(config)
    await stt.start_stream()
    # Give the receive loop a turn to process the error message and
    # schedule the Error emission.
    for _ in range(10):
        if captured:
            break
        await asyncio.sleep(0.01)
    await stt.end_stream()

    assert captured, "expected one Error event on the bus"
    err = captured[0]
    assert err.stage is ErrorStage.STT
    assert err.provider == "openai-realtime"
    assert "buffer too small" in str(err.exception)
    # Notes carry the code + buffer context for bundle replays.
    notes = getattr(err.exception, "__notes__", [])
    assert any("code=buffer_too_small" in n for n in notes)


@pytest.mark.asyncio
async def test_openai_realtime_commit_segment_keeps_stream_open_for_later_audio():
    factory = _MockWSFactory(
        [
            _make_transcription_completed("hello"),
            _make_transcription_completed("world"),
        ]
    )
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    collected = []
    await stt.start_stream()

    async def _collect() -> None:
        async for event in stt.events():
            collected.append(event)

    collect_task = asyncio.create_task(_collect())
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100))[0]

    await stt.send_audio(chunk)
    assert await stt.commit_segment() is True
    await asyncio.sleep(0)
    await stt.send_audio(chunk)
    await stt.end_stream()
    await collect_task

    finals = [event.text for event in collected if event.type == STTEventType.FINAL]
    assert finals == ["hello", "world"]

    commit_msgs = [
        json.loads(m)
        for m in factory.connection.sent
        if isinstance(m, str) and "input_audio_buffer.commit" in m
    ]
    assert len(commit_msgs) == 2
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
