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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from easycat._audio_utils import resample_chunk
from easycat._provider_helpers import get_package_version
from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.stt.websocket_base import WebSocketSTTBase

# OpenAI Realtime API expects 24 kHz PCM16 mono input by default.
_REALTIME_SAMPLE_RATE = 24000

# How long to wait for ``...transcription.completed`` after an
# end-of-turn commit before we give up and fall back to the most recent
# delta-accumulated partial.  OpenAI occasionally stalls for several
# seconds on this event; waiting it out shows up as a multi-second
# user-visible pause, so we'd rather ship slightly-less-corrected text
# quickly than sit on a perfect transcript.  The ``.completed`` message
# for this commit is still expected to arrive; we discard it so the
# session doesn't see two ``STTFinal`` events for one turn.
_FINAL_TRANSCRIPT_TIMEOUT_S = 2.0

logger = logging.getLogger(__name__)


@dataclass
class OpenAIRealtimeSTTConfig:
    """Configuration for the OpenAI Realtime streaming STT provider.

    .. note::

       ``api_key`` defaults to ``""`` to support the inject-the-key-later
       workflow (e.g. constructing the config first and assigning the key
       before use).  A missing key is therefore *not* validated at
       construction time — it surfaces on the first WebSocket connection
       rather than eagerly.  The :func:`easycat.stt.factory` path still
       fail-fasts on an empty key.
    """

    api_key: str = ""
    model: str = "gpt-4o-transcribe"
    connection_model: str = "gpt-realtime-mini"
    language: str | None = None
    ws_url: str = "wss://api.openai.com/v1/realtime"
    # Optional WebSocket factory override for testing.
    # Signature: async (url, **kwargs) -> connection
    ws_connect: Any = field(default=None, repr=False)
    # Optional EventBus for reconnect observability
    event_bus: Any = field(default=None, repr=False)


class OpenAIRealtimeSTT(WebSocketSTTBase):
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
        super().__init__(
            provider_name="openai_realtime_stt",
            provider_error_name="openai-realtime",
        )
        self._config = config
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
        # Set when ``_send_commit`` gave up waiting for the current
        # commit's ``.completed`` and already promoted ``_partial_text``
        # to a ``STTFinal``.  The flag causes the first subsequent
        # ``.completed`` to be dropped instead of producing a second
        # ``STTFinal`` for the same turn.  Cleared on the next commit.
        self._dropping_pending_final: bool = False

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

        loop = asyncio.get_running_loop()
        self._session_ready = loop.create_future()
        # Reset per-stream state BEFORE the receive loop starts so early
        # messages on the new socket (including any late ``.completed``
        # from the previous stream that slipped through before close)
        # can't observe stale flags from the prior run.
        self._partial_text = ""
        self._audio_pending_commit = False
        self._final_received = None
        self._dropping_pending_final = False
        await self._connect_websocket(
            url=url,
            headers=headers,
            event_bus=self._config.event_bus,
            connect_fn=self._config.ws_connect,
            on_reconnect=self._send_session_update,
        )
        try:
            await self._send_session_update()
            await asyncio.wait_for(self._session_ready, timeout=5.0)
        except TimeoutError as exc:
            self._schedule_close()
            self._session_ready = None
            raise TimeoutError("timed out waiting for OpenAI Realtime session.update") from exc
        except Exception:
            self._schedule_close()
            self._session_ready = None
            raise

    def _schedule_close(self) -> None:
        """Tear down the active socket in the background after a failed start.

        Detaches the current websocket/receive-loop from ``self`` and drains
        them via the shared base close path on a fire-and-forget task, stored
        on ``_close_task`` so the next ``_on_start`` can await it.
        """
        ws = self._ws
        if ws is None:
            return
        task = asyncio.create_task(self._close_active_websocket())
        task.add_done_callback(self._log_close_task_exception)
        self._close_task = task

    @staticmethod
    def _log_close_task_exception(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("OpenAI Realtime close task failed", exc_info=True)

    async def _send_session_update(self) -> None:
        """Configure a realtime session with input audio transcription enabled.

        Also called by :class:`ReconnectingWebSocket` on transparent
        reconnects, so reset local buffer-tracking state here — the
        server-side ``input_audio_buffer`` is empty on a fresh socket.
        """
        assert self._ws is not None
        self._partial_text = ""
        self._audio_pending_commit = False
        self._bytes_since_last_commit = 0
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
        if self._ws is not None and self._audio_pending_commit:
            await self._send_commit(wait_for_final=True)

        self._final_received = None
        self._session_ready = None
        try:
            # OpenAI keeps the realtime socket open after delivering the
            # final transcript, so draining first would block in the receive
            # loop until the close timeout fires.  Close-before-drain wakes
            # the receive loop, keeping turn-to-agent latency low.
            await self._close_active_websocket(close_before_drain=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("OpenAI Realtime close failed during end", exc_info=True)
        finally:
            self._partial_text = ""

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
        # Each fresh commit starts a clean slate — any stale drop flag
        # from a previous commit (e.g. one whose timed-out ``.completed``
        # never arrived) should not suppress this commit's final.
        self._dropping_pending_final = False
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
                await asyncio.wait_for(final_received.wait(), timeout=_FINAL_TRANSCRIPT_TIMEOUT_S)
            except TimeoutError:
                # Give up on OpenAI's final and promote whatever we've
                # streamed via ``...transcription.delta`` so the session
                # can drive the LLM with the best partial.  The real
                # ``.completed``, if it arrives later, will be dropped
                # via ``_dropping_pending_final`` so the turn doesn't
                # receive two ``STTFinal`` events.
                logger.warning(
                    "Timed out after %.1fs waiting for OpenAI Realtime final; "
                    "promoting %d-char partial to FINAL",
                    _FINAL_TRANSCRIPT_TIMEOUT_S,
                    len(self._partial_text),
                )
                if self._partial_text:
                    self._emit_event(STTEvent(type=STTEventType.FINAL, text=self._partial_text))
                    self._partial_text = ""
                self._dropping_pending_final = True
        return True

    def _on_receive_loop_end(self) -> None:
        """Fail-fast a pending handshake when the receive loop exits.

        OpenAI is the only provider with a ``session.update`` handshake
        gated on a ``_session_ready`` future, so this base hook rejects
        that future if the socket drops before the session is
        acknowledged.  Without it, ``_on_start``'s wait would block for
        the full 5s timeout instead of surfacing the close immediately.
        """
        if self._session_ready is not None and not self._session_ready.done():
            self._session_ready.set_exception(
                RuntimeError("OpenAI Realtime connection closed before session was ready")
            )

    def _handle_json_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")

        if msg_type == "conversation.item.input_audio_transcription.delta":
            delta = msg.get("delta", "")
            if delta:
                self._partial_text += delta
                self._emit_event(STTEvent(type=STTEventType.PARTIAL, text=self._partial_text))

        elif msg_type == "conversation.item.input_audio_transcription.completed":
            if self._dropping_pending_final:
                # A previous ``_send_commit`` already gave up on this
                # ``.completed`` and promoted the accumulated partial to
                # a FINAL, so silently discard this late revision to
                # avoid emitting a second STTFinal for the same turn.
                logger.debug("Dropping late OpenAI Realtime .completed (already promoted partial)")
                self._dropping_pending_final = False
                self._partial_text = ""
                if self._final_received is not None:
                    self._final_received.set()
            else:
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

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "openai-realtime",
            "model": self._config.model,
            "api_version": "v1",
            "sdk_version": get_package_version("websockets"),
        }
