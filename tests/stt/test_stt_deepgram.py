"""Tests for the Deepgram streaming STT provider."""

from __future__ import annotations

import json

import pytest

from easycat.events import Error, ErrorStage, EventBus, STTEventType
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


def _deepgram_result(
    transcript: str,
    is_final: bool = False,
    confidence: float = 0.95,
    words: list[dict] | None = None,
    speech_final: bool | None = None,
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


def _make_deepgram_stt(
    messages: list[str | bytes] | None = None,
    *,
    event_bus=None,
    model: str = "nova-2",
) -> tuple[DeepgramSTT, MockWebSocket]:
    """Create a DeepgramSTT with a mocked WebSocket."""
    ws = MockWebSocket(messages or [])

    async def mock_connect(url: str, **kwargs) -> MockWebSocket:
        return ws

    config = DeepgramSTTConfig(
        api_key="test-key",
        model=model,
        ws_connect=mock_connect,
        event_bus=event_bus,
    )
    return DeepgramSTT(config), ws


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
    assert len(audio_sent) == 1
    # Resampled 48k -> 16k should be roughly one third the byte count.
    assert len(audio_sent[0]) < len(pcm_48k)


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
