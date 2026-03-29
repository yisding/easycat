"""OpenAI Realtime API streaming STT provider.

Sends audio chunks over a WebSocket as they arrive (no buffering) and
receives partial/final transcription events in real time.  Uses the
``input_audio_transcription`` feature of the OpenAI Realtime API.

Unlike :class:`OpenAISTT` (which buffers all audio then POSTs a WAV),
this provider achieves much lower latency because transcription starts
while the user is still speaking.

.. note::

   The Realtime API is priced differently from the batch transcription
   API.  See https://openai.com/pricing for details.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import websockets

from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.stt.base import STTBase

logger = logging.getLogger(__name__)


@dataclass
class OpenAIRealtimeSTTConfig:
    """Configuration for the OpenAI Realtime streaming STT provider."""

    api_key: str
    model: str = "gpt-4o-transcribe"
    language: str | None = None
    ws_url: str = "wss://api.openai.com/v1/realtime"
    # Optional WebSocket factory override for testing.
    # Signature: async (url, **kwargs) -> connection
    ws_connect: Any = field(default=None, repr=False)
    # Optional EventBus for reconnect observability
    event_bus: Any = field(default=None, repr=False)


class OpenAIRealtimeSTT(STTBase):
    """Streaming STT using the OpenAI Realtime API WebSocket.

    Opens a WebSocket on ``start_stream``, forwards audio chunks in
    real time via ``send_audio``, and parses incoming transcription
    events in a background receive loop.  Audio is sent as base64-
    encoded PCM in ``input_audio_buffer.append`` messages.

    The session is configured with ``turn_detection: null`` so that
    EasyCat's own VAD controls turn boundaries, and with
    ``input_audio_transcription`` enabled for the configured model.
    """

    def __init__(self, config: OpenAIRealtimeSTTConfig) -> None:
        super().__init__()
        self._config = config
        self._ws: ReconnectingWebSocket | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._partial_text: str = ""

    async def _on_start(self) -> None:
        url = f"{self._config.ws_url}?model={self._config.model}"
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        self._ws = ReconnectingWebSocket(
            url=url,
            config=ReconnectConfig(extra_headers=headers),
            event_bus=self._config.event_bus,
            provider_name="openai_realtime_stt",
            connect_fn=self._config.ws_connect,
        )
        await self._ws.connect()

        # Configure the session for STT-only operation.
        session_update: dict[str, Any] = {
            "type": "session.update",
            "session": {
                "input_audio_transcription": {
                    "model": self._config.model,
                },
                # Disable server-side turn detection — EasyCat's VAD handles this.
                "turn_detection": None,
            },
        }
        if self._config.language:
            session_update["session"]["input_audio_transcription"]["language"] = (
                self._config.language
            )

        await self._ws.send(json.dumps(session_update))
        self._partial_text = ""
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _on_audio(self, chunk: AudioChunk) -> None:
        if self._ws is not None:
            audio_b64 = base64.b64encode(chunk.data).decode("ascii")
            msg = json.dumps({
                "type": "input_audio_buffer.append",
                "audio": audio_b64,
            })
            await self._ws.send(msg)

    async def _on_end(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            except Exception:
                logger.debug("Error sending input_audio_buffer.commit", exc_info=True)

            if self._receive_task is not None:
                try:
                    await asyncio.wait_for(self._receive_task, timeout=5.0)
                except TimeoutError:
                    self._receive_task.cancel()
                    logger.warning("OpenAI Realtime receive loop timed out on close")

            await self._ws.close()

        self._ws = None
        self._receive_task = None
        self._partial_text = ""

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw_message in self._ws.recv_iter():
                if isinstance(raw_message, bytes):
                    continue
                try:
                    msg = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                self._handle_message(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("OpenAI Realtime WebSocket closed")
        except Exception:
            logger.exception("Error in OpenAI Realtime receive loop")

    def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")

        if msg_type == "conversation.item.input_audio_transcription.delta":
            delta = msg.get("delta", "")
            if delta:
                self._partial_text += delta
                self._emit_event(
                    STTEvent(type=STTEventType.PARTIAL, text=self._partial_text)
                )

        elif msg_type == "conversation.item.input_audio_transcription.completed":
            transcript = msg.get("transcript", "")
            if transcript:
                self._emit_event(STTEvent(type=STTEventType.FINAL, text=transcript))
            elif self._partial_text:
                self._emit_event(STTEvent(type=STTEventType.FINAL, text=self._partial_text))

        elif msg_type == "error":
            error = msg.get("error", {})
            error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            logger.warning("OpenAI Realtime API error: %s", error_msg)

        elif msg_type in ("session.created", "session.updated"):
            logger.debug("OpenAI Realtime: %s", msg_type)
