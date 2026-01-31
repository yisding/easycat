"""Mid-utterance cancellation tests for all TTS providers.

Task 3.6: For each provider, verify:
- Start synthesis of a long text
- Cancel partway through
- No more audio chunks are yielded after cancel
- Provider connection is cleaned up (no resource leaks)
"""

from __future__ import annotations

import json
import struct
from unittest.mock import AsyncMock, patch

from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import (
    ElevenLabsStreamMode,
    ElevenLabsTTS,
    ElevenLabsTTSConfig,
)
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig


def _pcm16_bytes(n_samples: int = 480) -> bytes:
    """Generate PCM16 data simulating a chunk of speech."""
    return struct.pack(f"<{n_samples}h", *([1000] * n_samples))


LONG_TEXT = "This is a very long text that would produce many audio chunks from the TTS provider."


# ── Fake stream helpers ───────────────────────────────────────────


class FakeHTTPStream:
    def __init__(self, chunks: list[bytes], status_code: int = 200):
        self._chunks = chunks
        self.status_code = status_code
        self._closed = False

    def raise_for_status(self):
        pass

    async def aiter_bytes(self, chunk_size: int = 4096):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self._closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


class FakeWS:
    def __init__(self, messages: list[bytes | str]):
        self._messages = messages
        self._sent: list[str | bytes] = []
        self._closed = False
        self.connect = AsyncMock()

    async def send(self, message: str | bytes) -> None:
        self._sent.append(message)

    async def recv_iter(self):
        for msg in self._messages:
            yield msg

    async def close(self) -> None:
        self._closed = True


# ── OpenAI cancellation ──────────────────────────────────────────


class TestOpenAICancellation:
    async def test_cancel_mid_stream(self):
        """Cancel OpenAI TTS after receiving 3 chunks from a long stream."""
        provider = OpenAITTS(OpenAITTSConfig(api_key="test"))
        many_chunks = [_pcm16_bytes() for _ in range(20)]
        fake_response = FakeHTTPStream(many_chunks)

        with patch.object(provider._client, "stream", return_value=fake_response):
            events = []
            async for event in provider.synthesize(LONG_TEXT):
                events.append(event)
                if len(events) == 3:
                    await provider.cancel()

        assert len(events) == 3
        assert provider.is_cancelled
        assert not provider.is_active
        # HTTP response should have been closed
        assert provider._response is None

    async def test_cancel_on_first_chunk(self):
        """Cancel immediately on first chunk, should yield only 1 event."""
        provider = OpenAITTS(OpenAITTSConfig(api_key="test"))
        many_chunks = [_pcm16_bytes() for _ in range(10)]
        fake_response = FakeHTTPStream(many_chunks)

        with patch.object(provider._client, "stream", return_value=fake_response):
            events = []
            async for event in provider.synthesize(LONG_TEXT):
                events.append(event)
                await provider.cancel()

        assert len(events) == 1
        assert provider.is_cancelled
        assert provider._response is None

    async def test_cancel_cleanup(self):
        """Verify no resource leaks after cancel."""
        provider = OpenAITTS(OpenAITTSConfig(api_key="test"))
        fake_response = FakeHTTPStream([_pcm16_bytes()] * 5)

        with patch.object(provider._client, "stream", return_value=fake_response):
            events = []
            async for event in provider.synthesize(LONG_TEXT):
                events.append(event)
                if len(events) == 1:
                    await provider.cancel()

        assert provider._response is None
        assert not provider.is_active


# ── Deepgram cancellation ────────────────────────────────────────


class TestDeepgramCancellation:
    async def test_cancel_mid_stream(self):
        """Cancel Deepgram TTS after receiving 3 audio chunks."""
        provider = DeepgramTTS(DeepgramTTSConfig(api_key="test"))
        many_chunks = [_pcm16_bytes() for _ in range(20)]
        fake_ws = FakeWS(messages=many_chunks)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = []
            async for event in provider.synthesize(LONG_TEXT):
                events.append(event)
                if len(events) == 3:
                    await provider.cancel()

        assert len(events) == 3
        assert provider.is_cancelled
        assert not provider.is_active

    async def test_cancel_closes_websocket(self):
        """Verify WebSocket is closed after cancel."""
        provider = DeepgramTTS(DeepgramTTSConfig(api_key="test"))
        fake_ws = FakeWS(messages=[_pcm16_bytes()] * 10)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for event in provider.synthesize(LONG_TEXT):
                await provider.cancel()
                break

        assert provider._ws is None
        assert not provider.is_active

    async def test_cancel_on_first_chunk(self):
        """Cancel on the first audio chunk."""
        provider = DeepgramTTS(DeepgramTTSConfig(api_key="test"))
        fake_ws = FakeWS(messages=[_pcm16_bytes()] * 5)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = []
            async for event in provider.synthesize(LONG_TEXT):
                events.append(event)
                await provider.cancel()

        assert len(events) == 1
        assert provider.is_cancelled
        assert provider._ws is None


# ── ElevenLabs HTTP cancellation ─────────────────────────────────


class TestElevenLabsHTTPCancellation:
    def _make_provider(self) -> ElevenLabsTTS:
        return ElevenLabsTTS(
            ElevenLabsTTSConfig(
                api_key="test",
                stream_mode=ElevenLabsStreamMode.HTTP,
            )
        )

    async def test_cancel_mid_stream(self):
        """Cancel ElevenLabs HTTP TTS after receiving 3 chunks."""
        provider = self._make_provider()
        many_chunks = [_pcm16_bytes() for _ in range(20)]
        fake_response = FakeHTTPStream(many_chunks)

        client = provider._get_http_client()
        with patch.object(client, "stream", return_value=fake_response):
            events = []
            async for event in provider.synthesize(LONG_TEXT):
                events.append(event)
                if len(events) == 3:
                    await provider.cancel()

        assert len(events) == 3
        assert provider.is_cancelled
        assert not provider.is_active

    async def test_cancel_cleanup(self):
        """Verify no resource leaks after HTTP cancel."""
        provider = self._make_provider()
        fake_response = FakeHTTPStream([_pcm16_bytes()] * 5)

        client = provider._get_http_client()
        with patch.object(client, "stream", return_value=fake_response):
            events = []
            async for event in provider.synthesize(LONG_TEXT):
                events.append(event)
                if len(events) == 1:
                    await provider.cancel()

        assert provider._response is None
        assert not provider.is_active


# ── ElevenLabs WebSocket cancellation ────────────────────────────


class TestElevenLabsWSCancellation:
    def _make_provider(self) -> ElevenLabsTTS:
        return ElevenLabsTTS(
            ElevenLabsTTSConfig(
                api_key="test",
                stream_mode=ElevenLabsStreamMode.WEBSOCKET,
            )
        )

    def _audio_msg(self, n_samples: int = 480) -> str:
        import base64

        audio = _pcm16_bytes(n_samples)
        return json.dumps({"audio": base64.b64encode(audio).decode()})

    async def test_cancel_mid_stream(self):
        """Cancel ElevenLabs WS TTS after receiving 3 audio messages."""
        provider = self._make_provider()
        messages = [self._audio_msg() for _ in range(20)]
        fake_ws = FakeWS(messages=messages)

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            return_value=fake_ws,
        ):
            events = []
            async for event in provider.synthesize(LONG_TEXT):
                events.append(event)
                if len(events) == 3:
                    await provider.cancel()

        assert len(events) == 3
        assert provider.is_cancelled
        assert not provider.is_active

    async def test_cancel_closes_websocket(self):
        """Verify WS connection is cleaned up after cancel."""
        provider = self._make_provider()
        messages = [self._audio_msg() for _ in range(10)]
        fake_ws = FakeWS(messages=messages)

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            return_value=fake_ws,
        ):
            async for event in provider.synthesize(LONG_TEXT):
                await provider.cancel()
                break

        assert provider._ws is None
        assert not provider.is_active
