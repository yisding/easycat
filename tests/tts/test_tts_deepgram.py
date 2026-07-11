"""Tests for Deepgram TTS provider."""

from __future__ import annotations

import asyncio
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

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

    @property
    def is_connected(self) -> bool:
        return self.connect.await_count > 0 and not self._closed

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


class FakePersistentWS:
    """Queue-backed Deepgram socket supporting repeated Speak/Flush cycles."""

    def __init__(
        self,
        *,
        fail_connect: bool = False,
        hold_first_flush: bool = False,
    ) -> None:
        self._queue: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        self._pending_text: str | None = None
        self._fail_connect = fail_connect
        self._hold_first_flush = hold_first_flush
        self._connected = False
        self._closed = False
        self.connect_calls = 0
        self.sent: list[dict[str, str]] = []

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._closed

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._fail_connect:
            raise RuntimeError("connect boom")
        self._connected = True

    async def send(self, message: str | bytes) -> None:
        assert isinstance(message, str)
        frame = json.loads(message)
        self.sent.append(frame)
        if frame["type"] == "Speak":
            self._pending_text = frame["text"]
        elif frame["type"] == "Flush":
            assert self._pending_text is not None
            await self._queue.put(_pcm16_bytes(120))
            if self._hold_first_flush:
                self._hold_first_flush = False
            else:
                await self._queue.put(json.dumps({"type": "Flushed"}))
            self._pending_text = None
        elif frame["type"] == "Clear":
            self._pending_text = None
            await self._queue.put(json.dumps({"type": "Cleared"}))

    async def recv_iter(self):
        while True:
            message = await self._queue.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        self._closed = True
        self._connected = False
        await self._queue.put(None)


class TestDeepgramPersistent:
    def _make_provider(self) -> DeepgramTTS:
        return DeepgramTTS(DeepgramTTSConfig(api_key="test-key"))

    async def test_warmup_and_two_syntheses_reuse_one_connection(self):
        provider = self._make_provider()
        fake = FakePersistentWS()
        factory = MagicMock(return_value=fake)

        with patch.object(provider, "_create_ws", factory):
            await provider.warmup()
            first = [event async for event in provider.synthesize("first")]
            second = [event async for event in provider.synthesize("second")]

        assert first and second
        assert factory.call_count == 1
        assert fake.connect_calls == 1
        assert [frame["type"] for frame in fake.sent] == [
            "Speak",
            "Flush",
            "Speak",
            "Flush",
        ]
        assert not fake._closed
        await provider.close()
        assert fake._closed

    async def test_warmup_failure_retries_with_fresh_socket(self):
        provider = self._make_provider()
        failed = FakePersistentWS(fail_connect=True)
        working = FakePersistentWS()
        factory = MagicMock(side_effect=[failed, working])

        with patch.object(provider, "_create_ws", factory):
            await provider.warmup()
            events = [event async for event in provider.synthesize("retry")]

        assert events
        assert factory.call_count == 2
        assert failed._closed
        assert working.connect_calls == 1
        await provider.close()

    async def test_cancel_uses_clear_and_keeps_socket_for_next_turn(self):
        provider = self._make_provider()
        fake = FakePersistentWS(hold_first_flush=True)
        first_audio = asyncio.Event()
        cancelled_events = []

        async def _consume_cancelled_turn() -> None:
            async for event in provider.synthesize("cancel me"):
                cancelled_events.append(event)
                first_audio.set()

        with patch.object(provider, "_create_ws", return_value=fake):
            synthesis_task = asyncio.create_task(_consume_cancelled_turn())
            await first_audio.wait()
            await provider.cancel()
            await synthesis_task
            next_events = [event async for event in provider.synthesize("next turn")]

        assert len(cancelled_events) == 1
        assert next_events
        assert [frame["type"] for frame in fake.sent].count("Clear") == 1
        assert fake.connect_calls == 1
        assert not fake._closed
        await provider.close()

    async def test_stop_keeps_idle_persistent_socket_open(self):
        provider = self._make_provider()
        fake = FakePersistentWS()

        with patch.object(provider, "_create_ws", return_value=fake):
            await provider.warmup()
            await provider.stop()

        assert fake.sent == []
        assert not fake._closed
        await provider.close()

    async def test_cancel_does_not_wait_for_clear_ack(self):
        provider = self._make_provider()

        class NoClearAckWS(FakePersistentWS):
            async def send(self, message: str | bytes) -> None:
                assert isinstance(message, str)
                frame = json.loads(message)
                if frame["type"] == "Clear":
                    self.sent.append(frame)
                    return
                await super().send(message)

        fake = NoClearAckWS(hold_first_flush=True)
        first_audio = asyncio.Event()

        async def _consume() -> None:
            async for _ in provider.synthesize("cancel promptly"):
                first_audio.set()

        with patch.object(provider, "_create_ws", return_value=fake):
            synthesis_task = asyncio.create_task(_consume())
            await first_audio.wait()
            await asyncio.wait_for(provider.cancel(), timeout=0.05)
            synthesis_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await synthesis_task

        assert fake._closed
        assert provider._ws is None


class TestDeepgramTTSConfig:
    def test_defaults(self):
        config = DeepgramTTSConfig(api_key="test-key")
        assert config.model == "aura-asteria-en"
        assert config.encoding == "linear16"
        assert config.sample_rate == 24000
        assert config.output_format == PCM16_MONO_24K
        assert config.persistent_ws is True

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
        return DeepgramTTS(DeepgramTTSConfig(api_key=api_key, persistent_ws=False))

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

    async def test_stop_closes_one_shot_ws_without_redundant_flush(self):
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws

        await provider.stop()
        assert not provider.is_active
        assert fake_ws._sent == []
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

    async def test_non_object_control_frame_does_not_truncate_audio(self):
        provider = self._make_provider()
        flushed = json.dumps({"type": "Flushed"})
        messages = [_pcm16_bytes(240), "[]", _pcm16_bytes(240), flushed]
        fake_ws = FakeReconnectingWS(messages=messages)

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = []
            async for event in provider.synthesize("test"):
                events.append(event)

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
        try:
            events = []
            async for event in provider.synthesize("Hello, this is a test."):
                events.append(event)
        finally:
            await provider.close()

        assert len(events) > 0
        chunks = extract_audio_chunks(events)
        assert verify_pcm16_audio(chunks)
