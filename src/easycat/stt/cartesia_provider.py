"""Cartesia streaming STT (Ink) WebSocket provider."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from easycat._audio_utils import PCM16StreamResampler
from easycat._provider_helpers import get_package_version, word_timestamps_from_words
from easycat.audio_format import AudioChunk
from easycat.events import STTEvent, STTEventType
from easycat.stt.websocket_base import WebSocketSTTBase

# Models whose endpointing is driven by the volume-gate params
# (``min_volume`` / ``max_silence_duration_secs``).  ``ink-2`` replaces
# that gate with native semantic turn detection and rejects those query
# params, so they are only emitted for ``ink-whisper``.
_VOLUME_GATE_MODELS: frozenset[str] = frozenset({"ink-whisper"})


@dataclass
class CartesiaSTTConfig:
    """Configuration for the Cartesia streaming STT provider.

    Defaults to ``ink-2`` — Cartesia's latest streaming model, which ships
    *built-in semantic turn detection* (its own VAD/endpointing) and emits
    a ``turn.*`` event lifecycle alongside transcripts.  No external VAD is
    required for ink-2.  ``ink-whisper`` (the prior Whisper-based model) is
    still selectable and falls back to the volume-gate endpointing params
    (``min_volume`` / ``max_silence_duration_secs``), which ink-2 ignores.
    """

    api_key: str = field(default="", repr=False)
    # Model is resolved lazily (see ``resolved_model``) when left as ``None``:
    # ``ink-2`` (latest, lowest WER, built-in turn detection) for English,
    # falling back to the multilingual ``ink-whisper`` for non-English —
    # because ink-2 is currently English-only and would otherwise reject a
    # ``language != "en"`` config. Pin ``model`` explicitly to override.
    model: str | None = None
    language: str = "en"
    encoding: str = "pcm_s16le"
    sample_rate: int = 16000
    # VAD threshold (0.0–1.0), ``ink-whisper`` only. Kept at 0.0 so
    # EasyCat's own turn manager owns endpointing decisions; ink-whisper
    # won't close the turn on volume alone at this setting. Ignored by
    # ink-2 (which uses native semantic turn detection instead).
    min_volume: float = 0.0
    # How long of a silence gap ink-whisper waits before emitting a final
    # transcript. 5s is intentionally generous so the turn manager's
    # own silence detection fires first in most cases. ``ink-whisper`` only.
    max_silence_duration_secs: float = 5.0
    cartesia_version: str = "2026-03-01"
    base_url: str = "wss://api.cartesia.ai/stt/websocket"
    # Optional WebSocket factory override for testing.
    # Signature: async (url, **kwargs) -> connection
    ws_connect: Any = field(default=None, repr=False)
    # Optional EventBus for provider-error observability
    event_bus: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        normalized_encoding = (
            self.encoding.strip().lower() if isinstance(self.encoding, str) else ""
        )
        if normalized_encoding != "pcm_s16le":
            raise ValueError(
                f"Unsupported Cartesia STT encoding: {self.encoding!r}. "
                "EasyCat's streaming STT path sends pcm_s16le PCM; other "
                "encodings require a matching encoder."
            )
        self.encoding = normalized_encoding

    @property
    def resolved_model(self) -> str:
        """The model actually used, resolving the language-aware default.

        ``ink-2`` is English-only, so when ``model`` is left unset it is
        chosen for English and ``ink-whisper`` (multilingual) for any other
        language. An explicitly set ``model`` is always honored as-is.
        """
        if self.model is not None:
            return self.model
        return "ink-2" if self.language.lower().startswith("en") else "ink-whisper"

    @property
    def uses_volume_gate(self) -> bool:
        """True when the model endpoints via the volume-gate params.

        ``ink-2`` uses native semantic turn detection and rejects the
        volume-gate query params, so they must be omitted for it.
        """
        return self.resolved_model in _VOLUME_GATE_MODELS


class CartesiaSTT(WebSocketSTTBase):
    """Real-time streaming STT using Cartesia's Ink WebSocket API.

    Opens a WebSocket on :meth:`start_stream`, forwards audio as binary
    frames, and parses ``transcript`` messages (partial + final) in a
    background receive loop. A ``finalize`` control message flushes the
    buffered audio mid-stream (used by
    :meth:`~easycat.stt.base.STTBase.commit_segment`); a ``done``
    control message closes the session cleanly.

    With the default ``ink-2`` model, Cartesia also emits a ``turn.*``
    lifecycle (``turn.start`` / ``turn.update`` / ``turn.eager_end`` /
    ``turn.resume`` / ``turn.end``) from its built-in semantic turn
    detection. These are acknowledged but not required by EasyCat: the
    transcript ``is_final`` flag still drives FINAL emission, so the model's
    built-in endpointing flows through the normal partial/final path without
    a separate VAD stage.
    """

    def __init__(self, config: CartesiaSTTConfig) -> None:
        # Like the other bundled streaming STT providers, accept any upstream
        # PCM rate and resample to the configured ``sample_rate`` in
        # ``_on_audio`` rather than rejecting mismatches. ``expected_sample_rate``
        # is left as ``None`` so the base validator only enforces PCM encoding
        # and callers can swap providers without crashing.
        super().__init__(
            provider_name="cartesia_stt",
            provider_error_name="cartesia",
            expected_sample_rate=None,
            close_timeout=5.0,
        )
        self._config = config
        self._audio_resampler = PCM16StreamResampler(config.sample_rate)
        self._audio_epoch = 0
        self._finalized_epoch = 0
        self._latest_partial: STTEvent | None = None

    async def _on_start(self) -> None:
        self._audio_resampler.reset()
        self._audio_epoch = 0
        self._finalized_epoch = 0
        self._latest_partial = None
        url = self._build_url()
        headers = {
            "X-API-Key": self._config.api_key,
            "Cartesia-Version": self._config.cartesia_version,
        }
        await self._connect_websocket(
            url=url,
            headers=headers,
            event_bus=self._config.event_bus,
            connect_fn=self._config.ws_connect,
            on_reconnect=self._on_reconnect,
        )

    async def _on_reconnect(self) -> None:
        """Close the dropped socket's transcript boundary before resuming."""
        partial = self._latest_partial
        if partial is not None:
            self._emit_event(
                STTEvent(
                    type=STTEventType.FINAL,
                    text=partial.text,
                    confidence=partial.confidence,
                    language=partial.language,
                    word_timestamps=partial.word_timestamps,
                    ends_turn=False,
                )
            )
        self._audio_resampler.reset()
        self._latest_partial = None
        self._finalized_epoch = self._audio_epoch

    async def _on_audio(self, chunk: AudioChunk) -> None:
        await self._prepare_and_send_audio(
            lambda: self._audio_resampler.process(chunk.data, chunk.format.sample_rate)
        )

    async def _prepare_and_send_audio(self, prepare: Callable[[], bytes]) -> None:
        ws = self._ws
        if ws is None:
            return

        sent = await ws.send_prepared(lambda: prepare() or None)
        if sent:
            self._audio_epoch += 1

    async def _append_audio(self, data: bytes) -> None:
        if data:
            await self._send_ws(data)
            # A send fenced behind reconnect belongs to the replacement
            # socket, not the socket whose reconnect callback is currently
            # closing its transcript boundary. Count it only after the send
            # succeeds so that callback cannot finalize a future epoch.
            self._audio_epoch += 1

    async def _flush_audio_resampler(self) -> None:
        await self._prepare_and_send_audio(self._audio_resampler.finish)

    async def _on_commit_segment(self) -> bool:
        await self._flush_audio_resampler()
        # Cartesia's client commands are raw *text* messages, not JSON
        # envelopes: ``finalize`` flushes buffered audio mid-session and the
        # server acks with ``{"type": "flush_done"}``.  A JSON
        # ``{"type": "finalize"}`` frame is not that command, so the server
        # never flushed while ``commit_segment()`` still reported success —
        # mid-turn segment finalization was silently inert (gh 1065).
        return await self._send_text_control("finalize", label="Cartesia finalize")

    async def _on_end(self) -> None:
        if self._ws is not None:
            await self._flush_audio_resampler()
            # Cartesia closes the session itself after acking ``close``, so
            # declare that close expected before sending: ``recv_iter`` would
            # otherwise read it as a transient drop, open a replacement socket
            # and wait out the whole close timeout on every turn (gh 1066).
            self._ws.expect_peer_close()
            # ``close`` is the end-of-session command: it flushes the
            # remaining audio, closes the session, and is acked with
            # ``{"type": "done"}``.  ``done`` is the *server's* ack keyword
            # and was never a valid client command, so the tail of the
            # utterance went untranscribed and the drain waited out its whole
            # timeout for an ack that could not arrive (gh 1065).
            await self._send_text_control("close", label="Cartesia close")

        await self._close_active_websocket()

    def _handle_json_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        if msg_type == "transcript":
            self._handle_transcript(msg)
        elif msg_type == "error":
            self._emit_provider_error_from_message(msg, default_message="Cartesia STT error")
        # ``flush_done`` / ``done`` and the ink-2 ``turn.*`` lifecycle are
        # acks/signals — nothing to do; transcripts carry the text and the
        # ``is_final`` flag that drive STT events.

    def _handle_transcript(self, msg: dict[str, Any]) -> None:
        text = msg.get("text", "")
        if not text:
            return

        is_final = bool(msg.get("is_final"))
        word_timestamps = word_timestamps_from_words(msg.get("words"))
        event = STTEvent(
            type=STTEventType.FINAL if is_final else STTEventType.PARTIAL,
            text=text,
            confidence=msg.get("confidence"),
            language=msg.get("language") or self._config.language,
            word_timestamps=word_timestamps,
        )
        if is_final:
            self._latest_partial = None
            self._finalized_epoch = self._audio_epoch
        else:
            self._latest_partial = event
        self._emit_event(event)

    def _build_url(self) -> str:
        params = {
            "model": self._config.resolved_model,
            "language": self._config.language,
            "encoding": self._config.encoding,
            "sample_rate": str(self._config.sample_rate),
        }
        # ``min_volume`` / ``max_silence_duration_secs`` configure the
        # volume-gate endpointing used by ink-whisper. ink-2 endpoints via
        # native semantic turn detection and rejects these params, so they
        # are only sent for the volume-gate models.
        if self._config.uses_volume_gate:
            params["min_volume"] = str(self._config.min_volume)
            params["max_silence_duration_secs"] = str(self._config.max_silence_duration_secs)
        return f"{self._config.base_url}?{urlencode(params)}"

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "cartesia",
            "model": self._config.resolved_model,
            "api_version": self._config.cartesia_version,
            "sdk_version": get_package_version("websockets"),
        }
