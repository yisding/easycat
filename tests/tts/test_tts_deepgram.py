"""Tests for Deepgram TTS provider."""

from __future__ import annotations

import asyncio
import json
import struct
from unittest.mock import AsyncMock, patch

import pytest

from easycat.audio_format import PCM16_MONO_24K
from easycat.events import Error, ErrorStage, EventBus, TTSEventType
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from tests.tts._harness import extract_audio_chunks, verify_pcm16_audio


def _pcm16_bytes(n_samples: int = 240) -> bytes:
    return struct.pack(f"<{n_samples}h", *([500] * n_samples))


class FakeReconnectingWS:
    """Mock ReconnectingWebSocket for testing Deepgram TTS."""

    def __init__(self, messages: list[bytes | str] | None = None):
        self._messages = messages or []
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


class TestDeepgramTTSConfig:
    def test_defaults(self):
        config = DeepgramTTSConfig(api_key="test-key")
        assert config.model == "aura-asteria-en"
        assert config.encoding == "linear16"
        assert config.sample_rate == 24000
        assert config.output_format == PCM16_MONO_24K

    def test_custom_values(self):
        config = DeepgramTTSConfig(
            api_key="key",
            model="aura-orpheus-en",
            sample_rate=16000,
        )
        assert config.model == "aura-orpheus-en"
        assert config.sample_rate == 16000


class TestDeepgramTTS:
    def _make_provider(self, api_key: str = "test-key") -> DeepgramTTS:
        return DeepgramTTS(DeepgramTTSConfig(api_key=api_key))

    def test_build_url(self):
        provider = self._make_provider()
        url = provider._build_url()
        assert "model=aura-asteria-en" in url
        assert "encoding=linear16" in url
        assert "sample_rate=24000" in url

    async def test_synthesize_yields_audio_events(self):
        provider = self._make_provider()
        audio_chunks = [_pcm16_bytes(240), _pcm16_bytes(240)]
        flushed = json.dumps({"type": "Flushed"})
        messages = audio_chunks + [flushed]

        fake_ws = FakeReconnectingWS(messages=messages)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = []
            async for event in provider.synthesize("Hello"):
                events.append(event)

        assert len(events) == 2
        for e in events:
            assert e.type == TTSEventType.AUDIO

        chunks = extract_audio_chunks(events)
        assert verify_pcm16_audio(chunks)

    async def test_synthesize_sends_text_and_flush(self):
        provider = self._make_provider()
        flushed = json.dumps({"type": "Flushed"})
        fake_ws = FakeReconnectingWS(messages=[flushed])

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for _ in provider.synthesize("Test text"):
                pass

        assert len(fake_ws._sent) == 2
        speak_msg = json.loads(fake_ws._sent[0])
        assert speak_msg["type"] == "Speak"
        assert speak_msg["text"] == "Test text"

        flush_msg = json.loads(fake_ws._sent[1])
        assert flush_msg["type"] == "Flush"

    async def test_synthesize_stops_on_flush(self):
        provider = self._make_provider()
        audio = _pcm16_bytes(100)
        flushed = json.dumps({"type": "Flushed"})
        extra_audio = _pcm16_bytes(100)
        messages = [audio, flushed, extra_audio]

        fake_ws = FakeReconnectingWS(messages=messages)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = []
            async for event in provider.synthesize("test"):
                events.append(event)

        # Only the audio before "Flushed" should be yielded
        assert len(events) == 1

    async def test_cancel_stops_iteration(self):
        provider = self._make_provider()
        audio_chunks = [_pcm16_bytes(100)] * 10
        fake_ws = FakeReconnectingWS(messages=audio_chunks)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = []
            async for event in provider.synthesize("long text"):
                events.append(event)
                if len(events) == 2:
                    await provider.cancel()

        assert len(events) == 2
        assert provider.is_cancelled

    async def test_synthesize_tracks_active_state(self):
        provider = self._make_provider()
        flushed = json.dumps({"type": "Flushed"})
        fake_ws = FakeReconnectingWS(messages=[_pcm16_bytes(10), flushed])

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            assert not provider.is_active
            async for _ in provider.synthesize("hi"):
                assert provider.is_active
            assert not provider.is_active

    async def test_websocket_closed_after_synthesis(self):
        provider = self._make_provider()
        flushed = json.dumps({"type": "Flushed"})
        fake_ws = FakeReconnectingWS(messages=[flushed])

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for _ in provider.synthesize("test"):
                pass

        assert fake_ws._closed

    async def test_replay_disarmed_during_initial_connect(self):
        """on_reconnect fires for retries during the *initial* connect too.

        Replay must stay a no-op until the Speak/Flush frames have actually
        been sent on a connected stream; otherwise a retry mid-connect would
        send them before synthesize() does, duplicating the utterance.
        """
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws

        # State before the initial send: _pending_text is disarmed.
        provider._pending_text = None
        await provider._replay_request()
        assert fake_ws._sent == []

    async def test_replay_armed_after_initial_send(self):
        """After the initial send, a mid-stream reconnect replays the frames."""
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws

        provider._pending_text = "Hello"
        await provider._replay_request()
        assert len(fake_ws._sent) == 2
        assert json.loads(fake_ws._sent[0]) == {"type": "Speak", "text": "Hello"}
        assert json.loads(fake_ws._sent[1]) == {"type": "Flush"}

    async def test_stop_sends_flush_and_closes_ws(self):
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws

        await provider.stop()
        assert not provider.is_active
        assert len(fake_ws._sent) == 1
        msg = json.loads(fake_ws._sent[0])
        assert msg["type"] == "Flush"
        # stop() closes the socket so a graceful stop between turns does not
        # leave the WebSocket lingering until cancel()/close().
        assert fake_ws._closed
        assert provider._ws is None

    async def test_error_frame_posted_to_event_bus(self):
        bus = EventBus()
        errors: list[Error] = []
        bus.subscribe(Error, lambda e: errors.append(e))

        provider = DeepgramTTS(DeepgramTTSConfig(api_key="k", event_bus=bus))
        error_frame = json.dumps(
            {"type": "Error", "code": "INVALID_MODEL", "description": "bad model"}
        )
        fake_ws = FakeReconnectingWS(messages=[error_frame])

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for _ in provider.synthesize("test"):
                pass

        # Event bus emission is scheduled via create_task — yield once.
        await asyncio.sleep(0)
        assert len(errors) == 1
        err = errors[0]
        assert err.stage == ErrorStage.TTS
        assert err.provider == "deepgram"
        notes = getattr(err.exception, "__notes__", [])
        assert any("code=INVALID_MODEL" in n for n in notes)

    async def test_synthesis_exception_posted_to_event_bus(self):
        bus = EventBus()
        errors: list[Error] = []
        bus.subscribe(Error, lambda e: errors.append(e))

        provider = DeepgramTTS(DeepgramTTSConfig(api_key="k", event_bus=bus))
        fake_ws = FakeReconnectingWS()
        fake_ws.connect = AsyncMock(side_effect=RuntimeError("connect failed"))

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            with pytest.raises(RuntimeError, match="connect failed"):
                async for _ in provider.synthesize("test"):
                    pass

        await asyncio.sleep(0)
        assert len(errors) == 1
        assert errors[0].stage == ErrorStage.TTS
        assert errors[0].provider == "deepgram"

    @pytest.mark.integration_live
    @pytest.mark.provider_deepgram
    @pytest.mark.surface_tts
    async def test_live_deepgram_tts(self):
        """Integration test requiring DEEPGRAM_API_KEY env var."""
        import os

        api_key = os.environ.get("DEEPGRAM_API_KEY")
        if not api_key:
            pytest.skip("DEEPGRAM_API_KEY not set")

        provider = DeepgramTTS(DeepgramTTSConfig(api_key=api_key))
        events = []
        async for event in provider.synthesize("Hello, this is a test."):
            events.append(event)

        assert len(events) > 0
        chunks = extract_audio_chunks(events)
        assert verify_pcm16_audio(chunks)
