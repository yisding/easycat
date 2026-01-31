"""Tests for the Deepgram streaming STT provider."""

from __future__ import annotations

import json

import pytest

from easycat.events import STTEventType
from easycat.providers import STTProvider
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from tests.stt_helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks


class MockWebSocket:
    """Mock WebSocket connection for Deepgram tests."""

    def __init__(self, messages: list[str] | None = None) -> None:
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

    async def __anext__(self) -> str:
        if self._iter_index >= len(self.messages):
            raise StopAsyncIteration
        msg = self.messages[self._iter_index]
        self._iter_index += 1
        return msg


def _deepgram_result(
    transcript: str,
    is_final: bool = False,
    confidence: float = 0.95,
    words: list[dict] | None = None,
) -> str:
    """Create a Deepgram-format Results message."""
    alt: dict = {"transcript": transcript, "confidence": confidence}
    if words:
        alt["words"] = words
    return json.dumps(
        {
            "type": "Results",
            "channel": {"alternatives": [alt]},
            "is_final": is_final,
        }
    )


def _make_deepgram_stt(
    messages: list[str] | None = None,
) -> tuple[DeepgramSTT, MockWebSocket]:
    """Create a DeepgramSTT with a mocked WebSocket."""
    ws = MockWebSocket(messages or [])

    async def mock_connect(url: str, **kwargs) -> MockWebSocket:
        return ws

    config = DeepgramSTTConfig(api_key="test-key", ws_connect=mock_connect)
    return DeepgramSTT(config), ws


# ── Protocol conformance ─────────────────────────────────────────


def test_deepgram_stt_conforms_to_protocol():
    stt, _ = _make_deepgram_stt()
    assert isinstance(stt, STTProvider)


# ── Basic streaming ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deepgram_receives_final_transcript():
    messages = [_deepgram_result("hello world", is_final=True)]
    stt, ws = _make_deepgram_stt(messages)

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
    stt, ws = _make_deepgram_stt(messages)

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

    config = DeepgramSTTConfig(api_key="k", language="fr", ws_connect=mock_connect)
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
async def test_deepgram_ignores_empty_transcript():
    messages = [_deepgram_result("", is_final=False)]
    stt, _ = _make_deepgram_stt(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 0


# ── URL building ─────────────────────────────────────────────────


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


# ── Multiple streams ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deepgram_reusable_across_streams():
    call_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockWebSocket([_deepgram_result(f"stream {call_count}", is_final=True)])

    config = DeepgramSTTConfig(api_key="k", ws_connect=mock_connect)
    stt = DeepgramSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    events1 = await collect_stt_events(stt, chunks)
    assert events1[0].text == "stream 1"

    events2 = await collect_stt_events(stt, chunks)
    assert events2[0].text == "stream 2"
