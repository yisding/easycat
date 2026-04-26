"""Cartesia TTS (Sonic) WebSocket provider."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from easycat._provider_helpers import get_package_version
from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.events import TTSEvent
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.tts.base import TTSBase
from easycat.tts.input import TTSInput, coerce_tts_input, strip_ssml_tags

logger = logging.getLogger(__name__)


# Byte-width per sample for each encoding Cartesia returns on the wire.
# Only PCM16 is decoded into the internal audio contract in v1; float32
# / μ-law support belongs to the telephony-native output plan.
_ENCODING_SAMPLE_WIDTH: dict[str, int] = {
    "pcm_s16le": 2,
}


@dataclass
class CartesiaTTSConfig:
    """Configuration for the Cartesia TTS (Sonic) WebSocket provider."""

    api_key: str = ""
    # Sonic-3 is the default — best quality/latency balance. Use
    # ``sonic-turbo`` (~40ms TTFA) for latency-critical templates, or
    # ``sonic-2`` for the prior-gen quality profile.
    model_id: str = "sonic-3"
    # The public voice id used throughout Cartesia's own docs examples.
    # Override for production — Cartesia does not expose stable symbolic
    # voice names.
    voice_id: str = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b"
    language: str = "en"
    encoding: str = "pcm_s16le"
    sample_rate: int = 24000
    cartesia_version: str = "2026-03-01"
    base_url: str = "wss://api.cartesia.ai/tts/websocket"
    add_timestamps: bool = True
    max_buffer_delay_ms: int | None = None
    output_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_24K)
    event_bus: object | None = None

    def __post_init__(self) -> None:
        if self.encoding not in _ENCODING_SAMPLE_WIDTH:
            supported = ", ".join(sorted(_ENCODING_SAMPLE_WIDTH))
            raise ValueError(
                f"Unsupported Cartesia encoding: {self.encoding!r}. "
                f"Only PCM encodings are supported in v1: {supported}. "
                "μ-law / float32 support is tracked separately in "
                "peripheral-telephony-tts-output.md."
            )


class CartesiaTTS(TTSBase):
    """TTS provider using Cartesia's Sonic WebSocket API.

    One WebSocket connection is opened per :meth:`synthesize` call. The
    synthesis request is sent as a single JSON frame and audio chunks
    arrive as base64-encoded ``chunk`` messages on the same socket. A
    ``done`` message (or ``done: true`` on the final chunk) terminates
    the loop.
    """

    def __init__(self, config: CartesiaTTSConfig) -> None:
        super().__init__(output_format=config.output_format)
        self._config = config
        self._source_format = AudioFormat(
            sample_rate=config.sample_rate,
            channels=1,
            sample_width=_ENCODING_SAMPLE_WIDTH[config.encoding],
        )
        self._ws: ReconnectingWebSocket | None = None
        self._context_id: str | None = None

    def _create_ws(self) -> ReconnectingWebSocket:
        return ReconnectingWebSocket(
            url=self._config.base_url,
            config=ReconnectConfig(
                extra_headers={
                    "X-API-Key": self._config.api_key,
                    "Cartesia-Version": self._config.cartesia_version,
                },
            ),
            event_bus=self._config.event_bus,
            provider_name="cartesia_tts",
        )

    def _build_request(self, text: str, context_id: str) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model_id": self._config.model_id,
            "transcript": text,
            "context_id": context_id,
            "voice": {"mode": "id", "id": self._config.voice_id},
            "language": self._config.language,
            "output_format": {
                "container": "raw",
                "encoding": self._config.encoding,
                "sample_rate": self._config.sample_rate,
            },
            "continue": False,
            "add_timestamps": self._config.add_timestamps,
        }
        if self._config.max_buffer_delay_ms is not None:
            request["max_buffer_delay_ms"] = self._config.max_buffer_delay_ms
        return request

    @property
    def supports_ssml(self) -> bool:
        return False

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        self._start_synthesis()
        payload = coerce_tts_input(payload)
        text = payload.text if payload.format == "plain" else strip_ssml_tags(payload.text)

        self._ws = self._create_ws()
        context_id = str(uuid4())
        self._context_id = context_id

        try:
            await self._ws.connect()
            await self._ws.send(json.dumps(self._build_request(text, context_id)))

            async for message in self._ws.recv_iter():
                if self._cancelled:
                    break
                if not isinstance(message, str):
                    continue
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")
                if msg_type == "chunk":
                    data_b64 = msg.get("data")
                    if data_b64:
                        audio_bytes = base64.b64decode(data_b64)
                        if audio_bytes:
                            yield self._make_audio_event(audio_bytes, self._source_format)
                    if msg.get("done"):
                        break
                elif msg_type == "timestamps":
                    word_ts = msg.get("word_timestamps")
                    if word_ts:
                        yield self._make_markers_event([word_ts])
                elif msg_type == "done":
                    break
                elif msg_type == "error":
                    self._emit_provider_error_from_msg(msg)
                    break

        except Exception as exc:
            if not self._cancelled:
                logger.error("Cartesia TTS error: %s", exc)
                self._emit_provider_error(exc)
                raise
        finally:
            await self._close_ws()
            self._context_id = None
            self._end_synthesis()

    async def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Error closing Cartesia WebSocket", exc_info=True)
            finally:
                self._ws = None

    async def stop(self) -> None:
        await super().stop()
        await self._close_ws()

    async def cancel(self) -> None:
        # Mark cancelled BEFORE sending the cancel frame so the receive
        # loop treats any in-flight chunk as discarded.
        was_active = self._active
        await super().cancel()
        ws = self._ws
        ctx_id = self._context_id
        if was_active and ws is not None and ctx_id is not None:
            try:
                await ws.send(json.dumps({"context_id": ctx_id, "cancel": True}))
            except Exception:
                logger.debug("Error sending Cartesia cancel", exc_info=True)
        await self._close_ws()

    def _emit_provider_error_from_msg(self, msg: dict[str, Any]) -> None:
        message = msg.get("message") or msg.get("title") or "Cartesia TTS error"
        exc = RuntimeError(f"Cartesia TTS error: {message}")
        self._emit_provider_error(
            exc,
            code=msg.get("code"),
            status_code=msg.get("status_code"),
        )

    def _emit_provider_error(self, exc: BaseException, **context: Any) -> None:
        """Post a journal-visible ``Error`` event, with provider context."""
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
                bus.emit(Error(exception=exc, stage=ErrorStage.TTS, provider="cartesia"))
            )
        except RuntimeError:  # no running loop
            logger.debug("Could not emit provider error — no running loop", exc_info=True)

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "cartesia",
            "model": self._config.model_id,
            "api_version": self._config.cartesia_version,
            "sdk_version": get_package_version("websockets"),
        }
