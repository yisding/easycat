"""Tests for the ElevenLabs STT provider."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from easycat.events import STTEventType
from easycat.stt import elevenlabs_provider
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from tests.stt.helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks


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
    msg_type = "committed_transcript" if is_final else "partial_transcript"
    msg: dict = {"message_type": msg_type, "text": text}
    if confidence is not None:
        msg["confidence"] = confidence
    if language:
        msg["language_code"] = language
    if words:
        msg["message_type"] = "committed_transcript_with_timestamps"
        msg["words"] = words
    return json.dumps(msg)


def _make_el_stt_realtime(
    messages: list[str] | None = None,
) -> tuple[ElevenLabsSTT, MockWebSocket, dict[str, str]]:
    """Create an ElevenLabs realtime STT with a mocked WebSocket."""
    ws = MockWebSocket(messages or [])
    connect_meta: dict[str, str] = {}

    async def mock_connect(url: str, **kwargs) -> MockWebSocket:
        connect_meta["url"] = url
        return ws

    config = ElevenLabsSTTConfig(api_key="test-key", mode="realtime", ws_connect=mock_connect)
    return ElevenLabsSTT(config), ws, connect_meta


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


# ── Realtime mode ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_elevenlabs_realtime_receives_final():
    messages = [_el_transcript("hello world", is_final=True)]
    stt, ws, _ = _make_el_stt_realtime(messages)

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
    stt, ws, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 2
    assert events[0].type == STTEventType.PARTIAL
    assert events[1].type == STTEventType.FINAL


@pytest.mark.asyncio
async def test_elevenlabs_realtime_connects_with_query_params():
    stt, ws, connect_meta = _make_el_stt_realtime([])

    await stt.start_stream()
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)
    await stt.end_stream()

    url = connect_meta["url"]
    assert "/v1/speech-to-text/realtime?" in url
    assert "model_id=scribe_v2_realtime" in url
    assert "audio_format=pcm_16000" in url
    assert "commit_strategy=manual" in url


@pytest.mark.asyncio
async def test_elevenlabs_realtime_sends_audio_as_base64():
    stt, ws, _ = _make_el_stt_realtime([])

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm, chunk_duration_ms=100)

    await stt.start_stream()
    for c in chunks:
        await stt.send_audio(c)
    await stt.end_stream()

    # Audio messages should be base64-encoded JSON
    audio_msgs = [json.loads(s) for s in ws.sent if '"input_audio_chunk"' in s]
    assert len(audio_msgs) >= 1
    assert audio_msgs[0]["message_type"] == "input_audio_chunk"
    assert "audio_base_64" in audio_msgs[0]
    assert audio_msgs[0]["commit"] is False
    assert audio_msgs[0]["sample_rate"] == 16000


@pytest.mark.asyncio
async def test_elevenlabs_realtime_sends_stop():
    stt, ws, _ = _make_el_stt_realtime([])

    await stt.start_stream()
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)
    await stt.end_stream()

    json_sent = [json.loads(s) for s in ws.sent]
    stop_msgs = [
        m
        for m in json_sent
        if m.get("message_type") == "input_audio_chunk" and m.get("commit") is True
    ]
    assert len(stop_msgs) == 1


@pytest.mark.asyncio
async def test_elevenlabs_realtime_commit_segment_keeps_stream_open_for_later_audio():
    messages = [
        _el_transcript("hello", is_final=True),
        _el_transcript("world", is_final=True),
    ]
    stt, ws, _ = _make_el_stt_realtime(messages)

    collected = []
    await stt.start_stream()

    async def _collect() -> None:
        async for event in stt.events():
            collected.append(event)

    collect_task = asyncio.create_task(_collect())
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]

    await stt.send_audio(chunk)
    assert await stt.commit_segment() is True
    await stt.send_audio(chunk)
    await stt.end_stream()
    await collect_task

    finals = [event.text for event in collected if event.type == STTEventType.FINAL]
    assert finals == ["hello", "world"]

    json_sent = [json.loads(s) for s in ws.sent]
    commit_msgs = [
        m
        for m in json_sent
        if m.get("message_type") == "input_audio_chunk" and m.get("commit") is True
    ]
    assert len(commit_msgs) == 2


@pytest.mark.asyncio
async def test_elevenlabs_realtime_with_confidence():
    messages = [_el_transcript("test", is_final=True, confidence=0.92)]
    stt, _, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].confidence == 0.92


@pytest.mark.asyncio
async def test_elevenlabs_realtime_with_word_timestamps():
    words = [
        {"text": "hello", "start": 0.0, "end": 0.3},
        {"text": "world", "start": 0.4, "end": 0.7},
    ]
    messages = [_el_transcript("hello world", is_final=True, words=words)]
    stt, _, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].word_timestamps is not None
    assert len(events[0].word_timestamps) == 2


@pytest.mark.asyncio
async def test_elevenlabs_realtime_ignores_non_transcript():
    messages = [
        json.dumps({"message_type": "session_started"}),
        _el_transcript("hello", is_final=True),
    ]
    stt, _, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1


class _BlockingWebSocket:
    """Mock WebSocket that yields a fixed set of messages then blocks.

    Unlike :class:`MockWebSocket`, the receive iterator does not end after
    the canned messages — it waits until ``close()`` is called. This keeps
    the provider's receive loop alive so a commit can genuinely time out
    waiting for a final that never arrives.
    """

    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []
        self._iter_index = 0
        self._closed = asyncio.Event()

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self._closed.set()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._iter_index < len(self.messages):
            msg = self.messages[self._iter_index]
            self._iter_index += 1
            return msg
        await self._closed.wait()
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_elevenlabs_realtime_promotes_partial_on_commit_timeout(monkeypatch):
    # Server sends only a partial and never the committed transcript, so
    # the end-of-turn commit times out and the latest partial must be
    # promoted to a FINAL (mirroring OpenAIRealtimeSTT).
    monkeypatch.setattr(elevenlabs_provider, "_FINAL_TRANSCRIPT_TIMEOUT_S", 0.05)

    ws = _BlockingWebSocket([_el_transcript("hello wor", is_final=False)])

    async def mock_connect(url, **kwargs):
        return ws

    config = ElevenLabsSTTConfig(api_key="k", mode="realtime", ws_connect=mock_connect)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    finals = [e for e in events if e.type == STTEventType.FINAL]
    assert len(finals) == 1
    assert finals[0].text == "hello wor"


@pytest.mark.asyncio
async def test_elevenlabs_realtime_drops_late_final_after_timeout_promotion(monkeypatch):
    # After a commit timeout promotes the partial to FINAL, a late
    # committed transcript for the same turn must be dropped so the turn
    # does not get two FINAL events.
    monkeypatch.setattr(elevenlabs_provider, "_FINAL_TRANSCRIPT_TIMEOUT_S", 0.05)

    ws = _BlockingWebSocket([])

    async def mock_connect(url, **kwargs):
        return ws

    config = ElevenLabsSTTConfig(api_key="k", mode="realtime", ws_connect=mock_connect)
    stt = ElevenLabsSTT(config)

    collected: list = []
    await stt.start_stream()

    async def _collect() -> None:
        async for event in stt.events():
            collected.append(event)

    collect_task = asyncio.create_task(_collect())
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)

    # A partial arrives, then the commit times out and promotes it.
    stt._handle_json_message(json.loads(_el_transcript("hello wor", is_final=False)))
    assert await stt._send_commit(wait_for_final=True) is True

    # The real committed transcript shows up late — it must be dropped.
    stt._handle_json_message(json.loads(_el_transcript("hello world", is_final=True)))

    await stt.end_stream()
    await collect_task
    events = collected

    finals = [e for e in events if e.type == STTEventType.FINAL]
    assert len(finals) == 1
    assert finals[0].text == "hello wor"


@pytest.mark.asyncio
async def test_elevenlabs_realtime_keeps_late_final_when_no_partial_promoted(monkeypatch):
    # The commit times out but no partial ever arrived, so nothing was
    # promoted to a FINAL.  A real committed transcript arriving afterwards
    # is the turn's only transcript and must NOT be dropped.
    monkeypatch.setattr(elevenlabs_provider, "_FINAL_TRANSCRIPT_TIMEOUT_S", 0.05)

    ws = _BlockingWebSocket([])

    async def mock_connect(url, **kwargs):
        return ws

    config = ElevenLabsSTTConfig(api_key="k", mode="realtime", ws_connect=mock_connect)
    stt = ElevenLabsSTT(config)

    collected: list = []
    await stt.start_stream()

    async def _collect() -> None:
        async for event in stt.events():
            collected.append(event)

    collect_task = asyncio.create_task(_collect())
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)

    # No partial arrives; the commit times out without promoting anything.
    assert await stt._send_commit(wait_for_final=True) is True
    assert stt._dropping_pending_final is False

    # The committed transcript shows up late — it is the only transcript
    # for the turn and must be emitted, not dropped.
    stt._handle_json_message(json.loads(_el_transcript("hello world", is_final=True)))

    await stt.end_stream()
    await collect_task
    events = collected

    finals = [e for e in events if e.type == STTEventType.FINAL]
    assert len(finals) == 1
    assert finals[0].text == "hello world"


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


# ── Live integration ─────────────────────────────────────────────


@pytest.mark.integration_live
@pytest.mark.provider_elevenlabs
@pytest.mark.surface_stt
async def test_live_elevenlabs_stt_realtime():
    """Integration test requiring ELEVENLABS_API_KEY env var."""
    import os

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        pytest.skip("ELEVENLABS_API_KEY not set")

    stt = ElevenLabsSTT(ElevenLabsSTTConfig(api_key=api_key, mode="realtime"))

    pcm = generate_pcm_sine(duration_ms=500, sample_rate=16000)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))
    # Tone isn't real speech; smoke-gates auth + realtime WebSocket
    # session negotiation.
    assert isinstance(events, list)
