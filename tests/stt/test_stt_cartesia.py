"""Tests for the Cartesia streaming STT provider."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from easycat.events import Error, ErrorStage, EventBus, STTEvent, STTEventType
from easycat.stt.cartesia_provider import CartesiaSTT, CartesiaSTTConfig
from tests.stt.helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks


class MockWebSocket:
    """Mock WebSocket connection for Cartesia STT tests."""

    def __init__(self, messages: list[str | bytes] | None = None) -> None:
        self.messages = messages or []
        self.sent: list[bytes | str] = []
        self._closed = False
        self._iter_index = 0

    async def send(self, data: bytes | str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str | bytes:
        if self._iter_index >= len(self.messages):
            raise StopAsyncIteration
        msg = self.messages[self._iter_index]
        self._iter_index += 1
        return msg


class PersistentMockWebSocket:
    """Queue-backed socket used to exercise reconnect behavior."""

    _STOP = object()

    def __init__(self) -> None:
        self.sent: list[bytes | str] = []
        self.close_code: int | None = None
        self._queue: asyncio.Queue[str | bytes | object] = asyncio.Queue()

    async def send(self, data: bytes | str) -> None:
        self.sent.append(data)

    async def push(self, message: str | bytes) -> None:
        await self._queue.put(message)

    async def close(self) -> None:
        if self.close_code is not None:
            return
        self.close_code = 1000
        await self._queue.put(self._STOP)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str | bytes:
        message = await self._queue.get()
        if message is self._STOP:
            raise StopAsyncIteration
        assert isinstance(message, str | bytes)
        return message


class DropAfterAudioWebSocket(PersistentMockWebSocket):
    """Socket that drops after accepting one audio frame."""

    _DROP = object()

    def __init__(self) -> None:
        super().__init__()
        self._dropped = False

    async def send(self, data: bytes | str) -> None:
        await super().send(data)
        if self._dropped or not isinstance(data, bytes):
            return
        self._dropped = True
        await self._queue.put(self._DROP)

    async def __anext__(self) -> str | bytes:
        message = await self._queue.get()
        if message is self._DROP:
            close_frame = websockets.frames.Close(1006, "abnormal")
            raise websockets.exceptions.ConnectionClosed(close_frame, None)
        if message is self._STOP:
            raise StopAsyncIteration
        assert isinstance(message, str | bytes)
        return message


def _transcript_msg(
    text: str,
    *,
    is_final: bool = False,
    confidence: float | None = None,
    language: str | None = None,
    words: list[dict] | None = None,
) -> str:
    payload: dict[str, object] = {
        "type": "transcript",
        "request_id": "req-1",
        "text": text,
        "is_final": is_final,
        "duration": 0.5,
    }
    if confidence is not None:
        payload["confidence"] = confidence
    if language is not None:
        payload["language"] = language
    if words is not None:
        payload["words"] = words
    return json.dumps(payload)


def _error_msg(code: str = "invalid_input", status_code: int = 400) -> str:
    return json.dumps(
        {
            "type": "error",
            "code": code,
            "status_code": status_code,
            "title": "Bad request",
            "message": "sample_rate must be a positive integer",
            "request_id": "req-1",
        }
    )


async def _no_reconnect_backoff(delay: float) -> None:
    """Fail a test that reaches the reconnect backoff instead of waiting it out."""
    raise AssertionError(f"unexpected reconnect backoff of {delay}s")


class ServerClosingMockWebSocket:
    """Socket that terminates itself after ``close``, as Cartesia does.

    ``MockWebSocket`` raises ``StopAsyncIteration`` without setting
    ``close_code``, so ``recv_iter`` returns cleanly and the reconnect path is
    never exercised — the real ``websockets`` client sets ``close_code`` on a
    server close (gh 1066).
    """

    _STOP = object()

    def __init__(self, messages: list[str | bytes] | None = None) -> None:
        self.sent: list[bytes | str] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._queue: asyncio.Queue[str | bytes | object] = asyncio.Queue()
        for message in messages or []:
            self._queue.put_nowait(message)

    async def send(self, data: bytes | str) -> None:
        self.sent.append(data)
        if data == "close":
            self._queue.put_nowait(json.dumps({"type": "done"}))
            self._queue.put_nowait(self._STOP)

    async def close(self) -> None:
        self._queue.put_nowait(self._STOP)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str | bytes:
        message = await self._queue.get()
        if message is self._STOP:
            self.close_code = 1000
            self.close_reason = "server closed after close"
            raise StopAsyncIteration
        assert isinstance(message, (str, bytes))
        return message


def _make_cartesia_stt(
    messages: list[str | bytes] | None = None,
    *,
    event_bus=None,
    language: str = "en",
) -> tuple[CartesiaSTT, MockWebSocket]:
    ws = MockWebSocket(messages or [])

    async def mock_connect(url: str, **kwargs) -> MockWebSocket:
        return ws

    config = CartesiaSTTConfig(
        api_key="test-key",
        language=language,
        ws_connect=mock_connect,
        event_bus=event_bus,
    )
    return CartesiaSTT(config), ws


# ── Basic streaming ──────────────────────────────────────────────


async def test_cartesia_receives_final_transcript():
    messages = [_transcript_msg("hello world", is_final=True)]
    stt, _ = _make_cartesia_stt(messages)

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1
    assert events[0].type == STTEventType.FINAL
    assert events[0].text == "hello world"


async def test_cartesia_receives_partial_and_final():
    messages = [
        _transcript_msg("hel", is_final=False),
        _transcript_msg("hello world", is_final=True),
    ]
    stt, _ = _make_cartesia_stt(messages)

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 2
    assert events[0].type == STTEventType.PARTIAL
    assert events[1].type == STTEventType.FINAL


async def test_cartesia_sends_audio_bytes():
    stt, ws = _make_cartesia_stt([])

    pcm = generate_pcm_sine(duration_ms=200)
    chunks = make_audio_chunks(pcm, chunk_duration_ms=100)

    await stt.start_stream()
    for c in chunks:
        await stt.send_audio(c)
    await stt.end_stream()

    audio_sent = [s for s in ws.sent if isinstance(s, bytes)]
    assert len(audio_sent) == len(chunks)


def _is_json_object(message: str) -> bool:
    """Whether *message* is a JSON object rather than a bare text command."""
    try:
        return isinstance(json.loads(message), dict)
    except ValueError:
        return False


async def test_cartesia_sends_close_text_on_end_stream():
    """``close`` is the client's end-of-session command (gh 1065).

    It flushes remaining audio and is acked with ``{"type": "done"}``.
    ``done`` is the server's ack keyword, so sending it back left the
    utterance tail untranscribed and the drain waiting out its full timeout.
    """
    stt, ws = _make_cartesia_stt([])

    await stt.start_stream()
    await stt.end_stream()

    text_sent = [s for s in ws.sent if isinstance(s, str)]
    assert "close" in text_sent
    # The control commands are raw text, never JSON envelopes.
    assert not any(_is_json_object(s) for s in text_sent)


async def test_cartesia_finalize_sends_finalize_text():
    """``finalize`` is a raw text message, not ``{"type": "finalize"}``.

    The JSON form is not a command Cartesia recognizes, so the server never
    flushed while ``commit_segment()`` still reported success (gh 1065).
    """
    stt, ws = _make_cartesia_stt([])
    await stt.start_stream()

    result = await stt.commit_segment()
    assert result is True

    text_sent = [s for s in ws.sent if isinstance(s, str)]
    assert "finalize" in text_sent
    assert not any(_is_json_object(s) for s in text_sent)

    await stt.end_stream()


async def test_cartesia_close_does_not_reconnect_or_stall(monkeypatch):
    """Cartesia's expected end-of-session close must be terminal (gh 1066).

    ``_connect_websocket`` defaults to a no-op ``on_reconnect`` hook so a
    transient drop reconnects, and ``recv_iter`` treated *any*
    ``ConnectionClosed`` — a graceful code-1000 close included — as such a
    drop. The server closing after acking ``close`` therefore opened a
    replacement socket just to tear it down and left the receive task alive
    until the whole close timeout expired, on every turn.
    """
    connects = 0

    async def mock_connect(url: str, **kwargs) -> ServerClosingMockWebSocket:
        nonlocal connects
        connects += 1
        return ServerClosingMockWebSocket([_transcript_msg("hello", is_final=True)])

    errors: list[Error] = []
    bus = EventBus()
    bus.subscribe(Error, errors.append)
    stt = CartesiaSTT(CartesiaSTTConfig(api_key="k", ws_connect=mock_connect, event_bus=bus))
    monkeypatch.setattr("easycat.reconnecting_ws.asyncio.sleep", _no_reconnect_backoff)

    events = await asyncio.wait_for(
        collect_stt_events(stt, make_audio_chunks(generate_pcm_sine(duration_ms=100))),
        timeout=2,
    )

    assert [e.text for e in events] == ["hello"]
    assert connects == 1, "the expected close must not open a replacement socket"
    assert errors == [], f"no provider error is warranted here: {errors}"


async def test_cartesia_commit_segment_before_start_returns_false():
    stt, _ = _make_cartesia_stt([])
    assert await stt.commit_segment() is False


# ── Metadata ─────────────────────────────────────────────────────


async def test_cartesia_includes_confidence():
    messages = [_transcript_msg("test", is_final=True, confidence=0.92)]
    stt, _ = _make_cartesia_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].confidence == 0.92


async def test_cartesia_includes_word_timestamps():
    words = [
        {"word": "hello", "start": 0.0, "end": 0.3},
        {"word": "world", "start": 0.4, "end": 0.7},
    ]
    messages = [_transcript_msg("hello world", is_final=True, words=words)]
    stt, _ = _make_cartesia_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].word_timestamps is not None
    assert len(events[0].word_timestamps) == 2
    assert events[0].word_timestamps[0].word == "hello"
    assert events[0].word_timestamps[1].end == 0.7


async def test_cartesia_accepts_text_word_timestamp_key():
    words = [{"text": "hello", "start": 0.0, "end": 0.3}]
    messages = [_transcript_msg("hello", is_final=True, words=words)]
    stt, _ = _make_cartesia_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].word_timestamps is not None
    assert events[0].word_timestamps[0].word == "hello"


async def test_cartesia_language_from_config_when_missing_in_msg():
    messages = [_transcript_msg("bonjour", is_final=True)]
    stt, _ = _make_cartesia_stt(messages, language="fr")

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].language == "fr"


async def test_cartesia_language_from_transcript_overrides_config():
    messages = [_transcript_msg("hola", is_final=True, language="es")]
    stt, _ = _make_cartesia_stt(messages, language="en")

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].language == "es"


# ── Filtering ────────────────────────────────────────────────────


async def test_cartesia_ignores_empty_transcript():
    messages = [_transcript_msg("", is_final=False)]
    stt, _ = _make_cartesia_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 0


async def test_cartesia_ignores_unknown_message_types():
    messages = [
        json.dumps({"type": "flush_done", "request_id": "abc"}),
        _transcript_msg("hello", is_final=True),
    ]
    stt, _ = _make_cartesia_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1
    assert events[0].text == "hello"


async def test_cartesia_ignores_malformed_json():
    messages = [
        b"\x00\x01",
        "not valid json",
        _transcript_msg("hello", is_final=True),
    ]
    stt, _ = _make_cartesia_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1
    assert events[0].text == "hello"


# ── Errors ───────────────────────────────────────────────────────


async def test_cartesia_error_message_posted_to_event_bus():
    bus = EventBus()
    errors: list[Error] = []
    bus.subscribe(Error, lambda e: errors.append(e))

    stt, _ = _make_cartesia_stt([_error_msg()], event_bus=bus)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    # Event bus emission is scheduled via create_task — yield once.
    await asyncio.sleep(0)
    assert len(errors) == 1
    err = errors[0]
    assert err.stage == ErrorStage.STT
    assert err.provider == "cartesia"
    notes = getattr(err.exception, "__notes__", [])
    assert any("code=invalid_input" in n for n in notes)
    assert any("status_code=400" in n for n in notes)


# ── URL building ─────────────────────────────────────────────────


def test_cartesia_build_url_carries_required_params():
    config = CartesiaSTTConfig(
        api_key="k",
        model="ink-whisper",
        language="en",
        sample_rate=16000,
    )
    stt = CartesiaSTT(config)
    url = stt._build_url()

    assert url.startswith("wss://api.cartesia.ai/stt/websocket?")
    assert "model=ink-whisper" in url
    assert "language=en" in url
    assert "encoding=pcm_s16le" in url
    assert "sample_rate=16000" in url
    assert "min_volume=" in url
    assert "max_silence_duration_secs=" in url


def test_cartesia_build_url_omits_volume_gate_params_for_ink2():
    # ink-2 endpoints via native semantic turn detection and rejects the
    # volume-gate params, so they must not appear in the URL.
    config = CartesiaSTTConfig(api_key="k", model="ink-2", sample_rate=16000)
    stt = CartesiaSTT(config)
    url = stt._build_url()

    assert "model=ink-2" in url
    assert "min_volume=" not in url
    assert "max_silence_duration_secs=" not in url


def test_cartesia_default_model_is_ink2():
    config = CartesiaSTTConfig(api_key="k")
    assert config.resolved_model == "ink-2"
    assert config.uses_volume_gate is False


@pytest.mark.parametrize("encoding", ["pcm_s16le", "PCM_S16LE", " pcm_s16le "])
def test_cartesia_config_normalizes_pcm16_encoding(encoding: str):
    config = CartesiaSTTConfig(api_key="k", encoding=encoding)

    assert config.encoding == "pcm_s16le"


@pytest.mark.parametrize("encoding", ["pcm_f32le", "mulaw", "", None])
def test_cartesia_config_rejects_unsupported_encoding(encoding: object):
    with pytest.raises(ValueError, match="Unsupported Cartesia STT encoding"):
        CartesiaSTTConfig(api_key="k", encoding=encoding)  # type: ignore[arg-type]


def test_cartesia_default_falls_back_to_ink_whisper_for_non_english():
    # ink-2 is English-only; a non-English config with no explicit model must
    # resolve to the multilingual ink-whisper (and use its volume-gate params).
    config = CartesiaSTTConfig(api_key="k", language="fr")
    assert config.resolved_model == "ink-whisper"
    assert config.uses_volume_gate is True

    url = CartesiaSTT(config)._build_url()
    assert "model=ink-whisper" in url
    assert "language=fr" in url
    assert "min_volume=" in url


def test_cartesia_explicit_ink2_honored_for_non_english():
    # An explicit model is always honored, even for non-English.
    config = CartesiaSTTConfig(api_key="k", model="ink-2", language="fr")
    assert config.resolved_model == "ink-2"


async def test_cartesia_ignores_turn_lifecycle_events():
    # ink-2 emits a turn.* lifecycle alongside transcripts; these carry no
    # text and must be acknowledged without producing STT events.
    messages = [
        json.dumps({"type": "turn.start", "request_id": "r1"}),
        _transcript_msg("hello", is_final=False),
        json.dumps({"type": "turn.eager_end", "request_id": "r1"}),
        _transcript_msg("hello world", is_final=True),
        json.dumps({"type": "turn.end", "request_id": "r1"}),
    ]
    stt, _ = _make_cartesia_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert [e.type for e in events] == [STTEventType.PARTIAL, STTEventType.FINAL]
    assert events[-1].text == "hello world"


async def test_cartesia_reconnect_promotes_partial_before_replacement_final():
    first_socket = DropAfterAudioWebSocket()
    second_socket = PersistentMockWebSocket()
    reconnected = asyncio.Event()
    partial_seen = asyncio.Event()
    boundary_final_seen = asyncio.Event()
    replacement_final_seen = asyncio.Event()
    connect_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal connect_count
        connect_count += 1
        if connect_count == 2:
            reconnected.set()
            return second_socket
        return first_socket

    stt = CartesiaSTT(CartesiaSTTConfig(api_key="k", ws_connect=mock_connect))
    emitted = []

    def emit(event):
        emitted.append(event)
        if event.type == STTEventType.PARTIAL:
            partial_seen.set()
        if event.type == STTEventType.FINAL and event.text == "before reconnect":
            boundary_final_seen.set()
        if event.type == STTEventType.FINAL and event.text == "after reconnect":
            replacement_final_seen.set()

    stt._emit_event = emit  # type: ignore[method-assign]
    try:
        await stt.start_stream()
        await first_socket.push(_transcript_msg("before reconnect", is_final=False))
        await asyncio.wait_for(partial_seen.wait(), timeout=0.5)

        await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])
        await asyncio.wait_for(reconnected.wait(), timeout=0.5)
        await asyncio.wait_for(boundary_final_seen.wait(), timeout=0.5)
        await second_socket.push(_transcript_msg("after reconnect", is_final=True))
        await asyncio.wait_for(replacement_final_seen.wait(), timeout=0.5)
        await stt._close_active_websocket(close_before_drain=True)

        assert [(event.type, event.text) for event in emitted] == [
            (STTEventType.PARTIAL, "before reconnect"),
            (STTEventType.FINAL, "before reconnect"),
            (STTEventType.FINAL, "after reconnect"),
        ]
        assert emitted[1].ends_turn is False
        assert emitted[2].ends_turn is True
    finally:
        await stt.close()


async def test_cartesia_reconnect_does_not_finalize_replacement_socket_send():
    """Audio fenced behind reconnect belongs to the replacement epoch."""
    stt = CartesiaSTT(CartesiaSTTConfig(api_key="k"))
    emitted: list[STTEvent] = []
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def blocked_send(_data: bytes) -> None:
        send_started.set()
        await release_send.wait()

    stt._emit_event = emitted.append  # type: ignore[method-assign]
    stt._send_ws = blocked_send  # type: ignore[method-assign]
    stt._latest_partial = STTEvent(type=STTEventType.PARTIAL, text="dropped socket")

    send_task = asyncio.create_task(stt._append_audio(b"replacement audio"))
    try:
        await asyncio.wait_for(send_started.wait(), timeout=0.5)
        assert stt._audio_epoch == 0

        await stt._on_reconnect()
        assert stt._finalized_epoch == 0

        release_send.set()
        await asyncio.wait_for(send_task, timeout=0.5)
        assert stt._audio_epoch == 1

        stt._latest_partial = STTEvent(type=STTEventType.PARTIAL, text="replacement socket")
        await stt._on_reconnect()

        assert [event.text for event in emitted] == ["dropped socket", "replacement socket"]
    finally:
        release_send.set()
        await asyncio.gather(send_task, return_exceptions=True)


async def test_cartesia_resamples_replacement_audio_after_reconnect_reset():
    class ProbeResampler:
        def __init__(self) -> None:
            self.state = "dropped"
            self.processed_states: list[str] = []

        def reset(self) -> None:
            self.state = "replacement"

        def process(self, data: bytes, sample_rate: int) -> bytes:
            _ = data, sample_rate
            self.processed_states.append(self.state)
            return self.state.encode()

    class FencedSocket:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.sent: list[bytes] = []

        async def send_prepared(self, prepare):
            self.entered.set()
            await self.release.wait()
            message = prepare()
            if message is None:
                return False
            self.sent.append(message)
            return True

    stt = CartesiaSTT(CartesiaSTTConfig(api_key="k"))
    resampler = ProbeResampler()
    socket = FencedSocket()
    stt._audio_resampler = resampler  # type: ignore[assignment]
    stt._ws = socket  # type: ignore[assignment]
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=20))[0]

    send_task = asyncio.create_task(stt._on_audio(chunk))
    await asyncio.wait_for(socket.entered.wait(), timeout=0.5)
    assert resampler.processed_states == []

    await stt._on_reconnect()
    socket.release.set()
    await asyncio.wait_for(send_task, timeout=0.5)

    assert resampler.processed_states == ["replacement"]
    assert socket.sent == [b"replacement"]


# ── Multiple streams ─────────────────────────────────────────────


async def test_cartesia_reusable_across_streams():
    call_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockWebSocket([_transcript_msg(f"stream {call_count}", is_final=True)])

    config = CartesiaSTTConfig(api_key="k", ws_connect=mock_connect)
    stt = CartesiaSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    events1 = await collect_stt_events(stt, chunks)
    assert events1[0].text == "stream 1"

    events2 = await collect_stt_events(stt, chunks)
    assert events2[0].text == "stream 2"


# ── Version info ─────────────────────────────────────────────────


def test_cartesia_version_info_shape():
    stt, _ = _make_cartesia_stt()
    info = stt.version_info()
    assert info["provider"] == "cartesia"
    assert info["model"] == "ink-2"
    assert "api_version" in info
    assert "sdk_version" in info


# ── Live integration ─────────────────────────────────────────────


@pytest.mark.integration_live
@pytest.mark.provider_cartesia
@pytest.mark.surface_stt
async def test_live_cartesia_stt():
    """Integration test requiring CARTESIA_API_KEY env var."""
    import os

    api_key = os.environ.get("CARTESIA_API_KEY")
    if not api_key:
        pytest.skip("CARTESIA_API_KEY not set")

    config = CartesiaSTTConfig(api_key=api_key)
    stt = CartesiaSTT(config)

    pcm = generate_pcm_sine(duration_ms=500, sample_rate=16000)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))
    # Silence / tone can't produce a real transcript — we just verify
    # the round-trip completes without error.
    assert isinstance(events, list)
