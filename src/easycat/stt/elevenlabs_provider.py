"""ElevenLabs STT provider — realtime WebSocket + batch transcription."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from easycat._audio_utils import PCM16StreamResampler
from easycat._numeric import is_finite_number
from easycat._provider_helpers import get_package_version, word_timestamps_from_words
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import STTEvent, STTEventType
from easycat.stt.base import (
    DEFAULT_MAX_AUDIO_BUFFER_BYTES,
    DEFAULT_MAX_AUDIO_CHUNK_BYTES,
    DEFAULT_MAX_AUDIO_DURATION_MS,
    STTBase,
)
from easycat.stt.websocket_base import WebSocketSTTBase

logger = logging.getLogger(__name__)

# How long to wait for ``committed_transcript`` after an end-of-turn
# commit before we give up and promote the most recent
# ``partial_transcript`` to a FINAL, so a stalled provider final does not
# leave the turn with zero transcript.  Surfaced as
# ``ElevenLabsSTTConfig.final_transcript_timeout_s`` for tuning; this
# module constant is the field's default and remains the monkeypatch
# seam used by the provider tests.
_FINAL_TRANSCRIPT_TIMEOUT_S = 5.0


@dataclass(slots=True)
class _PendingManualCommit:
    """One manual commit awaiting its FIFO committed-transcript response."""

    final_received: asyncio.Event
    drop_late_final: bool = False
    final_lost_on_reconnect: bool = False


@dataclass
class ElevenLabsSTTConfig:
    """Configuration for the ElevenLabs STT provider.

    .. note::

       ``api_key`` defaults to ``""`` to support the inject-the-key-later
       workflow (e.g. constructing the config first and assigning the key
       before use).  A missing key is therefore *not* validated at
       construction time — it surfaces on the first live request rather
       than eagerly.  The :func:`easycat.stt.factory` path still
       fail-fasts on an empty key.
    """

    api_key: str = field(default="", repr=False)
    model: str | None = None
    language: str | None = None
    mode: str = "realtime"  # "realtime" or "batch"
    base_url: str = "https://api.elevenlabs.io/v1"
    ws_url: str = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    timeout: float = 30.0
    # Total attempt count for the batch ``/speech-to-text`` request. Retries
    # transient 429s and transport/timeout errors with exponential backoff.
    # ``0`` is clamped to a single attempt.
    max_retries: int = 3
    realtime_sample_rate: int = 16000
    # Endpointing strategy for the realtime model. ``"vad"`` (default) uses
    # Scribe v2 Realtime's *built-in* voice-activity detection to commit
    # segments automatically on detected silence — no external VAD needed.
    # ``"manual"`` defers every commit to EasyCat's turn manager.
    realtime_commit_strategy: str = "vad"  # "vad" or "manual"
    # Bounded wait (seconds) for ElevenLabs' ``committed_transcript`` after an
    # end-of-turn commit before promoting the most recent partial to FINAL.
    # Defaults to ``_FINAL_TRANSCRIPT_TIMEOUT_S`` (read at construction time)
    # so the provider tests can still monkeypatch that module constant.
    final_transcript_timeout_s: float = field(default_factory=lambda: _FINAL_TRANSCRIPT_TIMEOUT_S)
    realtime_include_timestamps: bool = False
    # Built-in VAD tuning, applied only when ``realtime_commit_strategy``
    # is ``"vad"``. ``None`` leaves the server default in place
    # (vad_threshold≈0.4, vad_silence_threshold_secs≈1.5,
    # min_speech_duration_ms≈100, min_silence_duration_ms≈100).
    realtime_vad_threshold: float | None = None
    realtime_vad_silence_threshold_secs: float | None = None
    realtime_min_speech_duration_ms: int | None = None
    realtime_min_silence_duration_ms: int | None = None
    # Bias Scribe v2 Realtime toward domain vocabulary (names, jargon).
    # Up to 50 terms of up to 20 characters each; sent as repeated query
    # params. ``None`` sends none.
    realtime_keyterms: list[str] | None = None
    # Strip filler words, false starts, and disfluencies from transcripts.
    realtime_no_verbatim: bool = False
    # Include the detected language code on committed transcripts (realtime).
    # ElevenLabs only carries language_code on committed_transcript_with_
    # timestamps events, so enabling this forces include_timestamps on too.
    realtime_include_language_detection: bool = False
    # Zero-retention mode: when False, ElevenLabs does not store request audio
    # or transcripts (history/abuse logging off). Sent as a query param on both
    # the realtime WebSocket and the batch /speech-to-text request. ``True``
    # keeps the server default.
    enable_logging: bool = True
    max_audio_chunk_bytes: int | None = DEFAULT_MAX_AUDIO_CHUNK_BYTES
    max_audio_buffer_bytes: int | None = DEFAULT_MAX_AUDIO_BUFFER_BYTES
    max_audio_duration_ms: float | None = DEFAULT_MAX_AUDIO_DURATION_MS
    # Optional overrides for testing
    http_client: httpx.AsyncClient | None = field(default=None, repr=False)
    ws_connect: Any = field(default=None, repr=False)
    event_bus: Any = field(default=None, repr=False)

    @property
    def resolved_model(self) -> str:
        """Return the model selected by the explicit value and operating mode."""
        if self.model is not None:
            return self.model
        if self.mode == "realtime":
            return "scribe_v2_realtime"
        return "scribe_v1"

    def __post_init__(self) -> None:
        normalized_mode = self.mode.strip().lower() if isinstance(self.mode, str) else ""
        if normalized_mode not in {"realtime", "batch"}:
            raise ValueError(
                f"ElevenLabsSTTConfig.mode must be 'realtime' or 'batch' (got {self.mode!r})"
            )
        self.mode = normalized_mode

        normalized_commit_strategy = (
            self.realtime_commit_strategy.strip().lower()
            if isinstance(self.realtime_commit_strategy, str)
            else ""
        )
        if normalized_commit_strategy not in {"vad", "manual"}:
            raise ValueError(
                "ElevenLabsSTTConfig.realtime_commit_strategy must be 'vad' or 'manual' "
                f"(got {self.realtime_commit_strategy!r})"
            )
        self.realtime_commit_strategy = normalized_commit_strategy

        if (
            not is_finite_number(self.final_transcript_timeout_s)
            or self.final_transcript_timeout_s <= 0
        ):
            raise ValueError(
                "ElevenLabsSTTConfig.final_transcript_timeout_s must be a finite positive number"
            )
        self.final_transcript_timeout_s = float(self.final_transcript_timeout_s)

        if self.max_retries < 0:
            raise ValueError(
                "ElevenLabsSTTConfig.max_retries must be >= 0 "
                f"(got {self.max_retries}); it is the total attempt count, "
                "where 0 is clamped to a single attempt"
            )
        STTBase._validate_positive_limit(
            "ElevenLabsSTTConfig.max_audio_chunk_bytes", self.max_audio_chunk_bytes
        )
        STTBase._validate_positive_limit(
            "ElevenLabsSTTConfig.max_audio_buffer_bytes", self.max_audio_buffer_bytes
        )
        STTBase._validate_positive_limit(
            "ElevenLabsSTTConfig.max_audio_duration_ms", self.max_audio_duration_ms
        )
        if self.realtime_keyterms is not None:
            if len(self.realtime_keyterms) > 50:
                raise ValueError(
                    "ElevenLabsSTTConfig.realtime_keyterms accepts at most 50 terms, "
                    f"got {len(self.realtime_keyterms)}"
                )
            for term in self.realtime_keyterms:
                if len(term) > 20:
                    raise ValueError(
                        "ElevenLabsSTTConfig.realtime_keyterms entries must be <= 20 "
                        f"characters, got {term!r}"
                    )


class ElevenLabsSTT(WebSocketSTTBase):
    """ElevenLabs STT supporting realtime WebSocket and batch modes.

    In **realtime** mode, a WebSocket connection is opened on ``start_stream``
    and audio chunks are forwarded in real time. The default model is
    ``scribe_v2_realtime``, which has *built-in voice-activity detection*;
    with the default ``realtime_commit_strategy="vad"`` the server commits
    transcript segments automatically on detected silence, so no external
    VAD is required. Set ``realtime_commit_strategy="manual"`` to defer all
    commits to EasyCat's turn manager instead.

    In **batch** mode, audio is buffered internally and submitted as a single
    HTTP request when ``end_stream`` is called.
    """

    def __init__(self, config: ElevenLabsSTTConfig) -> None:
        super().__init__(
            provider_name="elevenlabs_stt",
            provider_error_name="elevenlabs",
        )
        self._config = config
        # Batch mode can emit a cap-triggered HTTP transcription from
        # _on_audio; keep that operation under the lifecycle lock so
        # end_stream/start_stream cannot replace its event queue mid-emit.
        self._allow_end_during_audio_send = config.mode == "realtime"
        # Batch mode state
        self._buffer = bytearray()
        self._audio_format: AudioFormat | None = None
        # Realtime mode state
        self._final_received: asyncio.Event | None = None
        self._audio_pending_commit: bool = False
        # Monotonic count of realtime audio chunks streamed this session, and
        # the count already acknowledged committed. A committed_transcript can
        # arrive *after* newer audio has been streamed (manual commit_segment()
        # followed by more send_audio(), or VAD auto-commit while speech
        # resumes), so the pending flag is cleared only when the commit
        # actually covers the latest audio — never unconditionally.
        self._audio_epoch: int = 0
        self._committed_through_epoch: int = 0
        # Audio epoch the server has demonstrably *transcribed* (bounded by the
        # latest partial_transcript). An unsolicited VAD commit covers up to
        # here — not necessarily the newest audio — so trailing audio streamed
        # after the server's commit decision is not treated as committed.
        self._transcribed_through_epoch: int = 0
        # Manual commits (commit_segment / end-of-turn) we've sent and are
        # awaiting an ack for. Distinguishes a manual-commit ack from an
        # unsolicited server VAD commit in the committed_transcript handler.
        self._manual_commit_inflight: int = 0
        # ElevenLabs does not attach a request id to committed transcripts.
        # Manual commit acknowledgements are ordered on one socket, so retain
        # a FIFO waiter for each sent commit instead of letting an older final
        # release a newer end-of-turn waiter.
        self._pending_manual_commits: deque[_PendingManualCommit] = deque()
        # Latest partial transcript text, promoted to a FINAL if the
        # committed transcript stalls past the timeout.
        self._partial_text: str = ""
        # Set when ``_send_commit`` gave up waiting for the committed
        # transcript and already promoted ``_partial_text`` to a FINAL.
        # Causes the first subsequent ``committed_transcript`` to be
        # dropped instead of emitting a second FINAL for the same turn.
        self._dropping_pending_final: bool = False
        self._audio_resampler = PCM16StreamResampler(config.realtime_sample_rate)

    def _resolve_event_bus(self) -> Any | None:
        if self._config.mode == "batch":
            return self._config.event_bus
        return super()._resolve_event_bus()

    def _resolved_model(self) -> str:
        return self._config.resolved_model

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

        # The detected language_code is only delivered on
        # committed_transcript_with_timestamps events, which require
        # include_timestamps=true — so language detection implies timestamps,
        # otherwise the flag would be silently inert (STTEvent.language stays
        # empty). Force timestamps on when language detection is requested.
        include_timestamps = (
            self._config.realtime_include_timestamps
            or self._config.realtime_include_language_detection
        )
        params: dict[str, str] = {
            "model_id": self._resolved_model(),
            "audio_format": self._realtime_audio_format(),
            "commit_strategy": self._config.realtime_commit_strategy,
            "include_timestamps": str(include_timestamps).lower(),
        }
        if self._config.language:
            params["language_code"] = self._config.language
        if self._config.realtime_include_language_detection:
            params["include_language_detection"] = "true"
        # Zero-retention is opt-in; only send the param when disabling logging
        # so a default config keeps the server default (logging on).
        if not self._config.enable_logging:
            params["enable_logging"] = "false"
        # Built-in VAD tuning is only meaningful under the "vad" commit
        # strategy; omit any unset param so the server default applies.
        if self._config.realtime_commit_strategy == "vad":
            vad_params: dict[str, float | int | None] = {
                "vad_threshold": self._config.realtime_vad_threshold,
                "vad_silence_threshold_secs": self._config.realtime_vad_silence_threshold_secs,
                "min_speech_duration_ms": self._config.realtime_min_speech_duration_ms,
                "min_silence_duration_ms": self._config.realtime_min_silence_duration_ms,
            }
            for name, value in vad_params.items():
                if value is not None:
                    params[name] = str(value)
        if self._config.realtime_no_verbatim:
            params["no_verbatim"] = "true"
        # ``keyterms`` is sent as one repeated query param per term, so build
        # the query with ``doseq`` and pass the list through as-is.
        query: dict[str, str | list[str]] = dict(params)
        if self._config.realtime_keyterms:
            query["keyterms"] = self._config.realtime_keyterms
        return f"{self._config.ws_url}?{urlencode(query, doseq=True)}"

    @property
    def mode(self) -> str:
        return self._config.mode

    def _uses_streaming_audio_path(self) -> bool:
        """Apply WebSocket PCM16/mono normalization only in realtime mode."""
        return self._config.mode == "realtime"

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
            self._audio_format = self._latch_uniform_format(
                self._audio_format, chunk, provider_label="ElevenLabs batch STT"
            )
            await self._buffer_batch_audio_or_finalize(
                self._buffer,
                chunk,
                max_chunk_bytes=self._config.max_audio_chunk_bytes,
                max_buffer_bytes=self._config.max_audio_buffer_bytes,
                max_duration_ms=self._config.max_audio_duration_ms,
                provider_label="ElevenLabs batch STT",
                finalize=self._flush_batch_buffer,
            )

    async def _on_end(self) -> None:
        if self._config.mode == "realtime":
            await self._end_realtime()
        else:
            await self._end_batch()

    # -- Realtime (WebSocket) mode -----------------------------------------

    async def _start_realtime(self) -> None:
        headers = {"xi-api-key": self._config.api_key}
        self._audio_resampler.reset()
        self._final_received = None
        self._audio_pending_commit = False
        self._partial_text = ""
        self._dropping_pending_final = False
        self._audio_epoch = 0
        self._committed_through_epoch = 0
        self._transcribed_through_epoch = 0
        self._manual_commit_inflight = 0
        self._pending_manual_commits.clear()
        await self._connect_websocket(
            url=self._build_realtime_ws_url(),
            headers=headers,
            event_bus=self._config.event_bus,
            connect_fn=self._config.ws_connect,
            on_reconnect=self._on_reconnect,
        )

    async def _on_reconnect(self) -> None:
        """Recover local state after a transparent reconnect.

        The ElevenLabs realtime session is fully configured via the
        connection URL's query params (model, audio format, commit
        strategy, language), so nothing needs to be re-sent.  Its mere
        presence flips ``recv_iter`` from "re-raise on drop" to
        "reconnect and keep yielding".  The fresh socket starts with an
        empty server-side audio buffer, so reset the pending-commit flag
        to match.
        """
        had_uncommitted_audio = (
            self._audio_pending_commit
            or self._audio_epoch > self._committed_through_epoch
            or bool(self._pending_manual_commits)
            or self._manual_commit_inflight > 0
        )
        self._audio_resampler.reset()
        self._audio_pending_commit = False
        # Fresh socket: nothing is buffered server-side and any manual commit
        # we were awaiting will never be acked. Wake those waiters so an
        # end-of-turn commit can promote its partial immediately instead of
        # waiting out the full final-transcript timeout. The FIFO records must
        # then be discarded: finals on the fresh socket cannot acknowledge
        # commits from the disconnected one.
        self._committed_through_epoch = self._audio_epoch
        for pending_commit in self._pending_manual_commits:
            pending_commit.final_lost_on_reconnect = True
            pending_commit.final_received.set()
        self._manual_commit_inflight = 0
        self._pending_manual_commits.clear()
        self._final_received = None
        self._dropping_pending_final = False
        if had_uncommitted_audio:
            logger.warning(
                "ElevenLabs reconnected with uncommitted audio; containing the "
                "prior socket epoch before continuing"
            )
            self._promote_partial_to_final()

    async def _send_realtime(self, chunk: AudioChunk) -> None:
        payload_audio = self._audio_resampler.process(
            chunk.data,
            chunk.format.sample_rate,
        )
        await self._append_realtime_audio(payload_audio)

    async def _append_realtime_audio(self, data: bytes) -> None:
        if self._ws is None or not data:
            return
        payload = json.dumps(
            {
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(data).decode(),
                "commit": False,
                "sample_rate": self._config.realtime_sample_rate,
            }
        )
        await self._ws.send(payload)
        self._audio_pending_commit = True
        self._audio_epoch += 1

    async def _flush_audio_resampler(self) -> None:
        await self._append_realtime_audio(self._audio_resampler.finish())

    async def _on_commit_segment(self) -> bool:
        return await self._send_commit(wait_for_final=False)

    async def _end_realtime(self) -> None:
        if self._ws is not None and self._audio_pending_commit:
            await self._send_commit(wait_for_final=True)

        self._final_received = None
        # ElevenLabs keeps the realtime socket open after delivering the
        # committed transcript, so draining first would block in the receive
        # loop until the close timeout fires. Close-before-drain wakes the
        # receive loop, keeping turn-to-agent latency low. Cleanup failures
        # propagate to STTBase so the exact socket remains retry-owned.
        await self._close_active_websocket(close_before_drain=True)

    async def _send_commit(self, *, wait_for_final: bool) -> bool:
        ws = self._ws
        if ws is None:
            return False
        await self._flush_audio_resampler()
        if not self._audio_pending_commit:
            return False

        final_received = asyncio.Event()
        pending_commit = _PendingManualCommit(final_received=final_received)
        # Reserve the FIFO slot before sending. The receive loop can process a
        # very fast committed_transcript while ``ws.send`` is still suspended.
        self._pending_manual_commits.append(pending_commit)
        self._manual_commit_inflight = len(self._pending_manual_commits)
        # Kept for existing direct users/tests of this private seam. Runtime
        # acknowledgement routing uses ``_pending_manual_commits`` instead.
        self._final_received = final_received
        # Each fresh commit starts a clean slate — any stale drop flag
        # from a previous commit (e.g. one whose timed-out committed
        # transcript never arrived) should not suppress this commit's final.
        self._dropping_pending_final = False
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
            self._discard_pending_manual_commit(pending_commit)
            return False

        self._audio_pending_commit = False
        # This commit covers everything streamed so far; its ack is the next
        # committed_transcript we receive while a manual commit is in flight.
        self._committed_through_epoch = self._audio_epoch
        if wait_for_final:
            final_unavailable = False
            try:
                await asyncio.wait_for(
                    final_received.wait(), timeout=self._config.final_transcript_timeout_s
                )
            except TimeoutError:
                final_unavailable = True
                # Give up on ElevenLabs' committed transcript and promote
                # whatever we've streamed via ``partial_transcript`` so the
                # session can still drive the agent.  The real committed
                # transcript, if it arrives later, will be dropped via
                # ``_dropping_pending_final`` so the turn doesn't receive
                # two FINAL events.
                logger.warning(
                    "Timed out after %.1fs waiting for final transcript from "
                    "ElevenLabs; promoting %d-char partial to FINAL",
                    self._config.final_transcript_timeout_s,
                    len(self._partial_text),
                )
            final_unavailable = final_unavailable or pending_commit.final_lost_on_reconnect
            if final_unavailable:
                if pending_commit.final_lost_on_reconnect:
                    logger.warning(
                        "ElevenLabs reconnect invalidated an in-flight final transcript; "
                        "promoting %d-char partial to FINAL",
                        len(self._partial_text),
                    )
                if self._promote_partial_to_final():  # noqa: SIM102 nested branches preserve decision context
                    # Only suppress the late committed transcript when we
                    # actually promoted a partial to a FINAL.  If no partial
                    # ever arrived, a real ``committed_transcript`` showing up
                    # during the close/drain path is the turn's only
                    # transcript and must not be dropped.
                    if self._pending_manual_commit_is_active(pending_commit):
                        pending_commit.drop_late_final = True
                        self._dropping_pending_final = True
        return True

    def _handle_json_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("message_type") or msg.get("type", "")
        if msg_type == "partial_transcript":
            self._handle_partial_transcript(msg)
        elif msg_type in {"committed_transcript", "committed_transcript_with_timestamps"}:
            self._handle_committed_transcript(msg)

    def _handle_partial_transcript(self, msg: dict[str, Any]) -> None:
        text = msg.get("text", "")
        if not text:
            return
        self._partial_text = text
        # A partial means the server is transcribing audio it has not yet
        # committed, so there is genuinely uncommitted audio. Re-arm the
        # pending flag — this recovers the case where an earlier (stale)
        # committed_transcript optimistically cleared it while speech had
        # already resumed.
        self._audio_pending_commit = True
        # The server has now transcribed through the latest audio epoch; a
        # subsequent VAD commit covers at most this much.
        self._transcribed_through_epoch = self._audio_epoch
        self._emit_event(
            STTEvent(
                type=STTEventType.PARTIAL,
                text=text,
                confidence=msg.get("confidence"),
                language=msg.get("language_code") or self._config.language,
            )
        )

    def _promote_partial_to_final(self) -> bool:
        """Emit and clear the latest partial when its socket epoch cannot finish."""
        if not self._partial_text:
            return False
        self._emit_event(
            STTEvent(
                type=STTEventType.FINAL,
                text=self._partial_text,
                language=self._config.language,
            )
        )
        self._partial_text = ""
        return True

    def _reconcile_pending_commit_on_committed(self) -> _PendingManualCommit | None:
        """Clear the pending-commit flag only when the committed transcript
        actually covers the latest audio we've streamed.

        A committed transcript can arrive *after* newer audio was sent (manual
        commit_segment() then more send_audio(), or a VAD auto-commit while
        speech resumed); clearing unconditionally would make _end_realtime()
        skip the final commit for that newer audio and drop the trailing
        segment.
        """
        pending_commit: _PendingManualCommit | None = None
        if self._pending_manual_commits:
            # Ack of a manual commit we sent. Clear only if no audio was
            # streamed after that commit was requested; otherwise the newer
            # audio is still uncommitted and must be flushed at end-of-turn.
            pending_commit = self._pending_manual_commits.popleft()
            self._manual_commit_inflight = len(self._pending_manual_commits)
            self._final_received = (
                self._pending_manual_commits[-1].final_received
                if self._pending_manual_commits
                else None
            )
        elif self._manual_commit_inflight > 0:
            # Compatibility for custom subclasses/tests that set the legacy
            # counter and waiter directly. Production commits always take the
            # FIFO branch above.
            self._manual_commit_inflight -= 1
        else:
            # Unsolicited server VAD commit. It covers what the server has
            # transcribed (bounded by the latest partial), not necessarily the
            # newest audio.
            self._committed_through_epoch = max(
                self._committed_through_epoch, self._transcribed_through_epoch
            )
        if self._audio_epoch <= self._committed_through_epoch:
            self._audio_pending_commit = False
        return pending_commit

    def _handle_committed_transcript(self, msg: dict[str, Any]) -> None:
        text = msg.get("text", "")

        if self._transcribed_through_epoch <= self._committed_through_epoch:
            # Some valid VAD commits arrive without a preceding
            # ``partial_transcript``. The committed transcript itself proves
            # the server transcribed a segment, but not that it covered newer
            # trailing audio streamed before this message was processed. With
            # no partial boundary, acknowledge only the next uncommitted epoch
            # so resumed speech stays pending for end_stream().
            self._transcribed_through_epoch = max(
                self._transcribed_through_epoch,
                min(self._audio_epoch, self._committed_through_epoch + 1),
            )

        pending_commit = self._reconcile_pending_commit_on_committed()
        if pending_commit is not None and pending_commit.drop_late_final:
            # A previous ``_send_commit`` already gave up on this committed
            # transcript and promoted the accumulated partial to a FINAL, so
            # silently discard this late revision to avoid emitting a second
            # FINAL for the same turn.
            logger.debug(
                "Dropping late ElevenLabs committed_transcript (already promoted partial)"
            )
            self._dropping_pending_final = any(
                commit.drop_late_final for commit in self._pending_manual_commits
            )
            self._partial_text = ""
            pending_commit.final_received.set()
            return
        if pending_commit is None and self._dropping_pending_final:
            # Compatibility path for a pre-existing direct waiter with no
            # FIFO record. Runtime commits always use the branch above.
            logger.debug(
                "Dropping late ElevenLabs committed_transcript (already promoted partial)"
            )
            self._dropping_pending_final = False
            self._partial_text = ""
            if self._final_received is not None:
                self._final_received.set()
            return

        # An empty committed transcript is still the server's acknowledgment
        # of a manual or VAD commit (for example, a silence-only segment).
        # Keep the control-boundary reconciliation above and release the waiter
        # below, but do not expose an empty FINAL to downstream consumers.
        if text:
            self._emit_event(
                STTEvent(
                    type=STTEventType.FINAL,
                    text=text,
                    confidence=msg.get("confidence"),
                    language=msg.get("language_code") or self._config.language,
                    word_timestamps=word_timestamps_from_words(msg.get("words")),
                )
            )
        self._partial_text = ""
        if pending_commit is not None:
            pending_commit.final_received.set()
        elif self._final_received is not None:
            self._final_received.set()

    def _pending_manual_commit_is_active(self, pending_commit: _PendingManualCommit) -> bool:
        return any(commit is pending_commit for commit in self._pending_manual_commits)

    def _discard_pending_manual_commit(self, pending_commit: _PendingManualCommit) -> None:
        for commit in tuple(self._pending_manual_commits):
            if commit is pending_commit:
                self._pending_manual_commits.remove(commit)
                break
        self._manual_commit_inflight = len(self._pending_manual_commits)
        self._final_received = (
            self._pending_manual_commits[-1].final_received
            if self._pending_manual_commits
            else None
        )

    # -- Batch (HTTP) mode -------------------------------------------------

    async def _end_batch(self) -> None:
        await self._flush_batch_buffer()

    async def _flush_batch_buffer(self) -> None:
        """Transcribe and emit whatever is buffered, then reset for a fresh stream.

        Used both when the stream ends normally and when a cumulative buffer
        cap forces an early finalize mid-stream (long-talking caller). The
        latched format is preserved so the next utterance keeps the same
        first-seen format contract.
        """
        wav_data = self._drain_buffer_to_wav()
        if wav_data is None:
            return
        result = await self._transcribe_batch(wav_data)
        if result:
            self._emit_event(
                STTEvent(
                    type=STTEventType.FINAL,
                    text=result["text"],
                    # ``language_code`` is the response field; ``language``
                    # does not exist, so the detected language used to be
                    # discarded in favour of the configured one (gh 1064).
                    #
                    # ``confidence`` stays unset on purpose: the response
                    # carries no top-level transcription confidence.  Its only
                    # top-level score is ``language_probability``, which is
                    # language-detection confidence — a different quantity —
                    # and per-word scores are ``logprob`` values inside
                    # ``words``.  Recording either under ``confidence`` would
                    # put a number with the wrong meaning in the journal.
                    language=result.get("language_code") or self._config.language,
                )
            )

    async def _transcribe_batch(self, wav_data: bytes) -> dict[str, Any] | None:
        url = f"{self._config.base_url}/speech-to-text"
        headers = {"xi-api-key": self._config.api_key}

        data: dict[str, str] = {}
        # ``model_id`` (required) and ``language_code`` are the documented
        # multipart field names; the schema has no ``model``/``language``
        # field.  The older spelling therefore sent no model at all, the API
        # answered 422, and the bounded retry deliberately does not retry a
        # non-429 status — so batch mode failed on every attempt.  These match
        # the realtime query params in ``_build_realtime_ws_url`` (gh 1064).
        data["model_id"] = self._resolved_model()
        if self._config.language:
            data["language_code"] = self._config.language
        # Zero-retention mode is a *query* parameter on /v1/speech-to-text (the
        # multipart schema has no such field), so send it via ``params`` — in
        # ``data`` it would be silently ignored. Opt-in: only sent when
        # disabled so the default request keeps the server default (logging on).
        params = {"enable_logging": "false"} if not self._config.enable_logging else None

        # One client for all attempts: a per-attempt client would pay a fresh
        # TCP+TLS handshake on every retry, exactly when the provider is
        # already slow.
        client = self._config.http_client or httpx.AsyncClient(timeout=self._config.timeout)
        owns_client = self._config.http_client is None

        async def _attempt() -> dict[str, Any]:
            response = await client.post(
                url,
                headers=headers,
                params=params,
                files={"file": ("audio.wav", wav_data, "audio/wav")},
                data=data,
            )
            response.raise_for_status()
            return response.json()

        try:
            try:
                return await self._run_with_bounded_retry(
                    _attempt,
                    max_retries=self._config.max_retries,
                    provider_label="ElevenLabs batch STT",
                )
            except Exception as exc:
                context: dict[str, Any] = {}
                if isinstance(exc, httpx.HTTPStatusError):
                    context["http_status"] = exc.response.status_code
                self._emit_provider_error(exc, **context)
                raise
        finally:
            if owns_client:
                await client.aclose()

    def version_info(self) -> dict[str, str]:
        # Report the transport library the active mode actually uses:
        # realtime mode streams over a WebSocket, batch mode issues an
        # HTTP request. Mirror the runtime branch like ``_resolved_model``.
        transport_lib = "websockets" if self._config.mode == "realtime" else "httpx"
        return {
            "provider": "elevenlabs",
            "model": self._resolved_model(),
            "api_version": "v1",
            "sdk_version": get_package_version(transport_lib),
        }
