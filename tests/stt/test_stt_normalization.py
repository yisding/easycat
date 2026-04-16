"""Tests for STT transcript normalization.

Verifies that all three STT providers normalize output to the common
STTEvent format with consistent fields:
  - text: str
  - is_final: bool (derived from type field)
  - confidence: Optional[float]
  - language: Optional[str]
  - word_timestamps: Optional[list[WordTimestamp]]

NOTE: We do NOT compare transcript text across providers, since different
vendors produce different results for the same audio. We only verify schema
and field-type consistency.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from easycat.events import STTEvent, STTEventType, WordTimestamp
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from tests.stt.helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks

# ── Fixture helpers ──────────────────────────────────────────────


class _MockStreamingResponse:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code
        self.request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
        self.text = "error"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = httpx.Response(
                status_code=self.status_code,
                request=self.request,
                text=self.text,
            )
            raise httpx.HTTPStatusError("error", request=self.request, response=response)

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _MockStreamContext:
    def __init__(self, response: _MockStreamingResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _MockStreamingResponse:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _make_openai(text: str = "openai result") -> OpenAISTT:
    lines = [
        f"data: {json.dumps({'delta': text[:4]})}",
        f"data: {json.dumps({'delta': text[4:]})}",
        f"data: {json.dumps({'text': text, 'is_final': True})}",
        "data: [DONE]",
    ]
    mock_response = _MockStreamingResponse(lines=lines)
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream = MagicMock(return_value=_MockStreamContext(mock_response))
    mock_client.aclose = AsyncMock()
    return OpenAISTT(OpenAISTTConfig(api_key="k", http_client=mock_client))


class _MockWS:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []
        self._i = 0

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self.messages):
            raise StopAsyncIteration
        msg = self.messages[self._i]
        self._i += 1
        return msg


def _make_deepgram(
    transcript: str = "deepgram result",
    confidence: float = 0.95,
    words: list[dict] | None = None,
) -> DeepgramSTT:
    alt: dict = {"transcript": transcript, "confidence": confidence}
    if words:
        alt["words"] = words
    msg = json.dumps({"type": "Results", "channel": {"alternatives": [alt]}, "is_final": True})
    ws = _MockWS([msg])

    async def connect(url, **kw):
        return ws

    return DeepgramSTT(DeepgramSTTConfig(api_key="k", ws_connect=connect))


def _make_elevenlabs_batch(
    text: str = "elevenlabs result", confidence: float | None = None
) -> ElevenLabsSTT:
    body: dict = {"text": text}
    if confidence is not None:
        body["confidence"] = confidence
    resp = httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "https://api.elevenlabs.io/v1/speech-to-text"),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=resp)
    mock_client.aclose = AsyncMock()
    return ElevenLabsSTT(ElevenLabsSTTConfig(api_key="k", mode="batch", http_client=mock_client))


def _make_elevenlabs_realtime(
    text: str = "elevenlabs rt",
    confidence: float | None = None,
    words: list[dict] | None = None,
) -> ElevenLabsSTT:
    msg_data: dict = {"message_type": "committed_transcript", "text": text}
    if confidence is not None:
        msg_data["confidence"] = confidence
    if words:
        msg_data["message_type"] = "committed_transcript_with_timestamps"
        msg_data["words"] = words
    ws = _MockWS([json.dumps(msg_data)])

    async def connect(url, **kw):
        return ws

    return ElevenLabsSTT(ElevenLabsSTTConfig(api_key="k", mode="realtime", ws_connect=connect))


# ── Normalization tests ──────────────────────────────────────────


def _audio():
    pcm = generate_pcm_sine(duration_ms=200)
    return make_audio_chunks(pcm)


def _assert_stt_event_schema(event: STTEvent) -> None:
    """Assert that an STTEvent has the expected normalised schema."""
    assert isinstance(event, STTEvent)
    assert isinstance(event.type, STTEventType)
    assert isinstance(event.text, str)
    assert len(event.text) > 0
    assert isinstance(event.timestamp, float)

    # Optional fields should be correct types if present
    if event.confidence is not None:
        assert isinstance(event.confidence, float)
    if event.language is not None:
        assert isinstance(event.language, str)
    if event.word_timestamps is not None:
        assert isinstance(event.word_timestamps, list)
        for wt in event.word_timestamps:
            assert isinstance(wt, WordTimestamp)
            assert isinstance(wt.word, str)
            assert isinstance(wt.start, float)
            assert isinstance(wt.end, float)


@pytest.mark.asyncio
async def test_openai_event_schema():
    events = await collect_stt_events(_make_openai(), _audio())
    assert len(events) >= 2
    _assert_stt_event_schema(events[-1])
    assert events[-1].type == STTEventType.FINAL


@pytest.mark.asyncio
async def test_deepgram_event_schema():
    events = await collect_stt_events(_make_deepgram(), _audio())
    assert len(events) == 1
    _assert_stt_event_schema(events[0])
    assert events[0].type == STTEventType.FINAL


@pytest.mark.asyncio
async def test_elevenlabs_batch_event_schema():
    events = await collect_stt_events(_make_elevenlabs_batch(), _audio())
    assert len(events) == 1
    _assert_stt_event_schema(events[0])
    assert events[0].type == STTEventType.FINAL


@pytest.mark.asyncio
async def test_elevenlabs_realtime_event_schema():
    events = await collect_stt_events(_make_elevenlabs_realtime(), _audio())
    assert len(events) == 1
    _assert_stt_event_schema(events[0])
    assert events[0].type == STTEventType.FINAL


# ── All providers produce is_final consistently ──────────────────


@pytest.mark.asyncio
async def test_all_providers_produce_final_events():
    """All providers emit STTEventType.FINAL for completed transcripts."""
    providers = [
        _make_openai(),
        _make_deepgram(),
        _make_elevenlabs_batch(),
        _make_elevenlabs_realtime(),
    ]
    for provider in providers:
        events = await collect_stt_events(provider, _audio())
        assert len(events) >= 1
        final_events = [e for e in events if e.type == STTEventType.FINAL]
        assert len(final_events) >= 1, f"{type(provider).__name__} missing FINAL event"


# ── Confidence field consistency ─────────────────────────────────


@pytest.mark.asyncio
async def test_deepgram_provides_confidence():
    events = await collect_stt_events(_make_deepgram(confidence=0.99), _audio())
    assert events[0].confidence is not None
    assert 0.0 <= events[0].confidence <= 1.0


@pytest.mark.asyncio
async def test_elevenlabs_batch_provides_confidence():
    events = await collect_stt_events(_make_elevenlabs_batch(confidence=0.85), _audio())
    assert events[0].confidence == 0.85


@pytest.mark.asyncio
async def test_openai_confidence_is_none():
    """OpenAI API does not return confidence scores."""
    events = await collect_stt_events(_make_openai(), _audio())
    assert events[0].confidence is None


# ── Word timestamps consistency ──────────────────────────────────


@pytest.mark.asyncio
async def test_deepgram_word_timestamps():
    words = [{"word": "hello", "start": 0.0, "end": 0.3}]
    events = await collect_stt_events(_make_deepgram(words=words), _audio())
    assert events[0].word_timestamps is not None
    assert events[0].word_timestamps[0].word == "hello"


@pytest.mark.asyncio
async def test_elevenlabs_realtime_word_timestamps():
    words = [{"text": "test", "start": 0.1, "end": 0.5}]
    events = await collect_stt_events(_make_elevenlabs_realtime(words=words), _audio())
    assert events[0].word_timestamps is not None
    assert events[0].word_timestamps[0].word == "test"
