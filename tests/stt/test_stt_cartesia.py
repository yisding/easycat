"""Tests for the Cartesia streaming STT provider."""

from __future__ import annotations

import asyncio
import json

import pytest

from easycat.events import Error, ErrorStage, EventBus, STTEventType
from easycat.stt.cartesia_provider import CartesiaSTT, CartesiaSTTConfig
from tests.stt.helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks


class MockWebSocket:
    """Mock WebSocket connection for Cartesia STT tests."""

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


def _make_cartesia_stt(
    messages: list[str] | None = None,
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


async def test_cartesia_sends_done_on_end_stream():
    stt, ws = _make_cartesia_stt([])

    await stt.start_stream()
    await stt.end_stream()

    json_sent = [json.loads(s) for s in ws.sent if isinstance(s, str)]
    assert any(msg.get("type") == "done" for msg in json_sent)


async def test_cartesia_finalize_sends_finalize_frame():
    stt, ws = _make_cartesia_stt([])
    await stt.start_stream()

    result = await stt.commit_segment()
    assert result is True

    json_sent = [json.loads(s) for s in ws.sent if isinstance(s, str)]
    assert any(msg.get("type") == "finalize" for msg in json_sent)

    await stt.end_stream()


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
    assert info["model"] == "ink-whisper"
    assert "api_version" in info
    assert "sdk_version" in info


# ── Live integration ─────────────────────────────────────────────


@pytest.mark.integration_live
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
