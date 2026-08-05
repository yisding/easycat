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

from easycat._audio_utils import PCM16StreamResampler
from easycat._numeric import is_finite_number
from easycat._provider_helpers import get_package_version
from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.stt.base import _STT_RUNTIME_FINISH_POLICY
from easycat.stt.websocket_base import WebSocketSTTBase

# OpenAI Realtime API expects 24 kHz PCM16 mono input by default.
_REALTIME_SAMPLE_RATE = 24000

# Default wait for ``...transcription.completed`` after an end-of-turn
# commit before we give up and fall back to the most recent
# delta-accumulated partial.  OpenAI occasionally stalls for several
# seconds on this event; waiting it out shows up as a multi-second
# user-visible pause, so we'd rather ship slightly-less-corrected text
# quickly than sit on a perfect transcript.  The ``.completed`` message
# for this commit is still expected to arrive; we discard it so the
# session doesn't see two ``STTFinal`` events for one turn.  Surfaced as
# ``OpenAIRealtimeSTTConfig.final_transcript_timeout_s`` for tuning; this
# module constant is the field's default and remains the monkeypatch
# seam used by the provider tests.
_FINAL_TRANSCRIPT_TIMEOUT_S = 0.9

logger = logging.getLogger(__name__)

_BACKGROUND_CLOSE_TASK = "openai_realtime_close"


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

    api_key: str = field(default="", repr=False)
    model: str = "gpt-realtime-whisper"
    # Transcription-only Realtime WebSocket sessions connect with
    # ``intent=transcription``.  Set this to a realtime voice model (for
    # example, ``gpt-realtime-mini``) only when intentionally using the
    # legacy realtime-session transcription path.
    connection_model: str | None = None
    language: str | None = None
    # Optional latency/accuracy tradeoff for ``gpt-realtime-whisper``.
    # Supported by OpenAI: minimal, low, medium, high, xhigh.
    delay: str | None = None
    ws_url: str = "wss://api.openai.com/v1/realtime"
    # Bounded wait (seconds) for OpenAI's end-of-turn
    # ``...transcription.completed`` before promoting the
    # delta-accumulated partial to FINAL.  Lower trims the worst-case
    # end-of-turn pause; see ``_FINAL_TRANSCRIPT_TIMEOUT_S`` above for the
    # tradeoff.  Defaults to that module constant so the provider tests can
    # still monkeypatch it.
    final_transcript_timeout_s: float = field(default_factory=lambda: _FINAL_TRANSCRIPT_TIMEOUT_S)
    # Optional WebSocket factory override for testing.
    # Signature: async (url, **kwargs) -> connection
    ws_connect: Any = field(default=None, repr=False)
    # Optional EventBus for reconnect observability
    event_bus: Any = field(default=None, repr=False)
    # Keep one transcription WebSocket across logical voice turns. Appended
    # after the original public fields to preserve positional construction.
    # OpenAI's commit event clears the input buffer, so a single session can
    # accept the next turn without another connection/session.update handshake.
    persistent_ws: bool = True

    def __post_init__(self) -> None:
        if (
            not is_finite_number(self.final_transcript_timeout_s)
            or self.final_transcript_timeout_s <= 0
        ):
            raise ValueError(
                "OpenAIRealtimeSTTConfig.final_transcript_timeout_s must be a finite "
                "positive number"
            )
        self.final_transcript_timeout_s = float(self.final_transcript_timeout_s)


class OpenAIRealtimeSTT(WebSocketSTTBase):
    """Streaming STT using the OpenAI Realtime API WebSocket.

    Keeps one warmed WebSocket across logical turns by default, forwards audio
    chunks in real time via ``send_audio``, and parses incoming transcription
    events in a background receive loop. Audio is sent as base64-encoded PCM
    in ``input_audio_buffer.append`` messages; commits delimit turns and clear
    the server buffer.

    The session is configured with ``turn_detection: null`` so that
    EasyCat's own VAD controls turn boundaries, and with
    ``input_audio_transcription`` enabled for the configured model.
    """

    def __init__(self, config: OpenAIRealtimeSTTConfig) -> None:
        super().__init__(
            provider_name="openai_realtime_stt",
            provider_error_name="openai-realtime",
            dynamic_event_queue=config.persistent_ws,
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
        self._commit_pending: bool = False
        self._final_wait_timed_out: bool = False
        self._audio_resampler = PCM16StreamResampler(_REALTIME_SAMPLE_RATE)

    def _persistent_enabled(self) -> bool:
        return self._config.persistent_ws

    async def warmup(self) -> None:
        """Best-effort prime the Realtime connection before user audio.

        Persistent mode (the default) keeps the completed handshake alive for
        the first and subsequent turns. One-shot mode retains the historical
        connect-handshake-close warmup. Failures are swallowed because warmup
        must not make ``Session.start()`` fail.
        """
        if self._persistent_enabled():
            try:
                async with self._lifecycle_lock:
                    await self._ensure_persistent_connection()
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                logger.debug("OpenAI Realtime warmup skipped: %s", exc)
                try:
                    await self._discard_connection()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("OpenAI Realtime warmup cleanup failed", exc_info=True)
            return
        try:
            await self.start_stream()
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.debug("OpenAI Realtime warmup skipped: %s", exc)
            return
        try:
            await self.end_stream()
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.debug("OpenAI Realtime warmup close skipped: %s", exc)

    def _websocket_url(self) -> str:
        """Build the Realtime WebSocket URL for transcription mode."""
        parts = urlsplit(self._config.ws_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if self._config.connection_model:
            query.setdefault("model", self._config.connection_model)
        else:
            query.setdefault("intent", "transcription")
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    async def _on_start(self) -> None:
        if self._close_task is not None:
            # ``start_stream`` installs this turn's queue before entering the
            # provider hook. The old persistent receive loop may terminate
            # that queue while its close task drains, so replace it only after
            # the task has fully completed.
            await self._drain_scheduled_close()
            if self._persistent_enabled():
                self._event_queue = asyncio.Queue()
        self._reset_logical_turn_state()
        if self._persistent_enabled():
            await self._ensure_persistent_connection()
            return
        await self._connect_new_websocket()

    def _reset_logical_turn_state(self) -> None:
        """Reset state that belongs to one EasyCat voice turn."""
        self._partial_text = ""
        self._audio_pending_commit = False
        self._bytes_since_last_commit = 0
        self._final_received = None
        self._dropping_pending_final = False
        self._commit_pending = False
        self._final_wait_timed_out = False
        self._audio_resampler.reset()

    async def _ensure_persistent_connection(self) -> None:
        ws = self._ws
        if (
            ws is not None
            and ws.is_connected
            and self._receive_task is not None
            and not self._receive_task.done()
        ):
            return
        if ws is not None:
            await self._discard_connection()
            # A persistent receive loop signals the current logical queue when
            # it exits. Replace that just-terminated queue before this turn
            # starts consuming events from the new socket.
            self._event_queue = asyncio.Queue()
        await self._connect_new_websocket()

    async def _connect_new_websocket(self) -> None:
        url = self._websocket_url()
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
        }

        loop = asyncio.get_running_loop()
        self._session_ready = loop.create_future()
        await self._connect_websocket(
            url=url,
            headers=headers,
            event_bus=self._config.event_bus,
            connect_fn=self._config.ws_connect,
            on_reconnect=self._on_reconnect,
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

    async def _discard_connection(self) -> None:
        """Close a socket that cannot be safely reused by another turn."""
        if self._close_task is not asyncio.current_task():
            await self._drain_scheduled_close()
        await self._close_active_websocket(close_before_drain=True)
        self._session_ready = None
        self._reset_logical_turn_state()

    def _schedule_close(self) -> None:
        """Tear down the active socket in the background after a failed start.

        Detaches the current websocket/receive-loop from ``self`` and drains
        them via the shared base close path on a fire-and-forget task, stored
        on ``_close_task`` so the next ``_on_start`` can await it.
        """
        ws = self._ws
        if ws is None:
            return
        scope = self._ensure_runtime_scope()
        self._close_task = scope.create_task(
            _BACKGROUND_CLOSE_TASK,
            self._run_scheduled_close(),
            task_name=_BACKGROUND_CLOSE_TASK,
            policy=_STT_RUNTIME_FINISH_POLICY,
        )

    async def _run_scheduled_close(self) -> None:
        try:
            await self._close_active_websocket()
        except Exception:
            logger.debug("OpenAI Realtime close task failed", exc_info=True)

    async def _drain_scheduled_close(self) -> None:
        task = self._close_task
        scope = self._runtime_scope
        if scope is None:
            if task is None:
                return
            try:
                await task
            except Exception:
                logger.debug("OpenAI Realtime background close failed", exc_info=True)
            finally:
                if task.done() and self._close_task is task:
                    self._close_task = None
            return
        await scope.drain(_BACKGROUND_CLOSE_TASK, suppress_errors=True)
        if (task is None or task.done()) and self._close_task is task:
            self._close_task = None

    async def _send_session_update(self) -> None:
        """Configure a realtime session with input audio transcription enabled.

        Also called by :class:`ReconnectingWebSocket` on transparent
        reconnects, so reset local buffer-tracking state here — the
        server-side ``input_audio_buffer`` is empty on a fresh socket.
        """
        assert self._ws is not None
        self._reset_logical_turn_state()
        transcription: dict[str, Any] = {"model": self._config.model}
        if self._config.language:
            transcription["language"] = self._config.language
        if self._config.delay:
            transcription["delay"] = self._config.delay
        session_update: dict[str, Any] = {
            "type": "session.update",
            "session": {
                "type": "realtime" if self._config.connection_model else "transcription",
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

    async def _on_reconnect(self) -> None:
        """Contain the dropped socket's turn before configuring its replacement."""
        if self._partial_text:
            self._emit_event(STTEvent(type=STTEventType.FINAL, text=self._partial_text))
        if self._final_received is not None:
            self._final_received.set()
        self._reset_logical_turn_state()
        await self._send_session_update()

    async def _on_audio(self, chunk: AudioChunk) -> None:
        if self._ws is not None:
            data = self._audio_resampler.process(
                chunk.data,
                chunk.format.sample_rate,
            )
            await self._append_audio(data)

    async def _on_commit_segment(self) -> bool:
        return await self._send_commit(wait_for_final=False)

    def pending_commit_bytes(self) -> int | None:
        """Bytes appended to the input buffer since the last commit.

        Implements :class:`~easycat.providers.PendingCommitReporter` so the
        session journal can record *why* a segment commit was accepted or
        skipped (OpenAI Realtime refuses commits below a 100 ms floor).
        """
        return self._bytes_since_last_commit

    async def _on_end(self) -> None:
        await self._flush_audio_resampler()
        if self._ws is not None and self._audio_pending_commit:
            committed = await self._send_commit(wait_for_final=True)
            if not committed and self._audio_pending_commit:  # noqa: SIM102 nested branches preserve decision context
                if not await self._clear_input_buffer():
                    self._final_wait_timed_out = True
        elif self._commit_pending and self._final_received is not None:
            # ``commit_segment()`` deliberately does not wait, but a direct
            # caller may end the logical stream before its completed event
            # arrives. Wait here so a late prior-turn final cannot enter the
            # replacement event queue on the next start.
            await self._await_final(self._final_received)

        self._final_received = None
        self._session_ready = None
        if self._persistent_enabled() and not self._final_wait_timed_out:
            self._partial_text = ""
            return
        try:
            # OpenAI keeps the realtime socket open after delivering the
            # final transcript, so draining first would block in the receive
            # loop until the close timeout fires.  Close-before-drain wakes
            # the receive loop, keeping turn-to-agent latency low.
            await self._close_active_websocket(close_before_drain=True)
        finally:
            self._reset_logical_turn_state()

    async def _append_audio(self, data: bytes) -> None:
        if self._ws is None or not data:
            return
        audio_b64 = base64.b64encode(data).decode("ascii")
        await self._ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64,
                }
            )
        )
        self._audio_pending_commit = True
        self._bytes_since_last_commit += len(data)

    async def _flush_audio_resampler(self) -> None:
        await self._append_audio(self._audio_resampler.finish())

    async def _clear_input_buffer(self) -> bool:
        """Discard an uncommittable short tail before reusing the session."""
        cleared = await self._send_json_control(
            {"type": "input_audio_buffer.clear"},
            label="OpenAI input audio buffer clear",
        )
        if cleared:
            self._audio_pending_commit = False
            self._bytes_since_last_commit = 0
        return cleared

    # OpenAI Realtime requires commits to have at least 100ms of audio.
    # At 24 kHz mono 16-bit that is 4800 bytes.  Skip the commit when
    # the pending tail is shorter than this — the server would reject
    # it anyway and we'd surface a spurious warning plus leave the
    # downstream final_received event waiter hanging.
    _COMMIT_MIN_BYTES = _REALTIME_SAMPLE_RATE * 2 // 10  # 100ms of PCM16 mono

    async def _send_commit(self, *, wait_for_final: bool) -> bool:
        ws = self._ws
        if ws is None:
            return False

        # The protocol does not let the client attach an identifier to a
        # commit request. Serialize requests so one completion event can never
        # acknowledge a later logical segment or escape into the next turn.
        if self._commit_pending:
            if self._final_wait_timed_out:
                return False
            previous_final = self._final_received
            if previous_final is None:
                return False
            await self._await_final(previous_final)
            if self._commit_pending:
                return False

        committable_bytes = (
            self._bytes_since_last_commit + self._audio_resampler.pending_output_bytes
        )
        if committable_bytes == 0:
            return False
        if committable_bytes < self._COMMIT_MIN_BYTES:
            # Tail too short — skip the server round-trip (the server
            # would reject the commit and surface a warning).  Keep
            # ``_audio_pending_commit`` and ``_bytes_since_last_commit``
            # intact so a later commit that sees more audio (locally
            # small tail + fresh audio) still reflects the true server
            # buffer and eventually reaches the 100 ms threshold.
            logger.debug(
                "Skipping input_audio_buffer.commit: only %d bytes (<%d min)",
                committable_bytes,
                self._COMMIT_MIN_BYTES,
            )
            return False

        # Preserve the streaming resampler's interpolation state when a short
        # segment is rejected. Once the segment is large enough to commit,
        # append its retained tail before asking the server to finalize it.
        await self._flush_audio_resampler()
        final_received = asyncio.Event()
        self._final_received = final_received
        self._commit_pending = True
        self._final_wait_timed_out = False
        # Each fresh commit starts a clean slate — any stale drop flag
        # from a previous commit (e.g. one whose timed-out ``.completed``
        # never arrived) should not suppress this commit's final.
        self._dropping_pending_final = False
        try:
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception:
            logger.debug("Error sending input_audio_buffer.commit", exc_info=True)
            self._commit_pending = False
            if self._final_received is final_received:
                self._final_received = None
            return False

        self._audio_pending_commit = False
        self._bytes_since_last_commit = 0
        if wait_for_final:
            await self._await_final(final_received)
        return True

    async def _await_final(self, final_received: asyncio.Event) -> None:
        """Bound the final wait and mark the socket unsafe after a timeout."""
        timeout_s = self._config.final_transcript_timeout_s
        try:
            await asyncio.wait_for(final_received.wait(), timeout=timeout_s)
        except TimeoutError:
            # Give up on OpenAI's final and promote whatever streamed through
            # deltas. Persistent mode drops this socket afterward: even if a
            # fallback was available, a late completion must never race into
            # the next turn's replacement event queue.
            self._final_wait_timed_out = True
            logger.warning(
                "Timed out after %.1fs waiting for OpenAI Realtime final; "
                "promoting %d-char partial and reconnecting",
                timeout_s,
                len(self._partial_text),
            )
            if self._partial_text:
                self._emit_event(STTEvent(type=STTEventType.FINAL, text=self._partial_text))
                self._partial_text = ""
                self._dropping_pending_final = True

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
            self._commit_pending = False
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
            if msg_type in ("session.updated", "transcription_session.updated"):  # noqa: SIM102 nested branches preserve decision context
                if self._session_ready is not None and not self._session_ready.done():
                    self._session_ready.set_result(None)

    async def aclose(self) -> None:
        """Close a persistent Realtime socket during Session teardown."""
        try:
            await self._drain_scheduled_close()
            await self._close_active_websocket(close_before_drain=True)
            self._session_ready = None
            self._reset_logical_turn_state()
        finally:
            await self._close_owned_runtime_scope_if_idle()

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "openai-realtime",
            "model": self._config.model,
            "api_version": "v1",
            "sdk_version": get_package_version("websockets"),
        }
