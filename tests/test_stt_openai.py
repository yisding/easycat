"""Tests for the OpenAI STT provider."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from easycat.events import STTEventType
from easycat.providers import STTProvider
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from tests.stt_helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks


def _make_mock_client(text: str = "hello world", status_code: int = 200) -> httpx.AsyncClient:
    """Create a mock httpx.AsyncClient that returns a transcript."""
    mock_response = httpx.Response(
        status_code=status_code,
        json={"text": text},
        request=httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions"),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return mock_client


# ── Protocol conformance ─────────────────────────────────────────


def test_openai_stt_conforms_to_protocol():
    provider = OpenAISTT(OpenAISTTConfig(api_key="test-key"))
    assert isinstance(provider, STTProvider)


# ── Basic transcription ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stt_transcribes_audio():
    mock_client = _make_mock_client("hello world")
    config = OpenAISTTConfig(api_key="test-key", http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=500)
    chunks = make_audio_chunks(pcm)
    events = await collect_stt_events(stt, chunks)

    assert len(events) == 1
    assert events[0].type == STTEventType.FINAL
    assert events[0].text == "hello world"

    # Verify the API was called
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert "audio/transcriptions" in call_kwargs.args[0]


@pytest.mark.asyncio
async def test_openai_stt_no_event_on_empty_audio():
    mock_client = _make_mock_client()
    config = OpenAISTTConfig(api_key="test-key", http_client=mock_client)
    stt = OpenAISTT(config)

    events = await collect_stt_events(stt, [])
    assert len(events) == 0
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_openai_stt_sends_wav_file():
    mock_client = _make_mock_client("test")
    config = OpenAISTTConfig(api_key="test-key", http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=200)
    chunks = make_audio_chunks(pcm)
    await collect_stt_events(stt, chunks)

    call_kwargs = mock_client.post.call_args
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
    mock_client = _make_mock_client("test")
    config = OpenAISTTConfig(api_key="test-key", model="whisper-1", http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    call_kwargs = mock_client.post.call_args
    data = call_kwargs.kwargs.get("data", {})
    assert data["model"] == "whisper-1"


@pytest.mark.asyncio
async def test_openai_stt_sends_optional_params():
    mock_client = _make_mock_client("test")
    config = OpenAISTTConfig(
        api_key="test-key",
        language="en",
        prompt="This is a meeting transcript",
        http_client=mock_client,
    )
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    call_kwargs = mock_client.post.call_args
    data = call_kwargs.kwargs.get("data", {})
    assert data["language"] == "en"
    assert data["prompt"] == "This is a meeting transcript"


@pytest.mark.asyncio
async def test_openai_stt_custom_base_url():
    mock_client = _make_mock_client("test")
    config = OpenAISTTConfig(
        api_key="test-key",
        base_url="https://custom.api.com/v2",
        http_client=mock_client,
    )
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    url = mock_client.post.call_args.args[0]
    assert url == "https://custom.api.com/v2/audio/transcriptions"


# ── Authorization ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stt_sends_auth_header():
    mock_client = _make_mock_client("test")
    config = OpenAISTTConfig(api_key="sk-test-key-123", http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    headers = mock_client.post.call_args.kwargs.get("headers", {})
    assert headers["Authorization"] == "Bearer sk-test-key-123"


# ── Error handling ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stt_raises_on_api_error():
    error_response = httpx.Response(
        status_code=500,
        json={"error": "Internal Server Error"},
        request=httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions"),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=error_response)
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


# ── Multiple streams ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stt_reusable_across_streams():
    mock_client = _make_mock_client("stream two")
    config = OpenAISTTConfig(api_key="test-key", http_client=mock_client)
    stt = OpenAISTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    # First stream
    events1 = await collect_stt_events(stt, chunks)
    assert len(events1) == 1

    # Second stream (buffer should be cleared)
    mock_client.post.reset_mock()
    mock_response = _make_mock_client("stream two").post.return_value
    mock_client.post.return_value = mock_response

    events2 = await collect_stt_events(stt, chunks)
    assert len(events2) == 1
