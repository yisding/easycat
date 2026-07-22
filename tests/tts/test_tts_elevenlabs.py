"""Tests for ElevenLabs TTS provider."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from easycat._provider_helpers import get_package_version
from easycat.events import Error, ErrorStage, EventBus, TTSEventType
from easycat.tts.elevenlabs_tts import (
    ElevenLabsStreamMode,
    ElevenLabsTTS,
    ElevenLabsTTSConfig,
)
from tests.tts._harness import extract_audio_chunks, verify_pcm16_audio


def _pcm16_bytes(n_samples: int = 240) -> bytes:
    return struct.pack(f"<{n_samples}h", *([300] * n_samples))


async def _drain(agen) -> None:
    """Consume a synthesize() async generator to completion."""
    async for _ in agen:
        pass


class FakeHTTPStreamResponse:
    """Mock httpx streaming response for ElevenLabs HTTP mode."""

    def __init__(self, chunks: list[bytes], status_code: int = 200):
        self._chunks = chunks
        self.status_code = status_code
        self.is_closed = False

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

    async def aiter_bytes(self, chunk_size: int = 4096):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.is_closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


class FakeReconnectingWS:
    """Mock ReconnectingWebSocket for ElevenLabs WebSocket mode."""

    def __init__(
        self,
        messages: list[str] | None = None,
        fail_send_at: set[int] | None = None,
        recv_started: asyncio.Event | None = None,
        recv_wait: asyncio.Event | None = None,
        on_reconnect=None,
        reconnect_after: int | None = None,
        **_kwargs,
    ):
        self._messages = messages or []
        self._sent: list[str | bytes] = []
        self._closed = False
        self._is_connected = False
        self._send_count = 0
        self._fail_send_at = fail_send_at or set()
        self._recv_started = recv_started
        self._recv_wait = recv_wait
        # ``on_reconnect`` mirrors the hook the provider passes to the real
        # ReconnectingWebSocket constructor. ``reconnect_after`` (when set)
        # makes ``recv_iter`` invoke that hook after yielding that many
        # messages, simulating a mid-stream recv_iter-driven reconnect.
        self._on_reconnect = on_reconnect
        self._reconnect_after = reconnect_after
        self.connect = AsyncMock(side_effect=self._mark_connected)

    async def _mark_connected(self) -> None:
        self._is_connected = True

    @property
    def is_connected(self) -> bool:
        return self._is_connected and not self._closed

    async def send(self, message: str | bytes) -> None:
        self._send_count += 1
        if self._send_count in self._fail_send_at:
            raise RuntimeError("stale websocket")
        self._sent.append(message)

    async def recv_iter(self):
        if self._recv_started is not None:
            self._recv_started.set()
        for i, msg in enumerate(self._messages):
            yield msg
            if self._reconnect_after is not None and i + 1 == self._reconnect_after:
                # Simulate the ReconnectingWebSocket re-establishing the
                # socket mid-stream and firing the provider's recovery hook.
                result = self._on_reconnect()
                if asyncio.iscoroutine(result):
                    await result
        if self._recv_wait is not None:
            await self._recv_wait.wait()

    async def close(self) -> None:
        self._closed = True
        self._is_connected = False


class TestElevenLabsTTSConfig:
    def test_defaults(self):
        config = ElevenLabsTTSConfig(api_key="test-key")
        assert config.voice_id == "EXAVITQu4vr4xnSDxMaL"
        assert config.model_id == "eleven_flash_v2_5"
        assert config.stability == 0.5
        assert config.similarity_boost == 0.75
        assert config.output_format == "pcm_24000"
        assert config.stream_mode == ElevenLabsStreamMode.WEBSOCKET
        assert config.persistent_ws is True
        assert config.auto_mode is True
        assert config.warmup_timeout_s == 5.0

    @pytest.mark.parametrize("timeout", [0, -1, float("inf"), True])
    def test_rejects_invalid_warmup_timeout(self, timeout):
        with pytest.raises(ValueError, match="warmup_timeout_s"):
            ElevenLabsTTSConfig(api_key="key", warmup_timeout_s=timeout)

    def test_websocket_mode(self):
        config = ElevenLabsTTSConfig(
            api_key="key",
            stream_mode=ElevenLabsStreamMode.WEBSOCKET,
        )
        assert config.stream_mode == ElevenLabsStreamMode.WEBSOCKET
        assert config.persistent_ws is True

    def test_http_mode_disables_persistence_by_default(self):
        config = ElevenLabsTTSConfig(
            api_key="key",
            stream_mode=ElevenLabsStreamMode.HTTP,
        )
        assert config.persistent_ws is False

    def test_websocket_one_shot_can_be_requested_explicitly(self):
        config = ElevenLabsTTSConfig(
            api_key="key",
            stream_mode=ElevenLabsStreamMode.WEBSOCKET,
            persistent_ws=False,
        )
        assert config.persistent_ws is False

    def test_custom_values(self):
        config = ElevenLabsTTSConfig(
            api_key="key",
            voice_id="custom-voice",
            model_id="eleven_multilingual_v2",
            stability=0.8,
            similarity_boost=0.9,
            output_format="pcm_16000",
        )
        assert config.voice_id == "custom-voice"
        assert config.model_id == "eleven_multilingual_v2"
        assert config.stability == 0.8
        assert config.output_format == "pcm_16000"

    def test_version_info_sdk_matches_active_transport(self):
        """sdk_version reflects the transport the active stream_mode uses."""
        ws = ElevenLabsTTS(
            ElevenLabsTTSConfig(api_key="key", stream_mode=ElevenLabsStreamMode.WEBSOCKET)
        )
        assert ws.version_info()["sdk_version"] == get_package_version("websockets")

        http = ElevenLabsTTS(
            ElevenLabsTTSConfig(api_key="key", stream_mode=ElevenLabsStreamMode.HTTP)
        )
        assert http.version_info()["sdk_version"] == get_package_version("httpx")
        assert http._config.persistent_ws is False


class TestElevenLabsTTSValidation:
    def test_non_pcm_output_format_rejected_at_config(self):
        """Non-PCM formats (mp3, opus, etc.) must be rejected at config creation."""
        with pytest.raises(ValueError, match="Unsupported ElevenLabs output_format"):
            ElevenLabsTTSConfig(api_key="key", output_format="mp3_44100")

    def test_unknown_format_rejected_at_config(self):
        with pytest.raises(ValueError, match="Only PCM formats are supported"):
            ElevenLabsTTSConfig(api_key="key", output_format="ulaw_8000")

    def test_all_pcm_formats_accepted(self):
        for fmt in ("pcm_16000", "pcm_22050", "pcm_24000", "pcm_44100"):
            provider = ElevenLabsTTS(ElevenLabsTTSConfig(api_key="key", output_format=fmt))
            assert provider._source_format.sample_rate == int(fmt.split("_")[1])

    def test_invalid_text_normalization_rejected(self):
        with pytest.raises(ValueError, match="apply_text_normalization must be"):
            ElevenLabsTTSConfig(api_key="key", apply_text_normalization="sometimes")

    def test_out_of_range_style_rejected(self):
        with pytest.raises(ValueError, match="style must be in"):
            ElevenLabsTTSConfig(api_key="key", style=1.5)


class TestElevenLabsTTSHTTP:
    def _make_provider(self, **kwargs) -> ElevenLabsTTS:
        config = ElevenLabsTTSConfig(
            api_key="test-key",
            stream_mode=ElevenLabsStreamMode.HTTP,
            **kwargs,
        )
        return ElevenLabsTTS(config)

    async def test_warmup_primes_configured_voice_endpoint(self):
        provider = self._make_provider(voice_id="voice-latency")
        client = provider._get_http_client()
        response = MagicMock()
        response.aclose = AsyncMock()

        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value=response,
        ) as get:
            await provider.warmup()

        get.assert_awaited_once_with("/voices/voice-latency")
        response.aclose.assert_awaited_once()
        await provider.close()

    async def test_http_warmup_failure_is_best_effort(self):
        provider = self._make_provider()
        client = provider._get_http_client()

        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            side_effect=RuntimeError("connect boom"),
        ):
            await provider.warmup()

        await provider.close()

    async def test_http_warmup_timeout_is_best_effort(self):
        provider = self._make_provider(warmup_timeout_s=0.01)
        client = provider._get_http_client()

        async def _hang(_url: str):
            await asyncio.Event().wait()

        with patch.object(client, "get", new_callable=AsyncMock, side_effect=_hang):
            await asyncio.wait_for(provider.warmup(), timeout=0.1)

        await provider.close()

    async def test_synthesize_http_yields_audio(self):
        provider = self._make_provider()
        pcm_data = [_pcm16_bytes(240), _pcm16_bytes(240)]
        fake_response = FakeHTTPStreamResponse(pcm_data)

        client = provider._get_http_client()
        with patch.object(client, "stream", return_value=fake_response):
            events = []
            async for event in provider.synthesize("Hello"):
                events.append(event)

        assert len(events) == 2
        for e in events:
            assert e.type == TTSEventType.AUDIO

        chunks = extract_audio_chunks(events)
        assert verify_pcm16_audio(chunks)

    async def test_synthesize_http_sends_correct_request(self):
        provider = self._make_provider(
            voice_id="test-voice",
            model_id="test-model",
            stability=0.7,
            similarity_boost=0.8,
        )
        fake_response = FakeHTTPStreamResponse([_pcm16_bytes(10)])
        client = provider._get_http_client()
        mock_stream = MagicMock(return_value=fake_response)

        with patch.object(client, "stream", mock_stream):
            async for _ in provider.synthesize("Test"):
                pass

        mock_stream.assert_called_once()
        call_args = mock_stream.call_args
        assert call_args[0][0] == "POST"
        assert "/text-to-speech/test-voice/stream" in call_args[0][1]
        body = call_args[1]["json"]
        assert body["text"] == "Test"
        assert body["model_id"] == "test-model"
        assert body["voice_settings"]["stability"] == 0.7
        assert body["voice_settings"]["similarity_boost"] == 0.8
        # Style + speaker boost defaults are sent so callers can override them.
        assert body["voice_settings"]["style"] == 0.0
        assert body["voice_settings"]["use_speaker_boost"] is True
        # Default normalization mode is sent so callers can override it.
        assert body["apply_text_normalization"] == "auto"

    async def test_synthesize_http_sends_style_and_speaker_boost_overrides(self):
        provider = self._make_provider(style=0.6, use_speaker_boost=False)
        fake_response = FakeHTTPStreamResponse([_pcm16_bytes(10)])
        client = provider._get_http_client()
        mock_stream = MagicMock(return_value=fake_response)

        with patch.object(client, "stream", mock_stream):
            async for _ in provider.synthesize("Test"):
                pass

        vs = mock_stream.call_args[1]["json"]["voice_settings"]
        assert vs["style"] == 0.6
        assert vs["use_speaker_boost"] is False

    async def test_synthesize_http_sends_text_normalization_override(self):
        provider = self._make_provider(apply_text_normalization="off")
        fake_response = FakeHTTPStreamResponse([_pcm16_bytes(10)])
        client = provider._get_http_client()
        mock_stream = MagicMock(return_value=fake_response)

        with patch.object(client, "stream", mock_stream):
            async for _ in provider.synthesize("Test"):
                pass

        body = mock_stream.call_args[1]["json"]
        assert body["apply_text_normalization"] == "off"

    async def test_synthesize_http_cancel(self):
        provider = self._make_provider()
        pcm_data = [_pcm16_bytes(100)] * 10
        fake_response = FakeHTTPStreamResponse(pcm_data)

        client = provider._get_http_client()
        with patch.object(client, "stream", return_value=fake_response):
            events = []
            async for event in provider.synthesize("long text"):
                events.append(event)
                if len(events) == 2:
                    await provider.cancel()

        assert len(events) == 2
        assert provider.is_cancelled

    async def test_synthesize_http_active_tracking(self):
        provider = self._make_provider()
        fake_response = FakeHTTPStreamResponse([_pcm16_bytes(10)])
        client = provider._get_http_client()

        with patch.object(client, "stream", return_value=fake_response):
            assert not provider.is_active
            async for _ in provider.synthesize("hi"):
                assert provider.is_active
            assert not provider.is_active

    async def test_http_error_propagated(self):
        provider = self._make_provider()
        fake_response = FakeHTTPStreamResponse([], status_code=401)
        client = provider._get_http_client()

        with patch.object(client, "stream", return_value=fake_response):
            with pytest.raises(httpx.HTTPStatusError):
                async for _ in provider.synthesize("error test"):
                    pass

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

        provider = self._make_provider(event_bus=bus)
        provider._client = httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(handle),
        )
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
        assert err.provider == "elevenlabs"
        notes = getattr(err.exception, "__notes__", [])
        assert any(f"http_status={status_code}" in note for note in notes)


class ChunkedAsyncByteStream(httpx.AsyncByteStream):
    """Exercise the provider through httpx's real response byte iterator."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


class TestElevenLabsTTSWebSocket:
    def _make_provider(self, **kwargs) -> ElevenLabsTTS:
        kwargs.setdefault("persistent_ws", False)
        config = ElevenLabsTTSConfig(
            api_key="test-key",
            stream_mode=ElevenLabsStreamMode.WEBSOCKET,
            **kwargs,
        )
        return ElevenLabsTTS(config)

    def _audio_message(self, n_samples: int = 240) -> str:
        """Create a JSON message with base64-encoded audio."""
        audio_data = _pcm16_bytes(n_samples)
        return json.dumps({"audio": base64.b64encode(audio_data).decode()})

    def _final_message(self) -> str:
        return json.dumps({"isFinal": True})

    def _alignment_message(self) -> str:
        return json.dumps(
            {
                "alignment": {"chars": ["H", "i"], "charStartTimesMs": [0, 100]},
            }
        )

    async def test_synthesize_ws_yields_audio(self):
        provider = self._make_provider()
        messages = [
            self._audio_message(240),
            self._audio_message(240),
            self._final_message(),
        ]
        fake_ws = FakeReconnectingWS(messages=messages)

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            return_value=fake_ws,
        ):
            events = []
            async for event in provider.synthesize("Hello"):
                events.append(event)

        audio_events = [e for e in events if e.type == TTSEventType.AUDIO]
        assert len(audio_events) == 2
        chunks = extract_audio_chunks(audio_events)
        assert verify_pcm16_audio(chunks)

    async def test_synthesize_ws_url_includes_text_normalization(self):
        provider = self._make_provider(apply_text_normalization="on")
        fake_ws = FakeReconnectingWS(messages=[self._final_message()])
        mock_ctor = MagicMock(return_value=fake_ws)

        with patch("easycat.tts.elevenlabs_tts.ReconnectingWebSocket", mock_ctor):
            async for _ in provider.synthesize("Hello"):
                pass

        url = mock_ctor.call_args.kwargs["url"]
        assert "apply_text_normalization=on" in url
        assert "auto_mode=true" in url

    async def test_synthesize_ws_sends_init_text_and_eos(self):
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS(messages=[self._final_message()])

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            return_value=fake_ws,
        ):
            async for _ in provider.synthesize("Test"):
                pass

        assert len(fake_ws._sent) == 3  # init + text + EOS

        init_msg = json.loads(fake_ws._sent[0])
        assert init_msg["text"] == " "
        assert "voice_settings" in init_msg
        # Style + speaker boost travel in the WS init voice_settings too.
        assert init_msg["voice_settings"]["style"] == 0.0
        assert init_msg["voice_settings"]["use_speaker_boost"] is True

        text_msg = json.loads(fake_ws._sent[1])
        assert text_msg["text"] == "Test"

        eos_msg = json.loads(fake_ws._sent[2])
        assert eos_msg["text"] == ""

    async def test_synthesize_ws_handles_alignment(self):
        provider = self._make_provider()
        messages = [
            self._audio_message(100),
            self._alignment_message(),
            self._final_message(),
        ]
        fake_ws = FakeReconnectingWS(messages=messages)

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            return_value=fake_ws,
        ):
            events = []
            async for event in provider.synthesize("Hi"):
                events.append(event)

        audio_events = [e for e in events if e.type == TTSEventType.AUDIO]
        marker_events = [e for e in events if e.type == TTSEventType.MARKERS]
        assert len(audio_events) == 1
        assert len(marker_events) == 1

    async def test_synthesize_ws_cancel(self):
        provider = self._make_provider()
        messages = [self._audio_message(100)] * 10
        fake_ws = FakeReconnectingWS(messages=messages)

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            return_value=fake_ws,
        ):
            events = []
            async for event in provider.synthesize("long text"):
                events.append(event)
                if len(events) == 2:
                    await provider.cancel()

        assert len(events) == 2
        assert provider.is_cancelled

    async def test_ws_recreated_for_each_synthesis_call(self):
        provider = self._make_provider()
        fake_ws_one = FakeReconnectingWS(messages=[self._final_message()])
        fake_ws_two = FakeReconnectingWS(messages=[self._final_message()])

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            side_effect=[fake_ws_one, fake_ws_two],
        ) as mock_ws_cls:
            async for _ in provider.synthesize("test one"):
                pass
            async for _ in provider.synthesize("test two"):
                pass

        assert mock_ws_cls.call_count == 2
        fake_ws_one.connect.assert_awaited_once()
        fake_ws_two.connect.assert_awaited_once()

    async def test_ws_closed_after_synthesis(self):
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS(messages=[self._final_message()])

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            return_value=fake_ws,
        ):
            async for _ in provider.synthesize("test"):
                pass

        assert fake_ws._closed

    async def test_synthesize_ws_reconnects_and_replays_messages_after_send_failure(self):
        provider = self._make_provider()
        stale_ws = FakeReconnectingWS(fail_send_at={2})
        fresh_ws = FakeReconnectingWS(messages=[self._final_message()])

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            side_effect=[stale_ws, fresh_ws],
        ) as mock_ws_cls:
            async for _ in provider.synthesize("Test"):
                pass

        assert stale_ws._closed
        assert mock_ws_cls.call_count == 2
        stale_ws.connect.assert_awaited_once()
        fresh_ws.connect.assert_awaited_once()
        assert [json.loads(msg)["text"] for msg in stale_ws._sent] == [" "]
        assert [json.loads(msg)["text"] for msg in fresh_ws._sent] == [" ", "Test", ""]

    async def test_replay_request_resends_armed_messages_mid_stream(self):
        """A mid-stream recv_iter-driven reconnect replays the armed request.

        Drives the on_reconnect hook after one audio frame and asserts the
        full init/text/EOS sequence is re-sent on the same (fake) socket,
        restarting the utterance from the top.
        """
        provider = self._make_provider()
        # Fire the hook after the first audio frame, then finish.
        fake_ws = FakeReconnectingWS(
            messages=[self._audio_message(120), self._final_message()],
            on_reconnect=provider._replay_request,
            reconnect_after=1,
        )

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            return_value=fake_ws,
        ):
            async for _ in provider.synthesize("Test"):
                pass

        sent_texts = [json.loads(msg)["text"] for msg in fake_ws._sent]
        # Initial send (init/text/EOS) followed by the full replayed sequence.
        assert sent_texts == [" ", "Test", "", " ", "Test", ""]

    async def test_replay_request_noop_when_unarmed(self):
        """Replay is a no-op when armed state is None (initial-connect retry).

        Mirrors the race the arming gate prevents: ``_connect_with_retry``
        fires on_reconnect for retries during the *initial* connect, before
        the request has been sent, so ``_pending_messages`` is still None and
        nothing should be re-sent.
        """
        provider = self._make_provider()
        # Use a real (unconnected) fake socket so send() would record frames.
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws
        provider._pending_messages = None

        await provider._replay_request()

        assert fake_ws._sent == []

    async def test_replay_request_noop_when_cancelled(self):
        """Replay is a no-op once the provider is cancelled."""
        provider = self._make_provider()
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws
        provider._pending_messages = provider._build_ws_messages("Test")
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
        provider._pending_messages = provider._build_ws_messages("Test")
        # Simulate a split 16-bit sample held across the dropped frame.
        provider._sample_carry = b"\x01"

        await provider._replay_request()

        assert provider._sample_carry == b""
        assert len(fake_ws._sent) == 3

    async def test_synthesize_ws_task_cancellation_closes_socket(self):
        provider = self._make_provider()
        recv_started = asyncio.Event()
        recv_wait = asyncio.Event()
        fake_ws = FakeReconnectingWS(
            recv_started=recv_started,
            recv_wait=recv_wait,
        )

        with patch(
            "easycat.tts.elevenlabs_tts.ReconnectingWebSocket",
            return_value=fake_ws,
        ):

            async def consume() -> None:
                async for _ in provider.synthesize("Test"):
                    pass

            task = asyncio.create_task(consume())
            await recv_started.wait()
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

        assert fake_ws._closed


class FakePersistentWS:
    """Fake multi-context socket for the persistent ElevenLabs path."""

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
        msg = json.loads(message)
        # The multi-context endpoint uses snake_case ``is_final``.
        if msg.get("text") == "" and "context_id" in msg:
            ctx_id = msg["context_id"]
            audio = base64.b64encode(_pcm16_bytes(120)).decode()
            await self._queue.put(json.dumps({"audio": audio, "contextId": ctx_id}))
            await self._queue.put(json.dumps({"is_final": True, "contextId": ctx_id}))

    async def recv_iter(self):
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(None)


class TestElevenLabsPersistent:
    def _make_provider(self, **kwargs) -> ElevenLabsTTS:
        config = ElevenLabsTTSConfig(
            api_key="test-key",
            stream_mode=ElevenLabsStreamMode.WEBSOCKET,
            **kwargs,
        )
        return ElevenLabsTTS(config)

    def test_persistent_with_http_raises(self):
        with pytest.raises(ValueError, match="requires stream_mode=WEBSOCKET"):
            ElevenLabsTTSConfig(
                api_key="k",
                stream_mode=ElevenLabsStreamMode.HTTP,
                persistent_ws=True,
            )

    def test_multi_stream_url_includes_inactivity_timeout(self):
        provider = self._make_provider(inactivity_timeout=25)
        url = provider._multi_stream_url()
        assert "/multi-stream-input?" in url
        assert "inactivity_timeout=25" in url
        assert "auto_mode=true" in url

    def test_persistent_rejects_out_of_range_inactivity_timeout(self):
        with pytest.raises(ValueError, match=r"inactivity_timeout must be in \[1, 180\]"):
            ElevenLabsTTSConfig(api_key="k", persistent_ws=True, inactivity_timeout=600)

    async def test_persistent_connect_failure_ends_synthesis(self):
        # A failed initial /multi-stream-input connect must emit the error and
        # clear is_active (run _end_synthesis), not leave the provider stuck.
        provider = self._make_provider()

        class FailingConnectWS(FakePersistentWS):
            async def connect(self) -> None:
                raise RuntimeError("connect boom")

        with patch.object(provider, "_build_multi_ws", return_value=FailingConnectWS()):
            with pytest.raises(RuntimeError, match="connect boom"):
                async for _ in provider.synthesize("hi"):
                    pass

        assert not provider.is_active
        await provider.close()

    async def test_warmup_connects_once_and_first_synthesis_reuses_socket(self):
        provider = self._make_provider()
        fake = FakePersistentWS()
        factory = MagicMock(return_value=fake)

        with patch.object(provider, "_build_multi_ws", factory):
            await provider.warmup()
            assert factory.call_count == 1
            assert provider._mgr is not None and provider._mgr._contexts == {}
            async for _ in provider.synthesize("first"):
                pass

        assert factory.call_count == 1
        await provider.close()

    async def test_persistent_terminates_on_snake_case_is_final(self):
        # The /multi-stream-input endpoint marks completion with ``is_final``
        # (snake_case), unlike the one-shot ``isFinal``. The shared decoder must
        # accept it so the persistent turn completes instead of hanging until
        # the socket closes.
        provider = self._make_provider()

        class SnakeCaseFinalWS(FakePersistentWS):
            async def send(self, message: str) -> None:
                self.sent.append(message)
                msg = json.loads(message)
                if msg.get("text") == "" and "context_id" in msg:
                    ctx_id = msg["context_id"]
                    audio = base64.b64encode(_pcm16_bytes(120)).decode()
                    await self._queue.put(json.dumps({"audio": audio, "contextId": ctx_id}))
                    await self._queue.put(json.dumps({"is_final": True, "contextId": ctx_id}))

        events = []

        async def _collect(agen):
            async for event in agen:
                events.append(event)

        with patch.object(provider, "_build_multi_ws", return_value=SnakeCaseFinalWS()):
            await asyncio.wait_for(_collect(provider.synthesize("hi")), timeout=2.0)

        assert events  # audio decoded and the turn completed without hanging
        await provider.close()

    async def test_warmup_failure_is_retried_by_synthesis(self):
        provider = self._make_provider()

        class FailingConnectWS(FakePersistentWS):
            async def connect(self) -> None:
                raise RuntimeError("connect boom")

        failed = FailingConnectWS()
        working = FakePersistentWS()
        factory = MagicMock(side_effect=[failed, working])
        with patch.object(provider, "_build_multi_ws", factory):
            await provider.warmup()
            async for _ in provider.synthesize("retry"):
                pass

        assert factory.call_count == 2
        assert failed.closed
        await provider.close()

    async def test_unlimited_retry_warmup_times_out_before_synthesis_retry(self):
        provider = self._make_provider(reconnect_max_retries=-1, warmup_timeout_s=0.01)

        class HangingConnectWS(FakePersistentWS):
            async def connect(self) -> None:
                await asyncio.Event().wait()

        hanging = HangingConnectWS()
        working = FakePersistentWS()
        factory = MagicMock(side_effect=[hanging, working])

        with patch.object(provider, "_build_multi_ws", factory):
            await asyncio.wait_for(provider.warmup(), timeout=0.1)
            await _drain(provider.synthesize("retry after warmup timeout"))

        assert factory.call_count == 2
        assert hanging.closed
        await provider.close()

    async def test_persistent_context_error_frame_surfaced(self):
        # A context-scoped error frame (carrying contextId, no isFinal) must be
        # surfaced as a provider error and end the turn — not hang awaiting more
        # frames or end as a clean truncation.
        from easycat.events import Error, EventBus

        bus = EventBus()
        errors: list[Error] = []
        bus.subscribe(Error, lambda e: errors.append(e))
        provider = self._make_provider(event_bus=bus)

        class ErrorFrameWS(FakePersistentWS):
            async def send(self, message: str) -> None:
                self.sent.append(message)
                msg = json.loads(message)
                if msg.get("text") == "" and "context_id" in msg:
                    await self._queue.put(
                        json.dumps({"contextId": msg["context_id"], "error": "boom"})
                    )

        with patch.object(provider, "_build_multi_ws", return_value=ErrorFrameWS()):
            await asyncio.wait_for(_drain(provider.synthesize("hi")), timeout=2.0)
        await asyncio.sleep(0)  # let the fire-and-forget Error task run
        assert any("boom" in str(e.exception) for e in errors)
        await provider.close()

    async def test_persistent_midstream_socket_death_surfaces_error(self):
        # The socket dies mid-utterance (recv_iter ends before isFinal); the
        # persistent path must surface a provider error + raise, like one-shot.
        from easycat.events import Error, EventBus

        bus = EventBus()
        errors: list[Error] = []
        bus.subscribe(Error, lambda e: errors.append(e))
        provider = self._make_provider(event_bus=bus)

        class DyingWS(FakePersistentWS):
            async def send(self, message: str) -> None:
                self.sent.append(message)
                msg = json.loads(message)
                if msg.get("text") == "" and "context_id" in msg:
                    # One audio frame, then the socket dies (no isFinal).
                    cid = msg["context_id"]
                    audio = base64.b64encode(_pcm16_bytes(80)).decode()
                    await self._queue.put(json.dumps({"audio": audio, "contextId": cid}))
                    await self._queue.put(None)  # end recv_iter mid-utterance

        with patch.object(provider, "_build_multi_ws", return_value=DyingWS()):
            with pytest.raises(Exception):
                await asyncio.wait_for(_drain(provider.synthesize("hi")), timeout=2.0)
        await asyncio.sleep(0)
        assert errors  # a provider Error was emitted
        await provider.close()

    async def test_per_context_init_text_eos(self):
        provider = self._make_provider()
        fake = FakePersistentWS()

        with patch.object(provider, "_build_multi_ws", return_value=fake):
            async for _ in provider.synthesize("Hello"):
                pass

        frames = [json.loads(s) for s in fake.sent]
        # init(space) + text + EOS, all carrying the same context_id.
        init, text, eos = frames[0], frames[1], frames[2]
        assert init["text"] == " "
        assert "voice_settings" in init
        assert text["text"] == "Hello"
        assert eos["text"] == ""
        ctx_id = init["context_id"]
        assert text["context_id"] == ctx_id
        assert eos["context_id"] == ctx_id
        await provider.close()

    async def test_one_socket_across_two_calls(self):
        provider = self._make_provider()
        fake = FakePersistentWS()
        factory = MagicMock(return_value=fake)

        with patch.object(provider, "_build_multi_ws", factory):
            async for _ in provider.synthesize("first"):
                pass
            async for _ in provider.synthesize("second"):
                pass

        assert factory.call_count == 1
        ctx_ids = {
            json.loads(s)["context_id"]
            for s in fake.sent
            if json.loads(s).get("text") not in (None,) and "context_id" in json.loads(s)
        }
        # Two distinct contexts across the two utterances.
        assert len(ctx_ids) == 2
        close_frames = [
            json.loads(s) for s in fake.sent if json.loads(s).get("close_context") is True
        ]
        assert len(close_frames) == 2
        await provider.close()

    async def test_synthesize_yields_audio(self):
        provider = self._make_provider()
        fake = FakePersistentWS()

        with patch.object(provider, "_build_multi_ws", return_value=fake):
            audio = [e async for e in provider.synthesize("hi") if e.type == TTSEventType.AUDIO]
        assert len(audio) == 1
        assert verify_pcm16_audio(extract_audio_chunks(audio))
        await provider.close()

    async def test_cancel_sends_close_context_without_socket_close(self):
        provider = self._make_provider()
        fake = FakePersistentWS()

        with patch.object(provider, "_build_multi_ws", return_value=fake):
            async for event in provider.synthesize("long"):
                if event.type == TTSEventType.AUDIO:
                    await provider.cancel()

            assert not fake.closed
            close_ctx = [
                json.loads(s) for s in fake.sent if json.loads(s).get("close_context") is True
            ]
            assert close_ctx and close_ctx[0]["close_context"] is True
        await provider.close()

    async def test_close_sends_close_socket(self):
        provider = self._make_provider()
        fake = FakePersistentWS()

        with patch.object(provider, "_build_multi_ws", return_value=fake):
            async for _ in provider.synthesize("hi"):
                pass
            await provider.close()

        assert fake.closed
        assert any(json.loads(s).get("close_socket") for s in fake.sent)

    def test_version_info_still_websockets(self):
        provider = self._make_provider()
        assert provider.version_info()["sdk_version"] == get_package_version("websockets")


class TestElevenLabsTTSGeneral:
    async def test_close_cleans_up(self):
        config = ElevenLabsTTSConfig(api_key="test-key")
        provider = ElevenLabsTTS(config)
        # Force creation of HTTP client
        client = provider._get_http_client()
        with patch.object(client, "aclose", new_callable=AsyncMock) as mock_close:
            await provider.close()
            mock_close.assert_called_once()

    async def test_stop(self):
        config = ElevenLabsTTSConfig(api_key="test-key")
        provider = ElevenLabsTTS(config)
        provider._active = True
        await provider.stop()
        assert not provider.is_active

    async def test_stop_closes_websocket(self):
        """A graceful stop closes an explicitly one-shot synthesis WS.

        Persistent mode keeps its manager-owned socket warm instead.
        """
        config = ElevenLabsTTSConfig(
            api_key="test-key",
            stream_mode=ElevenLabsStreamMode.WEBSOCKET,
            persistent_ws=False,
        )
        provider = ElevenLabsTTS(config)
        provider._active = True
        fake_ws = FakeReconnectingWS()
        provider._ws = fake_ws

        await provider.stop()

        assert not provider.is_active
        assert fake_ws._closed
        assert provider._ws is None

    @pytest.mark.integration_live
    @pytest.mark.provider_elevenlabs
    @pytest.mark.surface_tts
    async def test_live_elevenlabs_tts(self):
        """Integration test requiring ELEVENLABS_API_KEY env var."""
        import os

        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            pytest.skip("ELEVENLABS_API_KEY not set")

        provider = ElevenLabsTTS(ElevenLabsTTSConfig(api_key=api_key))
        try:
            events = []
            async for event in provider.synthesize("Hello, this is a test."):
                events.append(event)

            assert len(events) > 0
            chunks = extract_audio_chunks(events)
            assert verify_pcm16_audio(chunks)
        finally:
            await provider.close()
