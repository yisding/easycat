"""Tests for Cartesia TTS provider."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

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


class FakePersistentWS:
    """Fake multi-context socket for the persistent Cartesia path.

    The manager calls ``connect_factory(on_reconnect)`` (recorded), then
    ``connect()``, then iterates ``recv_iter()``. The script can address frames
    to a specific context id via ``script_for``.
    """

    def __init__(self, on_reconnect=None, *, fail_send_at=None):
        self.on_reconnect = on_reconnect
        self.sent: list[str] = []
        self._send_count = 0
        self._fail_send_at = fail_send_at or set()
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None:
        return None

    async def send(self, message: str) -> None:
        self._send_count += 1
        if self._send_count in self._fail_send_at:
            raise RuntimeError("send failed")
        self.sent.append(message)
        # Auto-respond to a synthesis request with a chunk + done for its ctx.
        try:
            msg = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return
        if "transcript" in msg:
            ctx_id = msg["context_id"]
            await self._queue.put(
                json.dumps(
                    {
                        "type": "chunk",
                        "context_id": ctx_id,
                        "data": base64.b64encode(_pcm16_bytes(120)).decode("ascii"),
                        "done": False,
                    }
                )
            )
            await self._queue.put(json.dumps({"type": "done", "context_id": ctx_id, "done": True}))

    async def recv_iter(self):
        # Single await point per iteration so the reader task can be cancelled
        # cleanly without orphaning helper tasks.
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(None)


class TestCartesiaPersistent:
    def _make_provider(self, **kwargs) -> CartesiaTTS:
        return CartesiaTTS(CartesiaTTSConfig(api_key="test-key", persistent_ws=True, **kwargs))

    async def test_one_socket_across_two_synthesize_calls(self):
        provider = self._make_provider()
        fake = FakePersistentWS()
        factory = MagicMock(return_value=fake)

        with patch.object(provider, "_build_ws", factory):
            ctx_ids = []
            for text in ("first", "second"):
                async for _ in provider.synthesize(text):
                    pass
                ctx_ids.append(provider._context_id or "cleared")

        # connect_factory was invoked exactly once -> one socket reused.
        assert factory.call_count == 1
        # Two distinct fresh context ids were used (recorded from sent frames).
        sent_ctx = [
            json.loads(s)["context_id"] for s in fake.sent if "transcript" in json.loads(s)
        ]
        assert len(sent_ctx) == 2
        assert sent_ctx[0] != sent_ctx[1]
        assert all(len(c) >= 32 for c in sent_ctx)
        await provider.close()

    async def test_warmup_connects_before_first_synthesis(self):
        provider = self._make_provider()
        fake = FakePersistentWS()
        factory = MagicMock(return_value=fake)

        with patch.object(provider, "_build_ws", factory):
            await provider.warmup()
            assert factory.call_count == 1
            assert provider._mgr is not None and provider._mgr._contexts == {}

            async for _ in provider.synthesize("first"):
                pass

        assert factory.call_count == 1
        await provider.close()

    async def test_synthesize_yields_audio(self):
        provider = self._make_provider()
        fake = FakePersistentWS()

        with patch.object(provider, "_build_ws", return_value=fake):
            audio = [e async for e in provider.synthesize("hello") if e.type == TTSEventType.AUDIO]
        assert len(audio) == 1
        chunks = extract_audio_chunks(audio)
        assert verify_pcm16_audio(chunks)
        await provider.close()

    async def test_stray_non_object_frame_does_not_break_stream(self):
        # A valid-but-non-object JSON frame on the shared socket (exercising the
        # REAL Cartesia adapter) must be dropped, not crash the reader: audio
        # still flows and the turn completes.
        fake = FakePersistentWS()

        async def _send_with_stray(message: str) -> None:
            fake.sent.append(message)
            msg = json.loads(message)
            if "transcript" in msg:
                ctx_id = msg["context_id"]
                await fake._queue.put('"pong"')  # stray keepalive-ish frame
                await fake._queue.put(
                    json.dumps(
                        {
                            "type": "chunk",
                            "context_id": ctx_id,
                            "data": base64.b64encode(_pcm16_bytes(120)).decode("ascii"),
                            "done": True,
                        }
                    )
                )

        provider = self._make_provider()
        with patch.object(provider, "_build_ws", return_value=fake):
            fake.send = _send_with_stray  # type: ignore[method-assign]
            audio = [e async for e in provider.synthesize("hi") if e.type == TTSEventType.AUDIO]
        assert len(audio) == 1
        assert not fake.closed  # socket survived the stray frame
        await provider.close()

    async def test_cancel_sends_cancel_and_keeps_socket_open(self):
        provider = self._make_provider()
        fake = FakePersistentWS()

        with patch.object(provider, "_build_ws", return_value=fake):
            events = []
            async for event in provider.synthesize("long text"):
                events.append(event)
                if event.type == TTSEventType.AUDIO:
                    await provider.cancel()

            assert provider.is_cancelled
            # Socket NOT closed by the context-scoped cancel.
            assert not fake.closed
            cancel = [json.loads(s) for s in fake.sent if json.loads(s).get("cancel") is True]
            assert cancel and cancel[0]["cancel"] is True
        await provider.close()

    async def test_cancel_send_failure_falls_back_to_socket_close(self):
        provider = self._make_provider()
        # First send (the transcript) succeeds; the cancel frame send fails.
        fake = FakePersistentWS(fail_send_at={2})

        with patch.object(provider, "_build_ws", return_value=fake):
            async for event in provider.synthesize("text"):
                if event.type == TTSEventType.AUDIO:
                    await provider.cancel()
                    break

            assert fake.closed
        await provider.close()

    async def test_close_tears_down(self):
        provider = self._make_provider()
        fake = FakePersistentWS()

        with patch.object(provider, "_build_ws", return_value=fake):
            async for _ in provider.synthesize("hi"):
                pass
            await provider.close()

        assert fake.closed
        assert provider._mgr is not None and provider._mgr._closed

    async def test_persistent_connect_failure_ends_synthesis(self):
        # A failed initial connect on the persistent path must emit the error
        # and clear is_active (run _end_synthesis), like the one-shot path —
        # not leave the provider stuck active.
        provider = self._make_provider()

        class FailingConnectWS(FakePersistentWS):
            async def connect(self) -> None:
                raise RuntimeError("connect boom")

        with patch.object(provider, "_build_ws", return_value=FailingConnectWS()):
            with pytest.raises(RuntimeError, match="connect boom"):
                async for _ in provider.synthesize("hi"):
                    pass

        assert not provider.is_active
        await provider.close()


class TestCartesiaPersistentEquivalence:
    """Persistent transport must not change the emitted audio bytes."""

    async def test_decoded_pcm_identical_persistent_vs_default(self):
        audio_chunks = [_pcm16_bytes(241), _pcm16_bytes(239)]  # odd splits

        # Default one-shot path.
        default_provider = CartesiaTTS(CartesiaTTSConfig(api_key="k", persistent_ws=False))
        default_msgs = [_chunk_msg(c) for c in audio_chunks] + [_done_msg()]
        default_ws = FakeReconnectingWS(messages=default_msgs)
        with patch.object(default_provider, "_create_ws", return_value=default_ws):
            default_audio = b"".join(
                [
                    bytes(e.audio.data)
                    async for e in default_provider.synthesize("Hi")
                    if e.type == TTSEventType.AUDIO
                ]
            )

        # Persistent path: feed the SAME audio frames addressed to the ctx.
        persistent_provider = CartesiaTTS(CartesiaTTSConfig(api_key="k", persistent_ws=True))

        class ScriptedPersistentWS(FakePersistentWS):
            async def send(self, message: str) -> None:
                self._send_count += 1
                self.sent.append(message)
                msg = json.loads(message)
                if "transcript" in msg:
                    cid = msg["context_id"]
                    for c in audio_chunks:
                        await self._queue.put(
                            json.dumps(
                                {
                                    "type": "chunk",
                                    "context_id": cid,
                                    "data": base64.b64encode(c).decode("ascii"),
                                    "done": False,
                                }
                            )
                        )
                    await self._queue.put(
                        json.dumps({"type": "done", "context_id": cid, "done": True})
                    )

        scripted = ScriptedPersistentWS()
        with patch.object(persistent_provider, "_build_ws", return_value=scripted):
            persistent_audio = b"".join(
                [
                    bytes(e.audio.data)
                    async for e in persistent_provider.synthesize("Hi")
                    if e.type == TTSEventType.AUDIO
                ]
            )
        await persistent_provider.close()

        assert persistent_audio == default_audio
        assert len(persistent_audio) > 0


class TestCartesiaTTSConfig:
    def test_defaults(self):
        config = CartesiaTTSConfig(api_key="test-key")
        assert config.model_id == "sonic-3"
        assert config.encoding == "pcm_s16le"
        assert config.sample_rate == 24000
        assert config.output_format == PCM16_MONO_24K
        assert config.add_timestamps is True
        assert config.base_url.startswith("wss://api.cartesia.ai")
        assert config.persistent_ws is True

    def test_rejects_unsupported_encoding(self):
        with pytest.raises(ValueError, match="Unsupported Cartesia encoding"):
            CartesiaTTSConfig(api_key="k", encoding="pcm_mulaw")

    def test_rejects_out_of_range_speed(self):
        with pytest.raises(ValueError, match="speed must be in"):
            CartesiaTTSConfig(api_key="k", speed=2.0)

    def test_rejects_out_of_range_volume(self):
        with pytest.raises(ValueError, match="volume must be in"):
            CartesiaTTSConfig(api_key="k", volume=0.1)

    def test_generation_config_omitted_when_unset(self):
        provider = CartesiaTTS(CartesiaTTSConfig(api_key="k"))
        request = provider._build_request("hi", "ctx-1")
        assert "generation_config" not in request

    def test_generation_config_includes_only_set_controls(self):
        provider = CartesiaTTS(CartesiaTTSConfig(api_key="k", speed=1.2, emotion="excited"))
        request = provider._build_request("hi", "ctx-1")
        assert request["generation_config"] == {"speed": 1.2, "emotion": "excited"}

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
        kwargs.setdefault("persistent_ws", False)
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

        provider = CartesiaTTS(CartesiaTTSConfig(api_key="k", event_bus=bus, persistent_ws=False))
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

    async def test_replay_request_resets_sample_carry(self):
        """A held sub-sample byte is dropped before the utterance restarts.

        Without this reset, an odd-byte remainder left in ``_sample_carry``
        when the socket dropped would be prepended to the restarted-from-top
        stream's first chunk, shifting every replayed sample by one byte.
        """
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws
        provider._pending_request = json.dumps({"transcript": "Hello"})
        # Simulate a split 16-bit sample held across the dropped frame.
        provider._sample_carry = b"\x01"

        await provider._replay_request()

        assert provider._sample_carry == b""
        assert len(fake_ws._sent) == 1

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
