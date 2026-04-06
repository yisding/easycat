"""ElevenLabs STT provider — realtime WebSocket + batch transcription."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import STTEvent, STTEventType, WordTimestamp
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.stt.base import STTBase, pcm_to_wav

logger = logging.getLogger(__name__)


def _get_package_version(pkg: str) -> str:
    try:
        from importlib.metadata import version

        return version(pkg)
    except Exception:
        return "unknown"


@dataclass
class ElevenLabsSTTConfig:
    """Configuration for the ElevenLabs STT provider."""

    api_key: str
    model: str = "scribe_v1"
    language: str | None = None
    mode: str = "realtime"  # "realtime" or "batch"
    base_url: str = "https://api.elevenlabs.io/v1"
    ws_url: str = "wss://api.elevenlabs.io/v1/speech-to-text/ws"
    timeout: float = 30.0
    # Optional overrides for testing
    http_client: httpx.AsyncClient | None = field(default=None, repr=False)
    ws_connect: Any = field(default=None, repr=False)
    event_bus: Any = field(default=None, repr=False)


class ElevenLabsSTT(STTBase):
    """ElevenLabs STT supporting realtime WebSocket and batch modes.

    In **realtime** mode, a WebSocket connection is opened on ``start_stream``
    and audio chunks are forwarded in real time.

    In **batch** mode, audio is buffered internally and submitted as a single
    HTTP request when ``end_stream`` is called.
    """

    def __init__(self, config: ElevenLabsSTTConfig) -> None:
        super().__init__()
        self._config = config
        # Batch mode state
        self._buffer = bytearray()
        self._audio_format: AudioFormat | None = None
        # Realtime mode state
        self._ws: ReconnectingWebSocket | None = None
        self._receive_task: asyncio.Task[None] | None = None

    @property
    def mode(self) -> str:
        return self._config.mode

    # -- Lifecycle hooks ---------------------------------------------------

    async def _on_start(self) -> None:
        if self._config.mode == "realtime":
            await self._start_realtime()
        else:
            self._buffer.clear()
            self._audio_format = None

    async def _on_audio(self, chunk: AudioChunk) -> None:
        if self._config.mode == "realtime":
            await self._send_realtime(chunk)
        else:
            if self._audio_format is None:
                self._audio_format = chunk.format
            self._buffer.extend(chunk.data)

    async def _on_end(self) -> None:
        if self._config.mode == "realtime":
            await self._end_realtime()
        else:
            await self._end_batch()

    # -- Realtime (WebSocket) mode -----------------------------------------

    async def _start_realtime(self) -> None:
        headers = {"xi-api-key": self._config.api_key}
        self._ws = ReconnectingWebSocket(
            url=self._config.ws_url,
            config=ReconnectConfig(extra_headers=headers),
            event_bus=self._config.event_bus,
            provider_name="elevenlabs_stt",
            connect_fn=self._config.ws_connect,
        )
        await self._ws.connect()

        # Send initial configuration
        init_msg: dict[str, Any] = {"type": "start"}
        if self._config.model:
            init_msg["model"] = self._config.model
        if self._config.language:
            init_msg["language"] = self._config.language
        await self._ws.send(json.dumps(init_msg))

        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _send_realtime(self, chunk: AudioChunk) -> None:
        if self._ws is not None:
            import base64

            payload = json.dumps({"type": "audio", "data": base64.b64encode(chunk.data).decode()})
            await self._ws.send(payload)

    async def _end_realtime(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "stop"}))
            except Exception:
                logger.debug("Error sending stop message", exc_info=True)

            if self._receive_task is not None:
                try:
                    await asyncio.wait_for(self._receive_task, timeout=5.0)
                except TimeoutError:
                    self._receive_task.cancel()
                    logger.warning("ElevenLabs receive loop timed out on close")

            await self._ws.close()

        self._ws = None
        self._receive_task = None

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
                self._handle_ws_message(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("ElevenLabs WebSocket closed")
        except Exception:
            logger.exception("Error in ElevenLabs receive loop")
        finally:
            self._event_queue.put_nowait(None)

    def _handle_ws_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")

        if msg_type == "transcript":
            text = msg.get("text", "")
            if not text:
                return
            is_final = msg.get("is_final", False)
            confidence = msg.get("confidence")
            language = msg.get("language") or self._config.language

            word_timestamps = None
            words = msg.get("words")
            if words:
                parsed = []
                for w in words:
                    word = w.get("word") if isinstance(w, dict) else None
                    start = w.get("start") if isinstance(w, dict) else None
                    end = w.get("end") if isinstance(w, dict) else None
                    if word is not None and start is not None and end is not None:
                        parsed.append(WordTimestamp(word=word, start=float(start), end=float(end)))
                word_timestamps = parsed or None

            event_type = STTEventType.FINAL if is_final else STTEventType.PARTIAL
            self._emit_event(
                STTEvent(
                    type=event_type,
                    text=text,
                    confidence=confidence,
                    language=language,
                    word_timestamps=word_timestamps,
                )
            )

    # -- Batch (HTTP) mode -------------------------------------------------

    async def _end_batch(self) -> None:
        if not self._buffer or self._audio_format is None:
            return

        wav_data = pcm_to_wav(bytes(self._buffer), self._audio_format)
        result = await self._transcribe_batch(wav_data)
        if result:
            self._emit_event(
                STTEvent(
                    type=STTEventType.FINAL,
                    text=result["text"],
                    confidence=result.get("confidence"),
                    language=result.get("language") or self._config.language,
                )
            )

    async def _transcribe_batch(self, wav_data: bytes) -> dict[str, Any] | None:
        url = f"{self._config.base_url}/speech-to-text"
        headers = {"xi-api-key": self._config.api_key}

        data: dict[str, str] = {}
        if self._config.model:
            data["model"] = self._config.model
        if self._config.language:
            data["language"] = self._config.language

        client = self._config.http_client or httpx.AsyncClient(timeout=self._config.timeout)
        owns_client = self._config.http_client is None
        try:
            response = await client.post(
                url,
                headers=headers,
                files={"file": ("audio.wav", wav_data, "audio/wav")},
                data=data,
            )
            response.raise_for_status()
            return response.json()
        finally:
            if owns_client:
                await client.aclose()

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "elevenlabs",
            "model": self._config.model,
            "api_version": "v1",
            "sdk_version": _get_package_version("httpx"),
        }
