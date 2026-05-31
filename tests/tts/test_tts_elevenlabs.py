"""Tests for ElevenLabs TTS provider."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from easycat._provider_helpers import get_package_version
from easycat.events import TTSEventType
from easycat.tts.elevenlabs_tts import (
    ElevenLabsStreamMode,
    ElevenLabsTTS,
    ElevenLabsTTSConfig,
)
from tests.tts._harness import extract_audio_chunks, verify_pcm16_audio


def _pcm16_bytes(n_samples: int = 240) -> bytes:
    return struct.pack(f"<{n_samples}h", *([300] * n_samples))


class FakeHTTPStreamResponse:
    """Mock httpx streaming response for ElevenLabs HTTP mode."""

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

    def test_websocket_mode(self):
        config = ElevenLabsTTSConfig(
            api_key="key",
            stream_mode=ElevenLabsStreamMode.WEBSOCKET,
        )
        assert config.stream_mode == ElevenLabsStreamMode.WEBSOCKET

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


class TestElevenLabsTTSHTTP:
    def _make_provider(self, **kwargs) -> ElevenLabsTTS:
        config = ElevenLabsTTSConfig(
            api_key="test-key",
            stream_mode=ElevenLabsStreamMode.HTTP,
            **kwargs,
        )
        return ElevenLabsTTS(config)

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


class TestElevenLabsTTSWebSocket:
    def _make_provider(self, **kwargs) -> ElevenLabsTTS:
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
        chunks = extract_audio_chunks(events)
        assert verify_pcm16_audio(chunks)

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
        """A graceful stop closes the synthesis WS (default mode).

        Matches Cartesia/Deepgram ``stop()`` so a graceful stop between turns
        does not leave the socket lingering until the next cancel()/close().
        """
        config = ElevenLabsTTSConfig(
            api_key="test-key",
            stream_mode=ElevenLabsStreamMode.WEBSOCKET,
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
