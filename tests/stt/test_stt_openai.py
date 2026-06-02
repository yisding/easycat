"""Tests for the OpenAI STT provider."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import STTEventType
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from tests.stt.helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks


class _MockStreamingResponse:
    """Mock streaming response feeding raw bytes via ``aiter_bytes``.

    The provider parses lines off the raw byte stream itself (so the byte
    caps fire incrementally on network chunks), so this mock joins the
    supplied lines with newlines and yields them as one or more byte
    chunks. ``byte_chunks`` lets a test feed an explicit chunk sequence
    (e.g. a single huge no-newline body) instead.
    """

    def __init__(
        self,
        lines: list[str] | None = None,
        status_code: int = 200,
        *,
        byte_chunks: list[bytes] | None = None,
    ) -> None:
        if byte_chunks is not None:
            self._byte_chunks = byte_chunks
        else:
            body = "".join(f"{line}\n" for line in (lines or []))
            self._byte_chunks = [body.encode("utf-8")] if body else []
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

    async def aiter_bytes(self):
        for chunk in self._byte_chunks:
            yield chunk


class _MockStreamContext:
    def __init__(self, response: _MockStreamingResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _MockStreamingResponse:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _make_mock_client(
    lines: list[str] | None = None,
    status_code: int = 200,
    *,
    byte_chunks: list[bytes] | None = None,
) -> httpx.AsyncClient:
    """Create a mock httpx.AsyncClient that streams transcription events."""
    if lines is None and byte_chunks is None:
        lines = [
            'data: {"delta": "hello"}',
            'data: {"delta": " world"}',
            'data: {"text": "hello world", "is_final": true}',
            "data: [DONE]",
        ]
    mock_response = _MockStreamingResponse(
        lines=lines, status_code=status_code, byte_chunks=byte_chunks
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream = MagicMock(return_value=_MockStreamContext(mock_response))
    mock_client.aclose = AsyncMock()
    return mock_client


# ── Basic transcription ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stt_transcribes_audio():
    mock_client = _make_mock_client()
    config = OpenAISTTConfig(api_key="test-key", http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=500)
    chunks = make_audio_chunks(pcm)
    events = await collect_stt_events(stt, chunks)

    assert len(events) >= 2
    assert events[-1].type == STTEventType.FINAL
    assert events[-1].text == "hello world"
    assert any(event.type == STTEventType.PARTIAL for event in events)

    # Verify the API was called
    mock_client.stream.assert_called_once()
    call_kwargs = mock_client.stream.call_args
    assert "audio/transcriptions" in call_kwargs.args[1]


@pytest.mark.asyncio
async def test_openai_stt_rejects_oversized_audio_chunk_before_buffering():
    config = OpenAISTTConfig(
        api_key="test-key",
        max_audio_chunk_bytes=4,
        max_audio_buffer_bytes=100,
        http_client=_make_mock_client(),
    )
    stt = OpenAISTT(config)

    await stt.start_stream()
    with pytest.raises(ValueError, match="audio chunk exceeds"):
        await stt.send_audio(AudioChunk(data=b"\x00" * 6, format=PCM16_MONO_16K))

    assert len(stt._buffer) == 0


@pytest.mark.asyncio
async def test_openai_stt_finalizes_utterance_when_buffer_cap_hit():
    """A cumulative byte cap finalizes the current utterance, not an error.

    A long-talking caller hitting the buffer cap should have their speech so
    far transcribed (FINAL emitted) and a fresh buffer started with the chunk
    that tripped the cap — never an exception that the pipeline would treat as
    a fatal per-chunk error.
    """
    mock_client = _make_mock_client()
    config = OpenAISTTConfig(
        api_key="test-key",
        max_audio_chunk_bytes=10,
        max_audio_buffer_bytes=8,
        http_client=mock_client,
    )
    stt = OpenAISTT(config)

    await stt.start_stream()
    await stt.send_audio(AudioChunk(data=b"\x00" * 4, format=PCM16_MONO_16K))
    # Total would be 4 + 6 = 10 > cap of 8: finalize the buffered 4 bytes and
    # restart with the 6-byte chunk. No exception is raised.
    await stt.send_audio(AudioChunk(data=b"\x00" * 6, format=PCM16_MONO_16K))

    # The buffered audio so far was transcribed (one request) and the new
    # chunk now occupies a fresh buffer.
    mock_client.stream.assert_called_once()
    assert len(stt._buffer) == 6


@pytest.mark.asyncio
async def test_openai_stt_finalizes_utterance_when_duration_cap_hit():
    mock_client = _make_mock_client()
    config = OpenAISTTConfig(
        api_key="test-key",
        max_audio_chunk_bytes=10_000,
        max_audio_buffer_bytes=10_000,
        max_audio_duration_ms=10,
        http_client=mock_client,
    )
    stt = OpenAISTT(config)

    await stt.start_stream()
    # 320 bytes at 16kHz/16-bit mono == 10ms, right at the cap.
    await stt.send_audio(AudioChunk(data=b"\x00" * 320, format=PCM16_MONO_16K))
    # The next chunk pushes total duration past 10ms: finalize + restart.
    await stt.send_audio(AudioChunk(data=b"\x00" * 320, format=PCM16_MONO_16K))

    mock_client.stream.assert_called_once()
    assert len(stt._buffer) == 320


@pytest.mark.asyncio
async def test_openai_stt_rejects_nonpositive_byte_rate_for_duration_cap():
    """A non-positive byte rate must raise a clear error, not divide by zero."""
    config = OpenAISTTConfig(
        api_key="test-key",
        max_audio_duration_ms=1000,
        http_client=_make_mock_client(),
    )
    stt = OpenAISTT(config)
    bad_format = AudioFormat(sample_rate=0, channels=1, sample_width=2)

    await stt.start_stream()
    with pytest.raises(ValueError, match="non-positive byte rate"):
        await stt.send_audio(AudioChunk(data=b"\x00" * 4, format=bad_format))

    assert len(stt._buffer) == 0


@pytest.mark.asyncio
async def test_openai_stt_no_event_on_empty_audio():
    mock_client = _make_mock_client()
    config = OpenAISTTConfig(api_key="test-key", http_client=mock_client)
    stt = OpenAISTT(config)

    events = await collect_stt_events(stt, [])
    assert len(events) == 0
    mock_client.stream.assert_not_called()


@pytest.mark.asyncio
async def test_openai_stt_sends_wav_file():
    mock_client = _make_mock_client(
        [
            'data: {"text": "test", "is_final": true}',
            "data: [DONE]",
        ]
    )
    config = OpenAISTTConfig(api_key="test-key", http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=200)
    chunks = make_audio_chunks(pcm)
    await collect_stt_events(stt, chunks)

    call_kwargs = mock_client.stream.call_args
    files = call_kwargs.kwargs.get("files", {})
    assert "file" in files
    filename, data, mime = files["file"]
    assert filename == "audio.wav"
    assert mime == "audio/wav"
    # WAV file should start with RIFF header
    assert data[:4] == b"RIFF"


# ── Configuration ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stt_sends_model_in_request():
    mock_client = _make_mock_client(
        [
            'data: {"text": "test", "is_final": true}',
            "data: [DONE]",
        ]
    )
    config = OpenAISTTConfig(api_key="test-key", model="whisper-1", http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    call_kwargs = mock_client.stream.call_args
    data = call_kwargs.kwargs.get("data", {})
    assert data["model"] == "whisper-1"


@pytest.mark.asyncio
async def test_openai_stt_sends_optional_params():
    mock_client = _make_mock_client(
        [
            'data: {"text": "test", "is_final": true}',
            "data: [DONE]",
        ]
    )
    config = OpenAISTTConfig(
        api_key="test-key",
        language="en",
        prompt="This is a meeting transcript",
        http_client=mock_client,
    )
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    call_kwargs = mock_client.stream.call_args
    data = call_kwargs.kwargs.get("data", {})
    assert data["language"] == "en"
    assert data["prompt"] == "This is a meeting transcript"


@pytest.mark.asyncio
async def test_openai_stt_custom_base_url():
    mock_client = _make_mock_client(
        [
            'data: {"text": "test", "is_final": true}',
            "data: [DONE]",
        ]
    )
    config = OpenAISTTConfig(
        api_key="test-key",
        base_url="https://custom.api.com/v2",
        http_client=mock_client,
    )
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    url = mock_client.stream.call_args.args[1]
    assert url == "https://custom.api.com/v2/audio/transcriptions"


# ── Authorization ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stt_sends_auth_header():
    mock_client = _make_mock_client(
        [
            'data: {"text": "test", "is_final": true}',
            "data: [DONE]",
        ]
    )
    config = OpenAISTTConfig(api_key="sk-test-key-123", http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    headers = mock_client.stream.call_args.kwargs.get("headers", {})
    assert headers["Authorization"] == "Bearer sk-test-key-123"


# ── Error handling ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stt_raises_on_api_error():
    error_response = _MockStreamingResponse(lines=["data: error"], status_code=500)
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream = MagicMock(return_value=_MockStreamContext(error_response))
    mock_client.aclose = AsyncMock()

    config = OpenAISTTConfig(api_key="test-key", max_retries=1, http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    await stt.start_stream()
    for c in chunks:
        await stt.send_audio(c)

    with pytest.raises(httpx.HTTPStatusError):
        await stt.end_stream()


@pytest.mark.asyncio
async def test_openai_stt_max_retries_zero_still_sends_one_request():
    """max_retries=0 means a single attempt, not zero requests."""
    mock_client = _make_mock_client(
        [
            'data: {"text": "hi", "is_final": true}',
            "data: [DONE]",
        ]
    )
    config = OpenAISTTConfig(api_key="test-key", max_retries=0, http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[-1].type == STTEventType.FINAL
    assert events[-1].text == "hi"
    mock_client.stream.assert_called_once()


@pytest.mark.asyncio
async def test_openai_stt_max_retries_zero_raises_underlying_error():
    """A failing single attempt raises the real HTTP error, not a causeless one."""
    error_response = _MockStreamingResponse(lines=["data: error"], status_code=500)
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream = MagicMock(return_value=_MockStreamContext(error_response))
    mock_client.aclose = AsyncMock()

    config = OpenAISTTConfig(api_key="test-key", max_retries=0, http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await stt.start_stream()
    for c in make_audio_chunks(pcm):
        await stt.send_audio(c)

    # The single attempt's HTTP error must surface, never a causeless
    # "all attempts failed" RuntimeError with no chained cause.
    with pytest.raises(httpx.HTTPStatusError):
        await stt.end_stream()
    mock_client.stream.assert_called_once()


def test_openai_stt_config_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="max_retries"):
        OpenAISTTConfig(api_key="test-key", max_retries=-1)


@pytest.mark.asyncio
async def test_openai_stt_stream_event_limit_aborts_without_emitting_buffered_partials():
    mock_client = _make_mock_client(
        [
            'data: {"delta": "a"}',
            'data: {"delta": "b"}',
            'data: {"delta": "c"}',
        ]
    )
    config = OpenAISTTConfig(
        api_key="test-key",
        http_client=mock_client,
        max_retries=1,
        max_stream_events=2,
    )
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await stt.start_stream()
    for chunk in make_audio_chunks(pcm):
        await stt.send_audio(chunk)

    with pytest.raises(RuntimeError, match="exceeded 2 events"):
        await stt.end_stream()

    events = []
    try:
        while True:
            events.append(await asyncio.wait_for(stt._event_queue.get(), timeout=0.01))
    except TimeoutError:
        pass
    assert events == [None]


@pytest.mark.asyncio
async def test_openai_stt_transcript_limit_aborts_oversized_delta():
    mock_client = _make_mock_client(
        [
            'data: {"delta": "abc"}',
            'data: {"delta": "def"}',
        ]
    )
    config = OpenAISTTConfig(
        api_key="test-key",
        http_client=mock_client,
        max_retries=1,
        max_transcript_chars=5,
    )
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await stt.start_stream()
    for chunk in make_audio_chunks(pcm):
        await stt.send_audio(chunk)

    with pytest.raises(RuntimeError, match="exceeded 5 characters"):
        await stt.end_stream()


@pytest.mark.asyncio
async def test_openai_stt_line_byte_limit_aborts_unbounded_no_newline_body():
    """A no-newline body must trip the line cap incrementally, not after buffering it all.

    Each chunk has no newline, so the pending fragment keeps growing. The
    provider must raise once the pending fragment crosses
    ``max_stream_line_bytes`` rather than waiting for the (never-arriving)
    newline and materializing the whole multi-megabyte body.
    """
    chunk = b"x" * 1024

    consumed = 0

    async def _counting_chunks():
        nonlocal consumed
        # An effectively unbounded stream: if the provider buffered the whole
        # body before checking the cap, this generator would run forever.
        while True:
            consumed += len(chunk)
            yield chunk

    response = _MockStreamingResponse(byte_chunks=[])
    response.aiter_bytes = _counting_chunks  # type: ignore[method-assign]
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream = MagicMock(return_value=_MockStreamContext(response))
    mock_client.aclose = AsyncMock()

    config = OpenAISTTConfig(
        api_key="test-key",
        http_client=mock_client,
        max_retries=1,
        max_stream_line_bytes=4096,
        max_stream_total_bytes=10_000_000,
    )
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await stt.start_stream()
    for c in make_audio_chunks(pcm):
        await stt.send_audio(c)

    with pytest.raises(RuntimeError, match="line exceeded 4096 bytes"):
        await stt.end_stream()

    # The cap fired after only a handful of chunks crossed the line limit —
    # the whole (unbounded) body was never materialized.
    assert consumed <= 4096 + len(chunk)


@pytest.mark.asyncio
async def test_openai_stt_total_byte_limit_aborts_oversized_stream():
    """A stream of many small newline-terminated lines trips the total-bytes cap."""
    # Each line is small (well under the per-line cap) but together they
    # exceed the total-bytes ceiling; the running total must abort the stream.
    chunk = b'data: {"delta": "ab"}\n'

    consumed = 0

    async def _counting_chunks():
        nonlocal consumed
        while True:
            consumed += len(chunk)
            yield chunk

    response = _MockStreamingResponse(byte_chunks=[])
    response.aiter_bytes = _counting_chunks  # type: ignore[method-assign]
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream = MagicMock(return_value=_MockStreamContext(response))
    mock_client.aclose = AsyncMock()

    config = OpenAISTTConfig(
        api_key="test-key",
        http_client=mock_client,
        max_retries=1,
        max_stream_total_bytes=4096,
        # Keep the other caps generous so only the total-bytes cap can fire.
        max_stream_events=1_000_000,
        max_partial_events=1_000_000,
        max_transcript_chars=10_000_000,
    )
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await stt.start_stream()
    for c in make_audio_chunks(pcm):
        await stt.send_audio(c)

    with pytest.raises(RuntimeError, match="exceeded 4096 total bytes"):
        await stt.end_stream()

    assert consumed <= 4096 + len(chunk)


def test_openai_stt_config_rejects_nonpositive_max_stream_total_bytes():
    with pytest.raises(ValueError, match="max_stream_total_bytes"):
        OpenAISTTConfig(api_key="test-key", max_stream_total_bytes=0)


# ── Mid-stream format change ─────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stt_rejects_mid_stream_format_change():
    from easycat.audio_format import AudioChunk, AudioFormat

    mock_client = _make_mock_client()
    config = OpenAISTTConfig(api_key="test-key", http_client=mock_client)
    stt = OpenAISTT(config)

    fmt_16k = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
    fmt_8k = AudioFormat(sample_rate=8000, channels=1, sample_width=2)

    await stt.start_stream()
    await stt.send_audio(AudioChunk(data=b"\x00\x00" * 160, format=fmt_16k))
    with pytest.raises(ValueError, match="mid-stream audio format change"):
        await stt.send_audio(AudioChunk(data=b"\x00\x00" * 160, format=fmt_8k))


# ── Multiple streams ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stt_reusable_across_streams():
    mock_client = _make_mock_client(
        [
            'data: {"text": "stream one", "is_final": true}',
            "data: [DONE]",
        ]
    )
    config = OpenAISTTConfig(api_key="test-key", http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    # First stream
    events1 = await collect_stt_events(stt, chunks)
    finals1 = [e for e in events1 if e.type == STTEventType.FINAL]
    assert len(finals1) == 1

    # Second stream (buffer should be cleared)
    mock_client.stream.reset_mock()
    mock_response = _make_mock_client(
        [
            'data: {"text": "stream two", "is_final": true}',
            "data: [DONE]",
        ]
    ).stream.return_value
    mock_client.stream.return_value = mock_response

    events2 = await collect_stt_events(stt, chunks)
    finals2 = [e for e in events2 if e.type == STTEventType.FINAL]
    assert len(finals2) == 1


# ── Live integration ─────────────────────────────────────────────


@pytest.mark.integration_live
@pytest.mark.provider_openai
@pytest.mark.surface_stt
async def test_live_openai_stt():
    """Integration test requiring OPENAI_API_KEY env var."""
    import os

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set")

    stt = OpenAISTT(OpenAISTTConfig(api_key=api_key))

    pcm = generate_pcm_sine(duration_ms=500, sample_rate=16000)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))
    # Tone isn't real speech; we just verify the round-trip completes
    # without raising. Provider-specific event assertions stay in unit
    # tests; this smoke test gates auth + protocol handshake.
    assert isinstance(events, list)
