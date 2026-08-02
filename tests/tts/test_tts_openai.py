"""Tests for OpenAI TTS provider."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from easycat.audio_format import PCM16_MONO_24K
from easycat.events import Error, ErrorStage, EventBus, TTSEventType
from easycat.tts.openai_tts import (
    _FIRST_AUDIO_CHUNK_BYTES,
    _STEADY_AUDIO_CHUNK_BYTES,
    OpenAITTS,
    OpenAITTSConfig,
)
from tests.tts._harness import extract_audio_chunks, verify_pcm16_audio


def _pcm16_bytes(n_samples: int = 240) -> bytes:
    """Generate n_samples of PCM16 silence (zeros)."""
    return struct.pack(f"<{n_samples}h", *([0] * n_samples))


class FakeStreamResponse:
    """Mock httpx streaming response that yields predetermined chunks."""

    def __init__(self, chunks: list[bytes], status_code: int = 200):
        self._chunks = chunks
        self.status_code = status_code
        self.is_closed = False
        self.aiter_bytes_chunk_size: int | None = -1
        self.chunks_read = 0

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    async def aread(self) -> bytes:
        return b"error"

    def raise_for_status(self) -> None:
        if not self.is_success:
            response = MagicMock()
            response.status_code = self.status_code
            response.text = "error"
            raise httpx.HTTPStatusError("error", request=MagicMock(), response=response)

    async def aiter_bytes(self, chunk_size: int | None = None):
        self.aiter_bytes_chunk_size = chunk_size
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk

    async def aclose(self):
        self.is_closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


class StreamClosedResponse(FakeStreamResponse):
    """Yield one frame, then model a close racing the next stream read."""

    async def aiter_bytes(self, chunk_size: int | None = None):
        self.aiter_bytes_chunk_size = chunk_size
        self.chunks_read += 1
        yield _pcm16_bytes(480)
        raise httpx.StreamClosed()


class ChunkedAsyncByteStream(httpx.AsyncByteStream):
    """Exercise the provider through httpx's real response byte iterator."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


class TestOpenAITTSConfig:
    def test_defaults(self):
        config = OpenAITTSConfig(api_key="test-key")
        assert config.model == "gpt-4o-mini-tts"
        assert config.voice == "alloy"
        assert config.speed == 1.0
        assert config.output_format == PCM16_MONO_24K

    def test_custom_values(self):
        config = OpenAITTSConfig(
            api_key="key",
            model="tts-1-hd",
            voice="nova",
            speed=1.5,
        )
        assert config.model == "tts-1-hd"
        assert config.voice == "nova"
        assert config.speed == 1.5


class TestOpenAITTS:
    def _make_provider(self, api_key: str = "test-key") -> OpenAITTS:
        return OpenAITTS(OpenAITTSConfig(api_key=api_key))

    async def test_synthesize_yields_audio_events(self):
        provider = self._make_provider()
        pcm_data = [_pcm16_bytes(240), _pcm16_bytes(240)]
        fake_response = FakeStreamResponse(pcm_data)

        with patch.object(provider._client, "stream", return_value=fake_response):
            events = []
            async for event in provider.synthesize("Hello world"):
                events.append(event)

        assert len(events) == 1
        for e in events:
            assert e.type == TTSEventType.AUDIO
            assert e.audio is not None

        chunks = extract_audio_chunks(events)
        assert verify_pcm16_audio(chunks)
        assert chunks[0].data == b"".join(pcm_data)
        assert fake_response.aiter_bytes_chunk_size is None

    async def test_synthesize_releases_small_first_frame_then_steady_frames(self):
        provider = self._make_provider()
        source = b"".join(bytes([index]) * 480 for index in range(13))
        fake_response = FakeStreamResponse(
            [source[index : index + 480] for index in range(0, len(source), 480)]
        )

        with patch.object(provider._client, "stream", return_value=fake_response):
            events = [event async for event in provider.synthesize("Hello world")]

        chunks = extract_audio_chunks(events)
        assert [len(chunk.data) for chunk in chunks] == [
            _FIRST_AUDIO_CHUNK_BYTES,
            _STEADY_AUDIO_CHUNK_BYTES,
            480,
        ]
        assert b"".join(chunk.data for chunk in chunks) == source
        assert fake_response.aiter_bytes_chunk_size is None

    async def test_synthesize_uses_httpx_native_response_cadence(self):
        source = b"".join(bytes([index]) * 480 for index in range(13))
        stream = ChunkedAsyncByteStream(
            [source[index : index + 480] for index in range(0, len(source), 480)]
        )

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        provider = self._make_provider()
        original_client = provider._client
        provider._client = httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(handle),
        )
        await original_client.aclose()
        try:
            events = [event async for event in provider.synthesize("Hello world")]
        finally:
            await provider.close()

        chunks = extract_audio_chunks(events)
        assert [len(chunk.data) for chunk in chunks] == [
            _FIRST_AUDIO_CHUNK_BYTES,
            _STEADY_AUDIO_CHUNK_BYTES,
            480,
        ]
        assert b"".join(chunk.data for chunk in chunks) == source

    async def test_synthesize_ignores_empty_network_chunks(self):
        provider = self._make_provider()
        source = _pcm16_bytes(480)
        fake_response = FakeStreamResponse([b"", source[:480], b"", source[480:], b""])

        with patch.object(provider._client, "stream", return_value=fake_response):
            events = [event async for event in provider.synthesize("Hello world")]

        chunks = extract_audio_chunks(events)
        assert [chunk.data for chunk in chunks] == [source]

    async def test_synthesize_sends_correct_request(self):
        provider = OpenAITTS(
            OpenAITTSConfig(
                api_key="test-key",
                model="tts-1-hd",
                voice="nova",
                speed=1.25,
            )
        )
        fake_response = FakeStreamResponse([_pcm16_bytes(10)])
        mock_stream = MagicMock(return_value=fake_response)

        with patch.object(provider._client, "stream", mock_stream):
            async for _ in provider.synthesize("Test"):
                pass

        mock_stream.assert_called_once()
        call_args = mock_stream.call_args
        assert call_args[0][0] == "POST"
        assert call_args[0][1] == "/audio/speech"
        body = call_args[1]["json"]
        assert body["model"] == "tts-1-hd"
        assert body["voice"] == "nova"
        assert body["speed"] == 1.25
        assert body["response_format"] == "pcm"
        assert body["input"] == "Test"

    async def test_synthesize_tracks_active_state(self):
        provider = self._make_provider()
        fake_response = FakeStreamResponse([_pcm16_bytes(10)])

        with patch.object(provider._client, "stream", return_value=fake_response):
            assert not provider.is_active
            async for _ in provider.synthesize("hi"):
                assert provider.is_active
            assert not provider.is_active

    async def test_cancel_stops_iteration(self):
        provider = self._make_provider()
        pcm_data = [_pcm16_bytes(480)] * 20
        fake_response = FakeStreamResponse(pcm_data)

        with patch.object(provider._client, "stream", return_value=fake_response):
            events = []
            async for event in provider.synthesize("long text"):
                events.append(event)
                if len(events) == 2:
                    await provider.cancel()

        assert len(events) == 2
        assert provider.is_cancelled
        assert fake_response.chunks_read == 6

    async def test_cancel_suppresses_stream_closed_race(self):
        provider = self._make_provider()
        fake_response = StreamClosedResponse([])

        with patch.object(provider._client, "stream", return_value=fake_response):
            async for _event in provider.synthesize("long text"):
                await provider.cancel()

        assert provider.is_cancelled
        assert fake_response.chunks_read == 1

    async def test_stream_closed_without_cancel_propagates(self):
        provider = self._make_provider()
        fake_response = StreamClosedResponse([])

        with patch.object(provider._client, "stream", return_value=fake_response):  # noqa: SIM117 nested scopes clarify setup and cleanup
            with pytest.raises(httpx.StreamClosed):
                async for _event in provider.synthesize("long text"):
                    pass

    async def test_stop_sets_inactive(self):
        provider = self._make_provider()
        provider._active = True
        await provider.stop()
        assert not provider.is_active

    async def test_http_error_propagated(self):
        provider = self._make_provider()
        fake_response = FakeStreamResponse([], status_code=429)

        with patch.object(provider._client, "stream", return_value=fake_response):  # noqa: SIM117 nested scopes clarify setup and cleanup
            with pytest.raises(httpx.HTTPStatusError):
                async for _ in provider.synthesize("error test"):
                    pass

    async def test_http_error_posted_to_event_bus(self):
        """An HTTP status error emits a journal-visible provider Error.

        Brings OpenAI to parity with the WebSocket providers: a recorded
        bundle of an OpenAI TTS outage now carries a provider-scoped Error
        with HTTP status/body context, not just a logger line.
        """
        bus = EventBus()
        errors: list[Error] = []
        bus.subscribe(Error, lambda e: errors.append(e))

        provider = OpenAITTS(OpenAITTSConfig(api_key="k", event_bus=bus))
        fake_response = FakeStreamResponse([], status_code=429)

        with patch.object(provider._client, "stream", return_value=fake_response):  # noqa: SIM117 nested scopes clarify setup and cleanup
            with pytest.raises(httpx.HTTPStatusError):
                async for _ in provider.synthesize("error test"):
                    pass

        # Event bus emission is scheduled via create_task — yield once.
        await asyncio.sleep(0)
        assert len(errors) == 1
        err = errors[0]
        assert err.stage == ErrorStage.TTS
        assert err.provider == "openai"
        notes = getattr(err.exception, "__notes__", [])
        assert any("http_status=429" in n for n in notes)

    @pytest.mark.parametrize("status_code", [302, 401])
    async def test_streamed_http_error_surfaces_status_not_response_not_read(
        self,
        status_code: int,
    ) -> None:
        """A streamed 4xx/5xx surfaces the HTTPStatusError, not ResponseNotRead.

        client.stream() leaves the body unread, so accessing exc.response.text
        without a prior read raises httpx.ResponseNotRead on real httpx. The
        handler must read the body first so the real HTTP error propagates and a
        provider Error still reaches the event bus.
        """
        bus = EventBus()
        errors: list[Error] = []
        bus.subscribe(Error, lambda e: errors.append(e))

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code,
                stream=ChunkedAsyncByteStream([b"invalid api key"]),
            )

        provider = OpenAITTS(OpenAITTSConfig(api_key="bad", event_bus=bus))
        original_client = provider._client
        provider._client = httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(handle),
        )
        await original_client.aclose()
        try:
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                async for _ in provider.synthesize("error test"):
                    pass
        finally:
            await provider.close()

        assert exc_info.value.response.status_code == status_code

        await asyncio.sleep(0)
        assert len(errors) == 1
        err = errors[0]
        assert err.stage == ErrorStage.TTS
        assert err.provider == "openai"
        notes = getattr(err.exception, "__notes__", [])
        assert any(f"http_status={status_code}" in note for note in notes)

    async def test_no_event_bus_does_not_raise_on_error(self):
        """Without an event bus the error path stays a no-op (still raises)."""
        provider = self._make_provider()
        fake_response = FakeStreamResponse([], status_code=500)

        with patch.object(provider._client, "stream", return_value=fake_response):  # noqa: SIM117 nested scopes clarify setup and cleanup
            with pytest.raises(httpx.HTTPStatusError):
                async for _ in provider.synthesize("error test"):
                    pass

    async def test_close_closes_client(self):
        provider = self._make_provider()
        with patch.object(provider._client, "aclose", new_callable=AsyncMock) as mock_close:
            await provider.close()
            mock_close.assert_called_once()

    async def test_warmup_primes_models_endpoint(self):
        """warmup() issues a cheap GET /models (not a billed speech POST)."""
        provider = self._make_provider()
        fake_response = MagicMock()
        fake_response.aclose = AsyncMock()
        get_mock = AsyncMock(return_value=fake_response)

        with patch.object(provider._client, "get", get_mock):
            await provider.warmup()

        get_mock.assert_called_once_with("/models")
        fake_response.aclose.assert_awaited_once()

    async def test_warmup_swallows_errors(self):
        """A network/auth failure during warmup must not propagate."""
        provider = self._make_provider()
        get_mock = AsyncMock(side_effect=httpx.ConnectError("boom"))

        with patch.object(provider._client, "get", get_mock):
            # Must return cleanly: WarmupRunner re-raises, so a raise here
            # would abort Session.start().
            await provider.warmup()

    @pytest.mark.integration_live
    @pytest.mark.provider_openai
    @pytest.mark.surface_tts
    async def test_live_openai_tts(self):
        """Integration test requiring OPENAI_API_KEY env var."""
        import os

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            pytest.skip("OPENAI_API_KEY not set")

        provider = OpenAITTS(OpenAITTSConfig(api_key=api_key))
        try:
            events = []
            async for event in provider.synthesize("Hello, this is a test."):
                events.append(event)

            assert len(events) > 0
            chunks = extract_audio_chunks(events)
            assert verify_pcm16_audio(chunks)
        finally:
            await provider.close()
