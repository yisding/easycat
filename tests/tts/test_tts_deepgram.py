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

    def __init__(
        self,
        messages: list[bytes | str] | None = None,
        on_reconnect=None,
        reconnect_after: int | None = None,
    ):
        self._messages = messages or []
        self._sent: list[str | bytes] = []
        self._closed = False
        # ``on_reconnect`` mirrors the hook the provider passes to the real
        # ReconnectingWebSocket constructor. ``reconnect_after`` (when set)
        # makes ``recv_iter`` invoke that hook after yielding that many
        # messages, simulating a mid-stream recv_iter-driven reconnect.
        self._on_reconnect = on_reconnect
        self._reconnect_after = reconnect_after
        self.connect = AsyncMock()

    async def send(self, message: str | bytes) -> None:
        self._sent.append(message)

    async def recv_iter(self):
        for i, msg in enumerate(self._messages):
            yield msg
            if self._reconnect_after is not None and i + 1 == self._reconnect_after:
                result = self._on_reconnect()
                if asyncio.iscoroutine(result):
                    await result

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

    async def test_replay_request_resends_frames_mid_stream(self):
        """A mid-stream recv_iter-driven reconnect replays the Speak/Flush frames.

        Drives the on_reconnect hook after the first audio chunk and asserts
        the Speak + Flush frames are re-sent on the (fake) socket, restarting
        the utterance from the top.
        """
        provider = self._make_provider()
        flushed = json.dumps({"type": "Flushed"})
        fake_ws = FakeReconnectingWS(
            messages=[_pcm16_bytes(120), flushed],
            on_reconnect=provider._replay_request,
            reconnect_after=1,
        )

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for _ in provider.synthesize("Hello"):
                pass

        # Initial Speak + Flush, then the replayed Speak + Flush.
        assert [json.loads(m) for m in fake_ws._sent] == [
            {"type": "Speak", "text": "Hello"},
            {"type": "Flush"},
            {"type": "Speak", "text": "Hello"},
            {"type": "Flush"},
        ]

    async def test_replay_request_noop_when_cancelled(self):
        """Replay is a no-op once the provider is cancelled."""
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws
        provider._pending_text = "Hello"
        await provider.cancel()

        await provider._replay_request()

        assert fake_ws._sent == []

    async def test_replay_request_resets_sample_carry(self):
        """A held sub-sample byte is dropped before the utterance restarts.

        Without this reset, an odd-byte remainder left in ``_sample_carry``
        when the socket dropped would be prepended to the restarted-from-top
        stream's first chunk, shifting every replayed sample by one byte.
        """
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws
        provider._pending_text = "Hello"
        # Simulate a split 16-bit sample held across the dropped frame.
        provider._sample_carry = b"\x01"

        await provider._replay_request()

        assert provider._sample_carry == b""
        assert len(fake_ws._sent) == 2

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
        # The frame type ("Error") is redundant and must not be attached as a
        # ws_close_code note — that key is reserved for an actual WS close code.
        assert not any(n.startswith("ws_close_code=") for n in notes)

    async def test_warning_frame_does_not_truncate_or_emit_error(self):
        bus = EventBus()
        errors: list[Error] = []
        bus.subscribe(Error, lambda e: errors.append(e))

        provider = DeepgramTTS(DeepgramTTSConfig(api_key="k", event_bus=bus))
        # A Warning frame arrives mid-stream; synthesis must continue and the
        # audio after it must still be delivered (no premature break).
        warning_frame = json.dumps({"type": "Warning", "description": "TEXT_LENGTH_WARNING"})
        flushed = json.dumps({"type": "Flushed"})
        messages = [_pcm16_bytes(240), warning_frame, _pcm16_bytes(240), flushed]
        fake_ws = FakeReconnectingWS(messages=messages)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = []
            async for event in provider.synthesize("test"):
                events.append(event)

        await asyncio.sleep(0)
        # Warning is non-fatal: no Error emitted and all audio delivered.
        assert errors == []
        assert len(events) == 2
        for e in events:
            assert e.type == TTSEventType.AUDIO

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
