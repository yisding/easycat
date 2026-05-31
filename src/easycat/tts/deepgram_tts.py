"""Deepgram TTS (Aura) provider implementation."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from easycat._provider_helpers import get_package_version
from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.events import TTSEvent
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.tts._ws_base import _WSTTSBase
from easycat.tts.input import TTSInput, coerce_tts_input

logger = logging.getLogger(__name__)


@dataclass
class DeepgramTTSConfig:
    """Configuration for the Deepgram TTS provider."""

    api_key: str = ""
    model: str = "aura-asteria-en"
    encoding: str = "linear16"
    sample_rate: int = 24000
    base_url: str = "wss://api.deepgram.com/v1/speak"
    output_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_24K)
    event_bus: object | None = None
    # Reconnect tuning for the synthesis WebSocket. Defaults match
    # ReconnectConfig; lower max_retries to fail fast and defer to
    # turn-level retry, or raise it for flaky links.
    reconnect_max_retries: int = 3
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 30.0


class DeepgramTTS(_WSTTSBase):
    """TTS provider using Deepgram's Aura WebSocket API.

    Opens a WebSocket connection to Deepgram, sends text, and receives
    audio chunks as binary messages. Uses ReconnectingWebSocket for
    connection lifecycle management.

    Requests linear16 (PCM16) encoding directly from Deepgram to avoid
    needing audio decoding dependencies.
    """

    _provider_error_name = "deepgram"
    _provider_log_label = "Deepgram"

    def __init__(self, config: DeepgramTTSConfig) -> None:
        super().__init__(output_format=config.output_format)
        self._config = config
        # Text of the in-flight utterance, replayed by the on_reconnect hook
        # so a mid-stream drop restarts the utterance from the top instead of
        # aborting it. Known tradeoff: Deepgram synthesis is one-shot per
        # utterance, so any audio already emitted before the drop is re-emitted
        # after the restart (audible repetition), not a seamless resume.
        self._pending_text: str | None = None
        # Build the source format based on what Deepgram returns
        self._source_format = AudioFormat(
            sample_rate=config.sample_rate,
            channels=1,
            sample_width=2,
        )

    def _build_url(self) -> str:
        """Build the Deepgram TTS WebSocket URL with query parameters."""
        return (
            f"{self._config.base_url}"
            f"?model={self._config.model}"
            f"&encoding={self._config.encoding}"
            f"&sample_rate={self._config.sample_rate}"
        )

    def _create_ws(self) -> ReconnectingWebSocket:
        """Create a new ReconnectingWebSocket with auth headers."""
        return ReconnectingWebSocket(
            url=self._build_url(),
            config=ReconnectConfig(
                extra_headers={"Authorization": f"Token {self._config.api_key}"},
                max_retries=self._config.reconnect_max_retries,
                base_delay=self._config.reconnect_base_delay,
                max_delay=self._config.reconnect_max_delay,
            ),
            event_bus=self._config.event_bus,
            provider_name="deepgram_tts",
            on_reconnect=self._replay_request,
        )

    async def _replay_request(self) -> None:
        """Re-send the Speak + Flush frames after a reconnect.

        Deepgram synthesis is one-shot per utterance, so replaying the text
        and flush restarts it from the top on the fresh socket — it does NOT
        resume from the drop point. Any audio already emitted before the drop
        is re-emitted, producing audible repetition; this is an accepted
        tradeoff of stateless one-shot replay in exchange for not aborting the
        utterance entirely. Without this hook a transient drop would re-raise
        out of recv_iter and abort synthesis.
        """
        ws = self._ws
        text = self._pending_text
        if ws is None or text is None or self._cancelled:
            return
        # The replayed stream restarts from the top and is sample-aligned in
        # its own right, so drop any sub-sample byte held from before the drop
        # to avoid shifting every replayed sample by one byte.
        self._reset_audio_alignment()
        await ws.send(json.dumps({"type": "Speak", "text": text}))
        await ws.send(json.dumps({"type": "Flush"}))

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        """Synthesize text using Deepgram's WebSocket TTS API.

        Opens a WebSocket, sends the text, and yields audio chunks as
        they arrive. Sends a flush message after the text to signal
        end of input.

        SSML is not supported (``supports_ssml`` is ``False``), so the
        scheduler always delivers a plain-text payload here.
        """
        self._start_synthesis()
        self._ws = self._create_ws()
        text = coerce_tts_input(payload).text
        # Leave replay disarmed until the request has actually been sent on a
        # connected stream. ``on_reconnect`` fires for retries during the
        # *initial* connect too, and arming earlier would replay the Speak/
        # Flush frames before the sends below, duplicating the utterance.
        self._pending_text = None

        try:
            await self._ws.connect()

            # Send the text payload
            await self._ws.send(json.dumps({"type": "Speak", "text": text}))

            # Send flush to signal end of text input
            await self._ws.send(json.dumps({"type": "Flush"}))

            # Request is now live on a connected stream: arm replay so a
            # *mid-stream* reconnect re-sends these frames and restarts the
            # utterance from the top (see ``_replay_request`` for the
            # duplicate-audio tradeoff).
            self._pending_text = text

            # Receive audio chunks
            async for message in self._ws.recv_iter():
                if self._cancelled:
                    break

                if isinstance(message, bytes) and message:
                    yield self._make_audio_event(message, self._source_format)
                elif isinstance(message, str):
                    # Handle control messages from Deepgram
                    try:
                        ctrl = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    ctrl_type = ctrl.get("type")
                    if ctrl_type == "Flushed":
                        break
                    if ctrl_type == "Error":
                        # Deepgram surfaces invalid model / rate-limit /
                        # quota rejections as Error frames. Route them through
                        # the journal-visible Error event and stop the loop so
                        # the failure is observable instead of silently waiting
                        # for a socket close.
                        self._emit_provider_error_from_msg(ctrl)
                        break
                    if ctrl_type == "Warning":
                        # Warning frames are non-fatal advisories (e.g.
                        # TEXT_LENGTH_WARNING or the Flush rate-limit warning)
                        # that do not prevent the connection from continuing.
                        # Log and keep receiving so synthesis is not truncated.
                        logger.info(
                            "Deepgram TTS warning: %s",
                            ctrl.get("description")
                            or ctrl.get("message")
                            or ctrl.get("reason")
                            or ctrl,
                        )
                        continue

        except Exception as exc:
            if not self._cancelled:
                logger.error("Deepgram TTS error: %s", exc)
                self._emit_provider_error(exc, ws_close_code=getattr(exc, "code", None))
                raise
        finally:
            await self._close_ws()
            self._pending_text = None
            self._end_synthesis()

    async def stop(self) -> None:
        """Gracefully stop synthesis.

        Sends a final Flush so Deepgram emits any buffered audio, then closes
        the socket. Closing here matches Cartesia/ElevenLabs ``stop()`` so a
        graceful stop between turns does not leave the WebSocket lingering
        until the next ``cancel()``/``close()``.
        """
        await super().stop()
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "Flush"}))
            except Exception:
                logger.debug("Error sending Deepgram Flush on stop", exc_info=True)
        await self._close_ws()

    async def cancel(self) -> None:
        """Immediately cancel synthesis and close the WebSocket."""
        await super().cancel()
        await self._close_ws()

    def _emit_provider_error_from_msg(self, msg: dict[str, Any]) -> None:
        """Build and emit a provider Error from a Deepgram control frame."""
        message = (
            msg.get("description")
            or msg.get("message")
            or msg.get("reason")
            or "Deepgram TTS error"
        )
        exc = RuntimeError(f"Deepgram TTS error: {message}")
        # Note: the frame ``type`` ("Error") is redundant and is intentionally
        # not attached as ``ws_close_code`` — that note key is reserved for an
        # actual WebSocket close code (see the synthesis-exception path).
        self._emit_provider_error(
            exc,
            code=msg.get("code"),
        )

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "deepgram",
            "model": self._config.model,
            "api_version": "v1",
            "sdk_version": get_package_version("websockets"),
        }
