"""Tests for the Deepgram streaming STT provider."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from easycat._concurrency import RuntimeSupervisor
from easycat.events import Error, ErrorStage, EventBus, STTEventType
from easycat.runtime.scope import RuntimeScope
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from tests.stt.helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks


class MockWebSocket:
    """Mock WebSocket connection for Deepgram tests."""

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
    """Queue-backed socket that stays open across logical STT turns."""

    _STOP = object()

    def __init__(self, *, respond_to_finalize: bool = True) -> None:
        self.sent: list[bytes | str] = []
        self.close_code: int | None = None
        self.finalize_count = 0
        self.finalize_sent = asyncio.Event()
        self._respond_to_finalize = respond_to_finalize
        self._queue: asyncio.Queue[str | bytes | object] = asyncio.Queue()

    async def send(self, data: bytes | str) -> None:
        self.sent.append(data)
        if not isinstance(data, str):
            return
        message = json.loads(data)
        if message.get("type") == "Finalize":
            self.finalize_count += 1
            self.finalize_sent.set()
            if self._respond_to_finalize:
                await self.push_result(f"turn {self.finalize_count}", is_final=True)
                # Let the provider's receive loop observe an acknowledgment
                # before this send returns, exercising the reserve-before-send
                # race in the production socket path.
                await asyncio.sleep(0)

    async def push_result(
        self,
        text: str,
        *,
        is_final: bool,
        from_finalize: bool | None = None,
    ) -> None:
        if from_finalize is None:
            from_finalize = is_final
        await self._queue.put(
            _deepgram_result(text, is_final=is_final, from_finalize=from_finalize)
        )

    async def push_message(self, message: dict[str, object]) -> None:
        await self._queue.put(json.dumps(message))

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
        assert isinstance(message, (str, bytes))
        return message


class BufferedFinalOnCloseWebSocket(PersistentMockWebSocket):
    """Socket holding one Finalize-triggered final that surfaces during close.

    Models the websockets library delivering a frame that was already received
    before ``close()`` completes: the buffered Results frame is queued ahead of
    the close marker, so the provider's discard-drain still parses it.
    """

    def __init__(self, buffered_final: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._buffered_final = buffered_final

    async def close(self) -> None:
        if self.close_code is None:
            await self.push_result(self._buffered_final, is_final=True, from_finalize=True)
        await super().close()


class DropAfterFinalizeWebSocket(PersistentMockWebSocket):
    """Socket that drops after accepting a Finalize without acknowledging it."""

    _DROP = object()

    def __init__(self) -> None:
        super().__init__(respond_to_finalize=False)
        self._dropped = False

    async def send(self, data: bytes | str) -> None:
        await super().send(data)
        if self._dropped or not isinstance(data, str):
            return
        if json.loads(data).get("type") == "Finalize":
            self._dropped = True
            await self._queue.put(self._DROP)

    async def __anext__(self) -> str | bytes:
        message = await self._queue.get()
        if message is self._DROP:
            close_frame = websockets.frames.Close(1006, "abnormal")
            raise websockets.exceptions.ConnectionClosed(close_frame, None)
        if message is self._STOP:
            raise StopAsyncIteration
        assert isinstance(message, (str, bytes))
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
        assert isinstance(message, (str, bytes))
        return message


def _deepgram_result(
    transcript: str,
    is_final: bool = False,
    confidence: float = 0.95,
    words: list[dict] | None = None,
    speech_final: bool | None = None,
    from_finalize: bool | None = None,
) -> str:
    """Create a Deepgram-format Results message."""
    alt: dict = {"transcript": transcript, "confidence": confidence}
    if words:
        alt["words"] = words
    payload: dict[str, object] = {
        "type": "Results",
        "channel": {"alternatives": [alt]},
        "is_final": is_final,
    }
    if speech_final is not None:
        payload["speech_final"] = speech_final
    if from_finalize is not None:
        payload["from_finalize"] = from_finalize
    return json.dumps(payload)


def _deepgram_turn_info(
    transcript: str,
    *,
    event: str = "Update",
    end_of_turn_confidence: float | None = None,
) -> str:
    payload: dict[str, object] = {
        "type": "TurnInfo",
        "event": event,
        "transcript": transcript,
    }
    if end_of_turn_confidence is not None:
        payload["end_of_turn_confidence"] = end_of_turn_confidence
    return json.dumps(payload)


async def _no_reconnect_backoff(delay: float) -> None:
    """Fail a test that reaches the reconnect backoff instead of waiting it out."""
    raise AssertionError(f"unexpected reconnect backoff of {delay}s")


class ServerClosingMockWebSocket:
    """Socket that terminates itself after ``CloseStream``, as Deepgram does.

    Deepgram's docs say the server answers ``CloseStream`` with the remaining
    results and metadata "and then terminate[s] the WebSocket connection".
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
        if not isinstance(data, str):
            return
        if json.loads(data).get("type") == "CloseStream":
            self._queue.put_nowait(json.dumps({"type": "Metadata"}))
            self._queue.put_nowait(self._STOP)

    async def close(self) -> None:
        self._queue.put_nowait(self._STOP)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str | bytes:
        message = await self._queue.get()
        if message is self._STOP:
            self.close_code = 1000
            self.close_reason = "server closed after CloseStream"
            raise StopAsyncIteration
        assert isinstance(message, (str, bytes))
        return message


def _make_deepgram_stt(
    messages: list[str | bytes] | None = None,
    *,
    event_bus=None,
    model: str = "nova-2",
    persistent_ws: bool = False,
) -> tuple[DeepgramSTT, MockWebSocket]:
    """Create a DeepgramSTT with a mocked WebSocket."""
    ws = MockWebSocket(messages or [])

    async def mock_connect(url: str, **kwargs) -> MockWebSocket:
        return ws

    config = DeepgramSTTConfig(
        api_key="test-key",
        model=model,
        persistent_ws=persistent_ws,
        ws_connect=mock_connect,
        event_bus=event_bus,
    )
    return DeepgramSTT(config), ws


# ── Basic streaming ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deepgram_receives_final_transcript():
    messages = [_deepgram_result("hello world", is_final=True)]
    stt, _ws = _make_deepgram_stt(messages)

    pcm = generate_pcm_sine(duration_ms=200)
    chunks = make_audio_chunks(pcm)
    events = await collect_stt_events(stt, chunks)

    assert len(events) == 1
    assert events[0].type == STTEventType.FINAL
    assert events[0].text == "hello world"


@pytest.mark.asyncio
async def test_deepgram_receives_partial_and_final():
    messages = [
        _deepgram_result("hel", is_final=False),
        _deepgram_result("hello world", is_final=True),
    ]
    stt, _ws = _make_deepgram_stt(messages)

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 2
    assert events[0].type == STTEventType.PARTIAL
    assert events[0].text == "hel"
    assert events[1].type == STTEventType.FINAL
    assert events[1].text == "hello world"


@pytest.mark.asyncio
async def test_deepgram_sends_audio_bytes():
    stt, ws = _make_deepgram_stt([])

    pcm = generate_pcm_sine(duration_ms=200)
    chunks = make_audio_chunks(pcm, chunk_duration_ms=100)

    await stt.start_stream()
    for c in chunks:
        await stt.send_audio(c)
    await stt.end_stream()

    # Audio chunks should have been sent as raw bytes
    audio_sent = [s for s in ws.sent if isinstance(s, bytes)]
    assert len(audio_sent) == len(chunks)


@pytest.mark.asyncio
async def test_deepgram_resamples_mismatched_rate_instead_of_raising():
    # Deepgram is configured for 16 kHz but receives 48 kHz audio. It should
    # resample down to its configured rate rather than raising a ValueError,
    # matching the realtime providers' contract.
    from easycat.audio_format import AudioChunk, AudioFormat

    stt, ws = _make_deepgram_stt([])
    stt._config.sample_rate = 16000

    pcm_48k = generate_pcm_sine(duration_ms=100, sample_rate=48000)
    chunk = AudioChunk(
        data=pcm_48k,
        format=AudioFormat(sample_rate=48000, channels=1, sample_width=2),
    )

    await stt.start_stream()
    await stt.send_audio(chunk)
    await stt.end_stream()

    audio_sent = [s for s in ws.sent if isinstance(s, bytes)]
    assert audio_sent
    # Resampled 48k -> 16k should be roughly one third the byte count.
    assert sum(map(len, audio_sent)) < len(pcm_48k)
    assert sum(map(len, audio_sent)) == len(pcm_48k) // 3


@pytest.mark.asyncio
async def test_deepgram_sends_close_stream():
    stt, ws = _make_deepgram_stt([])

    await stt.start_stream()
    await stt.end_stream()

    # Should have sent a CloseStream JSON message
    json_sent = [s for s in ws.sent if isinstance(s, str)]
    assert any('"CloseStream"' in s for s in json_sent)


# ── Confidence and metadata ──────────────────────────────────────


@pytest.mark.asyncio
async def test_deepgram_includes_confidence():
    messages = [_deepgram_result("test", is_final=True, confidence=0.98)]
    stt, _ = _make_deepgram_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].confidence == 0.98


@pytest.mark.asyncio
async def test_deepgram_includes_language():
    messages = [_deepgram_result("test", is_final=True)]
    ws = MockWebSocket(messages)

    async def mock_connect(url, **kwargs):
        return ws

    config = DeepgramSTTConfig(
        api_key="k", language="fr", persistent_ws=False, ws_connect=mock_connect
    )
    stt = DeepgramSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].language == "fr"


@pytest.mark.asyncio
async def test_deepgram_includes_word_timestamps():
    words = [
        {"word": "hello", "start": 0.0, "end": 0.3},
        {"word": "world", "start": 0.4, "end": 0.7},
    ]
    messages = [_deepgram_result("hello world", is_final=True, words=words)]
    stt, _ = _make_deepgram_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].word_timestamps is not None
    assert len(events[0].word_timestamps) == 2
    assert events[0].word_timestamps[0].word == "hello"
    assert events[0].word_timestamps[1].end == 0.7


@pytest.mark.asyncio
async def test_deepgram_accepts_text_word_timestamp_key():
    words = [{"text": "hello", "start": 0.0, "end": 0.3}]
    messages = [_deepgram_result("hello", is_final=True, words=words)]
    stt, _ = _make_deepgram_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].word_timestamps is not None
    assert events[0].word_timestamps[0].word == "hello"


# ── Ignores non-transcript messages ─────────────────────────────


@pytest.mark.asyncio
async def test_deepgram_ignores_non_results_messages():
    messages = [
        json.dumps({"type": "Metadata", "request_id": "abc"}),
        _deepgram_result("hello", is_final=True),
    ]
    stt, _ = _make_deepgram_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1
    assert events[0].text == "hello"


@pytest.mark.asyncio
async def test_deepgram_ignores_binary_and_malformed_json_messages():
    messages = [
        b"\x00\x01",
        "{not json",
        _deepgram_result("hello", is_final=True),
    ]
    stt, _ = _make_deepgram_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1
    assert events[0].text == "hello"


@pytest.mark.asyncio
async def test_deepgram_ignores_empty_transcript():
    messages = [_deepgram_result("", is_final=False)]
    stt, _ = _make_deepgram_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 0


# ── URL building ─────────────────────────────────────────────────


def test_deepgram_config_constructs_without_api_key():
    # ``api_key`` defaults to ``""`` to support the inject-the-key-later
    # workflow, matching every sibling STT/TTS config (no construction-time
    # TypeError when the key is supplied later).
    config = DeepgramSTTConfig(model="nova-2")
    assert config.api_key == ""
    assert config.model == "nova-2"
    assert config.persistent_ws is True
    assert config.warmup_timeout_s == 5.0


@pytest.mark.parametrize("encoding", ["linear16", "LINEAR16", " Linear16 "])
def test_deepgram_config_normalizes_linear16_encoding(encoding: str):
    config = DeepgramSTTConfig(encoding=encoding)

    assert config.encoding == "linear16"


@pytest.mark.parametrize("encoding", ["mulaw", "pcm_s16le", "", None])
def test_deepgram_config_rejects_unsupported_encoding(encoding: object):
    with pytest.raises(ValueError, match="Unsupported Deepgram STT encoding"):
        DeepgramSTTConfig(encoding=encoding)  # type: ignore[arg-type]


def test_deepgram_flux_disables_persistence_by_default():
    config = DeepgramSTTConfig(model="flux-general-en")
    assert config.persistent_ws is False


def test_deepgram_flux_rejects_explicit_persistence():
    with pytest.raises(ValueError, match="not supported for Flux"):
        DeepgramSTTConfig(model="flux-general-en", persistent_ws=True)


@pytest.mark.parametrize(
    "field",
    ("keepalive_interval_s", "warmup_timeout_s", "final_transcript_timeout_s"),
)
@pytest.mark.parametrize("value", (0.0, -1.0, float("inf"), float("nan"), True))
def test_deepgram_rejects_invalid_persistent_timing(field: str, value: float):
    with pytest.raises(ValueError, match=field):
        DeepgramSTTConfig(**{field: value})


def test_deepgram_build_url():
    config = DeepgramSTTConfig(
        api_key="k",
        model="nova-2",
        language="en",
        punctuate=True,
        interim_results=True,
    )
    stt = DeepgramSTT(config)
    url = stt._build_url()

    assert "model=nova-2" in url
    assert "language=en" in url
    assert "punctuate=true" in url
    assert "interim_results=true" in url


def test_deepgram_build_url_advertises_mono_wire_payload_after_downmix():
    stt = DeepgramSTT(DeepgramSTTConfig(api_key="k", channels=2))

    assert "channels=1" in stt._build_url()


def test_deepgram_flux_build_url_uses_v2_without_legacy_params():
    config = DeepgramSTTConfig(
        api_key="k",
        model="flux-general-en",
        language="en",
        base_url="wss://api.deepgram.com/v1/listen",
    )
    stt = DeepgramSTT(config)
    url = stt._build_url()

    assert url.startswith("wss://api.deepgram.com/v2/listen?")
    assert "model=flux-general-en" in url
    assert "language=" not in url
    assert "interim_results=" not in url
    assert "punctuate=" not in url


# ── Multiple streams ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_persistent_close_does_not_reconnect_or_stall(monkeypatch):
    """Deepgram's expected end-of-stream close must be terminal (gh 1066).

    ``_connect_new_websocket`` wires ``on_reconnect`` unconditionally, and
    ``recv_iter`` treated *any* ``ConnectionClosed`` — a graceful code-1000
    close included — as a transient drop whenever such a hook exists.  So the
    server's documented post-``CloseStream`` shutdown opened a replacement
    connection just to tear it down, emitted a spurious provider error, and
    left the receive task alive until the whole ``close_timeout`` expired:
    about five seconds of dead latency at the end of every turn, since
    ``end_stream()`` is awaited by the STT committer.
    """
    connects = 0
    sockets: list[ServerClosingMockWebSocket] = []

    async def mock_connect(url, **kwargs):
        nonlocal connects
        connects += 1
        ws = ServerClosingMockWebSocket([_deepgram_result("hello", is_final=True)])
        sockets.append(ws)
        return ws

    errors: list[Error] = []
    bus = EventBus()
    bus.subscribe(Error, errors.append)

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            persistent_ws=False,
            ws_connect=mock_connect,
            event_bus=bus,
        )
    )
    # A reconnect would sleep on the backoff; fail loudly instead of waiting.
    monkeypatch.setattr(
        "easycat.reconnecting_ws.asyncio.sleep",
        _no_reconnect_backoff,
    )

    events = await asyncio.wait_for(
        collect_stt_events(stt, make_audio_chunks(generate_pcm_sine(duration_ms=100))),
        timeout=2,
    )

    assert [e.text for e in events] == ["hello"]
    assert connects == 1, "the expected close must not open a replacement socket"
    assert errors == [], f"no provider error is warranted here: {errors}"


@pytest.mark.asyncio
async def test_non_persistent_close_records_an_abnormal_server_death():
    """An abnormal close during the expected shutdown is still an abnormality.

    Skipping the reconnect must not turn a server that dies mid-drain into a
    silent clean finish.
    """

    class _AbnormalClose(ServerClosingMockWebSocket):
        async def __anext__(self) -> str | bytes:
            message = await self._queue.get()
            if message is self._STOP:
                self.close_code = 1006
                close_frame = websockets.frames.Close(1006, "abnormal")
                raise websockets.exceptions.ConnectionClosed(close_frame, None)
            assert isinstance(message, (str, bytes))
            return message

    sockets: list[_AbnormalClose] = []

    async def mock_connect(url, **kwargs):
        ws = _AbnormalClose([_deepgram_result("hello", is_final=True)])
        sockets.append(ws)
        return ws

    stt = DeepgramSTT(DeepgramSTTConfig(api_key="k", persistent_ws=False, ws_connect=mock_connect))
    await stt.start_stream()
    socket = stt._ws
    assert socket is not None
    for chunk in make_audio_chunks(generate_pcm_sine(duration_ms=100)):
        await stt.send_audio(chunk)
    await asyncio.wait_for(stt.end_stream(), timeout=2)
    await stt.close()

    assert len(sockets) == 1
    assert socket.died_abnormally is True
    assert socket.reconnect_exhaustion_reason == ("peer closed abnormally during end-of-stream")


@pytest.mark.asyncio
async def test_deepgram_reusable_across_streams():
    call_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockWebSocket([_deepgram_result(f"stream {call_count}", is_final=True)])

    config = DeepgramSTTConfig(api_key="k", persistent_ws=False, ws_connect=mock_connect)
    stt = DeepgramSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    events1 = await collect_stt_events(stt, chunks)
    assert events1[0].text == "stream 1"

    events2 = await collect_stt_events(stt, chunks)
    assert events2[0].text == "stream 2"


@pytest.mark.asyncio
async def test_deepgram_warmup_reuses_one_socket_across_turns():
    connect_count = 0
    ws = PersistentMockWebSocket()

    async def mock_connect(url, **kwargs):
        nonlocal connect_count
        connect_count += 1
        return ws

    stt = DeepgramSTT(DeepgramSTTConfig(api_key="k", ws_connect=mock_connect))
    await stt.warmup()

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)
    observed: list[str] = []
    for _ in range(2):
        events = await collect_stt_events(stt, chunks)
        observed.extend(event.text for event in events if event.type == STTEventType.FINAL)

    assert connect_count == 1
    assert observed == ["turn 1", "turn 2"]
    assert not any(
        json.loads(frame).get("type") == "CloseStream"
        for frame in ws.sent
        if isinstance(frame, str)
    )

    await stt.aclose()
    assert ws.close_code == 1000
    assert any(
        json.loads(frame).get("type") == "CloseStream"
        for frame in ws.sent
        if isinstance(frame, str)
    )


@pytest.mark.asyncio
async def test_deepgram_aclose_ends_active_stream_before_releasing_socket():
    """aclose() must leave the provider reusable after active-stream teardown."""

    first_socket = PersistentMockWebSocket()
    second_socket = PersistentMockWebSocket()
    sockets = [first_socket, second_socket]

    async def mock_connect(url, **kwargs):
        return sockets.pop(0)

    stt = DeepgramSTT(DeepgramSTTConfig(api_key="k", ws_connect=mock_connect))
    await stt.start_stream()

    await stt.aclose()

    assert stt._running is False
    assert first_socket.close_code == 1000

    await stt.start_stream()
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_warmup_timeout_retries_on_first_stream():
    connect_count = 0
    first_connect_started = asyncio.Event()
    working = PersistentMockWebSocket()

    async def mock_connect(url, **kwargs):
        nonlocal connect_count
        connect_count += 1
        if connect_count == 1:
            first_connect_started.set()
            await asyncio.Event().wait()
        return working

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            warmup_timeout_s=0.01,
            ws_connect=mock_connect,
        )
    )

    warmup_task = asyncio.create_task(stt.warmup())
    await first_connect_started.wait()
    # Queue the first real stream behind warmup. Failed warmup cleanup must
    # finish under the lifecycle lock before this stream creates its socket.
    start_task = asyncio.create_task(stt.start_stream())
    await asyncio.wait_for(warmup_task, timeout=0.1)
    await asyncio.wait_for(start_task, timeout=0.1)

    async def collect_events():
        return [event async for event in stt.events()]

    collector = asyncio.create_task(collect_events())
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])
    await stt.end_stream()
    events = await collector

    assert [event.text for event in events if event.type == STTEventType.FINAL] == ["turn 1"]
    assert connect_count == 2
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_persistent_socket_sends_idle_keepalive():
    ws = PersistentMockWebSocket()

    async def mock_connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            keepalive_interval_s=0.01,
            ws_connect=mock_connect,
        )
    )
    root = RuntimeScope.create_root(
        name="session",
        root_id="session:test",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )
    stt.set_runtime_scope(root, name="stt-provider-runtime")
    await stt.warmup()
    await asyncio.sleep(0.03)

    assert stt._keepalive_task in root.tasks("deepgram_keepalive")
    assert any(
        json.loads(frame).get("type") == "KeepAlive" for frame in ws.sent if isinstance(frame, str)
    )
    await stt.aclose()
    assert root.tasks("deepgram_keepalive") == ()
    await root.close()


@pytest.mark.asyncio
async def test_deepgram_prewarmed_keepalive_moves_to_session_scope() -> None:
    ws = PersistentMockWebSocket()

    async def mock_connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            keepalive_interval_s=60.0,
            ws_connect=mock_connect,
        )
    )
    await stt.warmup()
    standalone = stt._runtime_scope
    receive = stt._receive_task
    keepalive = stt._keepalive_task
    assert standalone is not None
    assert receive is not None
    assert keepalive is not None

    root = RuntimeScope.create_root(
        name="session",
        root_id="session:prewarmed",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )
    stt.set_runtime_scope(root, name="stt-provider-runtime")

    assert standalone.empty
    assert root.tasks("stt_receive_loop") == (receive,)
    assert root.tasks("deepgram_keepalive") == (keepalive,)

    await stt.aclose()
    await root.close()


@pytest.mark.asyncio
async def test_deepgram_keepalive_cleanup_preserves_caller_cancellation() -> None:
    stt = DeepgramSTT(DeepgramSTTConfig(api_key="k"))
    child_cancelled = asyncio.Event()
    release_child = asyncio.Event()

    async def cancellation_resistant_keepalive() -> None:
        while not release_child.is_set():
            try:
                await release_child.wait()
            except asyncio.CancelledError:
                child_cancelled.set()

    scope = stt._ensure_runtime_scope()
    stt._keepalive_task = scope.create_task(
        "deepgram_keepalive",
        cancellation_resistant_keepalive(),
    )
    cancelling = asyncio.create_task(stt._cancel_keepalive())
    await child_cancelled.wait()
    cancelling.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cancelling

    assert stt._keepalive_task is None
    assert scope.tasks("deepgram_keepalive")

    release_child.set()
    await stt._cancel_keepalive()
    assert scope.tasks("deepgram_keepalive") == ()


@pytest.mark.asyncio
async def test_deepgram_persistent_end_waits_for_finalize_marker():
    ws = PersistentMockWebSocket(respond_to_finalize=False)

    async def mock_connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            final_transcript_timeout_s=1.0,
            ws_connect=mock_connect,
        )
    )
    await stt.warmup()
    await stt.start_stream()
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])

    end_task = asyncio.create_task(stt.end_stream())
    await ws.finalize_sent.wait()
    await ws.push_result("natural segment", is_final=True, from_finalize=False)
    await asyncio.sleep(0)
    assert not end_task.done()

    await ws.push_result("finalized tail", is_final=True, from_finalize=True)
    await end_task
    events = [event async for event in stt.events()]
    assert [event.text for event in events if event.type == STTEventType.FINAL] == [
        "natural segment",
        "finalized tail",
    ]
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_persistent_end_accepts_bare_finalize_ack():
    # A bare ``{"from_finalize": true}`` ack (no Results body) must release
    # the end-of-turn waiter promptly — but it confirms audio without its
    # transcript, so the socket is discarded (not kept warm) and the latest
    # interim is promoted instead of the utterance being silently lost.
    ws = PersistentMockWebSocket(respond_to_finalize=False)

    async def mock_connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            final_transcript_timeout_s=5.0,
            ws_connect=mock_connect,
        )
    )
    await stt.warmup()
    await stt.start_stream()
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])
    await ws.push_result("kept speech", is_final=False)
    await asyncio.sleep(0)

    end_task = asyncio.create_task(stt.end_stream())
    await ws.finalize_sent.wait()
    await ws.push_message({"from_finalize": True})
    # Completes well inside the 5 s timeout: the bare ack released the waiter.
    await asyncio.wait_for(end_task, timeout=1.0)

    # Containment: the socket whose transcript is still in flight is dropped.
    assert stt._ws is None
    assert ws.close_code == 1000
    events = [event async for event in stt.events()]
    assert [(event.type, event.text) for event in events] == [
        (STTEventType.PARTIAL, "kept speech"),
        (STTEventType.FINAL, "kept speech"),
    ]
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_bare_finalize_ack_delivers_drained_final_to_ending_turn():
    # Bare ack, with the real Finalize-triggered Results frame already
    # buffered in the close window: the containment drain must deliver it to
    # the ending turn exactly once (previously it was lost behind the dead
    # queue's sentinel or bled into the next turn).
    ws = BufferedFinalOnCloseWebSocket("real final tail", respond_to_finalize=False)

    async def mock_connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            final_transcript_timeout_s=5.0,
            ws_connect=mock_connect,
        )
    )
    await stt.warmup()
    await stt.start_stream()
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])
    await ws.push_result("best interim", is_final=False)
    await asyncio.sleep(0)

    end_task = asyncio.create_task(stt.end_stream())
    await ws.finalize_sent.wait()
    await ws.push_message({"from_finalize": True})
    await asyncio.wait_for(end_task, timeout=1.0)

    assert stt._ws is None
    events = [event async for event in stt.events()]
    finals = [event.text for event in events if event.type == STTEventType.FINAL]
    # The drained real final wins; the interim is not promoted on top of it.
    assert finals == ["real final tail"]
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_bare_finalize_ack_does_not_bleed_final_into_next_turn():
    # Bare ack, with the real Finalize-triggered Results frame arriving only
    # after the turn ended: the discarded socket can no longer route it into
    # the next turn's stream, and the next turn runs on a fresh connection.
    sockets = [
        PersistentMockWebSocket(respond_to_finalize=False),
        PersistentMockWebSocket(),
    ]
    connect_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal connect_count
        socket = sockets[connect_count]
        connect_count += 1
        return socket

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            final_transcript_timeout_s=5.0,
            ws_connect=mock_connect,
        )
    )
    await stt.warmup()
    await stt.start_stream()
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])

    end_task = asyncio.create_task(stt.end_stream())
    await sockets[0].finalize_sent.wait()
    await sockets[0].push_message({"from_finalize": True})
    await asyncio.wait_for(end_task, timeout=1.0)
    [event async for event in stt.events()]

    # Turn one's real final arrives after the turn boundary; the socket was
    # discarded, so the frame has no live receive loop to bleed through.
    await sockets[0].push_result("turn one tail", is_final=True, from_finalize=True)

    second_events = await collect_stt_events(
        stt, make_audio_chunks(generate_pcm_sine(duration_ms=100))
    )
    second_finals = [event.text for event in second_events if event.type == STTEventType.FINAL]
    assert second_finals == ["turn 1"]
    assert "turn one tail" not in [event.text for event in second_events]
    assert connect_count == 2
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_end_reuses_outstanding_finalize_request():
    ws = PersistentMockWebSocket(respond_to_finalize=False)

    async def mock_connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            final_transcript_timeout_s=1.0,
            ws_connect=mock_connect,
        )
    )
    await stt.start_stream()
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])
    assert await stt.commit_segment()
    assert ws.finalize_count == 1

    end_task = asyncio.create_task(stt.end_stream())
    await asyncio.sleep(0)
    assert ws.finalize_count == 1

    await ws.push_result("committed tail", is_final=True)
    await asyncio.wait_for(end_task, timeout=0.1)

    assert stt._ws is not None
    assert stt._ws.is_connected
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_end_serializes_finalize_for_audio_after_pending_request():
    ws = PersistentMockWebSocket(respond_to_finalize=False)

    async def mock_connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            final_transcript_timeout_s=1.0,
            ws_connect=mock_connect,
        )
    )
    await stt.start_stream()
    chunks = make_audio_chunks(generate_pcm_sine(duration_ms=200))
    await stt.send_audio(chunks[0])
    assert await stt.commit_segment()
    await stt.send_audio(chunks[1])

    end_task = asyncio.create_task(stt.end_stream())
    await asyncio.sleep(0)
    assert ws.finalize_count == 1

    await ws.push_message({"from_finalize": True})
    for _ in range(10):
        if ws.finalize_count == 2:
            break
        await asyncio.sleep(0)
    assert ws.finalize_count == 2

    await ws.push_message({"from_finalize": True})
    await asyncio.wait_for(end_task, timeout=0.1)
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_reconnect_discards_stale_finalize_before_next_turn_audio():
    """A Finalize from a dropped socket cannot suppress the replacement's one."""
    first_socket = DropAfterFinalizeWebSocket()
    second_socket = PersistentMockWebSocket()
    reconnected = asyncio.Event()
    connect_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal connect_count
        connect_count += 1
        if connect_count == 2:
            reconnected.set()
            return second_socket
        return first_socket

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            final_transcript_timeout_s=1.0,
            ws_connect=mock_connect,
        )
    )
    chunks = make_audio_chunks(generate_pcm_sine(duration_ms=200))

    await stt.start_stream()
    await stt.send_audio(chunks[0])
    await first_socket.push_result("first socket partial", is_final=False)
    await asyncio.sleep(0)
    assert await stt.commit_segment()
    await asyncio.wait_for(reconnected.wait(), timeout=0.5)

    # The second socket represents a fresh Deepgram session.  Its Finalize
    # must be sent for audio admitted after reconnect instead of reusing the
    # stale request from the first socket.
    await stt.send_audio(chunks[1])
    await stt.end_stream()

    assert second_socket.finalize_count == 1
    events = [event async for event in stt.events()]
    assert [event.text for event in events if event.type == STTEventType.FINAL] == [
        "first socket partial",
        "turn 1",
    ]
    assert connect_count == 2
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_reconnect_releases_end_wait_for_dropped_finalize():
    """A reconnect resolves an ending turn's waiter for the dead socket."""
    first_socket = DropAfterFinalizeWebSocket()
    second_socket = PersistentMockWebSocket()
    reconnected = asyncio.Event()
    connect_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal connect_count
        connect_count += 1
        if connect_count == 2:
            reconnected.set()
            return second_socket
        return first_socket

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            final_transcript_timeout_s=1.0,
            ws_connect=mock_connect,
        )
    )
    await stt.start_stream()
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])

    end_task = asyncio.create_task(stt.end_stream())
    await asyncio.wait_for(reconnected.wait(), timeout=0.5)
    await asyncio.wait_for(end_task, timeout=0.5)

    # No audio reached the replacement session, so it needs no Finalize. The
    # original turn was contained at the reconnect boundary instead of
    # waiting for the full Finalize timeout.
    assert second_socket.finalize_count == 0
    assert connect_count == 2
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_nonpersistent_reconnect_contains_prior_partial():
    first_socket = DropAfterAudioWebSocket()
    second_socket = PersistentMockWebSocket()
    reconnected = asyncio.Event()
    partial_seen = asyncio.Event()
    final_seen = asyncio.Event()
    connect_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal connect_count
        connect_count += 1
        if connect_count == 2:
            reconnected.set()
            return second_socket
        return first_socket

    stt = DeepgramSTT(DeepgramSTTConfig(api_key="k", persistent_ws=False, ws_connect=mock_connect))
    emitted = []

    def emit(event):
        emitted.append(event)
        if event.type == STTEventType.PARTIAL:
            partial_seen.set()
        if event.type == STTEventType.FINAL and event.text == "after reconnect":
            final_seen.set()

    stt._emit_event = emit  # type: ignore[method-assign]
    try:
        await stt.start_stream()
        await first_socket.push_result("before reconnect", is_final=False)
        await asyncio.wait_for(partial_seen.wait(), timeout=0.5)

        await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])
        await asyncio.wait_for(reconnected.wait(), timeout=0.5)
        await second_socket.push_result("after reconnect", is_final=True)
        await asyncio.wait_for(final_seen.wait(), timeout=0.5)
        await stt._close_active_websocket(close_before_drain=True)

        assert [(event.type, event.text) for event in emitted] == [
            (STTEventType.PARTIAL, "before reconnect"),
            (STTEventType.FINAL, "before reconnect"),
            (STTEventType.FINAL, "after reconnect"),
        ]
    finally:
        await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_finalize_send_failure_promotes_partial(monkeypatch):
    ws = PersistentMockWebSocket(respond_to_finalize=False)

    async def mock_connect(url, **kwargs):
        return ws

    stt = DeepgramSTT(DeepgramSTTConfig(api_key="k", ws_connect=mock_connect))
    await stt.start_stream()
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])
    await ws.push_result("best interim", is_final=False)
    await asyncio.sleep(0)

    async def fail_finalize(payload, *, label):
        assert payload == {"type": "Finalize"}
        assert label == "Deepgram Finalize"
        return False

    monkeypatch.setattr(stt, "_send_json_control", fail_finalize)
    await stt.end_stream()

    events = [event async for event in stt.events()]
    assert [(event.type, event.text) for event in events] == [
        (STTEventType.PARTIAL, "best interim"),
        (STTEventType.FINAL, "best interim"),
    ]
    assert stt._partial_text == ""
    assert ws.close_code == 1000


@pytest.mark.asyncio
async def test_deepgram_finalize_timeout_promotes_partial_and_reconnects():
    sockets = [PersistentMockWebSocket(respond_to_finalize=False), PersistentMockWebSocket()]
    connect_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal connect_count
        socket = sockets[connect_count]
        connect_count += 1
        return socket

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            final_transcript_timeout_s=0.01,
            ws_connect=mock_connect,
        )
    )
    await stt.warmup()
    await stt.start_stream()
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])
    await sockets[0].push_result("best interim", is_final=False)
    await stt.end_stream()
    first_events = [event async for event in stt.events()]

    assert [(event.type, event.text) for event in first_events] == [
        (STTEventType.PARTIAL, "best interim"),
        (STTEventType.FINAL, "best interim"),
    ]
    assert sockets[0].close_code == 1000

    second_events = await collect_stt_events(
        stt, make_audio_chunks(generate_pcm_sine(duration_ms=100))
    )
    assert [event.text for event in second_events if event.type == STTEventType.FINAL] == [
        "turn 1"
    ]
    assert connect_count == 2
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_finalize_timeout_drains_late_final_without_duplicate():
    # A Finalize-triggered Results frame that misses the timeout but is
    # already buffered when the socket closes is parsed during the discard
    # drain. The turn must see exactly one FINAL — the drained real final —
    # not the promoted interim plus the late final for the same speech.
    sockets = [
        BufferedFinalOnCloseWebSocket("real final tail", respond_to_finalize=False),
        PersistentMockWebSocket(),
    ]
    connect_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal connect_count
        socket = sockets[connect_count]
        connect_count += 1
        return socket

    stt = DeepgramSTT(
        DeepgramSTTConfig(
            api_key="k",
            final_transcript_timeout_s=0.01,
            ws_connect=mock_connect,
        )
    )
    await stt.warmup()
    await stt.start_stream()
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])
    await sockets[0].push_result("best interim", is_final=False)
    await stt.end_stream()

    events = [event async for event in stt.events()]
    finals = [event.text for event in events if event.type == STTEventType.FINAL]
    assert finals == ["real final tail"]
    assert sockets[0].close_code == 1000

    second_events = await collect_stt_events(
        stt, make_audio_chunks(generate_pcm_sine(duration_ms=100))
    )
    assert [event.text for event in second_events if event.type == STTEventType.FINAL] == [
        "turn 1"
    ]
    assert connect_count == 2
    await stt.aclose()


@pytest.mark.asyncio
async def test_deepgram_flux_parses_turn_info_updates_and_end_of_turn():
    messages = [
        _deepgram_turn_info("hello", event="Update"),
        _deepgram_turn_info("hello world", event="EndOfTurn", end_of_turn_confidence=0.88),
    ]
    ws = MockWebSocket(messages)

    async def mock_connect(url: str, **kwargs) -> MockWebSocket:
        return ws

    stt = DeepgramSTT(
        DeepgramSTTConfig(api_key="test-key", model="flux-general-en", ws_connect=mock_connect)
    )

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 2
    assert events[0].type == STTEventType.PARTIAL
    assert events[0].text == "hello"
    assert events[1].type == STTEventType.FINAL
    assert events[1].text == "hello world"
    assert events[1].confidence == 0.88


# ── Segment commit (Finalize) ────────────────────────────────────


@pytest.mark.asyncio
async def test_deepgram_commit_segment_sends_finalize_frame():
    stt, ws = _make_deepgram_stt([])
    await stt.start_stream()

    result = await stt.commit_segment()
    assert result is True

    json_sent = [json.loads(s) for s in ws.sent if isinstance(s, str)]
    assert any(msg.get("type") == "Finalize" for msg in json_sent)

    await stt.end_stream()


@pytest.mark.asyncio
async def test_deepgram_commit_segment_before_start_returns_false():
    stt, _ = _make_deepgram_stt([])
    assert await stt.commit_segment() is False


@pytest.mark.asyncio
async def test_deepgram_flux_commit_segment_returns_false():
    stt, ws = _make_deepgram_stt([], model="flux-general-en")
    await stt.start_stream()

    assert await stt.commit_segment() is False

    json_sent = [json.loads(s) for s in ws.sent if isinstance(s, str)]
    assert not any(msg.get("type") == "Finalize" for msg in json_sent)

    await stt.end_stream()


# ── Errors ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deepgram_error_message_posted_to_event_bus():
    bus = EventBus()
    errors: list[Error] = []
    bus.subscribe(Error, lambda e: errors.append(e))

    error_frame = json.dumps(
        {
            "type": "Error",
            "description": "Sample rate is not supported",
            "message": "invalid configuration",
        }
    )
    stt, _ = _make_deepgram_stt([error_frame], event_bus=bus)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 0
    assert len(errors) == 1
    err = errors[0]
    assert err.stage == ErrorStage.STT
    assert err.provider == "deepgram"
    # The more descriptive ``description`` field must win over the generic
    # ``message`` so the surfaced text is actionable.
    assert "Sample rate is not supported" in str(err.exception)
    assert "invalid configuration" not in str(err.exception)


# ── Live integration ─────────────────────────────────────────────


@pytest.mark.integration_live
@pytest.mark.provider_deepgram
@pytest.mark.surface_stt
async def test_live_deepgram_stt():
    """Integration test requiring DEEPGRAM_API_KEY env var."""
    import os

    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        pytest.skip("DEEPGRAM_API_KEY not set")

    stt = DeepgramSTT(DeepgramSTTConfig(api_key=api_key))

    pcm = generate_pcm_sine(duration_ms=500, sample_rate=16000)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))
    # Tone isn't real speech; smoke-gates auth + WebSocket handshake.
    assert isinstance(events, list)
