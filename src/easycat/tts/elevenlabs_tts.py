"""ElevenLabs TTS provider implementation."""

from __future__ import annotations

import asyncio
import enum
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.events import TTSEvent
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.tts.base import TTSBase
from easycat.tts.input import TTSInput, coerce_tts_input, strip_ssml_tags

logger = logging.getLogger(__name__)


def _get_package_version(pkg: str) -> str:
    try:
        from importlib.metadata import version

        return version(pkg)
    except Exception:
        return "unknown"


class ElevenLabsStreamMode(enum.StrEnum):
    """Streaming mode for ElevenLabs TTS."""

    HTTP = "http"
    WEBSOCKET = "websocket"


# Map ElevenLabs output_format strings to AudioFormat.
# Only raw PCM formats are supported; compressed formats (mp3, opus, ulaw)
# would require a decoder and must not be silently treated as PCM.
_ELEVENLABS_FORMAT_MAP: dict[str, AudioFormat] = {
    "pcm_16000": AudioFormat(sample_rate=16000, channels=1, sample_width=2),
    "pcm_22050": AudioFormat(sample_rate=22050, channels=1, sample_width=2),
    "pcm_24000": AudioFormat(sample_rate=24000, channels=1, sample_width=2),
    "pcm_44100": AudioFormat(sample_rate=44100, channels=1, sample_width=2),
}


@dataclass
class ElevenLabsTTSConfig:
    """Configuration for the ElevenLabs TTS provider."""

    api_key: str = ""
    voice_id: str = "EXAVITQu4vr4xnSDxMaL"  # Sarah (default)
    # ``eleven_monolingual_v1`` / ``eleven_multilingual_v1`` are deprecated
    # and rejected on newer accounts (free tier refuses with 1008 policy
    # violation).  ``eleven_flash_v2_5`` is the low-latency option
    # ElevenLabs recommends for voice bots today — keeps first-byte
    # latency near 75ms.
    model_id: str = "eleven_flash_v2_5"
    stability: float = 0.5
    similarity_boost: float = 0.75
    output_format: str = "pcm_24000"
    stream_mode: ElevenLabsStreamMode = ElevenLabsStreamMode.WEBSOCKET
    base_url: str = "https://api.elevenlabs.io/v1"
    ws_base_url: str = "wss://api.elevenlabs.io/v1"
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_24K)
    event_bus: object | None = None

    def __post_init__(self) -> None:
        if self.output_format not in _ELEVENLABS_FORMAT_MAP:
            supported = ", ".join(sorted(_ELEVENLABS_FORMAT_MAP))
            raise ValueError(
                f"Unsupported ElevenLabs output_format: {self.output_format!r}. "
                f"Only PCM formats are supported: {supported}. "
                f"Non-PCM formats (mp3, opus, etc.) would require a decoder."
            )


class ElevenLabsTTS(TTSBase):
    """TTS provider using ElevenLabs API.

    Supports two streaming modes:
    - HTTP: Chunked transfer encoding via the streaming endpoint
    - WebSocket: Real-time streaming via WebSocket using ReconnectingWebSocket

    Requests PCM output format directly from ElevenLabs to avoid MP3 decoding.
    """

    def __init__(self, config: ElevenLabsTTSConfig) -> None:
        super().__init__(output_format=config.audio_format)
        self._config = config
        self._source_format = _ELEVENLABS_FORMAT_MAP[config.output_format]
        self._client: httpx.AsyncClient | None = None
        self._response: httpx.Response | None = None
        self._ws: ReconnectingWebSocket | None = None

    def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                headers={
                    "xi-api-key": self._config.api_key,
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    @property
    def supports_ssml(self) -> bool:
        return False

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        """Synthesize text using the configured streaming mode."""
        payload = coerce_tts_input(payload)
        text = payload.text if payload.format == "plain" else strip_ssml_tags(payload.text)
        if self._config.stream_mode == ElevenLabsStreamMode.WEBSOCKET:
            async for event in self._synthesize_ws(text):
                yield event
        else:
            async for event in self._synthesize_http(text):
                yield event

    async def _synthesize_http(self, text: str) -> AsyncIterator[TTSEvent]:
        """Synthesize via HTTP chunked transfer encoding."""
        self._start_synthesis()
        client = self._get_http_client()

        try:
            request_body = {
                "text": text,
                "model_id": self._config.model_id,
                "voice_settings": {
                    "stability": self._config.stability,
                    "similarity_boost": self._config.similarity_boost,
                },
            }

            url = f"/text-to-speech/{self._config.voice_id}/stream"
            params = {"output_format": self._config.output_format}

            async with client.stream(
                "POST",
                url,
                json=request_body,
                params=params,
            ) as response:
                self._response = response
                response.raise_for_status()

                async for chunk in response.aiter_bytes(chunk_size=4800):
                    if self._cancelled:
                        break
                    if chunk:
                        yield self._make_audio_event(chunk, self._source_format)

        except httpx.HTTPStatusError as exc:
            logger.error(
                "ElevenLabs TTS API error: %s %s",
                exc.response.status_code,
                exc.response.text,
            )
            self._emit_provider_error(
                exc, http_status=exc.response.status_code, body=exc.response.text[:400]
            )
            raise
        except httpx.HTTPError as exc:
            if not self._cancelled:
                logger.error("ElevenLabs TTS HTTP error: %s", exc)
                self._emit_provider_error(exc)
                raise
        finally:
            self._response = None
            self._end_synthesis()

    async def _synthesize_ws(self, text: str) -> AsyncIterator[TTSEvent]:
        """Synthesize via WebSocket streaming."""
        self._start_synthesis()

        try:
            ws = await self._start_ws_stream(text)

            # Receive audio chunks
            async for message in ws.recv_iter():
                if self._cancelled:
                    break

                if isinstance(message, str):
                    try:
                        data = json.loads(message)
                        if data.get("audio"):
                            import base64

                            audio_bytes = base64.b64decode(data["audio"])
                            if audio_bytes:
                                yield self._make_audio_event(audio_bytes, self._source_format)
                        if data.get("alignment"):
                            yield self._make_markers_event([data["alignment"]])
                        if data.get("isFinal"):
                            break
                    except (json.JSONDecodeError, KeyError):
                        pass

        except Exception as exc:
            await self._close_ws()
            if not self._cancelled:
                logger.error("ElevenLabs TTS WebSocket error: %s", exc)
                # WebSocket close codes (e.g. 1008 "policy violation" for
                # deprecated models on free tier) go in so replaying a
                # bundle shows the server's rejection reason, not just
                # "synthesis failed".
                close_code = getattr(exc, "code", None)
                self._emit_provider_error(exc, ws_close_code=close_code)
                raise
        except BaseException:
            # CancelledError is a BaseException; close the ws since it's mid-stream
            await self._close_ws()
            raise
        finally:
            await self._close_ws()
            self._end_synthesis()

    async def _start_ws_stream(self, text: str) -> ReconnectingWebSocket:
        """Send the full ElevenLabs stream-init sequence, retrying once on stale sockets."""
        messages = self._build_ws_messages(text)
        ws = await self._connect_ws()

        try:
            await self._send_ws_messages(ws, messages)
            return ws
        except Exception:
            if self._cancelled:
                raise
            await self._close_ws()
            ws = await self._connect_ws()
            await self._send_ws_messages(ws, messages)
            return ws

    def _build_ws_messages(self, text: str) -> tuple[str, str, str]:
        """Build the init, text, and EOS messages for a synthesis request."""
        init_msg = {
            "text": " ",
            "voice_settings": {
                "stability": self._config.stability,
                "similarity_boost": self._config.similarity_boost,
            },
        }
        return (
            json.dumps(init_msg),
            json.dumps({"text": text}),
            json.dumps({"text": ""}),
        )

    async def _send_ws_messages(
        self,
        ws: ReconnectingWebSocket,
        messages: tuple[str, ...],
    ) -> None:
        """Send a complete synthesis request over the active WebSocket."""
        for message in messages:
            await ws.send(message)

    async def _connect_ws(self) -> ReconnectingWebSocket:
        """Create and connect a fresh WebSocket for one synthesis request."""
        ws_url = (
            f"{self._config.ws_base_url}"
            f"/text-to-speech/{self._config.voice_id}"
            f"/stream-input?model_id={self._config.model_id}"
            f"&output_format={self._config.output_format}"
        )

        self._ws = ReconnectingWebSocket(
            url=ws_url,
            config=ReconnectConfig(
                extra_headers={"xi-api-key": self._config.api_key},
            ),
            event_bus=self._config.event_bus,
            provider_name="elevenlabs_tts",
        )
        await self._ws.connect()
        return self._ws

    async def _close_ws(self) -> None:
        """Close the WebSocket connection."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Error closing ElevenLabs WebSocket", exc_info=True)
            finally:
                self._ws = None

    async def stop(self) -> None:
        """Gracefully stop synthesis."""
        await super().stop()
        if self._response is not None:
            await self._response.aclose()

    async def cancel(self) -> None:
        """Immediately cancel synthesis and close connections."""
        await super().cancel()
        resp = self._response
        self._response = None
        if resp is not None:
            await resp.aclose()
        await self._close_ws()

    async def close(self) -> None:
        """Close all underlying connections."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._close_ws()

    def _emit_provider_error(self, exc: BaseException, **context: Any) -> None:
        """Fire a journal-visible ``Error`` event on the bus.

        Without this, WS close codes, HTTP status, and response bodies
        only land in the logger — a bundle recorded by a user can't
        explain *why* TTS failed.  Notes are attached to the exception
        so the existing ``Error`` event shape carries the context
        without requiring a new event type.
        """
        bus = getattr(self._config, "event_bus", None)
        if bus is None:
            return
        from easycat.events import Error, ErrorStage

        for key, value in context.items():
            if value is None:
                continue
            try:
                exc.add_note(f"{key}={value}")  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - pre-3.11
                pass
        try:
            asyncio.create_task(
                bus.emit(Error(exception=exc, stage=ErrorStage.TTS, provider="elevenlabs"))
            )
        except RuntimeError:  # no running loop
            logger.debug("Could not emit provider error — no running loop", exc_info=True)

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "elevenlabs",
            "model": self._config.model_id,
            "api_version": "v1",
            "sdk_version": _get_package_version("httpx"),
        }
