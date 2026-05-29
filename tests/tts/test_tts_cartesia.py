"""Tests for Cartesia TTS provider."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from unittest.mock import AsyncMock, patch

import pytest

from easycat.audio_format import PCM16_MONO_24K
from easycat.events import Error, ErrorStage, EventBus, TTSEventType
from easycat.tts.cartesia_tts import CartesiaTTS, CartesiaTTSConfig
from tests.tts._harness import extract_audio_chunks, verify_pcm16_audio


def _pcm16_bytes(n_samples: int = 240) -> bytes:
    return struct.pack(f"<{n_samples}h", *([500] * n_samples))


def _chunk_msg(audio: bytes, *, done: bool = False) -> str:
    return json.dumps(
        {
            "type": "chunk",
            "context_id": "ctx",
            "data": base64.b64encode(audio).decode("ascii"),
            "done": done,
            "status_code": 200,
        }
    )


def _done_msg() -> str:
    return json.dumps({"type": "done", "context_id": "ctx", "done": True, "status_code": 200})


def _timestamps_msg() -> str:
    return json.dumps(
        {
            "type": "timestamps",
            "context_id": "ctx",
            "word_timestamps": {
                "words": ["hello", "world"],
                "start": [0.0, 0.4],
                "end": [0.3, 0.7],
            },
        }
    )


def _error_msg() -> str:
    return json.dumps(
        {
            "type": "error",
            "context_id": "ctx",
            "code": "invalid_voice",
            "title": "Invalid voice",
            "message": "voice_id not found",
            "status_code": 400,
            "done": True,
        }
    )


class FakeReconnectingWS:
    """Mock ReconnectingWebSocket for Cartesia TTS tests."""

    def __init__(
        self,
        messages: list[str | bytes] | None = None,
        on_reconnect=None,
        reconnect_after: int | None = None,
    ) -> None:
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


class TestCartesiaTTSConfig:
    def test_defaults(self):
        config = CartesiaTTSConfig(api_key="test-key")
        assert config.model_id == "sonic-3"
        assert config.encoding == "pcm_s16le"
        assert config.sample_rate == 24000
        assert config.output_format == PCM16_MONO_24K
        assert config.add_timestamps is True
        assert config.base_url.startswith("wss://api.cartesia.ai")

    def test_rejects_unsupported_encoding(self):
        with pytest.raises(ValueError, match="Unsupported Cartesia encoding"):
            CartesiaTTSConfig(api_key="k", encoding="pcm_mulaw")

    def test_custom_values(self):
        config = CartesiaTTSConfig(
            api_key="k",
            model_id="sonic-turbo",
            voice_id="voice-xyz",
            sample_rate=16000,
        )
        assert config.model_id == "sonic-turbo"
        assert config.voice_id == "voice-xyz"
        assert config.sample_rate == 16000


class TestCartesiaTTS:
    def _make_provider(self, **kwargs) -> CartesiaTTS:
        return CartesiaTTS(CartesiaTTSConfig(api_key="test-key", **kwargs))

    async def test_synthesize_yields_audio_events(self):
        provider = self._make_provider()
        audio_chunks = [_pcm16_bytes(240), _pcm16_bytes(240)]
        messages = [_chunk_msg(c) for c in audio_chunks] + [_done_msg()]
        fake_ws = FakeReconnectingWS(messages=messages)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = []
            async for event in provider.synthesize("Hello"):
                events.append(event)

        audio_events = [e for e in events if e.type == TTSEventType.AUDIO]
        assert len(audio_events) == 2
        chunks = extract_audio_chunks(audio_events)
        assert verify_pcm16_audio(chunks)

    async def test_synthesize_sends_expected_request(self):
        provider = self._make_provider(model_id="sonic-turbo", voice_id="voice-abc")
        fake_ws = FakeReconnectingWS(messages=[_done_msg()])

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for _ in provider.synthesize("Hello world"):
                pass

        assert len(fake_ws._sent) == 1
        request = json.loads(fake_ws._sent[0])
        assert request["model_id"] == "sonic-turbo"
        assert request["transcript"] == "Hello world"
        assert request["voice"] == {"mode": "id", "id": "voice-abc"}
        assert request["output_format"] == {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": 24000,
        }
        assert request["continue"] is False
        assert request["add_timestamps"] is True
        # Every synthesis gets a fresh UUIDv4 context id.
        assert isinstance(request["context_id"], str) and len(request["context_id"]) >= 32

    async def test_chunk_with_done_terminates_loop(self):
        provider = self._make_provider()
        messages = [
            _chunk_msg(_pcm16_bytes(100)),
            _chunk_msg(_pcm16_bytes(100), done=True),
            # Anything after `done: true` must be ignored.
            _chunk_msg(_pcm16_bytes(999)),
        ]
        fake_ws = FakeReconnectingWS(messages=messages)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            audio_events = [
                e async for e in provider.synthesize("test") if e.type == TTSEventType.AUDIO
            ]

        assert len(audio_events) == 2

    async def test_timestamps_emitted_as_markers(self):
        provider = self._make_provider()
        messages = [
            _chunk_msg(_pcm16_bytes(100)),
            _timestamps_msg(),
            _done_msg(),
        ]
        fake_ws = FakeReconnectingWS(messages=messages)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = [e async for e in provider.synthesize("hello world")]

        markers = [e for e in events if e.type == TTSEventType.MARKERS]
        assert len(markers) == 1
        assert markers[0].markers == [
            {
                "words": ["hello", "world"],
                "start": [0.0, 0.4],
                "end": [0.3, 0.7],
            }
        ]

    async def test_error_message_posted_to_event_bus(self):
        bus = EventBus()
        errors: list[Error] = []
        bus.subscribe(Error, lambda e: errors.append(e))

        provider = CartesiaTTS(CartesiaTTSConfig(api_key="k", event_bus=bus))
        fake_ws = FakeReconnectingWS(messages=[_error_msg()])

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for _ in provider.synthesize("test"):
                pass

        # Event bus emission is scheduled via create_task — yield once.
        await asyncio.sleep(0)
        assert len(errors) == 1
        err = errors[0]
        assert err.stage == ErrorStage.TTS
        assert err.provider == "cartesia"
        notes = getattr(err.exception, "__notes__", [])
        assert any("code=invalid_voice" in n for n in notes)
        assert any("status_code=400" in n for n in notes)

    async def test_cancel_sends_cancel_frame(self):
        provider = self._make_provider()
        audio = [_chunk_msg(_pcm16_bytes(100)) for _ in range(10)]
        fake_ws = FakeReconnectingWS(messages=audio)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = []
            async for event in provider.synthesize("long text"):
                events.append(event)
                if len(events) == 2:
                    await provider.cancel()

        assert len(events) == 2
        assert provider.is_cancelled
        cancel_msgs = [json.loads(s) for s in fake_ws._sent if isinstance(s, str)]
        assert any(m.get("cancel") is True for m in cancel_msgs)

    async def test_websocket_closed_after_synthesis(self):
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS(messages=[_done_msg()])

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for _ in provider.synthesize("test"):
                pass

        assert fake_ws._closed

    async def test_replay_request_resends_armed_request_mid_stream(self):
        """A mid-stream recv_iter-driven reconnect replays the armed request.

        Drives the on_reconnect hook after the first chunk and asserts the
        full synthesis request is re-sent on the (fake) socket, restarting the
        utterance from the top.
        """
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS(
            messages=[_chunk_msg(_pcm16_bytes(100)), _done_msg()],
            on_reconnect=provider._replay_request,
            reconnect_after=1,
        )

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for _ in provider.synthesize("Hello world"):
                pass

        # Initial send plus the replayed request: two identical frames.
        assert len(fake_ws._sent) == 2
        first = json.loads(fake_ws._sent[0])
        second = json.loads(fake_ws._sent[1])
        assert first["transcript"] == "Hello world"
        assert second == first

    async def test_replay_request_noop_when_unarmed(self):
        """Replay is a no-op when armed state is None (initial-connect retry)."""
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws
        provider._pending_request = None

        await provider._replay_request()

        assert fake_ws._sent == []

    async def test_replay_request_noop_when_cancelled(self):
        """Replay is a no-op once the provider is cancelled."""
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws
        provider._pending_request = json.dumps({"transcript": "Hello"})
        await provider.cancel()

        await provider._replay_request()

        assert fake_ws._sent == []

    async def test_ignores_malformed_json(self):
        provider = self._make_provider()
        messages = [
            "not json at all",
            _chunk_msg(_pcm16_bytes(100)),
            _done_msg(),
        ]
        fake_ws = FakeReconnectingWS(messages=messages)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            audio = [e async for e in provider.synthesize("test") if e.type == TTSEventType.AUDIO]
        assert len(audio) == 1

    async def test_ignores_binary_messages(self):
        provider = self._make_provider()
        messages: list[str | bytes] = [
            b"\x00\x01\x02",
            _chunk_msg(_pcm16_bytes(100)),
            _done_msg(),
        ]
        fake_ws = FakeReconnectingWS(messages=messages)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            audio = [e async for e in provider.synthesize("test") if e.type == TTSEventType.AUDIO]
        assert len(audio) == 1

    async def test_request_carries_max_buffer_delay_when_set(self):
        provider = self._make_provider(max_buffer_delay_ms=500)
        fake_ws = FakeReconnectingWS(messages=[_done_msg()])

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for _ in provider.synthesize("hi"):
                pass

        request = json.loads(fake_ws._sent[0])
        assert request["max_buffer_delay_ms"] == 500

    async def test_request_omits_max_buffer_delay_when_unset(self):
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS(messages=[_done_msg()])

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            async for _ in provider.synthesize("hi"):
                pass

        request = json.loads(fake_ws._sent[0])
        assert "max_buffer_delay_ms" not in request

    def test_version_info_shape(self):
        provider = self._make_provider()
        info = provider.version_info()
        assert info["provider"] == "cartesia"
        assert info["model"] == "sonic-3"
        assert "api_version" in info
        assert "sdk_version" in info

    @pytest.mark.integration_live
    @pytest.mark.provider_cartesia
    @pytest.mark.surface_tts
    async def test_live_cartesia_tts(self):
        """Integration test requiring CARTESIA_API_KEY env var."""
        import os

        api_key = os.environ.get("CARTESIA_API_KEY")
        if not api_key:
            pytest.skip("CARTESIA_API_KEY not set")

        provider = CartesiaTTS(CartesiaTTSConfig(api_key=api_key))
        events = []
        async for event in provider.synthesize("Hello, this is a test."):
            events.append(event)

        audio = [e for e in events if e.type == TTSEventType.AUDIO]
        assert len(audio) > 0
        chunks = extract_audio_chunks(audio)
        assert verify_pcm16_audio(chunks)
