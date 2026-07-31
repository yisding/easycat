"""Tests for Deepgram TTS provider."""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from easycat.audio_format import PCM16_MONO_24K
from easycat.events import Error, ErrorStage, EventBus, TTSEventType
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from perf._deepgram_socket import QueueDeepgramSocket
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


class TestDeepgramPersistent:
    def _make_provider(self) -> DeepgramTTS:
        return DeepgramTTS(DeepgramTTSConfig(api_key="test-key"))

    async def test_warmup_and_two_syntheses_reuse_one_connection(self):
        provider = self._make_provider()
        fake = QueueDeepgramSocket(audio=_pcm16_bytes(120))
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
        failed = QueueDeepgramSocket(fail_connect=True)
        working = QueueDeepgramSocket(audio=_pcm16_bytes(120))
        factory = MagicMock(side_effect=[failed, working])

        with patch.object(provider, "_create_ws", factory):
            await provider.warmup()
            events = [event async for event in provider.synthesize("retry")]

        assert events
        assert factory.call_count == 2
        assert failed._closed
        assert working.connect_calls == 1
        await provider.close()

    async def test_unlimited_retry_warmup_times_out_before_synthesis_retry(self):
        provider = DeepgramTTS(
            DeepgramTTSConfig(
                api_key="test-key",
                reconnect_max_retries=-1,
                warmup_timeout_s=0.01,
            )
        )

        class HangingConnectSocket(QueueDeepgramSocket):
            async def connect(self) -> None:
                self.connect_calls += 1
                await asyncio.Event().wait()

        hanging = HangingConnectSocket()
        working = QueueDeepgramSocket(audio=_pcm16_bytes(120))
        factory = MagicMock(side_effect=[hanging, working])

        with patch.object(provider, "_create_ws", factory):
            await asyncio.wait_for(provider.warmup(), timeout=0.1)
            events = [event async for event in provider.synthesize("retry")]

        assert events
        assert factory.call_count == 2
        assert hanging._closed
        assert working.connect_calls == 1
        await provider.close()

    async def test_cancel_uses_clear_and_keeps_socket_for_next_turn(self):
        provider = self._make_provider()
        fake = QueueDeepgramSocket(audio=_pcm16_bytes(120), hold_first_flush=True)
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
            assert synthesis_task.done()
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
        fake = QueueDeepgramSocket()

        with patch.object(provider, "_create_ws", return_value=fake):
            await provider.warmup()
            await provider.stop()

        assert fake.sent == []
        assert not fake._closed
        await provider.close()

    async def test_idle_cancel_keeps_warmed_persistent_socket_open(self):
        provider = self._make_provider()
        fake = QueueDeepgramSocket(audio=_pcm16_bytes(120))
        factory = MagicMock(return_value=fake)

        with patch.object(provider, "_create_ws", factory):
            await provider.warmup()
            await provider.cancel()
            events = [event async for event in provider.synthesize("next turn")]

        assert events
        assert factory.call_count == 1
        assert fake.connect_calls == 1
        assert not fake._closed
        await provider.close()

    async def test_cancel_times_out_and_closes_without_clear_ack(self):
        provider = DeepgramTTS(DeepgramTTSConfig(api_key="test-key", clear_timeout_s=0.01))
        fake = QueueDeepgramSocket(acknowledge_clear=False, hold_first_flush=True)
        first_audio = asyncio.Event()

        async def _consume() -> None:
            async for _ in provider.synthesize("cancel promptly"):
                first_audio.set()

        with patch.object(provider, "_create_ws", return_value=fake):
            synthesis_task = asyncio.create_task(_consume())
            await first_audio.wait()
            await asyncio.wait_for(provider.cancel(), timeout=0.1)
            await synthesis_task

        assert fake._closed
        assert provider._ws is None

    async def test_cancel_during_blocked_speak_discards_socket_before_next_turn(self):
        class BlockingSpeakSocket(QueueDeepgramSocket):
            def __init__(self) -> None:
                super().__init__(audio=b"old-audio!")
                self.speak_started = asyncio.Event()
                self.release_speak = asyncio.Event()
                self._first_speak = True

            async def send(self, message: str | bytes) -> None:
                frame = json.loads(message)
                if frame["type"] == "Speak" and self._first_speak:
                    self._first_speak = False
                    self.speak_started.set()
                    await self.release_speak.wait()
                await super().send(message)

        provider = self._make_provider()
        blocked = BlockingSpeakSocket()
        fresh = QueueDeepgramSocket(audio=b"new-audio!")
        factory = MagicMock(side_effect=[blocked, fresh])

        async def consume_first() -> list:
            return [event async for event in provider.synthesize("first")]

        with patch.object(provider, "_create_ws", factory):
            first = asyncio.create_task(consume_first())
            await blocked.speak_started.wait()
            cancelling = asyncio.create_task(provider.cancel())
            await asyncio.sleep(0)
            blocked.release_speak.set()
            await asyncio.wait_for(cancelling, timeout=1)
            assert await first == []

            next_events = [event async for event in provider.synthesize("second")]

        audio = [
            bytes(event.audio.data) for event in next_events if event.type == TTSEventType.AUDIO
        ]
        assert audio == [b"new-audio!"]
        assert blocked._closed is True
        assert factory.call_count == 2
        await provider.close()

    async def test_early_exit_closes_inner_cycle_before_next_synthesis(self):
        provider = self._make_provider()
        abandoned = QueueDeepgramSocket(audio=_pcm16_bytes(120), hold_first_flush=True)
        next_socket = QueueDeepgramSocket(audio=_pcm16_bytes(120))
        factory = MagicMock(side_effect=[abandoned, next_socket])

        with patch.object(provider, "_create_ws", factory):
            stream = provider.synthesize("abandoned")
            async with contextlib.aclosing(stream):
                first = await anext(stream)
                assert first.type == TTSEventType.AUDIO

            assert abandoned._closed
            assert provider._ws is None
            assert not provider.is_active

            next_events = [event async for event in provider.synthesize("next")]

        assert next_events
        assert factory.call_count == 2
        assert not next_socket._closed
        await provider.close()

    async def test_cancel_finishes_when_consumer_exits_before_cleared(self):
        provider = DeepgramTTS(DeepgramTTSConfig(api_key="test-key", clear_timeout_s=1.0))
        fake = QueueDeepgramSocket(
            audio=_pcm16_bytes(120),
            acknowledge_clear=False,
            hold_first_flush=True,
        )
        first_audio = asyncio.Event()
        release_consumer = asyncio.Event()

        async def _consume() -> None:
            stream = provider.synthesize("cancel promptly")
            async with contextlib.aclosing(stream):
                async for _event in stream:
                    first_audio.set()
                    await release_consumer.wait()
                    break

        with patch.object(provider, "_create_ws", return_value=fake):
            synthesis_task = asyncio.create_task(_consume())
            await first_audio.wait()
            cancel_task = asyncio.create_task(provider.cancel())
            await asyncio.sleep(0)
            assert not cancel_task.done()

            release_consumer.set()
            await asyncio.wait_for(cancel_task, timeout=0.1)
            await synthesis_task

        assert fake._closed
        assert provider._ws is None
        assert not provider.is_active

    async def test_rotates_before_exceeding_flush_window_limit(self):
        provider = self._make_provider()
        first = QueueDeepgramSocket()
        second = QueueDeepgramSocket()
        factory = MagicMock(side_effect=[first, second])

        with patch.object(provider, "_create_ws", factory):
            for index in range(21):
                events = [event async for event in provider.synthesize(str(index))]
                assert events

        assert [frame["type"] for frame in first.sent].count("Flush") == 20
        assert [frame["type"] for frame in second.sent].count("Flush") == 1
        assert first._closed
        assert not second._closed
        await provider.close()

    async def test_reuses_socket_after_flush_window_expires(self):
        provider = self._make_provider()
        fake = QueueDeepgramSocket()
        now = 0.0

        with (
            patch.object(provider, "_create_ws", return_value=fake) as factory,
            patch(
                "easycat.tts.deepgram_tts.time.monotonic",
                side_effect=lambda: now,
            ),
        ):
            for index in range(20):
                assert [event async for event in provider.synthesize(str(index))]
            now = 61.0
            assert [event async for event in provider.synthesize("after window")]

        assert factory.call_count == 1
        assert [frame["type"] for frame in fake.sent].count("Flush") == 21
        assert not fake._closed
        await provider.close()


class TestDeepgramTTSConfig:
    def test_defaults(self):
        config = DeepgramTTSConfig(api_key="test-key")
        assert config.model == "aura-asteria-en"
        assert config.encoding == "linear16"
        assert config.sample_rate == 24000
        assert config.output_format == PCM16_MONO_24K
        assert config.persistent_ws is True
        assert config.clear_timeout_s == 1.0
        assert config.warmup_timeout_s == 5.0

    @pytest.mark.parametrize("name", ["clear_timeout_s", "warmup_timeout_s"])
    @pytest.mark.parametrize("value", [0, -1, float("inf"), True])
    def test_rejects_invalid_timeouts(self, name, value):
        with pytest.raises(ValueError, match=name):
            DeepgramTTSConfig(api_key="test-key", **{name: value})

    @pytest.mark.parametrize("sample_rate", [0, -1, True, 24_000.5, "24000"])
    def test_rejects_invalid_sample_rate(self, sample_rate):
        with pytest.raises(ValueError, match="sample_rate must be a positive integer"):
            DeepgramTTSConfig(api_key="test-key", sample_rate=sample_rate)

    @pytest.mark.parametrize("encoding", ["mulaw", "alaw", "mp3"])
    def test_rejects_non_linear16_encoding(self, encoding):
        with pytest.raises(
            ValueError,
            match="TTS audio normalizer only supports linear16 PCM",
        ):
            DeepgramTTSConfig(api_key="test-key", encoding=encoding)

    @pytest.mark.parametrize("encoding", ["linear16", "LINEAR16", " Linear16 "])
    def test_normalizes_linear16_encoding(self, encoding):
        config = DeepgramTTSConfig(api_key="test-key", encoding=encoding)

        assert config.encoding == "linear16"

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

    def test_build_url_uses_normalized_linear16_encoding(self):
        provider = DeepgramTTS(
            DeepgramTTSConfig(
                api_key="test-key",
                encoding=" LINEAR16 ",
                persistent_ws=False,
            )
        )

        assert "encoding=linear16" in provider._build_url()

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

    async def test_synthesize_does_not_overwrite_retained_failed_socket(self):
        class _RetainedWS:
            def __init__(self) -> None:
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1
                raise RuntimeError("retained socket close failed")

        provider = self._make_provider()
        retained = _RetainedWS()
        factory = MagicMock()
        provider._ws = retained  # type: ignore[assignment]

        with (
            patch.object(provider, "_create_ws", factory),
            pytest.raises(RuntimeError, match="retained socket close failed"),
        ):
            await anext(provider.synthesize("Hello"))

        factory.assert_not_called()
        assert provider._ws is retained
        assert retained.close_calls == 1
        assert not provider.is_active

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

    async def test_synthesize_raises_when_stream_ends_before_terminal_control(self):
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS(messages=[_pcm16_bytes(100)])
        events = []

        with (
            patch.object(provider, "_create_ws", return_value=fake_ws),
            pytest.raises(ConnectionError, match="before a terminal Flushed/Error"),
        ):
            async for event in provider.synthesize("truncated"):
                events.append(event)

        assert len(events) == 1
        assert fake_ws._closed
        assert not provider.is_active

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

    async def test_flush_rate_warning_rotates_socket_and_emits_error(self):
        bus = EventBus()
        errors: list[Error] = []
        bus.subscribe(Error, lambda e: errors.append(e))

        provider = DeepgramTTS(DeepgramTTSConfig(api_key="k", event_bus=bus))
        # Deepgram throttles the flush past its rate limit and sends a flush-rate
        # Warning instead of Flushed. Synthesis must not hang waiting for audio:
        # it surfaces a provider error and rotates (closes) the socket.
        warning_frame = json.dumps(
            {"type": "Warning", "description": "Exceeded 20 Flush messages in 60 seconds"}
        )
        fake_ws = FakeReconnectingWS(messages=[warning_frame])

        with patch.object(provider, "_create_ws", return_value=fake_ws):
            events = [event async for event in provider.synthesize("test")]

        await asyncio.sleep(0)
        assert events == []  # no audio, and no hang waiting for Flushed
        assert len(errors) == 1
        assert errors[0].stage == ErrorStage.TTS
        assert "flush rate limit" in str(errors[0].exception).lower()
        assert fake_ws._closed  # socket rotated so the next reply gets a fresh window
        assert provider._ws is None
        await provider.close()

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
