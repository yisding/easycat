"""Cartesia TTS (Sonic) WebSocket provider."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import uuid4

from easycat._provider_helpers import get_package_version
from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.events import TTSEvent
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.tts._multi_context_ws import (
    MultiContextAdapter,
    MultiContextWSManager,
    _Context,
    validate_context_queue_maxsize,
)
from easycat.tts._ws_base import _WSTTSBase
from easycat.tts.base import _AudioConversionState
from easycat.tts.input import TTSInput, coerce_tts_input

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

    # Name of the model field on this config — read by ``parse_tts_string``
    # to know that ``"cartesia/<model>"`` shortcuts populate ``model_id``
    # rather than the conventional ``model``.
    MODEL_FIELD: ClassVar[str] = "model_id"

    api_key: str = field(default="", repr=False)
    # Sonic-3 is the default — best quality/latency balance (~90ms TTFA).
    # Use ``sonic-3.5`` for Cartesia's latest, highest-naturalness profile,
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
    # Sonic-3/3.5 generation controls, sent under ``generation_config`` only
    # when set. ``speed`` ∈ [0.6, 1.5], ``volume`` ∈ [0.5, 2.0], ``emotion``
    # is a named mood ("neutral", "angry", "excited", "sad", …). ``None``
    # leaves the model default in place.
    speed: float | None = None
    volume: float | None = None
    emotion: str | None = None
    output_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_24K)
    event_bus: object | None = None
    # Reconnect tuning for the synthesis WebSocket. Defaults match
    # ReconnectConfig; lower max_retries to fail fast and defer to
    # turn-level retry, or raise it for flaky links.
    reconnect_max_retries: int = 3
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 30.0
    # Persistent multi-context socket. Default ``True`` keeps one WebSocket
    # warm across turns and removes connection setup from reply latency. Each
    # utterance is scoped by a fresh context_id, and barge-in cancels just the
    # context (falling back to a full socket close) rather than tearing the
    # socket down. Set ``False`` to restore the one-shot-per-synthesize path.
    # Accepted tradeoff: a mid-stream reconnect replays the context from the
    # top (audible repetition). Socket warmth between turns
    # relies on WebSocket-level ping/pong; after a very long idle gap the socket
    # may be closed server-side and is transparently reconnected on the next
    # utterance.
    persistent_ws: bool = True
    # Keep best-effort session startup bounded even when synthesis itself uses
    # unlimited reconnects. A timed-out attempt is discarded and first use
    # retries with the normal synthesis policy.
    warmup_timeout_s: float = 5.0
    # Bounded per-context queue for the persistent demux reader.
    context_queue_maxsize: int = 256

    def __post_init__(self) -> None:
        validate_context_queue_maxsize(self.context_queue_maxsize, provider="Cartesia")
        if self.encoding not in _ENCODING_SAMPLE_WIDTH:
            supported = ", ".join(sorted(_ENCODING_SAMPLE_WIDTH))
            raise ValueError(
                f"Unsupported Cartesia encoding: {self.encoding!r}. "
                f"Only PCM encodings are supported in v1: {supported}. "
                "μ-law / float32 support is tracked separately."
            )
        if self.speed is not None and not 0.6 <= self.speed <= 1.5:
            raise ValueError(f"Cartesia speed must be in [0.6, 1.5], got {self.speed}")
        if self.volume is not None and not 0.5 <= self.volume <= 2.0:
            raise ValueError(f"Cartesia volume must be in [0.5, 2.0], got {self.volume}")
        if (
            isinstance(self.warmup_timeout_s, bool)
            or not isinstance(self.warmup_timeout_s, int | float)
            or not math.isfinite(self.warmup_timeout_s)
            or self.warmup_timeout_s <= 0
        ):
            raise ValueError("Cartesia warmup_timeout_s must be a finite number > 0")


class CartesiaTTS(_WSTTSBase):
    """TTS provider using Cartesia's Sonic WebSocket API.

    By default one multi-context WebSocket is warmed at session startup and
    reused across turns. Set ``persistent_ws=False`` to open one connection
    per :meth:`synthesize` call. Synthesis requests are sent as JSON frames and
    audio arrives in base64-encoded ``chunk`` messages.
    """

    _provider_error_name = "cartesia"
    _provider_log_label = "Cartesia"

    def __init__(self, config: CartesiaTTSConfig) -> None:
        super().__init__(output_format=config.output_format)
        self._config = config
        self._source_format = AudioFormat(
            sample_rate=config.sample_rate,
            channels=1,
            sample_width=_ENCODING_SAMPLE_WIDTH[config.encoding],
        )
        self._context_id: str | None = None
        # The synthesis request frame for the in-flight utterance, replayed
        # by the on_reconnect hook so a mid-stream drop restarts the utterance
        # from the top instead of aborting it. Known tradeoff: Cartesia
        # synthesis is one-shot, so any audio already emitted before the drop
        # is re-emitted after the restart (audible repetition), not a seamless
        # resume.
        self._pending_request: str | None = None
        self._persistent_audio_states: dict[str, _AudioConversionState] = {}

    def _create_ws(self) -> ReconnectingWebSocket:
        return self._build_ws(self._replay_request)

    def _build_ws(self, on_reconnect) -> ReconnectingWebSocket:
        return ReconnectingWebSocket(
            url=self._config.base_url,
            config=ReconnectConfig(
                extra_headers={
                    "X-API-Key": self._config.api_key,
                    "Cartesia-Version": self._config.cartesia_version,
                },
                max_retries=self._config.reconnect_max_retries,
                base_delay=self._config.reconnect_base_delay,
                max_delay=self._config.reconnect_max_delay,
            ),
            event_bus=self._config.event_bus,
            provider_name="cartesia_tts",
            on_reconnect=on_reconnect,
        )

    def _get_mgr(self) -> MultiContextWSManager:
        """Build the persistent multi-context manager on first use."""
        if self._mgr is None:
            adapter = MultiContextAdapter(
                # The socket is built with the MANAGER's reconnect hook (which
                # replays every live context) instead of the single-context
                # _replay_request, so the two replay paths do not collide.
                connect_factory=lambda on_reconnect: self._build_ws(on_reconnect),
                parse_frame=self._parse_frame,
                route_key=self._route_key,
                context_cancel_frames=lambda ctx_id: [
                    json.dumps({"context_id": ctx_id, "cancel": True})
                ],
                on_context_replay=self._reset_persistent_audio_alignment,
                socket_close_frames=list,
                on_global_frame=self._on_global_frame,
                context_queue_maxsize=self._config.context_queue_maxsize,
            )
            self._mgr = self._make_multi_context_manager(adapter)
        return self._mgr

    async def warmup(self) -> None:
        """Best-effort connect the persistent socket before the first reply."""
        if not self._persistent_enabled():
            return
        try:
            await asyncio.wait_for(
                self._get_mgr().warmup(),
                timeout=self._config.warmup_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            # Startup warmup is an optimization, not a new availability gate.
            # The manager clears a failed socket so synthesize() can retry.
            logger.debug("Cartesia TTS warmup skipped: %s", exc)

    @staticmethod
    def _route_key(parsed: Any) -> str | None:
        return parsed.get("context_id") if isinstance(parsed, dict) else None

    def _on_global_frame(self, parsed: Any) -> None:
        if isinstance(parsed, dict) and parsed.get("type") == "error":
            self._emit_provider_error_from_msg(parsed)

    def _reset_persistent_audio_alignment(self, context_id: str) -> None:
        state = self._persistent_audio_states.get(context_id)
        if state is not None:
            self._reset_audio_alignment(state=state)

    def _discard_persistent_audio_state(self, context_id: str) -> None:
        state = self._persistent_audio_states.pop(context_id, None)
        if state is not None:
            self._reset_audio_alignment(state=state)

    def _decode_message(
        self,
        msg: dict[str, Any],
        *,
        state: _AudioConversionState | None = None,
    ) -> tuple[list[TTSEvent], bool]:
        """Decode one parsed Cartesia message into (events, is_terminal).

        Single source of the chunk/timestamps/done/error wire decoding, shared
        by the one-shot ``synthesize`` loop and the persistent path so they
        cannot drift. Emits a provider error internally for ``error`` frames.
        """
        msg_type = msg.get("type")
        if msg_type == "chunk":
            events: list[TTSEvent] = []
            data_b64 = msg.get("data")
            if data_b64:
                audio_bytes = base64.b64decode(data_b64)
                if audio_bytes:
                    event = self._make_audio_event(
                        audio_bytes,
                        self._source_format,
                        state=state,
                    )
                    if event is not None:
                        events.append(event)
            return events, bool(msg.get("done"))
        if msg_type == "timestamps":
            word_ts = msg.get("word_timestamps")
            return ([self._make_markers_event([word_ts])] if word_ts else []), False
        if msg_type == "done":
            return [], True
        if msg_type == "error":
            self._emit_provider_error_from_msg(msg)
            return [], True
        return [], False

    async def _replay_request(self) -> None:
        """Re-send the in-flight synthesis request after a reconnect.

        Cartesia synthesis is one-shot: the request frame carries the full
        transcript, so replaying it restarts the utterance from the top on a
        fresh socket — it does NOT resume from the drop point. Any audio
        already emitted before the drop is re-emitted, producing audible
        repetition; this is an accepted tradeoff of stateless one-shot replay
        in exchange for not aborting the utterance entirely. Without this hook
        a transient drop would re-raise out of recv_iter and abort the
        utterance.
        """
        ws = self._ws
        request = self._pending_request
        if ws is None or request is None or self._cancelled:
            return
        # The replayed stream restarts from the top and is sample-aligned in
        # its own right, so drop any sub-sample byte held from before the drop
        # to avoid shifting every replayed sample by one byte.
        self._reset_audio_alignment()
        await ws.send(request)

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
        # Sonic-3/3.5 speed/volume/emotion controls travel under
        # ``generation_config``; only include the keys the caller set so the
        # model default applies otherwise.
        generation_config = {
            key: value
            for key, value in (
                ("speed", self._config.speed),
                ("volume", self._config.volume),
                ("emotion", self._config.emotion),
            )
            if value is not None
        }
        if generation_config:
            request["generation_config"] = generation_config
        return request

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        # The default input policy makes the scheduler deliver plain text here.
        text = coerce_tts_input(payload).text
        if self._persistent_enabled():
            # ``async for`` alone does not close a delegated async generator
            # when our caller stops early. Own it so its finally sends the
            # context-scoped remote cancel rather than leaving synthesis live.
            async with contextlib.aclosing(self._synthesize_persistent(text)) as stream:
                async for event in stream:
                    yield event
            return

        # The one-shot stream has its own socket-closing ``finally``. Own the
        # delegated generator just as we do the persistent path: closing this
        # public generator after first audio otherwise abandons that finally
        # and leaves the one-shot connection live until garbage collection.
        async with contextlib.aclosing(self._synthesize_oneshot(text)) as stream:
            async for event in stream:
                yield event

    async def _synthesize_oneshot(self, text: str) -> AsyncGenerator[TTSEvent, None]:
        ws = await self._replace_oneshot_ws(self._create_ws)
        self._start_synthesis()

        context_id = str(uuid4())
        self._context_id = context_id

        request = json.dumps(self._build_request(text, context_id))
        # Leave replay disarmed until the request has actually been sent on a
        # connected stream. ``on_reconnect`` fires for retries during the
        # *initial* connect too, and arming earlier would replay the request
        # before the send below, duplicating the utterance.
        self._pending_request = None
        terminal_received = False

        try:
            await ws.connect()
            await ws.send(request)
            # Request is now live: arm replay so a *mid-stream* reconnect
            # re-sends it and restarts the utterance from the top (see
            # ``_replay_request`` for the duplicate-audio tradeoff).
            self._pending_request = request

            async for message in ws.recv_iter():
                if self._cancelled:
                    break
                msg = self._parse_frame(message)
                if msg is None:
                    continue
                events, terminal = self._decode_message(msg)
                for event in events:
                    yield event
                if terminal:
                    terminal_received = True
                    break
            self._require_terminal_response(
                terminal_received,
                terminal_label="done/error",
            )
            tail = self._finish_audio_event()
            if tail is not None:
                yield tail

        except Exception as exc:
            if not self._cancelled:
                logger.error("Cartesia TTS error: %s", exc)
                self._emit_provider_error(exc)
                raise
        finally:
            try:
                await self._close_ws()
            finally:
                self._context_id = None
                self._pending_request = None
                self._end_synthesis()

    @staticmethod
    def _context_frame(msg: Any, context_id: str) -> dict[str, Any] | None:
        if not isinstance(msg, dict):
            return None
        if msg.get("context_id") not in (None, context_id):
            return None
        return msg

    async def _synthesize_persistent(self, text: str) -> AsyncGenerator[TTSEvent, None]:
        """Synthesize over the shared persistent multi-context socket.

        Decoding is shared with the one-shot path via ``_decode_message``; only
        the transport (a reused socket + per-context queue of already-parsed
        frames) differs, plus the mandatory recv-side context_id guard that
        drops a stray late frame from a prior/cancelled context.
        """
        self._start_synthesis()
        mgr = self._get_mgr()
        # open_context() performs the initial connect when the socket is cold,
        # so it lives INSIDE the guarded block: a failed first connect must
        # still emit the provider error and run _end_synthesis() (clearing
        # is_active), exactly like the one-shot path.
        ctx: _Context | None = None
        audio_state: _AudioConversionState | None = None
        terminal_received = False
        try:
            ctx = await mgr.open_context()
            audio_state = self._new_audio_conversion_state()
            self._persistent_audio_states[ctx.context_id] = audio_state
            await mgr.send(ctx, [json.dumps(self._build_request(text, ctx.context_id))])

            # Frames arrive already parsed (the manager parses once); decode is
            # shared with the one-shot path.
            async for msg in ctx.frames():
                if self._cancelled:
                    break
                # Mandatory recv-side guard: drop any frame whose context_id
                # does not match the active utterance.
                msg = self._context_frame(msg, ctx.context_id)
                if msg is None:
                    continue
                events, terminal = self._decode_message(msg, state=audio_state)
                for event in events:
                    yield event
                if terminal:
                    terminal_received = True
                    break
            # Cancellation is context-scoped on the persistent socket. A
            # successor context can reset the provider-global cancellation
            # flag before this generator unwinds, so use the manager-owned
            # context state when deciding whether delayed resampler output is
            # still deliverable.
            tail = self._finish_audio_event(
                emit=not ctx.cancelled,
                state=audio_state,
            )
            if tail is not None:
                yield tail

        except Exception as exc:
            if not self._cancelled:
                logger.error("Cartesia TTS error: %s", exc)
                self._emit_provider_error(exc)
                raise
        finally:
            if ctx is not None:
                try:
                    await self._finish_persistent_context(mgr, ctx, terminal_received)
                finally:
                    self._discard_persistent_audio_state(ctx.context_id)
            self._end_synthesis()

    async def _finish_persistent_context(
        self,
        mgr: MultiContextWSManager,
        ctx: _Context,
        terminal_received: bool,
    ) -> None:
        """Release a completed context or cancel an abandoned remote stream."""
        if terminal_received or ctx.cancelled or ctx.done.is_set():
            mgr.finish_context(ctx)
            return
        # The consumer can stop early (for example after the first audio chunk).
        # Tell Cartesia to stop that remote context before unregistering it
        # locally, or it keeps producing billed audio as unroutable late frames.
        await mgr.cancel_context(ctx)

    async def stop(self) -> None:
        if self._persistent_enabled():
            await super().stop()
            if self._mgr is not None:
                await self._mgr.cancel_all()
            return
        await super().stop()
        await self._close_ws()

    async def cancel(self) -> None:
        # Mark cancelled BEFORE sending the cancel frame so the receive
        # loop treats any in-flight chunk as discarded.
        was_active = self._active
        await super().cancel()
        if self._persistent_enabled():
            # Context-scoped barge-in: cancel the live context(s) via the
            # manager and keep the shared socket open (the manager falls back to
            # a full socket close if the cancel send fails). Targeting the
            # manager's live contexts — not a shared self._current_ctx field the
            # synthesize task's finally can null underneath us — avoids a
            # cross-task race where the cancel frame is never sent.
            if self._mgr is not None:
                await self._mgr.cancel_all()
            return
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

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "cartesia",
            "model": self._config.model_id,
            "api_version": self._config.cartesia_version,
            "sdk_version": get_package_version("websockets"),
        }
