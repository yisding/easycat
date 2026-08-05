"""Tests for the OpenAI Realtime streaming STT provider."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat._concurrency import RuntimeSupervisor
from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.runtime.scope import RuntimeScope
from easycat.stt import openai_realtime_provider as realtime_provider
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig
from tests.stt.helpers import (
    collect_stt_events as _collect_stt_events,
)
from tests.stt.helpers import (
    generate_pcm_sine,
    make_audio_chunks,
)


async def collect_stt_events(
    provider: OpenAIRealtimeSTT,
    audio_chunks: list[AudioChunk],
) -> list[STTEvent]:
    """Run one isolated test turn, then release the default persistent socket."""
    try:
        return await _collect_stt_events(provider, audio_chunks)
    finally:
        await provider.aclose()


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
            raise ConnectionError("connection closed")
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


class _PersistentMockWSConnection:
    """Queue-backed Realtime socket that survives logical STT turns."""

    _STOP = object()

    def __init__(self, *, respond_to_commit: bool = True) -> None:
        self.sent: list[str | bytes] = []
        self.close_code: int | None = None
        self.commit_count = 0
        self.clear_count = 0
        self._respond_to_commit = respond_to_commit
        self._queue: asyncio.Queue[str | object] = asyncio.Queue()

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data)
        if not isinstance(data, str):
            return
        message = json.loads(data)
        message_type = message.get("type")
        if message_type == "session.update":
            await self.push({"type": "transcription_session.updated", "session": {}})
        elif message_type == "input_audio_buffer.commit":
            self.commit_count += 1
            if self._respond_to_commit:
                item_id = f"item-{self.commit_count}"
                await self.push(
                    {
                        "type": "input_audio_buffer.committed",
                        "item_id": item_id,
                    }
                )
                await self.push(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": item_id,
                        "transcript": f"turn {self.commit_count}",
                    }
                )
        elif message_type == "input_audio_buffer.clear":
            self.clear_count += 1
            await self.push({"type": "input_audio_buffer.cleared"})
        # Exercise the reserve-before-send path by allowing the receive loop
        # to handle acknowledgements before send() returns.
        await asyncio.sleep(0)

    async def push(self, message: dict[str, Any]) -> None:
        await self._queue.put(json.dumps(message))

    async def close(self) -> None:
        if self.close_code is not None:
            return
        self.close_code = 1000
        await self._queue.put(self._STOP)

    def __aiter__(self) -> _PersistentMockWSConnection:
        return self

    async def __anext__(self) -> str:
        message = await self._queue.get()
        if message is self._STOP:
            raise StopAsyncIteration
        assert isinstance(message, str)
        return message


class _PersistentMockWSFactory:
    def __init__(
        self,
        sockets: list[_PersistentMockWSConnection],
        *,
        connect_delay_s: float = 0.0,
    ) -> None:
        self.sockets = sockets
        self.connect_delay_s = connect_delay_s
        self.call_count = 0

    async def __call__(self, _url: str, **_kwargs: Any) -> _PersistentMockWSConnection:
        await asyncio.sleep(self.connect_delay_s)
        socket = self.sockets[self.call_count]
        self.call_count += 1
        return socket


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
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", persistent_ws=False, ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, [])

    # The first sent message should be the session.update config.
    assert len(factory.connection.sent) >= 1
    session_msg = json.loads(factory.connection.sent[0])
    assert session_msg["type"] == "session.update"
    session = session_msg["session"]
    assert session["type"] == "transcription"
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["input"]["turn_detection"] is None
    assert session["audio"]["input"]["transcription"]["model"] == "gpt-realtime-whisper"


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
async def test_openai_realtime_sends_delay_in_session_update():
    factory = _MockWSFactory([_make_transcription_completed("fast")])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", delay="low", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, [])

    session_msg = json.loads(factory.connection.sent[0])
    assert session_msg["session"]["audio"]["input"]["transcription"]["delay"] == "low"


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
    """The transcription model is set in the transcription session update."""
    factory = _MockWSFactory([_make_transcription_completed("hi")])
    config = OpenAIRealtimeSTTConfig(
        api_key="sk-test", model="gpt-4o-mini-transcribe", ws_connect=factory
    )
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, [])

    assert factory.call_url is not None
    assert "intent=transcription" in factory.call_url
    # The transcription model also belongs in the session.update payload.
    session_msg = json.loads(factory.connection.sent[0])
    assert (
        session_msg["session"]["audio"]["input"]["transcription"]["model"]
        == "gpt-4o-mini-transcribe"
    )


def test_openai_realtime_version_info_reports_transcription_model():
    """version_info keeps the canonical 4-key shape and reports the
    transcription ``model`` (not the connection model).

    The transcription model and the realtime ``connection_model`` are
    distinct config knobs; ``version_info`` deliberately surfaces only the
    transcription ``model`` so it matches the cross-provider shape invariant
    guarded by ``tests/providers/test_version_info.py``.  The connection model
    is exercised separately via the websocket URL.
    """
    config = OpenAIRealtimeSTTConfig(
        api_key="sk-test",
        model="gpt-4o-mini-transcribe",
        connection_model="gpt-realtime-mini",
    )
    # The two models stay distinct on the config.
    assert config.model == "gpt-4o-mini-transcribe"
    assert config.connection_model == "gpt-realtime-mini"

    info = OpenAIRealtimeSTT(config).version_info()
    assert set(info.keys()) == {"provider", "model", "api_version", "sdk_version"}
    assert info["model"] == "gpt-4o-mini-transcribe"


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
    session_msg = json.loads(factory.connection.sent[0])
    assert session_msg["session"]["type"] == "realtime"


# ── Audio sending ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_realtime_sends_audio_as_base64():
    factory = _MockWSFactory([_make_transcription_completed("test")])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", persistent_ws=False, ws_connect=factory)
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
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", persistent_ws=False, ws_connect=factory)
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
async def test_openai_realtime_close_failure_blocks_restart_until_cleanup_retry(
    monkeypatch,
):
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test", persistent_ws=False))
    close_calls = 0

    async def fail_once_close(*, close_before_drain: bool = False) -> None:
        nonlocal close_calls
        assert close_before_drain is True
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError("socket close failed")

    async def connect_replacement() -> None:
        return None

    monkeypatch.setattr(stt, "_close_active_websocket", fail_once_close)
    monkeypatch.setattr(stt, "_connect_new_websocket", connect_replacement)
    stt._running = True

    with pytest.raises(RuntimeError, match="socket close failed"):
        await stt.end_stream()

    assert stt._failed_end_cleanup_pending is True
    assert close_calls == 1

    await stt.start_stream()

    assert close_calls == 2
    assert stt._failed_end_cleanup_pending is False
    assert stt._running is True
    await stt.end_stream()


@pytest.mark.asyncio
async def test_openai_realtime_skips_commit_on_short_tail():
    """OpenAI Realtime refuses commits with <100ms of buffered audio
    (1008 policy violation).  The provider must skip the send locally
    rather than surface that as a spurious warning — but must keep
    ``_audio_pending_commit`` / ``_bytes_since_last_commit`` intact so
    a later commit that sees more audio still reflects the true server
    buffer.
    """
    factory = _MockWSFactory()
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", persistent_ws=False, ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    # ~20ms at 16 kHz → well below the 100ms OpenAI Realtime minimum
    pcm = generate_pcm_sine(duration_ms=20)
    await stt.start_stream()
    for chunk in make_audio_chunks(pcm):
        await stt.send_audio(chunk)
    bytes_after_first = stt._bytes_since_last_commit
    committed = await stt.commit_segment()
    assert committed is False, "short tail must not be sent to the server"

    commit_msgs = [
        m
        for m in factory.connection.sent
        if isinstance(m, str) and "input_audio_buffer.commit" in m
    ]
    assert commit_msgs == [], "provider should not send a commit it knows will fail"
    # State is preserved across both server-emitted bytes and any converter
    # tail, so a subsequent append + commit still counts all retained audio
    # toward the 100 ms floor.
    assert stt._audio_pending_commit is (bytes_after_first > 0)
    assert stt._bytes_since_last_commit == bytes_after_first
    assert bytes_after_first + stt._audio_resampler.pending_output_bytes > 0
    await stt.end_stream()


@pytest.mark.asyncio
async def test_openai_realtime_short_tail_then_more_audio_commits():
    """A short commit attempt followed by more audio must eventually
    cross the 100 ms floor and send a commit, since the server still
    has the buffered bytes from the first (skipped) attempt.
    """
    factory = _MockWSFactory()
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", persistent_ws=False, ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    await stt.start_stream()
    # First batch: 50 ms → below 100 ms floor.
    for chunk in make_audio_chunks(generate_pcm_sine(duration_ms=50)):
        await stt.send_audio(chunk)
    assert await stt.commit_segment() is False

    # Second batch: 60 ms.  Combined with the first, the server has
    # 110 ms buffered, so this commit must be sent.
    for chunk in make_audio_chunks(generate_pcm_sine(duration_ms=60)):
        await stt.send_audio(chunk)
    assert await stt.commit_segment() is True

    commit_msgs = [
        m
        for m in factory.connection.sent
        if isinstance(m, str) and "input_audio_buffer.commit" in m
    ]
    assert len(commit_msgs) == 1
    await stt.end_stream()


@pytest.mark.asyncio
async def test_openai_realtime_reports_pending_commit_bytes():
    """The provider exposes a public ``pending_commit_bytes()`` so the
    session journal can record uncommitted audio without reaching into the
    private ``_bytes_since_last_commit`` field."""
    from easycat.providers import PendingCommitReporter

    factory = _MockWSFactory()
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", persistent_ws=False, ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)
    assert isinstance(stt, PendingCommitReporter)

    await stt.start_stream()
    assert stt.pending_commit_bytes() == 0

    pcm = generate_pcm_sine(duration_ms=50)
    for chunk in make_audio_chunks(pcm):
        await stt.send_audio(chunk)
    assert stt.pending_commit_bytes() == stt._bytes_since_last_commit > 0
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
    config = OpenAIRealtimeSTTConfig(
        api_key="sk-test",
        persistent_ws=False,
        ws_connect=factory,
        event_bus=bus,
    )
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
    socket = _PersistentMockWSConnection()
    factory = _PersistentMockWSFactory([socket])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", persistent_ws=False, ws_connect=factory)
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
    assert finals == ["turn 1", "turn 2"]

    commit_msgs = [
        json.loads(m)
        for m in socket.sent
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


def test_openai_realtime_config_final_timeout_default() -> None:
    """The bounded final-wait defaults to the lowered module constant."""
    config = OpenAIRealtimeSTTConfig(api_key="sk-test")

    assert config.final_transcript_timeout_s == realtime_provider._FINAL_TRANSCRIPT_TIMEOUT_S
    assert config.final_transcript_timeout_s == pytest.approx(0.9)
    assert config.persistent_ws is True


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf")])
def test_openai_realtime_config_rejects_invalid_final_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="final_transcript_timeout_s"):
        OpenAIRealtimeSTTConfig(api_key="sk-test", final_transcript_timeout_s=timeout)


def test_openai_realtime_config_preserves_legacy_positional_field_order() -> None:
    factory = _MockWSFactory()
    event_bus = object()

    config = OpenAIRealtimeSTTConfig(
        "sk-test",
        "gpt-realtime-whisper",
        None,
        "en",
        "low",
        "wss://example.test/realtime",
        0.5,
        factory,
        event_bus,
    )

    assert config.ws_connect is factory
    assert config.event_bus is event_bus
    assert config.persistent_ws is True


@pytest.mark.asyncio
async def test_openai_realtime_reconnect_promotes_partial_and_releases_final_waiter() -> None:
    class _StubWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    stt._ws = _StubWS()  # type: ignore[assignment]
    stt._partial_text = "before reconnect"
    stt._audio_pending_commit = True
    stt._bytes_since_last_commit = stt._COMMIT_MIN_BYTES
    stt._commit_pending = True
    final_received = asyncio.Event()
    stt._final_received = final_received
    emitted: list[STTEvent] = []
    stt._emit_event = emitted.append  # type: ignore[method-assign]
    waiter = asyncio.create_task(stt._await_final(final_received))
    await asyncio.sleep(0)

    await stt._on_reconnect()
    await asyncio.wait_for(waiter, timeout=0.1)

    assert [(event.type, event.text) for event in emitted] == [
        (STTEventType.FINAL, "before reconnect")
    ]
    assert stt._partial_text == ""
    assert not stt._audio_pending_commit
    assert stt._bytes_since_last_commit == 0
    assert not stt._commit_pending


@pytest.mark.asyncio
async def test_openai_realtime_promotes_partial_on_final_timeout_via_config_field() -> None:
    """The config field — not just the module constant — drives the wait."""
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", final_transcript_timeout_s=0.05)

    class _StubWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

    stt = OpenAIRealtimeSTT(config)
    stt._ws = _StubWS()  # type: ignore[assignment]
    stt._audio_pending_commit = True
    stt._bytes_since_last_commit = stt._COMMIT_MIN_BYTES
    stt._partial_text = "hello"
    emitted: list[STTEvent] = []
    stt._emit_event = emitted.append  # type: ignore[method-assign]

    result = await stt._send_commit(wait_for_final=True)

    assert result is True
    finals = [e for e in emitted if e.type == STTEventType.FINAL]
    assert [f.text for f in finals] == ["hello"]
    assert stt._dropping_pending_final is True


@pytest.mark.asyncio
async def test_openai_realtime_promotes_partial_on_final_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``.completed`` stalls past the timeout, the accumulated
    partial is promoted to ``STTFinal`` so the session can drive the
    LLM without waiting on OpenAI's long-tail response."""
    monkeypatch.setattr(realtime_provider, "_FINAL_TRANSCRIPT_TIMEOUT_S", 0.05)

    class _StubWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    stt._ws = _StubWS()  # type: ignore[assignment]
    stt._audio_pending_commit = True
    stt._bytes_since_last_commit = stt._COMMIT_MIN_BYTES
    stt._partial_text = "hello"
    emitted: list[STTEvent] = []
    stt._emit_event = emitted.append  # type: ignore[method-assign]

    result = await stt._send_commit(wait_for_final=True)

    assert result is True, "commit should still report a successful send"
    finals = [e for e in emitted if e.type == STTEventType.FINAL]
    assert [f.text for f in finals] == ["hello"], (
        "timeout must promote accumulated partial to FINAL"
    )
    assert stt._dropping_pending_final is True, (
        "drop flag should be armed to discard the late .completed"
    )
    assert stt._partial_text == ""


@pytest.mark.asyncio
async def test_openai_realtime_allows_late_completed_when_timeout_had_no_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If no partial was available to promote on timeout, the delayed
    ``.completed`` transcript is still the only final and must be emitted."""
    monkeypatch.setattr(realtime_provider, "_FINAL_TRANSCRIPT_TIMEOUT_S", 0.05)

    class _StubWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    stt._ws = _StubWS()  # type: ignore[assignment]
    stt._audio_pending_commit = True
    stt._bytes_since_last_commit = stt._COMMIT_MIN_BYTES
    stt._partial_text = ""
    emitted: list[STTEvent] = []
    stt._emit_event = emitted.append  # type: ignore[method-assign]

    result = await stt._send_commit(wait_for_final=True)

    assert result is True, "commit should still report a successful send"
    assert emitted == [], "no fallback final should be emitted without partial text"
    assert stt._dropping_pending_final is False, (
        "drop flag should not be armed unless a fallback FINAL was emitted"
    )

    stt._handle_json_message(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "delayed only final transcript",
        }
    )

    finals = [e for e in emitted if e.type == STTEventType.FINAL]
    assert [f.text for f in finals] == ["delayed only final transcript"]


@pytest.mark.asyncio
async def test_openai_realtime_drops_late_completed_after_timeout() -> None:
    """Once the timeout has promoted the partial to FINAL, a late
    ``.completed`` for the same commit is swallowed rather than
    producing a second ``STTFinal`` for the turn."""
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    stt._dropping_pending_final = True
    stt._partial_text = "lingering partial"
    emitted: list[STTEvent] = []
    stt._emit_event = emitted.append  # type: ignore[method-assign]

    stt._handle_json_message(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "revised transcript",
        }
    )

    assert emitted == [], "late .completed must not produce a second STTFinal"
    assert stt._dropping_pending_final is False, (
        "drop flag should self-clear so the next commit behaves normally"
    )
    assert stt._partial_text == "", "partial text should be cleared alongside the drop"


@pytest.mark.asyncio
async def test_openai_realtime_next_commit_reemits_after_drop() -> None:
    """After a drop, the following ``.completed`` for a new commit
    flows through normally.  Guards against the flag accidentally
    suppressing transcripts on subsequent turns."""
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    stt._dropping_pending_final = True  # simulate a prior drop
    emitted: list[STTEvent] = []
    stt._emit_event = emitted.append  # type: ignore[method-assign]

    # First message: the late .completed from the previous turn — dropped.
    stt._handle_json_message(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "stale",
        }
    )
    # Next turn's delta + .completed — must emit normally.
    stt._handle_json_message(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": "fresh transcript",
        }
    )
    stt._handle_json_message(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "fresh transcript",
        }
    )

    finals = [e for e in emitted if e.type == STTEventType.FINAL]
    assert [f.text for f in finals] == ["fresh transcript"]


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


# ── Shared WebSocket lifecycle (WebSocketSTTBase reuse) ──────────


@pytest.mark.asyncio
async def test_openai_realtime_close_drains_receive_loop_with_shared_timeout() -> None:
    """The realtime ``end`` path uses the shared base close semantics:
    close-before-drain wakes the receive loop, which drains within the
    base ``_close_timeout`` rather than being re-implemented locally."""
    factory = _MockWSFactory([_make_transcription_completed("done")])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    await collect_stt_events(stt, make_audio_chunks(generate_pcm_sine(duration_ms=100)))

    # Receive task and socket are released through the shared base teardown.
    assert stt._ws is None
    assert stt._receive_task is None
    # The mock socket was closed (close-before-drain on the realtime path).
    assert factory.connection.close_code == 1000


@pytest.mark.asyncio
async def test_openai_realtime_close_cancels_stuck_receive_loop() -> None:
    """If the receive loop will not exit, the shared close path cancels it
    after ``_close_timeout`` instead of hanging the end-of-turn forever."""
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    stt._close_timeout = 0.05

    class _StuckWS:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    async def _never_returns() -> None:
        # Ignores close() — only a cancel can stop it, exercising the
        # shared base's TimeoutError -> cancel fallback.
        while True:
            await asyncio.sleep(0.01)

    ws = _StuckWS()
    stt._ws = ws  # type: ignore[assignment]
    stt._receive_task = asyncio.create_task(_never_returns())

    await asyncio.wait_for(stt._close_active_websocket(close_before_drain=True), timeout=1.0)

    assert ws.closed is True
    assert stt._ws is None
    assert stt._receive_task is None


@pytest.mark.asyncio
async def test_openai_realtime_scheduled_close_is_runtime_scoped_and_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = RuntimeScope.create_root(
        name="session",
        root_id="session:test",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    stt.set_runtime_scope(root, name="stt-provider-runtime")
    stt._ws = object()  # type: ignore[assignment]
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def blocking_close(*, close_before_drain: bool = False) -> None:
        _ = close_before_drain
        close_started.set()
        await release_close.wait()

    monkeypatch.setattr(stt, "_close_active_websocket", blocking_close)
    stt._schedule_close()
    close_task = stt._close_task
    assert close_task is not None
    await close_started.wait()

    draining = asyncio.create_task(stt._drain_scheduled_close())
    await asyncio.sleep(0)
    draining.cancel()
    with pytest.raises(asyncio.CancelledError):
        await draining

    assert close_task in root.tasks("openai_realtime_close")
    assert close_task.cancelled() is False

    release_close.set()
    await stt._drain_scheduled_close()
    assert stt._close_task is None
    assert root.tasks("openai_realtime_close") == ()
    stt._ws = None
    await root.close()


@pytest.mark.asyncio
async def test_openai_realtime_receive_loop_end_fails_pending_handshake() -> None:
    """When the socket drops before ``session.updated`` arrives, the
    base receive-loop-end hook rejects the pending ``_session_ready``
    future so ``_on_start`` surfaces the close instead of waiting out
    its full timeout."""
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    loop = asyncio.get_running_loop()
    stt._session_ready = loop.create_future()

    stt._on_receive_loop_end()

    assert stt._session_ready.done()
    with pytest.raises(RuntimeError, match="closed before session was ready"):
        stt._session_ready.result()


@pytest.mark.asyncio
async def test_openai_realtime_receive_loop_end_leaves_resolved_future() -> None:
    """The hook must not clobber an already-resolved handshake future."""
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    loop = asyncio.get_running_loop()
    stt._session_ready = loop.create_future()
    stt._session_ready.set_result(None)

    stt._on_receive_loop_end()

    # No exception was injected over the successful result.
    assert stt._session_ready.result() is None


# ── Warmup ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_realtime_warmup_keeps_default_persistent_socket():
    """Default warmup completes one handshake and retains it for user audio."""
    factory = _MockWSFactory([])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory)
    stt = OpenAIRealtimeSTT(config)

    await stt.warmup()

    # The handshake session.update was sent during connect.
    session_msgs = [
        m
        for m in factory.connection.sent
        if isinstance(m, str) and '"type": "session.update"' in m
    ]
    assert len(session_msgs) == 1
    assert stt._ws is not None
    assert stt._receive_task is not None
    assert factory.connection.close_code is None
    await stt.aclose()
    assert factory.connection.close_code == 1000


@pytest.mark.asyncio
async def test_openai_realtime_one_shot_warmup_closes_socket():
    factory = _MockWSFactory([])
    stt = OpenAIRealtimeSTT(
        OpenAIRealtimeSTTConfig(
            api_key="sk-test",
            persistent_ws=False,
            ws_connect=factory,
        )
    )

    await stt.warmup()

    assert stt._ws is None
    assert stt._receive_task is None
    assert factory.connection.close_code == 1000


@pytest.mark.asyncio
async def test_openai_realtime_warmup_swallows_start_errors():
    """A connect/handshake failure during warmup must not propagate to
    Session.start() — WarmupRunner re-raises, so warmup must return cleanly."""

    async def _boom() -> None:
        raise ConnectionError("boom")

    config = OpenAIRealtimeSTTConfig(api_key="sk-test", persistent_ws=False)
    stt = OpenAIRealtimeSTT(config)
    stt.start_stream = _boom  # type: ignore[method-assign]

    # Returns cleanly despite start_stream raising.
    await stt.warmup()


@pytest.mark.asyncio
async def test_openai_realtime_persistent_warmup_swallows_connect_errors():
    async def _boom() -> None:
        raise ConnectionError("boom")

    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    stt._ensure_persistent_connection = _boom  # type: ignore[method-assign]

    await stt.warmup()

    assert stt._ws is None
    assert stt._receive_task is None


@pytest.mark.asyncio
async def test_openai_realtime_persistent_warmup_swallows_cleanup_errors():
    async def _connect_boom() -> None:
        raise ConnectionError("connect boom")

    async def _cleanup_boom() -> None:
        raise ConnectionError("cleanup boom")

    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test"))
    stt._ensure_persistent_connection = _connect_boom  # type: ignore[method-assign]
    stt._discard_connection = _cleanup_boom  # type: ignore[method-assign]

    await stt.warmup()


# ── Reusability ─────────────────────────────────────────────────


async def _run_persistent_turn(
    stt: OpenAIRealtimeSTT,
    *,
    duration_ms: int = 100,
    commit_before_end: bool = False,
) -> list[STTEvent]:
    collected: list[STTEvent] = []
    await stt.start_stream()

    async def _collect() -> None:
        async for event in stt.events():
            collected.append(event)

    collector = asyncio.create_task(_collect())
    for chunk in make_audio_chunks(generate_pcm_sine(duration_ms=duration_ms)):
        await stt.send_audio(chunk)
    if commit_before_end:
        assert await stt.commit_segment() is True
        await asyncio.sleep(0)
    await stt.end_stream()
    await collector
    return collected


@pytest.mark.asyncio
async def test_openai_realtime_warmup_reuses_one_socket_across_turns():
    socket = _PersistentMockWSConnection()
    factory = _PersistentMockWSFactory([socket])
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory))

    await stt.warmup()
    observed: list[str] = []
    for _ in range(2):
        events = await _run_persistent_turn(stt, commit_before_end=True)
        observed.extend(event.text for event in events if event.type is STTEventType.FINAL)

    assert factory.call_count == 1
    assert observed == ["turn 1", "turn 2"]
    assert socket.close_code is None

    await stt.aclose()
    assert socket.close_code == 1000


@pytest.mark.asyncio
async def test_openai_realtime_retry_replaces_queue_after_failed_close():
    socket = _PersistentMockWSConnection()
    factory = _PersistentMockWSFactory([socket])
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory))

    async def _finish_failed_close() -> None:
        await asyncio.sleep(0)
        stt._event_queue.put_nowait(None)

    stt._close_task = asyncio.create_task(_finish_failed_close())

    events = await _run_persistent_turn(stt)

    assert [event.text for event in events if event.type is STTEventType.FINAL] == ["turn 1"]
    await stt.aclose()


@pytest.mark.asyncio
async def test_openai_realtime_serializes_overlapping_commits():
    socket = _PersistentMockWSConnection(respond_to_commit=False)
    factory = _PersistentMockWSFactory([socket])
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory))
    await stt.start_stream()
    chunks = make_audio_chunks(generate_pcm_sine(duration_ms=200))
    await stt.send_audio(chunks[0])
    assert await stt.commit_segment()
    await stt.send_audio(chunks[1])

    second_commit = asyncio.create_task(stt.commit_segment())
    await asyncio.sleep(0)
    assert socket.commit_count == 1
    assert not second_commit.done()

    await socket.push(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-1",
            "transcript": "first segment",
        }
    )
    assert await asyncio.wait_for(second_commit, timeout=0.1)
    assert socket.commit_count == 2

    end_task = asyncio.create_task(stt.end_stream())
    await asyncio.sleep(0)
    assert not end_task.done()
    await socket.push(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-2",
            "transcript": "second segment",
        }
    )
    await asyncio.wait_for(end_task, timeout=0.1)

    assert stt._ws is not None
    assert stt._ws.is_connected
    await stt.aclose()


@pytest.mark.asyncio
async def test_openai_realtime_persistent_end_clears_short_tail():
    socket = _PersistentMockWSConnection()
    factory = _PersistentMockWSFactory([socket])
    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key="sk-test", ws_connect=factory))
    await stt.warmup()

    first_events = await _run_persistent_turn(stt, duration_ms=20)
    second_events = await _run_persistent_turn(stt, duration_ms=100)

    assert first_events == []
    assert socket.clear_count == 1
    assert socket.commit_count == 1
    assert [event.text for event in second_events if event.type is STTEventType.FINAL] == [
        "turn 1"
    ]
    assert factory.call_count == 1
    await stt.aclose()


@pytest.mark.asyncio
async def test_openai_realtime_final_timeout_discards_socket_before_next_turn():
    first_socket = _PersistentMockWSConnection(respond_to_commit=False)
    second_socket = _PersistentMockWSConnection()
    factory = _PersistentMockWSFactory([first_socket, second_socket])
    stt = OpenAIRealtimeSTT(
        OpenAIRealtimeSTTConfig(
            api_key="sk-test",
            final_transcript_timeout_s=0.01,
            ws_connect=factory,
        )
    )
    await stt.warmup()

    await stt.start_stream()
    collector = asyncio.create_task(_collect_events(stt))
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])
    await first_socket.push(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": "best interim",
        }
    )
    await stt.end_stream()
    first_events = await collector

    assert [(event.type, event.text) for event in first_events] == [
        (STTEventType.PARTIAL, "best interim"),
        (STTEventType.FINAL, "best interim"),
    ]
    assert first_socket.close_code == 1000

    second_events = await _run_persistent_turn(stt)
    assert [event.text for event in second_events if event.type is STTEventType.FINAL] == [
        "turn 1"
    ]
    assert factory.call_count == 2
    await stt.aclose()


async def _collect_events(stt: OpenAIRealtimeSTT) -> list[STTEvent]:
    return [event async for event in stt.events()]


@pytest.mark.asyncio
async def test_openai_realtime_reusable_across_streams():
    factory1 = _MockWSFactory([_make_transcription_completed("stream one")])
    config = OpenAIRealtimeSTTConfig(api_key="sk-test", persistent_ws=False, ws_connect=factory1)
    stt = OpenAIRealtimeSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    events1 = await collect_stt_events(stt, chunks)
    finals1 = [e for e in events1 if e.type == STTEventType.FINAL]
    assert len(finals1) == 1
    assert finals1[0].text == "stream one"

    # Second stream with a fresh factory.
    factory2 = _MockWSFactory([_make_transcription_completed("stream two")])
    stt._config = OpenAIRealtimeSTTConfig(
        api_key="sk-test", persistent_ws=False, ws_connect=factory2
    )

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
    from easycat.errors import EasyCatError
    from easycat.stt.factory import STTProviderConfig, create_stt_provider

    with pytest.raises(EasyCatError) as exc_info:
        create_stt_provider(STTProviderConfig(provider="nonexistent", api_key="k"))
    assert exc_info.value.code == "EASYCAT_E104"


# ── Live integration ─────────────────────────────────────────────


@pytest.mark.integration_live
@pytest.mark.provider_openai
@pytest.mark.surface_stt
async def test_live_openai_realtime_stt():
    """Integration test requiring OPENAI_API_KEY env var."""
    import os

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set")

    stt = OpenAIRealtimeSTT(OpenAIRealtimeSTTConfig(api_key=api_key))

    pcm = generate_pcm_sine(duration_ms=500, sample_rate=16000)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))
    # Tone isn't real speech; smoke-gates auth + Realtime WebSocket
    # session negotiation.
    assert isinstance(events, list)
