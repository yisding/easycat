"""ElevenLabs STT provider — realtime WebSocket + batch transcription."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from easycat._provider_helpers import get_package_version
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.audio_utils import resample
from easycat.events import STTEvent, STTEventType, WordTimestamp
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.stt.base import STTBase, pcm_to_wav

logger = logging.getLogger(__name__)


@dataclass
class ElevenLabsSTTConfig:
    """Configuration for the ElevenLabs STT provider."""

    api_key: str
    model: str | None = None
    language: str | None = None
    mode: str = "realtime"  # "realtime" or "batch"
    base_url: str = "https://api.elevenlabs.io/v1"
    ws_url: str = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    timeout: float = 30.0
    realtime_sample_rate: int = 16000
    realtime_commit_strategy: str = "manual"  # "manual" or "vad"
    realtime_include_timestamps: bool = False
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
        self._close_task: asyncio.Task[None] | None = None
        self._final_received: asyncio.Event | None = None
        self._audio_pending_commit: bool = False

    def _resolved_model(self) -> str:
        if self._config.model is not None:
            return self._config.model
        if self._config.mode == "realtime":
            return "scribe_v2_realtime"
        return "scribe_v1"

    def _realtime_audio_format(self) -> str:
        sample_rate = self._config.realtime_sample_rate
        supported = {
            8000: "pcm_8000",
            16000: "pcm_16000",
            22050: "pcm_22050",
            24000: "pcm_24000",
            44100: "pcm_44100",
            48000: "pcm_48000",
        }
        if sample_rate not in supported:
            raise ValueError(
                "ElevenLabs realtime STT requires one of "
                f"{sorted(supported)} Hz, got {sample_rate}"
            )
        return supported[sample_rate]

    def _build_realtime_ws_url(self) -> str:
        from urllib.parse import urlencode

        params: dict[str, str] = {
            "model_id": self._resolved_model(),
            "audio_format": self._realtime_audio_format(),
            "commit_strategy": self._config.realtime_commit_strategy,
            "include_timestamps": str(self._config.realtime_include_timestamps).lower(),
        }
        if self._config.language:
            params["language_code"] = self._config.language
        return f"{self._config.ws_url}?{urlencode(params)}"

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
        if self._close_task is not None:
            try:
                await self._close_task
            except Exception:
                pass
            self._close_task = None
        headers = {"xi-api-key": self._config.api_key}
        self._ws = ReconnectingWebSocket(
            url=self._build_realtime_ws_url(),
            config=ReconnectConfig(extra_headers=headers),
            event_bus=self._config.event_bus,
            provider_name="elevenlabs_stt",
            connect_fn=self._config.ws_connect,
        )
        await self._ws.connect()
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._final_received = None
        self._audio_pending_commit = False

    async def _send_realtime(self, chunk: AudioChunk) -> None:
        if self._ws is not None:
            payload_audio = chunk.data
            if chunk.format.sample_rate != self._config.realtime_sample_rate:
                payload_audio = resample(
                    payload_audio,
                    chunk.format.sample_rate,
                    self._config.realtime_sample_rate,
                )
            payload = json.dumps(
                {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": base64.b64encode(payload_audio).decode(),
                    "commit": False,
                    "sample_rate": self._config.realtime_sample_rate,
                }
            )
            await self._ws.send(payload)
            self._audio_pending_commit = True

    async def _on_commit_segment(self) -> bool:
        return await self._send_commit(wait_for_final=False)

    async def _end_realtime(self) -> None:
        ws = self._ws
        receive_task = self._receive_task
        if ws is not None and self._audio_pending_commit:
            await self._send_commit(wait_for_final=True)

        self._ws = None
        self._receive_task = None
        self._final_received = None
        if ws is not None:
            try:
                await self._close_connection(ws, receive_task)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("ElevenLabs realtime close failed during end", exc_info=True)

    async def _send_commit(self, *, wait_for_final: bool) -> bool:
        ws = self._ws
        if ws is None or not self._audio_pending_commit:
            return False

        final_received = asyncio.Event()
        self._final_received = final_received
        try:
            await ws.send(
                json.dumps(
                    {
                        "message_type": "input_audio_chunk",
                        "audio_base_64": "",
                        "commit": True,
                        "sample_rate": self._config.realtime_sample_rate,
                    }
                )
            )
        except Exception:
            logger.debug("Error sending realtime commit", exc_info=True)
            if self._final_received is final_received:
                self._final_received = None
            return False

        self._audio_pending_commit = False
        if wait_for_final:
            try:
                await asyncio.wait_for(final_received.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning("Timed out waiting for final transcript from ElevenLabs")
        return True

    async def _close_connection(
        self,
        ws: ReconnectingWebSocket,
        receive_task: asyncio.Task[None] | None,
    ) -> None:
        await ws.close()
        if receive_task is not None:
            try:
                await asyncio.wait_for(receive_task, timeout=2.0)
            except TimeoutError:
                receive_task.cancel()
                logger.warning("ElevenLabs receive loop timed out on close")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("ElevenLabs close task ignored receive-loop error", exc_info=True)

    @staticmethod
    def _log_close_task_exception(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("ElevenLabs close task failed", exc_info=True)

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        queue = self._event_queue
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
            queue.put_nowait(None)

    def _handle_ws_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("message_type") or msg.get("type", "")

        if msg_type == "partial_transcript":
            text = msg.get("text", "")
            if not text:
                return
            self._emit_event(
                STTEvent(
                    type=STTEventType.PARTIAL,
                    text=text,
                    confidence=msg.get("confidence"),
                    language=msg.get("language_code") or self._config.language,
                )
            )
            return

        if msg_type in {"committed_transcript", "committed_transcript_with_timestamps"}:
            text = msg.get("text", "")
            if not text:
                return

            word_timestamps = None
            words = msg.get("words")
            if words:
                parsed = []
                for w in words:
                    word = None
                    start = None
                    end = None
                    if isinstance(w, dict):
                        word = w.get("text") or w.get("word")
                        start = w.get("start")
                        end = w.get("end")
                    if word is not None and start is not None and end is not None:
                        parsed.append(WordTimestamp(word=word, start=float(start), end=float(end)))
                word_timestamps = parsed or None

            self._emit_event(
                STTEvent(
                    type=STTEventType.FINAL,
                    text=text,
                    confidence=msg.get("confidence"),
                    language=msg.get("language_code") or self._config.language,
                    word_timestamps=word_timestamps,
                )
            )
            if self._final_received is not None:
                self._final_received.set()

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
        data["model"] = self._resolved_model()
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
            "model": self._resolved_model(),
            "api_version": "v1",
            "sdk_version": get_package_version("httpx"),
        }
