"""Tests for the OpenAI STT provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from easycat.events import STTEventType
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from tests.stt.helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks


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


def _make_mock_client(
    lines: list[str] | None = None,
    status_code: int = 200,
) -> httpx.AsyncClient:
    """Create a mock httpx.AsyncClient that streams transcription events."""
    if lines is None:
        lines = [
            'data: {"delta": "hello"}',
            'data: {"delta": " world"}',
            'data: {"text": "hello world", "is_final": true}',
            "data: [DONE]",
        ]
    mock_response = _MockStreamingResponse(lines=lines, status_code=status_code)
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
