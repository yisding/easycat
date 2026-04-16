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
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

from easycat.audio_format import AudioChunk
from easycat.audio_utils import resample_chunk
from easycat.events import STTEvent, STTEventType
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.stt.base import STTBase

# OpenAI Realtime API expects 24 kHz PCM16 mono input by default.
_REALTIME_SAMPLE_RATE = 24000

logger = logging.getLogger(__name__)


def _get_package_version(pkg: str) -> str:
    try:
        from importlib.metadata import version

        return version(pkg)
    except Exception:
        return "unknown"


@dataclass
class OpenAIRealtimeSTTConfig:
    """Configuration for the OpenAI Realtime streaming STT provider."""

    api_key: str
    model: str = "gpt-4o-transcribe"
    connection_model: str = "gpt-realtime-mini"
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
        self._close_task: asyncio.Task[None] | None = None
        self._partial_text: str = ""
        self._final_received: asyncio.Event | None = None
        self._audio_pending_commit: bool = False
        # Bytes appended to the server's input_audio_buffer since the
        # last commit.  OpenAI Realtime refuses commits with <100ms of
        # audio (rate: 24 kHz mono 16-bit → 4800 B/100 ms).  We track
        # locally so ``_send_commit`` can skip the server round-trip
        # when the tail is too short — the previous code sent the
        # doomed commit and surfaced it as a warning in the logs.
        self._bytes_since_last_commit: int = 0
        self._session_ready: asyncio.Future[None] | None = None

    def _websocket_url(self) -> str:
        """Build the Realtime WebSocket URL with the required realtime model."""
        parts = urlsplit(self._config.ws_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.setdefault("model", self._config.connection_model)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    async def _on_start(self) -> None:
        if self._close_task is not None:
            try:
                await self._close_task
            except Exception:
                pass
            self._close_task = None
        url = self._websocket_url()
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
        }

        self._ws = ReconnectingWebSocket(
            url=url,
            config=ReconnectConfig(extra_headers=headers),
            event_bus=self._config.event_bus,
            provider_name="openai_realtime_stt",
            connect_fn=self._config.ws_connect,
            on_reconnect=self._send_session_update,
        )
        await self._ws.connect()
        loop = asyncio.get_running_loop()
        self._session_ready = loop.create_future()
        self._receive_task = asyncio.create_task(self._receive_loop())
        try:
            await self._send_session_update()
        except Exception:
            ws = self._ws
            receive_task = self._receive_task
            self._ws = None
            self._receive_task = None
            self._session_ready = None
            if ws is not None:
                task = asyncio.create_task(self._close_connection(ws, receive_task))
                task.add_done_callback(self._log_close_task_exception)
                self._close_task = task
            raise
        self._partial_text = ""
        self._audio_pending_commit = False
        self._final_received = None
        try:
            await asyncio.wait_for(self._session_ready, timeout=5.0)
        except TimeoutError as exc:
            ws = self._ws
            receive_task = self._receive_task
            self._ws = None
            self._receive_task = None
            self._session_ready = None
            if ws is not None:
                task = asyncio.create_task(self._close_connection(ws, receive_task))
                task.add_done_callback(self._log_close_task_exception)
                self._close_task = task
            raise TimeoutError("timed out waiting for OpenAI Realtime session.update") from exc

    async def _send_session_update(self) -> None:
        """Configure a realtime session with input audio transcription enabled."""
        assert self._ws is not None
        transcription: dict[str, Any] = {"model": self._config.model}
        if self._config.language:
            transcription["language"] = self._config.language
        session_update: dict[str, Any] = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": _REALTIME_SAMPLE_RATE,
                        },
                        "transcription": transcription,
                        # Disable server-side VAD — EasyCat's VAD handles turns.
                        "turn_detection": None,
                    }
                },
            },
        }
        await self._ws.send(json.dumps(session_update))

    async def _on_audio(self, chunk: AudioChunk) -> None:
        if self._ws is not None:
            if chunk.format.sample_rate != _REALTIME_SAMPLE_RATE:
                chunk = resample_chunk(chunk, _REALTIME_SAMPLE_RATE)
            audio_b64 = base64.b64encode(chunk.data).decode("ascii")
            msg = json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64,
                }
            )
            await self._ws.send(msg)
            self._audio_pending_commit = True
            self._bytes_since_last_commit += len(chunk.data)

    async def _on_commit_segment(self) -> bool:
        return await self._send_commit(wait_for_final=False)

    async def _on_end(self) -> None:
        ws = self._ws
        receive_task = self._receive_task
        if ws is not None and self._audio_pending_commit:
            await self._send_commit(wait_for_final=True)

        self._ws = None
        self._receive_task = None
        self._partial_text = ""
        self._final_received = None
        self._session_ready = None
        if ws is not None:
            try:
                await self._close_connection(ws, receive_task)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("OpenAI Realtime close failed during end", exc_info=True)

    # OpenAI Realtime requires commits to have at least 100ms of audio.
    # At 24 kHz mono 16-bit that is 4800 bytes.  Skip the commit when
    # the pending tail is shorter than this — the server would reject
    # it anyway and we'd surface a spurious warning plus leave the
    # downstream final_received event waiter hanging.
    _COMMIT_MIN_BYTES = _REALTIME_SAMPLE_RATE * 2 // 10  # 100ms of PCM16 mono

    async def _send_commit(self, *, wait_for_final: bool) -> bool:
        ws = self._ws
        if ws is None or not self._audio_pending_commit:
            return False
        if self._bytes_since_last_commit < self._COMMIT_MIN_BYTES:
            # Tail too short — skip the server round-trip (the server
            # would reject the commit and surface a warning).  Keep
            # ``_audio_pending_commit`` and ``_bytes_since_last_commit``
            # intact so a later commit that sees more audio (locally
            # small tail + fresh audio) still reflects the true server
            # buffer and eventually reaches the 100 ms threshold.
            logger.debug(
                "Skipping input_audio_buffer.commit: only %d bytes (<%d min)",
                self._bytes_since_last_commit,
                self._COMMIT_MIN_BYTES,
            )
            return False

        final_received = asyncio.Event()
        self._final_received = final_received
        try:
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception:
            logger.debug("Error sending input_audio_buffer.commit", exc_info=True)
            if self._final_received is final_received:
                self._final_received = None
            return False

        self._audio_pending_commit = False
        self._bytes_since_last_commit = 0
        if wait_for_final:
            try:
                await asyncio.wait_for(final_received.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning("Timed out waiting for final transcript from OpenAI Realtime")
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
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
                logger.warning("OpenAI Realtime receive loop timed out on close")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "OpenAI Realtime close task ignored receive-loop error",
                    exc_info=True,
                )

    @staticmethod
    def _log_close_task_exception(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("OpenAI Realtime close task failed", exc_info=True)

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
                self._handle_message(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("OpenAI Realtime WebSocket closed")
        except Exception:
            logger.exception("Error in OpenAI Realtime receive loop")
        finally:
            if self._session_ready is not None and not self._session_ready.done():
                self._session_ready.set_exception(
                    RuntimeError("OpenAI Realtime connection closed before session was ready")
                )
            queue.put_nowait(None)

    def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")

        if msg_type == "conversation.item.input_audio_transcription.delta":
            delta = msg.get("delta", "")
            if delta:
                self._partial_text += delta
                self._emit_event(STTEvent(type=STTEventType.PARTIAL, text=self._partial_text))

        elif msg_type == "conversation.item.input_audio_transcription.completed":
            transcript = msg.get("transcript", "")
            if transcript:
                self._emit_event(STTEvent(type=STTEventType.FINAL, text=transcript))
            elif self._partial_text:
                self._emit_event(STTEvent(type=STTEventType.FINAL, text=self._partial_text))
            self._partial_text = ""
            if self._final_received is not None:
                self._final_received.set()

        elif msg_type == "error":
            error = msg.get("error", {})
            error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            error_code = error.get("code") if isinstance(error, dict) else None
            logger.warning("OpenAI Realtime API error: %s", error_msg)
            # Surface provider errors into the journal via an ``Error``
            # event.  Without this, diagnosis-from-bundle for buffer-
            # too-small / auth / rate-limit issues has to reach for the
            # live log output.  Attach structured context (error code,
            # buffer state) so the bundle shows everything a user sees.
            self._emit_provider_error(
                RuntimeError(error_msg),
                code=error_code,
                buffer_bytes=self._bytes_since_last_commit,
            )
            if self._session_ready is not None and not self._session_ready.done():
                self._session_ready.set_exception(RuntimeError(error_msg))

        elif msg_type in (
            "session.created",
            "session.updated",
            "transcription_session.updated",
        ):
            logger.debug("OpenAI Realtime: %s", msg_type)
            if msg_type in ("session.updated", "transcription_session.updated"):
                if self._session_ready is not None and not self._session_ready.done():
                    self._session_ready.set_result(None)

    def _emit_provider_error(
        self,
        exc: BaseException,
        *,
        code: str | None = None,
        buffer_bytes: int | None = None,
    ) -> None:
        """Fire an ``Error`` event on the event bus when the server reports
        an error.  Session's journal sink subscribes to ``Error`` events so
        this lands as a journal record with the provider name, the error
        message, and the attached buffer-state context.  Without this,
        debugging from a recorded bundle is blind to provider-reported
        errors — they previously went to ``logger.warning`` only.
        """
        bus = getattr(self._config, "event_bus", None)
        if bus is None:
            return
        from easycat.events import Error, ErrorStage

        # Attach diagnostic notes to the exception so the ``Error`` event
        # keeps all the context without requiring a new Error subtype.
        notes_parts: list[str] = []
        if code:
            notes_parts.append(f"code={code}")
        if buffer_bytes is not None:
            notes_parts.append(f"buffer_bytes={buffer_bytes}")
        if notes_parts:
            for note in notes_parts:
                try:
                    exc.add_note(note)  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover - pre-3.11
                    pass
        try:
            asyncio.create_task(
                bus.emit(Error(exception=exc, stage=ErrorStage.STT, provider="openai-realtime"))
            )
        except RuntimeError:  # no running loop
            logger.debug("Could not emit provider error — no running loop", exc_info=True)

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "openai-realtime",
            "model": self._config.model,
            "api_version": "v1",
            "sdk_version": _get_package_version("websockets"),
        }
