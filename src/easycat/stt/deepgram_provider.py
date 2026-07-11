"""Deepgram streaming STT provider — real-time WebSocket transcription."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from easycat._audio_utils import resample_chunk
from easycat._provider_helpers import get_package_version, word_timestamps_from_words
from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.stt.websocket_base import WebSocketSTTBase

logger = logging.getLogger(__name__)


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

    api_key: str = ""
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
    # Bound the wait for the Finalize-triggered result before falling back to
    # the latest interim transcript and reconnecting on the next turn.
    final_transcript_timeout_s: float = 2.0
    # Optional WebSocket factory override for testing.
    # Signature: async (url, **kwargs) -> connection
    ws_connect: Any = field(default=None, repr=False)
    # Optional EventBus for reconnect observability
    event_bus: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        requested_persistence = self.persistent_ws
        if requested_persistence is None:
            self.persistent_ws = not self.is_flux
        if requested_persistence is True and self.is_flux:
            raise ValueError(
                "Deepgram persistent_ws=True is not supported for Flux; "
                "the v2 endpoint does not support Finalize"
            )
        if self.keepalive_interval_s <= 0:
            raise ValueError("Deepgram keepalive_interval_s must be positive")
        if self.final_transcript_timeout_s <= 0:
            raise ValueError("Deepgram final_transcript_timeout_s must be positive")

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
            dynamic_event_queue=True,
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

    def _persistent_enabled(self) -> bool:
        return bool(self._config.persistent_ws and not self._config.is_flux)

    async def warmup(self) -> None:
        """Best-effort establish the reusable Nova socket before user audio."""
        if not self._persistent_enabled():
            return
        try:
            async with self._lifecycle_lock:
                await self._ensure_persistent_connection()
        except Exception as exc:
            logger.debug("Deepgram STT warmup skipped: %s", exc)
            await self._discard_connection()

    async def _on_start(self) -> None:
        self._partial_text = ""
        self._final_received = None
        self._final_wait_sequence = None
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
        )

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
            # Closing the stale receive loop terminates the queue that was
            # current at that instant. Start/re-warm on a fresh queue so its
            # sentinel cannot make the replacement stream appear exhausted.
            self._event_queue = asyncio.Queue()
        await self._connect_new_websocket()
        self._ensure_keepalive_task()

    def _ensure_keepalive_task(self) -> None:
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

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
        task = self._keepalive_task
        self._keepalive_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _discard_connection(self) -> None:
        await self._cancel_keepalive()
        await self._close_active_websocket(close_before_drain=True)
        self._pending_finalizes.clear()
        # A discarded socket cannot deliver any more results. Treat its audio
        # epochs as closed so only fresh audio on the replacement connection
        # needs a future Finalize.
        self._finalized_epoch = self._audio_epoch

    async def _on_audio(self, chunk: AudioChunk) -> None:
        if chunk.format.sample_rate != self._config.sample_rate:
            chunk = resample_chunk(chunk, self._config.sample_rate)
        await self._send_ws(chunk.data)
        self._audio_epoch += 1

    async def _on_commit_segment(self) -> bool:
        # Flux uses provider-side EndOfTurn endpointing, so an explicit
        # Finalize control message is unnecessary (and unsupported on the
        # v2 endpoint); keep returning the base ``False`` for Flux models.
        if self._config.is_flux:
            return False
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
                self._promote_partial_to_final()
                await self._discard_connection()
                return
            try:
                await asyncio.wait_for(
                    final_received.wait(), timeout=self._config.final_transcript_timeout_s
                )
            except TimeoutError:
                logger.warning(
                    "Timed out after %.1fs waiting for Deepgram Finalize; "
                    "promoting %d-char interim transcript and reconnecting",
                    self._config.final_transcript_timeout_s,
                    len(self._partial_text),
                )
                self._promote_partial_to_final()
                # Prevent a late result from this turn entering the next turn's
                # replacement queue. The next start reconnects transparently.
                await self._discard_connection()
                return
            finally:
                if self._final_received is final_received:
                    self._final_received = None
                    self._final_wait_sequence = None

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
        """Close a persistent socket during Session teardown."""
        await self._cancel_keepalive()
        if self._ws is not None:
            await self._send_json_control({"type": "CloseStream"}, label="CloseStream")
        await self._close_active_websocket(close_before_drain=True)

    def _handle_json_message(self, msg: dict[str, Any]) -> None:
        # Deepgram may acknowledge Finalize with a bare
        # ``{"from_finalize": true}`` control frame: advance lifecycle state
        # before any type/channel/transcript guards. A Results-shaped ack then
        # continues below so its transcript is still emitted normally.
        if msg.get("from_finalize") is True:
            self._handle_finalize_ack()
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

    def _handle_finalize_ack(self) -> None:
        """Advance the oldest pending Finalize and release its matching waiter."""
        if not self._pending_finalizes:
            return
        sequence, finalize_epoch = self._pending_finalizes.popleft()
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
                    "channels": str(self._config.channels),
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
