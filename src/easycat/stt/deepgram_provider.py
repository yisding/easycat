"""Deepgram streaming STT provider — real-time WebSocket transcription."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from easycat._audio_utils import PCM16StreamResampler
from easycat._provider_helpers import get_package_version, word_timestamps_from_words
from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.runtime.scope import RuntimeScope
from easycat.stt.base import _STT_RECEIVE_FINISH_POLICY, _STT_RUNTIME_CANCEL_POLICY
from easycat.stt.websocket_base import _RECEIVE_TASK, WebSocketSTTBase

logger = logging.getLogger(__name__)

_KEEPALIVE_TASK = "deepgram_keepalive"


@dataclass
class DeepgramSTTConfig:
    """Configuration for the Deepgram STT provider.

    .. note::

       ``api_key`` defaults to ``""`` to support the inject-the-key-later
       workflow (e.g. constructing the config first and assigning the key
       before use).  A missing key is therefore *not* validated at
       construction time — it surfaces on the first live request rather
       than eagerly.  The :func:`easycat.stt.factory` path still
       fail-fasts on an empty key.
    """

    api_key: str = field(default="", repr=False)
    model: str = "nova-2"
    language: str = "en"
    encoding: str = "linear16"
    sample_rate: int = 16000
    channels: int = 1
    punctuate: bool = True
    interim_results: bool = True
    smart_format: bool = False
    base_url: str = "wss://api.deepgram.com/v1/listen"
    # Reuse one Nova WebSocket across turns by default. ``None`` selects the
    # model-aware default: enabled for v1/Nova and disabled for Flux, whose v2
    # endpoint does not support the explicit Finalize control message needed
    # to delimit EasyCat-managed turns without closing the connection.
    persistent_ws: bool | None = None
    # Deepgram closes an idle streaming socket after 10 seconds. Its documented
    # recommendation is one text-frame KeepAlive every 3-5 seconds.
    keepalive_interval_s: float = 4.0
    # Keep best-effort session startup bounded independently of the socket's
    # reconnect/backoff policy. A timed-out attempt is discarded and first use
    # retries through the normal stream lifecycle.
    warmup_timeout_s: float = 5.0
    # Bound the wait for the Finalize-triggered result. On timeout the socket
    # is discarded (draining any result already buffered in the close window)
    # and the latest interim transcript is promoted only if no final arrived
    # during that drain; the next turn reconnects transparently.
    final_transcript_timeout_s: float = 2.0
    # Optional WebSocket factory override for testing.
    # Signature: async (url, **kwargs) -> connection
    ws_connect: Any = field(default=None, repr=False)
    # Optional EventBus for reconnect observability
    event_bus: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        normalized_encoding = (
            self.encoding.strip().lower() if isinstance(self.encoding, str) else ""
        )
        if normalized_encoding != "linear16":
            raise ValueError(
                f"Unsupported Deepgram STT encoding: {self.encoding!r}. "
                "EasyCat's streaming STT path sends linear16 PCM; other "
                "encodings require a matching encoder."
            )
        self.encoding = normalized_encoding
        requested_persistence = self.persistent_ws
        if requested_persistence is None:
            self.persistent_ws = not self.is_flux
        if requested_persistence is True and self.is_flux:
            raise ValueError(
                "Deepgram persistent_ws=True is not supported for Flux; "
                "the v2 endpoint does not support Finalize"
            )
        for name in (
            "keepalive_interval_s",
            "warmup_timeout_s",
            "final_transcript_timeout_s",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"Deepgram {name} must be positive and finite")

    @property
    def is_flux(self) -> bool:
        """Whether this config uses a Flux model with provider-side endpointing."""
        return self.model.lower().startswith("flux")


class DeepgramSTT(WebSocketSTTBase):
    """Real-time streaming STT using Deepgram WebSocket API.

    Nova keeps one warmed WebSocket across logical turns by default, delimiting
    each turn with ``Finalize`` and sending Deepgram ``KeepAlive`` text frames
    while idle. Flux and explicit ``persistent_ws=False`` configurations keep
    the one-socket-per-turn lifecycle.
    """

    def __init__(self, config: DeepgramSTTConfig) -> None:
        # Like the realtime STT providers, accept any upstream PCM rate and
        # resample to the configured ``sample_rate`` in ``_on_audio`` rather
        # than rejecting mismatches. ``expected_sample_rate`` is left as
        # ``None`` so the base validator only enforces PCM encoding.
        super().__init__(
            provider_name="deepgram_stt",
            provider_error_name="deepgram",
            expected_sample_rate=None,
            close_timeout=5.0,
            dynamic_event_queue=bool(config.persistent_ws and not config.is_flux),
        )
        self._config = config
        self._keepalive_task: asyncio.Task[None] | None = None
        self._final_received: asyncio.Event | None = None
        self._partial_text = ""
        self._audio_epoch = 0
        self._finalize_seq = 0
        self._pending_finalizes: deque[tuple[int, int]] = deque()
        self._finalized_epoch = 0
        self._final_wait_sequence: int | None = None
        # Set when a bare ``{"from_finalize": true}`` ack (no Results body)
        # confirmed a Finalize that covered unflushed audio: the transcript
        # for that audio has not arrived yet, so the socket must not be
        # reused by the next turn until it is contained.
        self._bare_finalize_ack_pending = False
        self._audio_resampler = PCM16StreamResampler(config.sample_rate)

    def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach provider work, preserving a pre-warmed keepalive task."""
        current = self._runtime_scope
        keepalive = self._keepalive_task
        if (
            current is None
            or current.parent is parent
            or not self._owns_runtime_scope
            or keepalive is None
            or keepalive.done()
        ):
            super().set_runtime_scope(parent, name=name)
            return

        receive = self._receive_task
        current_tasks = current.tasks()
        if keepalive not in current_tasks or (
            receive is not None and not receive.done() and receive not in current_tasks
        ):
            raise RuntimeError("Cannot reattach unowned Deepgram runtime work")

        task_members = [
            (_KEEPALIVE_TASK, keepalive, _STT_RUNTIME_CANCEL_POLICY),
        ]
        if receive is not None and receive in current_tasks:
            task_members.insert(0, (_RECEIVE_TASK, receive, _STT_RECEIVE_FINISH_POLICY))
        movable_tasks = {task for _, task, _ in task_members}
        if any(task not in movable_tasks for task in current_tasks):
            raise RuntimeError("Cannot reattach active Deepgram runtime work")

        # Validate loop affinity before registering the child so an off-loop
        # caller cannot leave a duplicate child behind after a failed attach.
        running_loop = asyncio.get_running_loop()
        if any(task.get_loop() is not running_loop for task in movable_tasks):
            raise RuntimeError("Cannot reattach Deepgram runtime work from another event loop")
        attached = parent.create_child(name)
        added: list[asyncio.Task[None]] = []
        try:
            for task_name, task, policy in task_members:
                attached.add_task(task_name, task, policy=policy)
                added.append(task)
        except BaseException:
            for task in added:
                attached.discard(task)
            raise
        for task in added:
            current.discard(task)
        self._runtime_scope = attached
        self._owns_runtime_scope = False

    def _persistent_enabled(self) -> bool:
        return bool(self._config.persistent_ws and not self._config.is_flux)

    async def warmup(self) -> None:
        """Best-effort establish the reusable Nova socket before user audio."""
        if not self._persistent_enabled():
            return
        async with self._lifecycle_lock:
            try:
                await asyncio.wait_for(
                    self._ensure_persistent_connection(),
                    timeout=self._config.warmup_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                logger.debug("Deepgram STT warmup skipped: %s", exc)
                # Keep cleanup serialized with a concurrently queued first
                # stream so it cannot close that stream's replacement socket.
                await self._discard_connection()

    async def _on_start(self) -> None:
        self._audio_resampler.reset()
        self._partial_text = ""
        self._final_received = None
        self._final_wait_sequence = None
        self._bare_finalize_ack_pending = False
        if self._persistent_enabled():
            await self._ensure_persistent_connection()
            return
        self._pending_finalizes.clear()
        await self._connect_new_websocket()

    async def _connect_new_websocket(self) -> None:
        url = self._build_url()
        headers = {"Authorization": f"Token {self._config.api_key}"}
        await self._connect_websocket(
            url=url,
            headers=headers,
            event_bus=self._config.event_bus,
            connect_fn=self._config.ws_connect,
            on_reconnect=self._on_persistent_reconnect,
        )

    async def _on_persistent_reconnect(self) -> None:
        """Contain unfinalized audio when a Deepgram socket reconnects.

        Deepgram's ``from_finalize`` acknowledgements carry no request id and
        are scoped to one physical WebSocket.  A Finalize sent before a drop
        can therefore never acknowledge audio on the replacement connection.
        Do not let that stale ledger suppress the replacement connection's
        next Finalize; close the old audio epoch locally and promote its last
        partial, if any, into a boundary final.
        """
        had_unfinalized_audio = (
            self._audio_epoch > self._finalized_epoch
            or bool(self._pending_finalizes)
            or self._bare_finalize_ack_pending
        )
        self._audio_resampler.reset()
        self._pending_finalizes.clear()
        self._final_wait_sequence = None
        self._bare_finalize_ack_pending = False
        self._finalized_epoch = self._audio_epoch

        if had_unfinalized_audio:
            logger.warning(
                "Deepgram reconnected with unfinalized audio; containing the "
                "prior socket epoch before continuing"
            )
            self._promote_partial_to_final()

        # ``end_stream`` may be waiting on a Finalize sent through the dead
        # socket.  Wake it after the ledger reset so it can either complete
        # (when no replacement-epoch audio was sent) or issue a fresh
        # Finalize for audio that arrived after reconnect.
        if self._final_received is not None:
            self._final_received.set()

    async def _ensure_persistent_connection(self) -> None:
        ws = self._ws
        if (
            ws is not None
            and ws.is_connected
            and self._receive_task is not None
            and not self._receive_task.done()
        ):
            self._ensure_keepalive_task()
            return
        if ws is not None:
            await self._discard_connection()
            # Draining the stale socket may parse leftover frames (or, when
            # the loop died on its own, a terminal sentinel) into the queue
            # that was current at that instant. Start/re-warm on a fresh
            # queue so stale events cannot pollute the replacement stream.
            self._event_queue = asyncio.Queue()
        await self._connect_new_websocket()
        self._ensure_keepalive_task()

    def _ensure_keepalive_task(self) -> None:
        task = self._keepalive_task
        if task is not None and not task.done():
            return
        scope = self._ensure_runtime_scope()
        if task is not None:
            scope.discard(task)
        self._keepalive_task = scope.create_task(
            _KEEPALIVE_TASK,
            self._keepalive_loop(),
            task_name=_KEEPALIVE_TASK,
            policy=_STT_RUNTIME_CANCEL_POLICY,
        )

    async def _keepalive_loop(self) -> None:
        try:
            while self._ws is not None:
                await asyncio.sleep(self._config.keepalive_interval_s)
                # Audio itself keeps an active stream alive. Send the
                # application-level text control only between logical turns.
                if self._running or self._ws is None:
                    continue
                if not await self._send_json_control(
                    {"type": "KeepAlive"}, label="Deepgram KeepAlive"
                ):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Deepgram KeepAlive loop stopped", exc_info=True)

    async def _cancel_keepalive(self) -> None:
        self._keepalive_task = None
        scope = self._runtime_scope
        if scope is None:
            return
        await scope.cancel_and_drain(_KEEPALIVE_TASK)

    async def _discard_connection(self) -> None:
        self._audio_resampler.reset()
        await self._cancel_keepalive()
        # Frames already buffered on the closing socket are still parsed while
        # the receive loop drains, so a late Finalize-triggered final is
        # emitted into the current turn's queue. Suppress the loop's terminal
        # sentinel during this deliberate discard so an event emitted after
        # the drain (e.g. a promoted interim) cannot land behind it.
        self._suppress_terminal_sentinel = True
        try:
            await self._close_active_websocket(close_before_drain=True)
        finally:
            self._suppress_terminal_sentinel = False
        self._pending_finalizes.clear()
        self._bare_finalize_ack_pending = False
        # A discarded socket cannot deliver any more results. Treat its audio
        # epochs as closed so only fresh audio on the replacement connection
        # needs a future Finalize.
        self._finalized_epoch = self._audio_epoch

    async def _on_audio(self, chunk: AudioChunk) -> None:
        await self._append_audio(
            self._audio_resampler.process(chunk.data, chunk.format.sample_rate)
        )

    async def _append_audio(self, data: bytes) -> None:
        if not data:
            return
        await self._send_ws(data)
        self._audio_epoch += 1

    async def _flush_audio_resampler(self) -> None:
        await self._append_audio(self._audio_resampler.finish())

    async def _on_commit_segment(self) -> bool:
        # Flux uses provider-side EndOfTurn endpointing, so an explicit
        # Finalize control message is unnecessary (and unsupported on the
        # v2 endpoint); keep returning the base ``False`` for Flux models.
        if self._config.is_flux:
            return False
        await self._flush_audio_resampler()
        return await self._send_finalize() is not None

    async def _send_finalize(self, *, wait_for_ack: bool = False) -> int | None:
        # Deepgram acknowledgments carry no request identifier. Keep at most
        # one Finalize in flight so a later waiter cannot mistake which audio
        # epoch a bare ``from_finalize`` frame covers.
        if self._pending_finalizes:
            sequence, _ = self._pending_finalizes[-1]
            if wait_for_ack:
                self._final_wait_sequence = sequence
            return sequence

        # Reserve the sequence before sending: the receive loop can process a
        # very fast acknowledgment while ``ws.send`` is still yielding.
        self._finalize_seq += 1
        sequence = self._finalize_seq
        self._pending_finalizes.append((sequence, self._audio_epoch))
        if wait_for_ack:
            self._final_wait_sequence = sequence
        sent = await self._send_json_control({"type": "Finalize"}, label="Deepgram Finalize")
        if not sent:
            if self._pending_finalizes and self._pending_finalizes[-1][0] == sequence:
                self._pending_finalizes.pop()
            if self._final_wait_sequence == sequence:
                self._final_wait_sequence = None
            return None
        return sequence

    async def _on_end(self) -> None:
        await self._flush_audio_resampler()
        if self._persistent_enabled():
            await self._finish_persistent_turn()
            return
        if self._ws is not None:
            await self._send_json_control({"type": "CloseStream"}, label="CloseStream")

        await self._close_active_websocket()

    async def _finish_persistent_turn(self) -> None:
        # A pause-triggered commit may already have finalized every audio frame.
        # In that common case there is nothing to flush; keep the socket warm.
        while self._audio_epoch > self._finalized_epoch:
            final_received = asyncio.Event()
            self._final_received = final_received
            wait_sequence = await self._send_finalize(wait_for_ack=True)
            if wait_sequence is None:
                self._final_received = None
                await self._contain_unflushed_turn()
                return
            try:
                await asyncio.wait_for(
                    final_received.wait(), timeout=self._config.final_transcript_timeout_s
                )
            except TimeoutError:
                logger.warning(
                    "Timed out after %.1fs waiting for Deepgram Finalize; "
                    "draining the stale socket and reconnecting next turn",
                    self._config.final_transcript_timeout_s,
                )
                await self._contain_unflushed_turn()
                return
            finally:
                if self._final_received is final_received:
                    self._final_received = None
                    self._final_wait_sequence = None
        if self._bare_finalize_ack_pending:
            # A bare ``{"from_finalize": true}`` ack confirmed this turn's
            # audio without carrying its transcript, and Deepgram acks have no
            # request id. A warm socket could deliver that transcript into the
            # next turn's queue (or into this turn's closed queue, silently
            # losing it). Contain it exactly like a Finalize timeout.
            logger.warning(
                "Deepgram acknowledged Finalize without a transcript; draining "
                "the socket and reconnecting so the late final cannot cross "
                "the turn boundary"
            )
            await self._contain_unflushed_turn()

    async def _contain_unflushed_turn(self) -> None:
        """Discard the socket first, then emit whichever transcript survived.

        Frames already buffered on the closing socket are parsed while
        ``_discard_connection`` drains the receive loop (with the terminal
        sentinel suppressed), so a Finalize-triggered final that arrives
        inside the close window is emitted normally into this turn's queue
        and clears the interim. Promoting afterwards therefore yields exactly
        one FINAL for the turn: the real drained final when it made it, the
        latest interim otherwise — never both.
        """
        await self._discard_connection()
        self._promote_partial_to_final()

    def _promote_partial_to_final(self) -> None:
        """Emit the latest interim transcript when Finalize cannot complete."""
        if not self._partial_text:
            return
        self._emit_event(
            STTEvent(
                type=STTEventType.FINAL,
                text=self._partial_text,
                language=self._config.language,
            )
        )
        self._partial_text = ""

    async def aclose(self) -> None:
        """End any logical stream, then release the persistent socket."""
        try:
            # ``close_if_supported`` prefers ``aclose`` over ``close``.  Do
            # not bypass STTBase.close(): doing so left an active logical
            # stream marked running after its socket had been torn down,
            # making a later start_stream() fail as "already started".
            await super().close()
        finally:
            try:
                await self._cancel_keepalive()
                if self._ws is not None:
                    await self._send_json_control({"type": "CloseStream"}, label="CloseStream")
                await self._close_active_websocket(close_before_drain=True)
            finally:
                await self._close_owned_runtime_scope_if_idle()

    def _handle_json_message(self, msg: dict[str, Any]) -> None:
        # Deepgram may acknowledge Finalize with a bare
        # ``{"from_finalize": true}`` control frame: advance lifecycle state
        # before any type/channel/transcript guards. A Results-shaped ack then
        # continues below so its transcript is still emitted normally.
        if msg.get("from_finalize") is True:
            self._handle_finalize_ack(results_shaped=msg.get("type") == "Results")
        msg_type = msg.get("type", "")
        if msg_type == "Error":
            # Deepgram error frames carry the human-readable text under
            # ``description`` (and sometimes ``message``); the ``description``
            # is the more descriptive field, so surface it via
            # ``override_message`` so it wins over the generic ``message``.
            self._emit_provider_error_from_message(
                msg,
                override_message=msg.get("description"),
                default_message="Deepgram STT error",
            )
            return
        if self._config.is_flux:
            self._handle_flux_message(msg_type, msg)
            return
        if msg_type == "Results":
            self._handle_results_message(msg)

    def _handle_finalize_ack(self, *, results_shaped: bool) -> None:
        """Advance the oldest pending Finalize and release its matching waiter."""
        if results_shaped and self._bare_finalize_ack_pending:
            # Acks are FIFO on one socket, so this Results frame is the late
            # transcript for the Finalize that a bare ack already confirmed.
            # Resolve that bare ack instead of consuming the entry for a
            # still-outstanding Finalize.
            self._bare_finalize_ack_pending = False
            return
        if not self._pending_finalizes:
            return
        sequence, finalize_epoch = self._pending_finalizes.popleft()
        if not results_shaped and finalize_epoch > self._finalized_epoch:
            # A bare ack (no Results body) confirmed audio whose transcript
            # has not arrived. ``_finish_persistent_turn`` must not keep this
            # socket warm: the real ``from_finalize`` Results frame may still
            # be in flight and would bleed into the next turn's stream.
            self._bare_finalize_ack_pending = True
        self._finalized_epoch = max(self._finalized_epoch, finalize_epoch)
        if (
            self._final_received is not None
            and self._final_wait_sequence is not None
            and sequence >= self._final_wait_sequence
        ):
            self._final_received.set()

    def _handle_results_message(self, msg: dict[str, Any]) -> None:
        """Parse one Nova Results frame and advance finalize bookkeeping."""
        channel = msg.get("channel", {})
        alternatives = channel.get("alternatives", [])
        if not alternatives:
            return

        best = alternatives[0]
        transcript = best.get("transcript", "")
        is_final = bool(msg.get("is_final", False))
        if is_final:
            self._partial_text = ""
        elif transcript:
            # Deepgram interim Results replace the current hypothesis rather
            # than append a delta; keep the latest for timeout degradation.
            self._partial_text = transcript

        if not transcript:
            return

        # ``confidence`` and ``word_timestamps`` are provider-captured metadata
        # carried on STTEvent. They are not yet consumed by the pipeline (the
        # session reads only ``text``/``track``); populate them so the data is
        # available to future observability/journal wiring.
        confidence = best.get("confidence")
        word_timestamps = word_timestamps_from_words(best.get("words"))
        event_type = STTEventType.FINAL if is_final else STTEventType.PARTIAL
        self._emit_event(
            STTEvent(
                type=event_type,
                text=transcript,
                confidence=confidence,
                language=self._config.language,
                word_timestamps=word_timestamps,
            )
        )

    def _handle_flux_message(self, msg_type: str, msg: dict[str, Any]) -> None:
        if msg_type != "TurnInfo":
            return

        transcript = msg.get("transcript", "")
        if not transcript:
            return

        turn_event = msg.get("event", "")
        if turn_event == "EndOfTurn":
            event_type = STTEventType.FINAL
            confidence = msg.get("end_of_turn_confidence")
        else:
            event_type = STTEventType.PARTIAL
            confidence = None

        self._emit_event(
            STTEvent(
                type=event_type,
                text=transcript,
                confidence=confidence,
                language=self._config.language,
                word_timestamps=word_timestamps_from_words(msg.get("words")),
            )
        )

    def _build_url(self) -> str:
        params = {
            "model": self._config.model,
            "encoding": self._config.encoding,
            "sample_rate": str(self._config.sample_rate),
        }
        if self._config.is_flux:
            base_url = _flux_base_url(self._config.base_url)
        else:
            base_url = self._config.base_url
            params.update(
                {
                    "language": self._config.language,
                    # WebSocketSTTBase normalizes streaming payloads to mono.
                    "channels": "1",
                    "punctuate": str(self._config.punctuate).lower(),
                    "interim_results": str(self._config.interim_results).lower(),
                    "smart_format": str(self._config.smart_format).lower(),
                }
            )
        return f"{base_url}?{urlencode(params)}"

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "deepgram",
            "model": self._config.model,
            "api_version": "v1",
            "sdk_version": get_package_version("websockets"),
        }


def _flux_base_url(base_url: str) -> str:
    if base_url.endswith("/v1/listen"):
        return f"{base_url[: -len('/v1/listen')]}/v2/listen"
    return base_url
