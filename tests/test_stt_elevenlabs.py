"""Tests for the ElevenLabs STT provider."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from easycat.events import STTEventType
from easycat.providers import STTProvider
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from tests.stt_helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks


class MockWebSocket:
    """Mock WebSocket connection for ElevenLabs tests."""

    def __init__(self, messages: list[str] | None = None) -> None:
        self.messages = messages or []
        self.sent: list[str] = []
        self._closed = False
        self._iter_index = 0

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._iter_index >= len(self.messages):
            raise StopAsyncIteration
        msg = self.messages[self._iter_index]
        self._iter_index += 1
        return msg


def _el_transcript(
    text: str,
    is_final: bool = False,
    confidence: float | None = None,
    language: str | None = None,
    words: list[dict] | None = None,
) -> str:
    """Create an ElevenLabs-format transcript message."""
    msg: dict = {"type": "transcript", "text": text, "is_final": is_final}
    if confidence is not None:
        msg["confidence"] = confidence
    if language:
        msg["language"] = language
    if words:
        msg["words"] = words
    return json.dumps(msg)


def _make_el_stt_realtime(
    messages: list[str] | None = None,
) -> tuple[ElevenLabsSTT, MockWebSocket]:
    """Create an ElevenLabs realtime STT with a mocked WebSocket."""
    ws = MockWebSocket(messages or [])

    async def mock_connect(url: str, **kwargs) -> MockWebSocket:
        return ws

    config = ElevenLabsSTTConfig(api_key="test-key", mode="realtime", ws_connect=mock_connect)
    return ElevenLabsSTT(config), ws


def _make_mock_http_client(
    text: str = "hello world", confidence: float | None = None
) -> httpx.AsyncClient:
    """Create a mock httpx.AsyncClient for batch transcription."""
    body: dict = {"text": text}
    if confidence is not None:
        body["confidence"] = confidence
    mock_response = httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "https://api.elevenlabs.io/v1/speech-to-text"),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return mock_client


# ── Protocol conformance ─────────────────────────────────────────


def test_elevenlabs_stt_conforms_to_protocol():
    stt, _ = _make_el_stt_realtime()
    assert isinstance(stt, STTProvider)


def test_elevenlabs_batch_conforms_to_protocol():
    mock_client = _make_mock_http_client()
    config = ElevenLabsSTTConfig(api_key="k", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)
    assert isinstance(stt, STTProvider)


# ── Realtime mode ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_elevenlabs_realtime_receives_final():
    messages = [_el_transcript("hello world", is_final=True)]
    stt, ws = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1
    assert events[0].type == STTEventType.FINAL
    assert events[0].text == "hello world"


@pytest.mark.asyncio
async def test_elevenlabs_realtime_partial_and_final():
    messages = [
        _el_transcript("hel", is_final=False),
        _el_transcript("hello world", is_final=True),
    ]
    stt, ws = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 2
    assert events[0].type == STTEventType.PARTIAL
    assert events[1].type == STTEventType.FINAL


@pytest.mark.asyncio
async def test_elevenlabs_realtime_sends_init_message():
    stt, ws = _make_el_stt_realtime([])

    await stt.start_stream()
    await stt.end_stream()

    # First sent message should be the init config
    assert len(ws.sent) >= 1
    init = json.loads(ws.sent[0])
    assert init["type"] == "start"


@pytest.mark.asyncio
async def test_elevenlabs_realtime_sends_audio_as_base64():
    stt, ws = _make_el_stt_realtime([])

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm, chunk_duration_ms=100)

    await stt.start_stream()
    for c in chunks:
        await stt.send_audio(c)
    await stt.end_stream()

    # Audio messages should be base64-encoded JSON
    audio_msgs = [json.loads(s) for s in ws.sent if '"audio"' in s]
    assert len(audio_msgs) >= 1
    assert audio_msgs[0]["type"] == "audio"
    assert "data" in audio_msgs[0]


@pytest.mark.asyncio
async def test_elevenlabs_realtime_sends_stop():
    stt, ws = _make_el_stt_realtime([])

    await stt.start_stream()
    await stt.end_stream()

    json_sent = [json.loads(s) for s in ws.sent]
    stop_msgs = [m for m in json_sent if m.get("type") == "stop"]
    assert len(stop_msgs) == 1


@pytest.mark.asyncio
async def test_elevenlabs_realtime_with_confidence():
    messages = [_el_transcript("test", is_final=True, confidence=0.92)]
    stt, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].confidence == 0.92


@pytest.mark.asyncio
async def test_elevenlabs_realtime_with_word_timestamps():
    words = [
        {"word": "hello", "start": 0.0, "end": 0.3},
        {"word": "world", "start": 0.4, "end": 0.7},
    ]
    messages = [_el_transcript("hello world", is_final=True, words=words)]
    stt, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].word_timestamps is not None
    assert len(events[0].word_timestamps) == 2


@pytest.mark.asyncio
async def test_elevenlabs_realtime_ignores_non_transcript():
    messages = [
        json.dumps({"type": "status", "status": "connected"}),
        _el_transcript("hello", is_final=True),
    ]
    stt, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1


# ── Batch mode ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_elevenlabs_batch_transcribes():
    mock_client = _make_mock_http_client("batch result")
    config = ElevenLabsSTTConfig(api_key="test-key", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1
    assert events[0].type == STTEventType.FINAL
    assert events[0].text == "batch result"


@pytest.mark.asyncio
async def test_elevenlabs_batch_sends_wav():
    mock_client = _make_mock_http_client("test")
    config = ElevenLabsSTTConfig(api_key="test-key", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    call_kwargs = mock_client.post.call_args
    files = call_kwargs.kwargs.get("files", {})
    assert "file" in files
    _, data, _ = files["file"]
    assert data[:4] == b"RIFF"


@pytest.mark.asyncio
async def test_elevenlabs_batch_sends_auth():
    mock_client = _make_mock_http_client("test")
    config = ElevenLabsSTTConfig(api_key="xi-key-123", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    headers = mock_client.post.call_args.kwargs.get("headers", {})
    assert headers["xi-api-key"] == "xi-key-123"


@pytest.mark.asyncio
async def test_elevenlabs_batch_no_event_on_empty():
    mock_client = _make_mock_http_client()
    config = ElevenLabsSTTConfig(api_key="k", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    events = await collect_stt_events(stt, [])
    assert len(events) == 0


@pytest.mark.asyncio
async def test_elevenlabs_batch_with_confidence():
    mock_client = _make_mock_http_client("test", confidence=0.88)
    config = ElevenLabsSTTConfig(api_key="k", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].confidence == 0.88


@pytest.mark.asyncio
async def test_elevenlabs_batch_error_handling():
    error_response = httpx.Response(
        status_code=401,
        json={"error": "Unauthorized"},
        request=httpx.Request("POST", "https://api.elevenlabs.io/v1/speech-to-text"),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=error_response)
    mock_client.aclose = AsyncMock()

    config = ElevenLabsSTTConfig(api_key="bad-key", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    await stt.start_stream()
    for c in chunks:
        await stt.send_audio(c)

    with pytest.raises(httpx.HTTPStatusError):
        await stt.end_stream()


# ── Mode property ────────────────────────────────────────────────


def test_elevenlabs_mode_property():
    config = ElevenLabsSTTConfig(api_key="k", mode="realtime")
    stt = ElevenLabsSTT(config)
    assert stt.mode == "realtime"

    config2 = ElevenLabsSTTConfig(api_key="k", mode="batch")
    stt2 = ElevenLabsSTT(config2)
    assert stt2.mode == "batch"


# ── Multiple streams ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_elevenlabs_realtime_reusable():
    call_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockWebSocket([_el_transcript(f"stream {call_count}", is_final=True)])

    config = ElevenLabsSTTConfig(api_key="k", mode="realtime", ws_connect=mock_connect)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    events1 = await collect_stt_events(stt, chunks)
    assert events1[0].text == "stream 1"

    events2 = await collect_stt_events(stt, chunks)
    assert events2[0].text == "stream 2"
