"""ElevenLabs TTS provider implementation."""

from __future__ import annotations

import base64
import enum
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

from easycat._provider_helpers import get_package_version
from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.events import TTSEvent
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.tts._multi_context_ws import MultiContextAdapter, MultiContextWSManager, _Context
from easycat.tts._ws_base import _WSTTSBase
from easycat.tts.input import TTSInput, coerce_tts_input

logger = logging.getLogger(__name__)


class ElevenLabsStreamMode(enum.StrEnum):
    """Streaming mode for ElevenLabs TTS."""

    HTTP = "http"
    WEBSOCKET = "websocket"


# Map ElevenLabs output_format strings to AudioFormat.
# Only raw PCM formats are supported; compressed formats (mp3, opus, ulaw)
# would require a decoder and must not be silently treated as PCM.
_ELEVENLABS_FORMAT_MAP: dict[str, AudioFormat] = {
    "pcm_16000": AudioFormat(sample_rate=16000, channels=1, sample_width=2),
    "pcm_22050": AudioFormat(sample_rate=22050, channels=1, sample_width=2),
    "pcm_24000": AudioFormat(sample_rate=24000, channels=1, sample_width=2),
    "pcm_44100": AudioFormat(sample_rate=44100, channels=1, sample_width=2),
}


@dataclass
class ElevenLabsTTSConfig:
    """Configuration for the ElevenLabs TTS provider."""

    # Name of the model field on this config — read by ``parse_tts_string``
    # to know that ``"elevenlabs/<model>"`` shortcuts populate ``model_id``
    # rather than the conventional ``model``.
    MODEL_FIELD: ClassVar[str] = "model_id"

    api_key: str = ""
    voice_id: str = "EXAVITQu4vr4xnSDxMaL"  # Sarah (default)
    # ``eleven_monolingual_v1`` / ``eleven_multilingual_v1`` are deprecated
    # and rejected on newer accounts (free tier refuses with 1008 policy
    # violation).  ``eleven_flash_v2_5`` is the low-latency option
    # ElevenLabs recommends for voice bots today — keeps first-byte
    # latency near 75ms, so it stays the default. ``eleven_v3`` is the
    # latest flagship (richest expressivity) but carries higher latency and
    # is *not* recommended for realtime/conversational use.
    model_id: str = "eleven_flash_v2_5"
    stability: float = 0.5
    similarity_boost: float = 0.75
    # Style exaggeration (0.0–1.0). 0.0 (default) is fastest and most stable;
    # higher values add expressivity at some latency/stability cost and are
    # ignored by models that don't support style (e.g. eleven_flash_v2_5).
    style: float = 0.0
    # Boost similarity to the original speaker. ElevenLabs' own default.
    use_speaker_boost: bool = True
    output_format: str = "pcm_24000"
    # Controls spelling-out of numbers, dates, currency, etc.
    # "auto" (default) lets the model decide, "on" forces normalization,
    # "off" disables it. Note: "on" requires an Enterprise plan for
    # ``eleven_flash_v2_5``.
    apply_text_normalization: str = "auto"
    stream_mode: ElevenLabsStreamMode = ElevenLabsStreamMode.WEBSOCKET
    base_url: str = "https://api.elevenlabs.io/v1"
    ws_base_url: str = "wss://api.elevenlabs.io/v1"
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_24K)
    event_bus: object | None = None
    # Reconnect tuning for the synthesis WebSocket. Defaults match
    # ReconnectConfig; lower max_retries to fail fast and defer to
    # turn-level retry, or raise it for flaky links.
    reconnect_max_retries: int = 3
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 30.0
    # Persistent multi-context socket policy (WEBSOCKET mode only). ``None``
    # selects the latency-oriented mode default: enabled for WebSocket and
    # disabled for HTTP. When enabled one ``/multi-stream-input`` socket is
    # warmed at session startup and reused across turns, each utterance scoped
    # by a fresh context_id, with
    # context-scoped barge-in (``close_context``). Socket warmth between turns
    # relies on WebSocket-level ping/pong; after a very long idle gap exceeding
    # the per-context ``inactivity_timeout`` the socket may be closed
    # server-side and is transparently reconnected on the next utterance.
    persistent_ws: bool | None = None
    # Surfaced as the ``inactivity_timeout`` query param of
    # ``/multi-stream-input`` (seconds): the per-context server-side idle
    # timeout. Only used when ``persistent_ws=True``.
    inactivity_timeout: int = 20
    # Bounded per-context queue for the persistent demux reader.
    context_queue_maxsize: int = 256

    def __post_init__(self) -> None:
        if self.persistent_ws is None:
            self.persistent_ws = self.stream_mode == ElevenLabsStreamMode.WEBSOCKET
        if self.output_format not in _ELEVENLABS_FORMAT_MAP:
            supported = ", ".join(sorted(_ELEVENLABS_FORMAT_MAP))
            raise ValueError(
                f"Unsupported ElevenLabs output_format: {self.output_format!r}. "
                f"Only PCM formats are supported: {supported}. "
                f"Non-PCM formats (mp3, opus, etc.) would require a decoder."
            )
        if self.apply_text_normalization not in {"auto", "on", "off"}:
            raise ValueError(
                "ElevenLabs apply_text_normalization must be 'auto', 'on', or 'off', "
                f"got {self.apply_text_normalization!r}"
            )
        if not 0.0 <= self.style <= 1.0:
            raise ValueError(f"ElevenLabs style must be in [0.0, 1.0], got {self.style}")
        if self.persistent_ws and self.stream_mode == ElevenLabsStreamMode.HTTP:
            raise ValueError(
                "ElevenLabs persistent_ws=True requires stream_mode=WEBSOCKET; "
                "persistence is meaningless on the HTTP path."
            )
        # Fail fast on the documented /multi-stream-input inactivity_timeout
        # range (1–180s) when opting into the persistent path, rather than
        # deferring to a runtime API rejection.
        if self.persistent_ws and not 1 <= self.inactivity_timeout <= 180:
            raise ValueError(
                "ElevenLabs inactivity_timeout must be in [1, 180] seconds for "
                f"persistent_ws=True, got {self.inactivity_timeout}"
            )


class ElevenLabsTTS(_WSTTSBase):
    """TTS provider using ElevenLabs API.

    Supports two streaming modes:
    - HTTP: Chunked transfer encoding via the streaming endpoint
    - WebSocket: Real-time streaming via WebSocket using ReconnectingWebSocket

    Requests PCM output format directly from ElevenLabs to avoid MP3 decoding.
    """

    _provider_error_name = "elevenlabs"
    _provider_log_label = "ElevenLabs"

    def __init__(self, config: ElevenLabsTTSConfig) -> None:
        super().__init__(output_format=config.audio_format)
        self._config = config
        self._source_format = _ELEVENLABS_FORMAT_MAP[config.output_format]
        self._client: httpx.AsyncClient | None = None
        self._response: httpx.Response | None = None
        # Init/text/EOS frames for the in-flight utterance, replayed by the
        # on_reconnect hook so a mid-stream drop restarts the utterance from
        # the top instead of aborting it. Known tradeoff: replaying the full
        # init+text+EOS sequence restarts synthesis, so any audio already
        # emitted before the drop is re-emitted (audible repetition), not a
        # seamless resume.
        self._pending_messages: tuple[str, ...] | None = None

    def _voice_settings(self) -> dict[str, float | bool]:
        """The voice_settings payload shared by the HTTP and WebSocket paths."""
        return {
            "stability": self._config.stability,
            "similarity_boost": self._config.similarity_boost,
            "style": self._config.style,
            "use_speaker_boost": self._config.use_speaker_boost,
        }

    def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                headers={
                    "xi-api-key": self._config.api_key,
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    async def warmup(self) -> None:
        """Prime the selected transport without requesting synthesized audio."""
        if self._config.stream_mode == ElevenLabsStreamMode.HTTP:
            await self._warmup_http()
        elif self._persistent_enabled():
            await self._warmup_persistent_ws()

    async def _warmup_http(self) -> None:
        """Best-effort warm DNS/TLS/keep-alive against the configured voice."""
        try:
            response = await self._get_http_client().get(f"/voices/{self._config.voice_id}")
            await response.aclose()
        except Exception as exc:
            logger.debug("ElevenLabs HTTP TTS warmup skipped: %s", exc)

    async def _warmup_persistent_ws(self) -> None:
        """Best-effort connect the shared multi-stream socket before traffic."""
        manager = self._get_mgr()
        context: _Context | None = None
        try:
            # open_context() lazily establishes the socket. No request frame is
            # sent, so this performs only the authenticated WebSocket handshake.
            context = await manager.open_context()
        except Exception as exc:
            logger.debug("ElevenLabs WebSocket TTS warmup skipped: %s", exc)
        finally:
            if context is not None:
                manager.finish_context(context)

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        """Synthesize text using the configured streaming mode.

        SSML is not supported (``supports_ssml`` is ``False``), so the
        scheduler always delivers a plain-text payload here.
        """
        text = coerce_tts_input(payload).text
        if self._config.stream_mode == ElevenLabsStreamMode.WEBSOCKET:
            async for event in self._synthesize_ws(text):
                yield event
        else:
            async for event in self._synthesize_http(text):
                yield event

    async def _synthesize_http(self, text: str) -> AsyncIterator[TTSEvent]:
        """Synthesize via HTTP chunked transfer encoding."""
        self._start_synthesis()
        client = self._get_http_client()

        try:
            request_body = {
                "text": text,
                "model_id": self._config.model_id,
                "voice_settings": self._voice_settings(),
                "apply_text_normalization": self._config.apply_text_normalization,
            }

            url = f"/text-to-speech/{self._config.voice_id}/stream"
            params = {"output_format": self._config.output_format}

            async with client.stream(
                "POST",
                url,
                json=request_body,
                params=params,
            ) as response:
                self._response = response
                response.raise_for_status()

                async for chunk in response.aiter_bytes(chunk_size=4800):
                    if self._cancelled:
                        break
                    if chunk:
                        yield self._make_audio_event(chunk, self._source_format)

        except httpx.HTTPStatusError as exc:
            logger.error(
                "ElevenLabs TTS API error: %s %s",
                exc.response.status_code,
                exc.response.text,
            )
            self._emit_provider_error(
                exc, http_status=exc.response.status_code, body=exc.response.text[:400]
            )
            raise
        except httpx.HTTPError as exc:
            if not self._cancelled:
                logger.error("ElevenLabs TTS HTTP error: %s", exc)
                self._emit_provider_error(exc)
                raise
        finally:
            self._response = None
            self._end_synthesis()

    async def _synthesize_ws(self, text: str) -> AsyncIterator[TTSEvent]:
        """Synthesize via WebSocket streaming (one-shot or persistent)."""
        if self._persistent_enabled():
            async for event in self._synthesize_ws_persistent(text):
                yield event
        else:
            async for event in self._synthesize_ws_oneshot(text):
                yield event

    async def _synthesize_ws_oneshot(self, text: str) -> AsyncIterator[TTSEvent]:
        """Explicit one-shot WebSocket path: fresh socket per synthesize call."""
        self._start_synthesis()

        try:
            ws = await self._start_ws_stream(text)

            # Receive audio chunks; decode is shared with the persistent path.
            async for message in ws.recv_iter():
                if self._cancelled:
                    break
                data = self._parse_frame(message)
                if data is None:
                    continue
                events, terminal = self._decode_message(data)
                for event in events:
                    yield event
                if terminal:
                    break

        except Exception as exc:
            if not self._cancelled:
                logger.error("ElevenLabs TTS WebSocket error: %s", exc)
                # WebSocket close codes (e.g. 1008 "policy violation" for
                # deprecated models on free tier) go in so replaying a
                # bundle shows the server's rejection reason, not just
                # "synthesis failed".
                close_code = getattr(exc, "code", None)
                self._emit_provider_error(exc, ws_close_code=close_code)
                raise
        finally:
            # Single idempotent teardown covers every exit path, including
            # CancelledError (BaseException) which skips the except above.
            await self._close_ws()
            self._pending_messages = None
            self._end_synthesis()

    async def _start_ws_stream(self, text: str) -> ReconnectingWebSocket:
        """Send the full ElevenLabs stream-init sequence, retrying once on stale sockets."""
        messages = self._build_ws_messages(text)
        # Leave replay disarmed until the request has actually been sent on a
        # connected stream. ``on_reconnect`` fires for retries during the
        # *initial* connect too, and arming earlier would replay the
        # init/text/EOS frames before the sends below, duplicating the utterance.
        self._pending_messages = None
        ws = await self._connect_ws()

        try:
            await self._send_ws_messages(ws, messages)
            # Request is now live: arm replay so a *mid-stream* reconnect
            # re-sends these frames and restarts the utterance from the top
            # (see ``_replay_request`` for the duplicate-audio tradeoff).
            self._pending_messages = messages
            return ws
        except Exception:
            if self._cancelled:
                raise
            await self._close_ws()
            ws = await self._connect_ws()
            await self._send_ws_messages(ws, messages)
            self._pending_messages = messages
            return ws

    def _build_ws_messages(self, text: str) -> tuple[str, str, str]:
        """Build the init, text, and EOS messages for a synthesis request."""
        init_msg = {
            "text": " ",
            "voice_settings": self._voice_settings(),
        }
        return (
            json.dumps(init_msg),
            json.dumps({"text": text}),
            json.dumps({"text": ""}),
        )

    async def _send_ws_messages(
        self,
        ws: ReconnectingWebSocket,
        messages: tuple[str, ...],
    ) -> None:
        """Send a complete synthesis request over the active WebSocket."""
        for message in messages:
            await ws.send(message)

    def _make_ws(self, url: str, on_reconnect) -> ReconnectingWebSocket:
        """Build a ReconnectingWebSocket with the shared auth/retry config.

        Single source of the headers/retry tuning/event-bus/provider-name so the
        one-shot ``/stream-input`` and persistent ``/multi-stream-input`` sockets
        can't drift apart when one is later tuned. Only the URL and reconnect
        hook differ.
        """
        return ReconnectingWebSocket(
            url=url,
            config=ReconnectConfig(
                extra_headers={"xi-api-key": self._config.api_key},
                max_retries=self._config.reconnect_max_retries,
                base_delay=self._config.reconnect_base_delay,
                max_delay=self._config.reconnect_max_delay,
            ),
            event_bus=self._config.event_bus,
            provider_name="elevenlabs_tts",
            on_reconnect=on_reconnect,
        )

    async def _connect_ws(self) -> ReconnectingWebSocket:
        """Create and connect a fresh WebSocket for one synthesis request."""
        ws_url = (
            f"{self._config.ws_base_url}"
            f"/text-to-speech/{self._config.voice_id}"
            f"/stream-input?model_id={self._config.model_id}"
            f"&output_format={self._config.output_format}"
            f"&apply_text_normalization={self._config.apply_text_normalization}"
        )
        self._ws = self._make_ws(ws_url, self._replay_request)
        await self._ws.connect()
        return self._ws

    # ── persistent multi-context path ─────────────────────────────

    def _multi_stream_url(self) -> str:
        return (
            f"{self._config.ws_base_url}"
            f"/text-to-speech/{self._config.voice_id}"
            f"/multi-stream-input?model_id={self._config.model_id}"
            f"&output_format={self._config.output_format}"
            f"&apply_text_normalization={self._config.apply_text_normalization}"
            f"&inactivity_timeout={self._config.inactivity_timeout}"
        )

    def _build_multi_ws(self, on_reconnect) -> ReconnectingWebSocket:
        return self._make_ws(self._multi_stream_url(), on_reconnect)

    def _context_init_frame(self, ctx_id: str) -> str:
        return json.dumps(
            {
                "text": " ",
                "voice_settings": self._voice_settings(),
                "context_id": ctx_id,
            }
        )

    def _get_mgr(self) -> MultiContextWSManager:
        """Build the persistent multi-context manager on first use."""
        if self._mgr is None:
            adapter = MultiContextAdapter(
                connect_factory=lambda on_reconnect: self._build_multi_ws(on_reconnect),
                parse_frame=self._parse_frame,
                route_key=self._route_key,
                context_cancel_frames=lambda ctx_id: [
                    json.dumps({"context_id": ctx_id, "close_context": True})
                ],
                on_context_replay=lambda _ctx_id: self._reset_audio_alignment(),
                socket_close_frames=lambda: [json.dumps({"close_socket": True})],
                on_global_frame=self._on_global_frame,
                context_queue_maxsize=self._config.context_queue_maxsize,
            )
            self._mgr = MultiContextWSManager(adapter)
        return self._mgr

    @staticmethod
    def _route_key(parsed: Any) -> str | None:
        # ElevenLabs responses carry the context id under camelCase ``contextId``
        # (requests use snake_case ``context_id``).
        return parsed.get("contextId") if isinstance(parsed, dict) else None

    def _on_global_frame(self, parsed: Any) -> None:
        if not isinstance(parsed, dict):
            return
        message = parsed.get("message") or parsed.get("error")
        if message:
            self._emit_provider_error(RuntimeError(f"ElevenLabs TTS error: {message}"))

    def _decode_message(self, data: dict[str, Any]) -> tuple[list[TTSEvent], bool]:
        """Decode one parsed ElevenLabs message into (events, is_terminal).

        Shared by the one-shot and persistent paths so their wire decoding
        cannot drift. A context-scoped error frame is surfaced as a provider
        error and ends the turn.
        """
        if data.get("error") or data.get("message"):
            detail = data.get("error") or data.get("message")
            self._emit_provider_error(RuntimeError(f"ElevenLabs TTS error: {detail}"))
            return [], True
        events: list[TTSEvent] = []
        if data.get("audio"):
            audio_bytes = base64.b64decode(data["audio"])
            if audio_bytes:
                events.append(self._make_audio_event(audio_bytes, self._source_format))
        if data.get("alignment"):
            events.append(self._make_markers_event([data["alignment"]]))
        return events, bool(data.get("isFinal"))

    async def _synthesize_ws_persistent(self, text: str) -> AsyncIterator[TTSEvent]:
        """Synthesize over the shared persistent multi-stream-input socket.

        The decode loop body matches the default one-shot WS path; only the
        transport differs, plus the mandatory recv-side contextId guard.
        """
        self._start_synthesis()
        mgr = self._get_mgr()
        # open_context() performs the initial /multi-stream-input connect when
        # the socket is cold, so it lives INSIDE the guarded block: a failed
        # first connect must still emit the provider error and run
        # _end_synthesis() (clearing is_active), like the one-shot path.
        ctx: _Context | None = None
        try:
            ctx = await mgr.open_context()
            pending = [
                self._context_init_frame(ctx.context_id),
                json.dumps({"text": text, "context_id": ctx.context_id}),
                json.dumps({"text": "", "context_id": ctx.context_id}),
            ]
            await mgr.send(ctx, pending)

            async for event in self._decode_persistent_frames(ctx):
                yield event

        except Exception as exc:
            if not self._cancelled:
                logger.error("ElevenLabs TTS WebSocket error: %s", exc)
                close_code = getattr(exc, "code", None)
                self._emit_provider_error(exc, ws_close_code=close_code)
                raise
        finally:
            if ctx is not None:
                mgr.finish_context(ctx)
            self._end_synthesis()

    async def _decode_persistent_frames(self, ctx: _Context) -> AsyncIterator[TTSEvent]:
        """Decode one context's already-parsed frames into TTSEvents.

        Decoding (incl. context-scoped error surfacing) is shared with the
        one-shot path via ``_decode_message``; this adds the mandatory recv-side
        ``contextId`` guard that drops a stray late frame from another context.
        """
        async for data in ctx.frames():
            if self._cancelled:
                return
            if not isinstance(data, dict):
                continue
            # Drop frames for a different context.
            if data.get("contextId") not in (None, ctx.context_id):
                continue
            events, terminal = self._decode_message(data)
            for event in events:
                yield event
            if terminal:
                return

    async def _replay_request(self) -> None:
        """Re-send the init/text/EOS frames after a reconnect.

        ElevenLabs streaming is stateful: the init + text + EOS sequence is
        replayed on the fresh socket, which restarts the utterance from the
        top — it does NOT resume from the drop point. Any audio already
        emitted before the drop is re-emitted, producing audible repetition;
        this is an accepted tradeoff of stateless one-shot replay in exchange
        for not aborting the utterance entirely. Without this hook a transient
        drop would re-raise out of recv_iter and abort the utterance.
        """
        ws = self._ws
        messages = self._pending_messages
        if ws is None or messages is None or self._cancelled:
            return
        # The replayed stream restarts from the top and is sample-aligned in
        # its own right, so drop any sub-sample byte held from before the drop
        # to avoid shifting every replayed sample by one byte.
        self._reset_audio_alignment()
        await self._send_ws_messages(ws, messages)

    async def stop(self) -> None:
        """Gracefully stop synthesis.

        In persistent WebSocket mode the shared socket stays open and only the
        in-flight context is cancelled. Explicit one-shot mode closes its
        socket; HTTP mode closes any active response.
        """
        await super().stop()
        if self._persistent_enabled():
            if self._mgr is not None:
                await self._mgr.cancel_all()
            return
        if self._response is not None:
            await self._response.aclose()
        await self._close_ws()

    async def cancel(self) -> None:
        """Immediately cancel synthesis and close connections."""
        await super().cancel()
        if self._persistent_enabled():
            # Context-scoped barge-in: close the live context(s) via the manager
            # (keeping the shared socket open). Targeting the manager's live
            # contexts rather than a shared self._current_ctx field the
            # synthesize task's finally can null underneath us avoids a
            # cross-task race where the cancel frame is never sent.
            if self._mgr is not None:
                await self._mgr.cancel_all()
            return
        resp = self._response
        self._response = None
        if resp is not None:
            await resp.aclose()
        await self._close_ws()

    async def close(self) -> None:
        """Close all underlying connections."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        # Base close() closes the WebSocket and drains the Error-emit tasks.
        await super().close()

    def version_info(self) -> dict[str, str]:
        # Report the transport library the active mode actually uses:
        # WEBSOCKET streams over a WebSocket, HTTP issues an HTTP request.
        transport_lib = (
            "websockets" if self._config.stream_mode == ElevenLabsStreamMode.WEBSOCKET else "httpx"
        )
        return {
            "provider": "elevenlabs",
            "model": self._config.model_id,
            "api_version": "v1",
            "sdk_version": get_package_version(transport_lib),
        }
