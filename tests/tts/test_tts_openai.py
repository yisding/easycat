"""Tests for OpenAI TTS provider."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from easycat.audio_format import PCM16_MONO_24K
from easycat.events import TTSEventType
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
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

    def raise_for_status(self):
        if self.status_code >= 400:
            response = MagicMock()
            response.status_code = self.status_code
            response.text = "error"
            raise httpx.HTTPStatusError("error", request=MagicMock(), response=response)

    async def aiter_bytes(self, chunk_size: int = 4096):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.is_closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


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

        assert len(events) == 2
        for e in events:
            assert e.type == TTSEventType.AUDIO
            assert e.audio is not None

        chunks = extract_audio_chunks(events)
        assert verify_pcm16_audio(chunks)

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
        pcm_data = [_pcm16_bytes(100)] * 10
        fake_response = FakeStreamResponse(pcm_data)

        with patch.object(provider._client, "stream", return_value=fake_response):
            events = []
            async for event in provider.synthesize("long text"):
                events.append(event)
                if len(events) == 2:
                    await provider.cancel()

        assert len(events) == 2
        assert provider.is_cancelled

    async def test_stop_sets_inactive(self):
        provider = self._make_provider()
        provider._active = True
        await provider.stop()
        assert not provider.is_active

    async def test_http_error_propagated(self):
        provider = self._make_provider()
        fake_response = FakeStreamResponse([], status_code=429)

        with patch.object(provider._client, "stream", return_value=fake_response):
            with pytest.raises(httpx.HTTPStatusError):
                async for _ in provider.synthesize("error test"):
                    pass

    async def test_close_closes_client(self):
        provider = self._make_provider()
        with patch.object(provider._client, "aclose", new_callable=AsyncMock) as mock_close:
            await provider.close()
            mock_close.assert_called_once()

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
