"""Telnyx Call Control media-streams transport.

Handles Telnyx's bidirectional WebSocket protocol for real phone calls.
Telnyx streams L16 (raw PCM16) at 16 kHz by default — EasyCat's internal bus
format — with PCMU 8 kHz as a configured fallback, and emits DTMF / control
events into the Session event bus.

The media WebSocket handshake is NOT signed (Telnyx has no
``X-Twilio-Signature`` equivalent): authentication rests entirely on a
one-time stream token carried in the ``stream_url`` query string and bound to
the call control id in the ``start`` frame.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Literal, cast, get_type_hints
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.server import ServerConnection

from easycat._audio_utils import PCM16StreamResampler, resample
from easycat._epoch import Epoch, Lease
from easycat._net import is_loopback_host
from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import (
    DTMF,
    CallAnswered,
    CallEnded,
    EventBus,
    PlaybackMarkAck,
)
from easycat.runtime._event_tasks import RuntimeTaskScope
from easycat.runtime.scope import BackgroundTaskScope, RuntimeScope
from easycat.telephony._stream_tokens import (
    STREAM_TOKEN_PARAMETER,
    StreamTokenContext,
    StreamTokenStore,
)
from easycat.telephony.dtmf import VALID_DTMF_DIGITS
from easycat.transports._base import (
    AudioQueueMixin,
    ServerTransportBase,
    make_version_info,
)
from easycat.transports._g711 import _mulaw_decode, pcm16_to_mulaw
from easycat.transports._limits import DEFAULT_INBOUND_AUDIO_MAX_BYTES

logger = logging.getLogger(__name__)

TelnyxCodec = Literal["L16", "PCMU"]
TELNYX_PREFERRED_TTS_OUTPUT_FORMAT = PCM16_MONO_16K
TelnyxStreamTokenStore = StreamTokenStore

_DEGRADED_TELNYX_SEQUENCE_GAP = "telnyx_sequence_gap"
_DEGRADED_TELNYX_MEDIA_FORMAT = "telnyx_unsupported_media_format"
_DEGRADED_TELNYX_ERROR = "telnyx_stream_error"

# Error codes from the Telnyx media-streams WebSocket protocol.
_TELNYX_ERROR_RATE_LIMIT = "100005"

# Outbound media coalescing bounds. Telnyx rate-limits tiny sends (error
# 100005), and huge sends defeat ``clear``-based barge-in, so frames are
# flushed between ~20 ms (minimum) and ~100 ms (hard cap) of audio.
_MIN_FLUSH_MS = 20
_MAX_FLUSH_MS = 100

_TELNYX_RECEIVE_TASK_NAME = "telnyx_receive"
_TELNYX_RECEIVE_COHORT = "transport-receive"
_TELNYX_INBOUND_TRACKS = {"inbound", "inbound_track"}
_L16_ENCODINGS = {"L16", "PCM16", "PCM_S16LE"}
_PCMU_ENCODINGS = {"PCMU", "G711U", "MULAW"}


def _codec_bytes_per_ms(encoding: str, sample_rate: int, channels: int) -> int:
    """Return wire bytes per millisecond for one negotiated media format."""
    sample_width = 1 if encoding.upper() == "PCMU" else 2
    return max(1, sample_width * max(1, channels) * max(1, sample_rate) // 1000)


@dataclass
class TelnyxTransportConfig:
    """Configuration for :class:`TelnyxTransport`.

    The configured bidirectional codec defaults to L16 @ 16 kHz, which matches
    EasyCat's internal ``PCM16_MONO_16K`` bus exactly — no companding, no
    resampling. PCMU @ 8 kHz is a supported fallback. The ``start.media_format``
    frame is authoritative and re-negotiates the decode path per call;
    :attr:`preferred_tts_output_format` is derived per instance from this
    config so TTS output needs only a trivial encode on send.
    """

    host: str = "127.0.0.1"
    port: int = 8767
    codec: TelnyxCodec = "L16"
    sampling_rate: int = 16000
    send_silence_when_idle: bool = True
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200
    # Legacy validators receive the raw token string. Annotating the accepted
    # parameter as StreamTokenContext explicitly opts into start-frame context.
    stream_token_validator: StreamTokenValidator | None = None
    stream_token_parameter: str = STREAM_TOKEN_PARAMETER
    stream_token_validation_timeout_s: float = 5.0
    unsafe_allow_no_auth: bool = False
    max_pending_bytes: int = DEFAULT_INBOUND_AUDIO_MAX_BYTES

    def __post_init__(self) -> None:
        if self.codec not in ("L16", "PCMU"):
            raise ValueError("codec must be 'L16' or 'PCMU'")
        if isinstance(self.sampling_rate, bool) or not isinstance(self.sampling_rate, int):
            raise TypeError("sampling_rate must be an integer")
        if self.codec == "PCMU":
            if self.sampling_rate != 8000:
                raise ValueError("PCMU requires sampling_rate=8000")
        elif self.sampling_rate not in (8000, 16000, 24000, 48000):
            raise ValueError("sampling_rate must be one of 8000, 16000, 24000, 48000 for L16")
        if self.codec == "L16" and self.audio_format.sample_rate != self.sampling_rate:
            raise ValueError(
                f"L16 transport requires audio_format.sample_rate == {self.sampling_rate}; "
                f"got {self.audio_format.sample_rate}"
            )
        if not self.stream_token_parameter:
            raise ValueError("stream_token_parameter must be non-empty")
        timeout = self.stream_token_validation_timeout_s
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("stream_token_validation_timeout_s must be a positive finite number")

    @property
    def preferred_tts_output_format(self) -> AudioFormat:
        """TTS output contract derived per instance from the configured codec."""
        if self.codec == "PCMU":
            return PCM16_MONO_8K
        return AudioFormat(sample_rate=self.sampling_rate, channels=1, sample_width=2)


def _parse_telnyx_message(raw: str) -> dict[str, Any] | None:
    """Parse one Telnyx WebSocket message and require a JSON object."""
    try:
        msg = json.loads(raw)
    except (RecursionError, ValueError):
        logger.warning("Ignoring invalid JSON from Telnyx")
        return None
    if not isinstance(msg, dict):
        logger.warning("Ignoring non-object JSON from Telnyx")
        return None
    return msg


def _decode_telnyx_raw(raw: str | bytes) -> str | None:
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("Ignoring non-UTF-8 Telnyx message")
        return None


StreamTokenClaims = Mapping[str, Any]
StreamTokenValidatorResult = bool | StreamTokenClaims | None
StreamTokenValidator = (
    Callable[[str], StreamTokenValidatorResult | Awaitable[StreamTokenValidatorResult]]
    | Callable[
        [StreamTokenContext], StreamTokenValidatorResult | Awaitable[StreamTokenValidatorResult]
    ]
)


def handshake_stream_token(ws: ServerConnection | None, *, parameter: str) -> str | None:
    """Extract the one-time stream token from the handshake query string.

    Telnyx does not sign the media WebSocket handshake; the answer/dial
    command embeds the one-time token in the ``stream_url`` it dials, so the
    upgrade request's query string is the only transport for it.
    """
    request = getattr(ws, "request", None) if ws is not None else None
    raw_path = getattr(request, "path", None)
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        query = parse_qs(urlsplit(raw_path).query, keep_blank_values=True)
    except ValueError:
        return None
    values = query.get(parameter)
    if values and isinstance(values[0], str) and values[0]:
        return values[0]
    return None


def _stream_token_validator_parameter(
    validator: StreamTokenValidator,
) -> tuple[inspect.Parameter | None, bool]:
    """Return the validator parameter and whether it opts into context."""
    try:
        signature = inspect.signature(validator)
    except (TypeError, ValueError):
        return None, False
    try:
        hints = get_type_hints(validator)
    except (NameError, TypeError):
        hints = {}
    parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    ]
    parameter = next(
        (candidate for candidate in parameters if candidate.default is inspect.Parameter.empty),
        parameters[0] if parameters else None,
    )
    if parameter is None:
        return None, False
    annotation = hints.get(parameter.name, parameter.annotation)
    if (
        annotation is StreamTokenContext
        or annotation == "StreamTokenContext"
        or (isinstance(annotation, str) and annotation.endswith(".StreamTokenContext"))
        or getattr(annotation, "__name__", None) == "StreamTokenContext"
    ):
        return parameter, True
    return parameter, False


def _call_stream_token_validator(
    validator: StreamTokenValidator,
    *,
    token: str,
    context: StreamTokenContext,
) -> StreamTokenValidatorResult | Awaitable[StreamTokenValidatorResult]:
    parameter, wants_context = _stream_token_validator_parameter(validator)
    argument = context if wants_context else token
    if parameter is not None and parameter.kind is inspect.Parameter.KEYWORD_ONLY:
        return validator(**{parameter.name: argument})  # type: ignore[call-arg]
    return validator(argument)  # type: ignore[arg-type]


async def _maybe_await_stream_token_result(
    result: StreamTokenValidatorResult | Awaitable[StreamTokenValidatorResult],
) -> StreamTokenValidatorResult:
    if inspect.isawaitable(result):
        return await result
    return result


def _coerce_stream_token_claims(result: StreamTokenValidatorResult) -> dict[str, str] | None:
    if isinstance(result, Mapping):
        return {str(key): str(value) for key, value in result.items() if value is not None}
    return {} if bool(result) else None


async def _telnyx_stream_token_claims(
    *,
    token: str | None,
    call_control_id: str | None,
    stream_id: str | None,
    config: TelnyxTransportConfig,
) -> dict[str, str] | None:
    validator = config.stream_token_validator
    if validator is None:
        return {}
    if not token:
        return None
    context = StreamTokenContext(
        token=token,
        call_sid=call_control_id,
        stream_sid=stream_id,
        parameters={},
    )
    try:
        async with asyncio.timeout(config.stream_token_validation_timeout_s):
            if inspect.iscoroutinefunction(validator):
                result_or_awaitable = _call_stream_token_validator(
                    cast(StreamTokenValidator, validator),
                    token=token,
                    context=context,
                )
            else:
                result_or_awaitable = await asyncio.to_thread(
                    _call_stream_token_validator,
                    validator,
                    token=token,
                    context=context,
                )
            result = await _maybe_await_stream_token_result(result_or_awaitable)
        claims = _coerce_stream_token_claims(result)
        if claims is not None:
            claims.pop(config.stream_token_parameter, None)
        return claims
    except TimeoutError:
        logger.warning("Telnyx stream token validator timed out")
        return None
    except Exception:
        logger.warning("Telnyx stream token validator raised", exc_info=True)
        return None


def _decode_client_state(raw: Any) -> dict[str, str]:
    """Best-effort decode the base64 ``client_state`` blob we minted server-side."""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        decoded = json.loads(base64.b64decode(raw, validate=True).decode("utf-8"))
    except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in decoded.items()
        if isinstance(value, str | int) and not isinstance(value, bool)
    }


def _clean_telnyx_number(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _parse_telnyx_start_identity(
    start: dict[str, Any],
    call_control_id: str | None,
    client_state_fields: Mapping[str, str],
) -> tuple[Any, str, str]:
    """Build a :class:`CallIdentity` from a Telnyx ``start`` frame.

    Caller/called numbers come from the start frame; direction comes from the
    ``client_state`` blob embedded by our answer/dial commands (defaulting to
    inbound). Remaining client-state fields survive as custom fields.
    """
    from easycat.session._types import CallDirection, CallIdentity

    fields = dict(client_state_fields)
    direction_raw = str(fields.pop("direction", "")).strip().lower()
    direction: CallDirection
    if direction_raw.startswith("outbound"):
        direction = "outbound"
    elif direction_raw.startswith("inbound") or not direction_raw:
        direction = "inbound"
    else:
        direction = "unknown"

    caller = _clean_telnyx_number(start.get("from")) or fields.pop("from_", "")
    called = _clean_telnyx_number(start.get("to")) or fields.pop("to", "")
    if direction == "outbound":
        caller, called = called, caller

    identity = CallIdentity(
        caller_number=caller,
        called_number=called,
        direction=direction,
        display_name=None,
        call_sid=call_control_id,
        custom_fields=fields,
    )
    return identity, caller, called


def _parse_telnyx_int(value: Any) -> int | None:
    """Coerce Telnyx sequence numbers, which arrive as strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _telnyx_sequence_number(
    message: Mapping[str, Any],
    nested: Mapping[str, Any] | None = None,
) -> int | None:
    """Return a frame's ``sequence_number``, top-level first.

    Telnyx carries ``sequence_number`` beside ``event`` on the message itself
    (like Twilio's ``sequenceNumber``); the nested payload is accepted as a
    fallback so either placement drives gap detection.
    """
    sequence = _parse_telnyx_int(message.get("sequence_number"))
    if sequence is None and nested is not None:
        sequence = _parse_telnyx_int(nested.get("sequence_number"))
    return sequence


def _telnyx_stream_id(
    message: Mapping[str, Any],
    nested: Mapping[str, Any] | None = None,
) -> str | None:
    """Return a frame's ``stream_id``, accepting either placement.

    Telnyx sends ``stream_id`` as a sibling of ``event``; the nested payload
    is checked as well so both shapes resolve to the same stream identity.
    """
    for source in (message, nested):
        if source is None:
            continue
        value = source.get("stream_id")
        if isinstance(value, str) and value:
            return value
    return None


class _TelnyxStreamDiagnostics:
    """Sequence-gap detection across the negotiated stream.

    Gaps are reported as degraded events but audio is never re-requested or
    buffered for replay. This is intentional: in real-time voice, stale media
    is worthless by the time it could be recovered, and holding the pipeline
    to resync would add latency that compounds with every subsequent gap.
    Downstream consumers (session metrics, journaling) decide whether a gap
    rate warrants operator attention.
    """

    def __init__(self, emit_degraded: Callable[..., None]) -> None:
        self._emit_degraded = emit_degraded
        self._last_sequence_number: int | None = None

    def reset(self) -> None:
        self._last_sequence_number = None

    def start(self, message: Mapping[str, Any], nested: Mapping[str, Any] | None = None) -> None:
        self.reset()
        self._last_sequence_number = _telnyx_sequence_number(message, nested)

    def observe(
        self,
        message: Mapping[str, Any],
        nested: Mapping[str, Any] | None = None,
    ) -> None:
        sequence = _telnyx_sequence_number(message, nested)
        if sequence is None:
            return
        previous = self._last_sequence_number
        if previous is not None and sequence != previous + 1:
            self._emit_degraded(
                _DEGRADED_TELNYX_SEQUENCE_GAP,
                f"expected sequence_number {previous + 1}, got {sequence}",
            )
        self._last_sequence_number = sequence


async def _emit_telnyx_call_ended(
    event_bus: EventBus | None,
    *,
    call_control_id: str | None,
    answered_at: float | None,
    call_identity: Any | None,
    session_id: str | None,
) -> None:
    if event_bus is None or call_control_id is None:
        return
    duration = None
    if answered_at is not None:
        duration = max(0.0, time.monotonic() - answered_at)
    await event_bus.emit(
        CallEnded(
            call_sid=call_control_id,
            duration_s=duration,
            number=call_identity.caller_number if call_identity is not None else None,
            session_id=session_id,
        )
    )


def _accepted_telnyx_media(
    msg: dict[str, Any],
    *,
    active_stream_id: str | None,
) -> dict[str, Any] | None:
    """Return the Telnyx media payload only when the frame belongs to the active stream."""
    if active_stream_id is None:
        logger.debug("Ignoring Telnyx media before start")
        return None

    media = msg.get("media", {})
    if not isinstance(media, dict):
        logger.debug("Ignoring Telnyx media frame with non-object media payload")
        return None

    track = media.get("track", "")
    if not track or track in _TELNYX_INBOUND_TRACKS:
        # One bidirectional stream per call; untagged frames are inbound.
        return media
    logger.debug("Ignoring Telnyx non-inbound media track: %s", track)
    return None


def _is_active_telnyx_stream_event(
    msg: dict[str, Any],
    *,
    active_stream_id: str | None,
    event_name: str,
) -> bool:
    """Return True only when a Telnyx control event belongs to the active stream."""
    if active_stream_id is None:
        logger.debug("Ignoring Telnyx %s before start", event_name)
        return False
    nested = msg.get(event_name)
    stream_id = _telnyx_stream_id(msg, nested if isinstance(nested, dict) else None)
    if stream_id is not None and stream_id != active_stream_id:
        logger.debug(
            "Ignoring Telnyx %s for stream_id=%s while active stream_id=%s",
            event_name,
            stream_id,
            active_stream_id,
        )
        return False
    return True


def _parse_telnyx_dtmf_digit(msg: dict[str, Any]) -> str | None:
    nested = msg.get("dtmf")
    if not isinstance(nested, dict):
        return None
    digit = nested.get("digit")
    if not isinstance(digit, str) or len(digit) != 1:
        return None
    digit = digit.upper()
    if digit not in VALID_DTMF_DIGITS:
        return None
    return digit


class _TelnyxOutboundCoalescer:
    """Buffer outbound encoded audio into ~20–100 ms media frames.

    Telnyx rate-limits tiny sends (error 100005), and oversized sends delay
    ``clear``-based barge-in cutoff, so encoded audio accumulates here until
    at least ~20 ms is pending and never exceeds ~100 ms per wire frame.
    """

    def __init__(self, bytes_per_ms: int) -> None:
        self._min_flush_bytes = max(1, bytes_per_ms * _MIN_FLUSH_MS)
        self._max_flush_bytes = max(self._min_flush_bytes, bytes_per_ms * _MAX_FLUSH_MS)
        self._buffer = bytearray()

    def append(self, data: bytes) -> list[bytes]:
        """Append encoded audio; return any frames ready to send.

        Anything at or above the ~20 ms minimum ships immediately (capped at
        ~100 ms per wire frame); holding out for a full max-sized frame would
        add up to ~100 ms to the start of every utterance.
        """
        self._buffer.extend(data)
        frames: list[bytes] = []
        while len(self._buffer) >= self._min_flush_bytes:
            size = min(len(self._buffer), self._max_flush_bytes)
            frames.append(bytes(self._buffer[:size]))
            del self._buffer[:size]
        return frames

    def flush(self) -> bytes | None:
        """Return buffered audio even below a full frame, or ``None``."""
        if not self._buffer:
            return None
        frame = bytes(self._buffer)
        self._buffer.clear()
        return frame

    def reset(self) -> None:
        self._buffer.clear()


class _TelnyxProtocolMixin:
    """Shared Telnyx media-streams inbound routing + handlers.

    Mirrors the Twilio split: :class:`TelnyxTransport` (a
    :class:`ServerTransportBase` that owns its listener) and
    :class:`TelnyxConnectionTransport` (an :class:`AudioQueueMixin` wrapping
    one injected connection) speak the same wire protocol, so this mixin owns
    the single copy of ``_handle_message`` and its handlers, the once-only
    ``CallEnded`` emitter, read-only accessors, and the reconnect-race-guarded
    finally cleanup.

    Hooks capturing class divergence:

    * ``_current_ws()`` — the active :class:`ServerConnection` (or ``None``).
    * ``_reset_connection_state()`` — per-class ownership reset run inside the
      guarded finally.

    Outbound encode/coalesce paths stay per-class because their
    ``ConnectionClosed`` error-path resets model genuinely different
    lifecycles.
    """

    # Base-provided members this mixin relies on (supplied by
    # ServerTransportBase / AudioQueueMixin at runtime).
    _emit_degraded: Any
    _record_transport_disconnect: Any
    _enqueue_chunk: Any
    _enqueue_sentinel: Any
    _client_connected: Any
    _diagnostics: _TelnyxStreamDiagnostics
    _event_bus: EventBus | None
    _easycat_session_id: str | None
    _config: TelnyxTransportConfig
    _audio_format: AudioFormat
    _stream_id: str | None
    _call_control_id: str | None
    _call_identity: Any | None
    _identity_sink: Any
    _answered_at: float | None
    _call_ended_emitted: bool
    _pending_marks: dict[str, None]
    _inbound_resampler: PCM16StreamResampler
    _negotiated_encoding: str
    _negotiated_sample_rate: int
    _coalescer: _TelnyxOutboundCoalescer

    # ── Per-class hooks ───────────────────────────────────────────

    def _init_telnyx_protocol(
        self,
        config: TelnyxTransportConfig,
        event_bus: EventBus | None,
    ) -> None:
        """Initialize state shared by both Telnyx transport lifecycles.

        Queue/server ownership must be initialized by the concrete transport
        before this method runs so ``_emit_degraded`` is ready for diagnostics.
        """
        self._config = config
        self._audio_format = config.audio_format
        self._event_bus = event_bus
        self._stream_id = None
        self._call_control_id = None
        self._call_identity = None
        self._identity_sink = None
        self._answered_at = None
        self._call_ended_emitted = False
        self._pending_marks = {}
        self._diagnostics = _TelnyxStreamDiagnostics(self._emit_degraded)
        self._negotiated_encoding = config.codec
        self._negotiated_sample_rate = config.audio_format.sample_rate
        self._inbound_resampler = PCM16StreamResampler(config.audio_format.sample_rate)

    def _current_ws(self) -> ServerConnection | None:
        """Return the active Telnyx WebSocket connection (or ``None``)."""
        raise NotImplementedError

    def _reset_connection_state(self) -> None:
        """Reset per-class ownership state inside the guarded finally."""
        raise NotImplementedError

    # ── Read-only accessors ───────────────────────────────────────

    @property
    def request(self) -> Any | None:
        """Accepted Telnyx media WebSocket handshake request, when available."""
        return getattr(self._current_ws(), "request", None)

    @property
    def call_identity(self) -> Any | None:
        """Latest :class:`CallIdentity` parsed from the Telnyx start event."""
        return self._call_identity

    @property
    def transport_kind(self) -> str:
        return "telephony"

    def bind_identity_sink(self, sink: Any) -> None:
        """Register a callback that receives every identity update.

        Used by :func:`easycat.config.create_session` to bridge the start
        frame's numbers / ``client_state`` claims onto the session's
        :attr:`~easycat.session._session.Session.call_identity` without making
        Session depend on the transport directly.
        """
        self._identity_sink = sink

    @property
    def stream_id(self) -> str | None:
        return self._stream_id

    @property
    def call_control_id(self) -> str | None:
        return self._call_control_id

    @property
    def preferred_tts_output_format(self) -> AudioFormat:
        """Per-instance TTS contract derived from the configured codec."""
        return self._config.preferred_tts_output_format

    # ── Inbound routing ───────────────────────────────────────────

    async def _receive_telnyx_messages(self, ws: ServerConnection) -> None:
        """Drive one Telnyx receive stream and perform guarded cleanup."""
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        logger.warning("Ignoring non-UTF-8 Telnyx message")
                        continue
                await self._handle_message(raw)
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("Telnyx media stream disconnected")
            if isinstance(exc, websockets.exceptions.ConnectionClosedError):
                self._record_transport_disconnect("telnyx stream closed abnormally")
        finally:
            await self._finalize_after_receive(ws)

    async def _finalize_after_receive(self, ws: ServerConnection) -> None:
        """Run the reconnect-race-guarded finally cleanup for a receive driver.

        Only tears down when this handler still owns the active connection
        (``_current_ws() is ws``) or when the slot has already been cleared by
        a send-path error and no newer client has claimed it. A newer client
        owning the slot is left untouched — the prerequisite reconnect-race
        guard, kept in the single shared copy.
        """
        if self._current_ws() is ws or self._current_ws() is None:
            await self._expire_pending_marks()
            await self._emit_call_ended_once()
            tail = self._inbound_resampler.finish()
            if tail:
                self._enqueue_chunk(
                    AudioChunk(data=tail, format=self._audio_format),
                    context="Telnyx",
                )
            self._reset_connection_state()
            self._client_connected.clear()
            self._stream_id = None
            self._call_control_id = None
            self._answered_at = None
            self._diagnostics.reset()
            self._enqueue_sentinel()
        # else: a newer client owns the connection -> leave it alone.

    async def _handle_message(self, raw: str) -> None:
        """Route a Telnyx JSON message to the appropriate handler."""
        msg = _parse_telnyx_message(raw)
        if msg is None:
            return

        event = msg.get("event")
        handler = self._MESSAGE_HANDLERS.get(event) if isinstance(event, str) else None
        if handler is None:
            logger.debug("Unknown Telnyx event: %s", event)
            return
        await handler(self, msg)

    async def _handle_connected(self, msg: dict[str, Any]) -> None:
        logger.debug("Telnyx connected event")

    async def _negotiate_media_format(self, media_format: Any) -> bool:
        """Adopt the authoritative ``start.media_format`` codec path.

        Returns False (after a fatal degradation) for formats EasyCat cannot
        decode — garbage audio must never reach the pipeline.
        """
        if not isinstance(media_format, dict):
            self._emit_degraded(
                _DEGRADED_TELNYX_MEDIA_FORMAT,
                f"start.media_format missing or malformed: {media_format!r}",
                fatal=True,
            )
            return False
        encoding = str(media_format.get("encoding", "")).strip().upper()
        sample_rate = _parse_telnyx_int(media_format.get("sample_rate"))
        channels = _parse_telnyx_int(media_format.get("channels")) or 1
        if encoding in _L16_ENCODINGS:
            sample_rate = sample_rate or 16000
        elif encoding in _PCMU_ENCODINGS:
            encoding = "PCMU"
            sample_rate = 8000
        else:
            self._emit_degraded(
                _DEGRADED_TELNYX_MEDIA_FORMAT,
                f"unsupported media_format.encoding {encoding!r}",
                fatal=True,
            )
            return False
        if channels != 1:
            self._emit_degraded(
                _DEGRADED_TELNYX_MEDIA_FORMAT,
                f"unsupported channel count {channels}; mono required",
                fatal=True,
            )
            return False
        self._negotiated_encoding = encoding
        self._negotiated_sample_rate = sample_rate
        # Outbound frame bounds are sized by the wire codec; a start frame
        # that negotiates away from the configured codec must resize the
        # coalescer or a full L16-sized frame becomes ~400 ms of PCMU audio.
        self._coalescer = _TelnyxOutboundCoalescer(
            _codec_bytes_per_ms(encoding, sample_rate, channels)
        )
        return True

    async def _reject_start(self, close_code: int, reason: str) -> None:
        """Close the socket when a start frame fails validation."""
        logger.warning("Rejecting Telnyx start: %s", reason)
        ws = self._current_ws()
        if ws is not None:
            await ws.close(close_code, reason)

    async def _validated_start_payload(
        self,
        msg: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str] | None:
        """Validate shape/ids/media_format of a start frame; close on failure."""
        start = msg.get("start", {})
        if not isinstance(start, dict):
            logger.debug("Ignoring Telnyx start with non-object payload")
            return None
        stream_id = _telnyx_stream_id(msg, start)
        call_control_id = start.get("call_control_id")
        if not isinstance(stream_id, str) or not stream_id:
            await self._reject_start(4003, "Missing stream_id")
            return None
        if not isinstance(call_control_id, str) or not call_control_id:
            await self._reject_start(4003, "Missing call_control_id")
            return None
        if not await self._negotiate_media_format(start.get("media_format")):
            await self._reject_start(4003, "Unsupported media format")
            return None
        return start, stream_id, call_control_id

    async def _accept_start(
        self,
        msg: dict[str, Any],
        *,
        token_prevalidated: bool,
        prevalidated_claims: dict[str, str] | None = None,
    ) -> bool:
        """Extract stream metadata from the Telnyx ``start`` message.

        The ``start`` payload carries ``stream_id``, ``call_control_id``, the
        authoritative ``media_format``, caller/called numbers, and the
        ``client_state`` blob our answer/dial command embedded. A
        :class:`~easycat.events.CallAnswered` event is emitted so observers get
        a consistent inbound + outbound lifecycle.
        """
        validated = await self._validated_start_payload(msg)
        if validated is None:
            return False
        start, stream_id, call_control_id = validated

        if token_prevalidated:
            token_claims = prevalidated_claims
        else:
            token_claims = await _telnyx_stream_token_claims(
                token=handshake_stream_token(
                    self._current_ws(),
                    parameter=self._config.stream_token_parameter,
                ),
                call_control_id=call_control_id,
                stream_id=stream_id,
                config=self._config,
            )
        if token_claims is None:
            await self._reject_start(4003, "Missing or invalid stream token")
            return False

        self._inbound_resampler.reset()
        self._stream_id = stream_id
        self._call_control_id = call_control_id
        self._answered_at = time.monotonic()
        self._call_ended_emitted = False
        self._pending_marks = {}
        self._diagnostics.start(msg, start)
        identity, caller, called = _parse_telnyx_start_identity(
            start,
            call_control_id,
            _decode_client_state(start.get("client_state")),
        )
        if token_claims:
            identity = replace(
                identity,
                custom_fields={**identity.custom_fields, **token_claims},
            )
        self._call_identity = identity
        if self._identity_sink is not None:
            try:
                self._identity_sink(identity)
            except Exception:
                logger.debug("Identity sink raised on start", exc_info=True)

        if self._event_bus is not None:
            await self._event_bus.emit(
                CallAnswered(
                    call_sid=call_control_id,
                    answered_by="human",
                    session_id=self._easycat_session_id,
                )
            )

        logger.info(
            "Telnyx stream started: stream_id=%s call_control_id=%s "
            "encoding=%s rate=%d from=%s to=%s",
            self._stream_id,
            self._call_control_id,
            self._negotiated_encoding,
            self._negotiated_sample_rate,
            caller,
            called,
        )
        return True

    async def _handle_start(self, msg: dict[str, Any]) -> None:
        await self._accept_start(msg, token_prevalidated=False)

    def _decode_inbound_payload(self, payload: bytes) -> bytes | None:
        """Decode one inbound media payload into PCM16 at the negotiated rate."""
        if self._negotiated_encoding == "PCMU":
            return _mulaw_decode(payload)
        if len(payload) % 2 != 0:
            self._emit_degraded(_DEGRADED_TELNYX_ERROR, "dropped L16 frame with odd byte count")
            return None
        return payload

    async def _handle_media(self, msg: dict[str, Any]) -> None:
        """Decode one ``media`` frame and enqueue as internal-format PCM16."""
        media = _accepted_telnyx_media(msg, active_stream_id=self._stream_id)
        if media is None:
            return
        self._diagnostics.observe(msg, media)
        payload_text = media.get("payload", "")
        if not isinstance(payload_text, str) or not payload_text:
            return

        try:
            payload = base64.b64decode(payload_text, validate=True)
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.warning("Ignoring Telnyx media frame with invalid base64 payload")
            self._emit_degraded(_DEGRADED_TELNYX_ERROR, "invalid base64 media payload")
            return
        pcm_data = self._decode_inbound_payload(payload)
        if not pcm_data:
            return
        pcm_data = self._inbound_resampler.process(pcm_data, self._negotiated_sample_rate)

        if pcm_data:
            chunk = AudioChunk(data=pcm_data, format=self._audio_format)
            self._enqueue_chunk(chunk, context="Telnyx")

    async def _handle_stop(self, msg: dict[str, Any]) -> None:
        if not _is_active_telnyx_stream_event(
            msg,
            active_stream_id=self._stream_id,
            event_name="stop",
        ):
            return
        nested = msg.get("stop")
        self._diagnostics.observe(msg, nested if isinstance(nested, dict) else None)
        logger.info("Telnyx stream stopped (stream_id=%s)", self._stream_id)
        tail = self._inbound_resampler.finish()
        if tail:
            self._enqueue_chunk(
                AudioChunk(data=tail, format=self._audio_format),
                context="Telnyx",
            )
        # Mirror the outbound call manager lifecycle for inbound calls.
        await self._expire_pending_marks()
        await self._emit_call_ended_once()
        self._stream_id = None
        self._call_control_id = None
        self._answered_at = None
        self._diagnostics.reset()
        self._enqueue_sentinel()

    async def _handle_mark(self, msg: dict[str, Any]) -> None:
        if not _is_active_telnyx_stream_event(
            msg,
            active_stream_id=self._stream_id,
            event_name="mark",
        ):
            return
        mark = msg.get("mark")
        if not isinstance(mark, dict):
            logger.debug("Ignoring Telnyx mark with non-object payload")
            return
        self._diagnostics.observe(msg, mark)
        mark_name = mark.get("name")
        if not isinstance(mark_name, str) or not mark_name:
            logger.debug("Ignoring Telnyx mark with invalid name")
            return
        self._pending_marks.pop(mark_name, None)
        logger.debug("Telnyx mark acknowledged: %s", mark_name)
        if self._event_bus is not None:
            await self._event_bus.emit(
                PlaybackMarkAck(mark_name=mark_name, session_id=self._easycat_session_id)
            )

    async def _expire_pending_marks(self) -> None:
        """Acknowledge outstanding marks locally after a clear/stop/close.

        Telnyx under-documents whether queued marks echo after ``clear``;
        playback bookkeeping must not depend on it. Expiring locally keeps
        barge-in tracking correct even if acks never arrive.
        """
        if not self._pending_marks:
            return
        expired = list(self._pending_marks)
        self._pending_marks.clear()
        bus = self._event_bus
        if bus is None:
            return
        for mark_name in expired:
            await bus.emit(
                PlaybackMarkAck(mark_name=mark_name, session_id=self._easycat_session_id)
            )

    async def _emit_call_ended_once(self) -> None:
        if self._call_ended_emitted or self._call_control_id is None:
            return
        self._call_ended_emitted = True
        await _emit_telnyx_call_ended(
            self._event_bus,
            call_control_id=self._call_control_id,
            answered_at=self._answered_at,
            call_identity=self._call_identity,
            session_id=self._easycat_session_id,
        )

    async def _handle_dtmf(self, msg: dict[str, Any]) -> None:
        """Emit a DTMF event for the pressed digit."""
        if not _is_active_telnyx_stream_event(
            msg,
            active_stream_id=self._stream_id,
            event_name="dtmf",
        ):
            return
        digit = _parse_telnyx_dtmf_digit(msg)
        if digit is None:
            logger.debug("Ignoring Telnyx DTMF with invalid payload")
            return
        if self._event_bus is not None:
            await self._event_bus.emit(DTMF(digit=digit, session_id=self._easycat_session_id))

    async def _handle_error(self, msg: dict[str, Any]) -> None:
        """Map a Telnyx protocol error onto diagnostics; violations are fatal."""
        error = msg.get("error")
        code = ""
        detail = ""
        if isinstance(error, dict):
            code = str(error.get("code", ""))
            detail = str(error.get("message", ""))
        elif error is not None:
            detail = str(error)
        reason_detail = f"code={code} {detail}".strip()
        if code == _TELNYX_ERROR_RATE_LIMIT:
            logger.warning("Telnyx media send rate limited: %s", reason_detail)
            self._emit_degraded(_DEGRADED_TELNYX_ERROR, f"rate limited: {reason_detail}")
            return
        logger.warning("Telnyx stream error: %s", reason_detail)
        self._emit_degraded(_DEGRADED_TELNYX_ERROR, reason_detail, fatal=True)

    _MessageHandler = Callable[["_TelnyxProtocolMixin", dict[str, Any]], Awaitable[None]]
    _MESSAGE_HANDLERS: ClassVar[dict[str, _MessageHandler]] = {
        "connected": _handle_connected,
        "start": _handle_start,
        "media": _handle_media,
        "dtmf": _handle_dtmf,
        "stop": _handle_stop,
        "mark": _handle_mark,
        "error": _handle_error,
    }


class TelnyxTransport(_TelnyxProtocolMixin, ServerTransportBase):
    """Transport for Telnyx Call Control bidirectional media streaming.

    Implements the ``Transport`` protocol from :mod:`easycat.providers`.

    Telnyx message types handled:
      - ``connected`` — initial connection acknowledgement
      - ``start``     — stream metadata (stream_id, call_control_id,
                        authoritative ``media_format``, numbers, client_state)
      - ``media``     — base64-encoded L16 / PCMU audio frames
      - ``stop``      — stream ended
      - ``mark``      — playback mark acknowledgement
      - ``dtmf``      — DTMF digit pressed by caller
      - ``error``     — protocol error (rate limiting is non-fatal)

    The handshake is not signed by Telnyx, so a non-loopback bind requires a
    ``stream_token_validator`` (the one-time token minted into the answer /
    dial ``stream_url``) unless ``unsafe_allow_no_auth`` is set explicitly.
    """

    _transport_name = "Telnyx"
    # Telephony policy: leave EasyCat-side echo cancellation off by default.
    # The PSTN path handles line echo upstream and there is no reliable local
    # reference signal in a carrier media stream. Declared explicitly so the
    # choice is intentional, not a getattr fallback.
    default_echo_cancellation_enabled = False
    # Captured audio is inbound-only: ``_accepted_telnyx_media`` drops every
    # non-inbound frame at ingest before it reaches STT.
    inbound_stt_track = "inbound"
    # Class-level default for introspection; instances shadow this with the
    # codec-derived format from their config (L16@16k by default, 8k PCMU).
    preferred_tts_output_format: AudioFormat = TELNYX_PREFERRED_TTS_OUTPUT_FORMAT

    def __init__(
        self,
        config: TelnyxTransportConfig | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        resolved_config = config or TelnyxTransportConfig()
        super().__init__(
            host=resolved_config.host,
            port=resolved_config.port,
            max_pending_chunks=resolved_config.max_pending_chunks,
            max_pending_bytes=resolved_config.max_pending_bytes,
        )
        self._init_telnyx_protocol(resolved_config, event_bus)
        self.preferred_tts_output_format = resolved_config.preferred_tts_output_format
        self._coalescer = _TelnyxOutboundCoalescer(
            _codec_bytes_per_ms(
                self._negotiated_encoding,
                self._negotiated_sample_rate,
                1,
            )
        )

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Start the media listener after enforcing a safe public bind."""
        if (
            not is_loopback_host(self._config.host)
            and self._config.stream_token_validator is None
            and not self._config.unsafe_allow_no_auth
        ):
            raise ValueError(
                "TelnyxTransportConfig.stream_token_validator is required when "
                "binding Telnyx media to a non-loopback host; pass "
                "unsafe_allow_no_auth=True only for an intentionally unauthenticated listener"
            )
        await super().connect()

    def _current_ws(self) -> ServerConnection | None:
        return self._ws

    def _reset_connection_state(self) -> None:
        self._ws = None

    async def disconnect(self) -> None:
        """Disconnect Telnyx and stop the server."""
        await super().disconnect()
        await self._expire_pending_marks()
        self._stream_id = None
        self._call_control_id = None
        self._call_identity = None
        self._call_ended_emitted = False
        self._diagnostics.reset()
        self._inbound_resampler.reset()
        self._coalescer.reset()

    def _encode_outbound(self, chunk: AudioChunk) -> bytes:
        """Encode one PCM16 chunk into the negotiated wire codec."""
        if self._negotiated_encoding == "PCMU":
            return pcm16_to_mulaw(chunk.data, chunk.format.sample_rate)
        if chunk.format.sample_rate != self._negotiated_sample_rate:
            return resample(
                chunk.data,
                chunk.format.sample_rate,
                self._negotiated_sample_rate,
            )
        return chunk.data

    async def _send_media_frames(self, frames: list[bytes]) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            for frame in frames:
                payload = base64.b64encode(frame).decode("ascii")
                await ws.send(json.dumps({"event": "media", "media": {"payload": payload}}))
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send audio: Telnyx disconnected")
            # Emit the dead call's ``CallEnded`` BEFORE releasing the slot:
            # once ``self._ws`` is cleared a replacement connection can claim
            # it, and the old receive handler's reconnect-race-guarded finally
            # then skips its emit. The once-only flag makes the finally's emit
            # a no-op afterwards.
            await self._emit_call_ended_once()
            self._ws = None
            self._stream_id = None
            self._client_connected.clear()
            return False

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Encode a PCM16 chunk to the negotiated codec and send to Telnyx."""
        if self._ws is None or self._stream_id is None:
            return False
        encoded = self._coalescer.append(self._encode_outbound(chunk))
        if not encoded:
            return True
        return await self._send_media_frames(encoded)

    # ── Mark support ──────────────────────────────────────────────

    async def send_mark(self, name: str | None = None) -> str:
        """Send a ``mark`` message so Telnyx acknowledges playback position.

        Any coalesced audio is flushed first so mark ordering matches the
        application's speak/mark sequencing.
        """
        ws = self._ws
        if ws is None or self._stream_id is None:
            logger.debug("Cannot send mark: no active Telnyx stream")
            raise RuntimeError("Cannot send Telnyx mark without an active stream")

        if name is None:
            name = f"mark_{len(self._pending_marks) + 1}_{int(time.monotonic() * 1000) % 10_000}"
            while name in self._pending_marks:
                name = f"{name}_"

        tail = self._coalescer.flush()
        try:
            if tail is not None:
                payload = base64.b64encode(tail).decode("ascii")
                await ws.send(json.dumps({"event": "media", "media": {"payload": payload}}))
            await ws.send(json.dumps({"event": "mark", "mark": {"name": name}}))
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send mark: Telnyx disconnected")
            # Same ordering as ``send_audio``: emit the dead call's
            # ``CallEnded`` before releasing the slot to a replacement
            # connection (see the reconnect-race note there).
            await self._emit_call_ended_once()
            self._ws = None
            self._stream_id = None
            self._client_connected.clear()
            raise RuntimeError("Cannot send Telnyx mark: Telnyx disconnected") from None
        self._pending_marks[name] = None
        return name

    async def send_playback_mark(self, name: str | None = None) -> str:
        """Compatibility wrapper for generic playback-mark capability."""
        return await self.send_mark(name=name)

    async def clear_audio(self) -> None:
        """Flush queued playback and expire outstanding marks locally.

        Telnyx may or may not echo marks queued before a ``clear``; expiring
        them here keeps barge-in bookkeeping correct either way.
        """
        ws = self._ws
        if ws is None or self._stream_id is None:
            return

        self._coalescer.reset()
        try:
            await ws.send(json.dumps({"event": "clear", "stream_id": self._stream_id}))
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot clear audio: Telnyx disconnected")
        await self._expire_pending_marks()

    # ── Telnyx WebSocket handler ──────────────────────────────────

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """Handle the Telnyx media WebSocket connection."""
        if self._ws is not None:
            logger.warning("Rejecting additional Telnyx connection")
            await ws.close(4000, "Only one stream at a time")
            return

        self._ws = ws
        self._client_connected.set()
        logger.info("Telnyx media stream connected")
        await self._receive_telnyx_messages(ws)

    def version_info(self) -> dict[str, str]:
        return make_version_info("telnyx", "websockets")


class TelnyxConnectionTransport(_TelnyxProtocolMixin, AudioQueueMixin):
    """Telnyx media transport for one accepted WebSocket connection.

    Used by the multi-call telephony server: the HTTP webhook listener mints a
    one-time stream token, answers via Call Control, and hands the accepted
    media socket to this class. ``wait_for_start()`` consumes the handshake
    stream token before a Session is compiled; ``connect()`` replays the
    stored start frame transactionally with rollback on failure.
    """

    # Telephony policy: see ``TelnyxTransport.default_echo_cancellation_enabled``.
    default_echo_cancellation_enabled = False
    inbound_stt_track = "inbound"
    # Class-level default for introspection; instances shadow this with the
    # codec-derived format from their config.
    preferred_tts_output_format: AudioFormat = TELNYX_PREFERRED_TTS_OUTPUT_FORMAT

    def __init__(
        self,
        ws: ServerConnection,
        *,
        event_bus: EventBus | None = None,
        config: TelnyxTransportConfig | None = None,
    ) -> None:
        self._ws = ws
        resolved_config = config or TelnyxTransportConfig()
        # AudioQueueMixin preserves a constructor-injected event bus while it
        # initializes the queue and diagnostics machinery.
        self._event_bus = event_bus
        self._receive_task: asyncio.Task[None] | None = None
        self._receive_tasks = RuntimeTaskScope(
            owner_label="telnyx-connection-receive",
            member_name=_TELNYX_RECEIVE_TASK_NAME,
            cohort=_TELNYX_RECEIVE_COHORT,
            logger=logger,
            failure_message="Telnyx receive loop failed",
            drop_if_closed=False,
        )
        self._pending_start_message: dict[str, Any] | None = None
        self._pending_start_claims: dict[str, str] | None = None
        self._connection_epoch: Epoch[ServerConnection | None] = Epoch(None)
        # One accepted WebSocket supports one connection lifecycle. A shared
        # task makes concurrent connect() callers observe the same tentative
        # start/observer outcome instead of treating `_connected=True` as a
        # completed handshake.
        self._connect_task: asyncio.Task[None] | None = None
        self._lifecycle_tasks = BackgroundTaskScope(name="telnyx-connection-lifecycle")
        self._socket_consumed = False
        # The accepted socket remains cleanup-owned until close succeeds.
        self._socket_close_pending = True
        self._disconnect_cleanup_error: Exception | None = None
        # Serialize socket ownership transitions; connect releases this lock
        # while dispatching deferred events so observers can still initiate
        # disconnect, and its publish phase reacquires it.
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_owner: asyncio.Task[Any] | None = None
        self._lifecycle_action: str | None = None
        self._init_audio_queue(
            resolved_config.max_pending_chunks,
            resolved_config.max_pending_bytes,
        )
        self._init_telnyx_protocol(resolved_config, event_bus)
        self.preferred_tts_output_format = resolved_config.preferred_tts_output_format
        self._coalescer = _TelnyxOutboundCoalescer(
            _codec_bytes_per_ms(self._negotiated_encoding, self._negotiated_sample_rate, 1)
        )

    def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach receive and event work to the owning transport scope."""
        super().set_runtime_scope(parent, name=name)
        scope = self._emit_scope
        assert scope is not None
        self._receive_tasks.bind(scope)

    def _current_ws(self) -> ServerConnection | None:
        return self._ws

    def _reset_connection_state(self) -> None:
        self._connected = False
        if self._connection_epoch.capture().value is self._ws:
            self._connection_epoch.bump(None)

    async def connect(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._connect_task is current:
            return
        if current is not None and self._lifecycle_owner is current:
            raise RuntimeError(
                "TelnyxConnectionTransport.connect() cannot run during disconnect()"
            )
        leader = False
        connect_task: asyncio.Task[None] | None = None
        async with self._lifecycle_lock:
            connect_task = self._connect_task
            leader = connect_task is None or connect_task.done()
            if leader:
                connect_task = self._lifecycle_tasks.create_task(
                    "telnyx-connection-connect",
                    self._connect_transaction(),
                    log_errors=False,
                )
                self._connect_task = connect_task
        if connect_task is None:
            raise RuntimeError("Telnyx connection transaction was not initialized")
        if leader:
            await connect_task
        else:
            # A secondary caller may abandon its wait without cancelling the
            # connection transaction owned by the initiating caller.
            await asyncio.shield(connect_task)

    async def _connect_transaction(self) -> None:
        """Run one shared connection attempt with serialized publish phases."""
        current = asyncio.current_task()
        async with self._lifecycle_lock:
            self._lifecycle_owner = current
            self._lifecycle_action = "connect"
            try:
                connect_state = self._begin_connect_unlocked()
            finally:
                self._lifecycle_owner = None
                self._lifecycle_action = None
        if connect_state is None:
            return
        connection, pending_start, pending_claims = connect_state
        accepted = True
        try:
            # Event handlers run outside the lifecycle lock. In particular, a
            # CallAnswered observer may synchronously await disconnect().
            if pending_start is not None:
                accepted = await self._accept_start(
                    pending_start,
                    token_prevalidated=True,
                    prevalidated_claims=pending_claims,
                )
            async with self._lifecycle_lock:
                self._lifecycle_owner = current
                self._lifecycle_action = "connect"
                try:
                    if not accepted:
                        await self._rollback_connect_unlocked(connection)
                        return
                    if (
                        not connection.guard()
                        or connection.value is not self._ws
                        or not self._connected
                    ):
                        self._clear_connection_metadata()
                        self._enqueue_sentinel()
                        raise ConnectionError("Telnyx transport disconnected during connect")
                    receive_task = self._receive_tasks.create_task(
                        self._receive_loop(),
                        task_name="telnyx-connection-receive",
                    )
                    assert receive_task is not None
                    self._receive_task = receive_task
                finally:
                    self._lifecycle_owner = None
                    self._lifecycle_action = None
        except BaseException:
            await self._rollback_connect(connection)
            raise

    def _begin_connect_unlocked(
        self,
    ) -> (
        tuple[
            Lease[ServerConnection | None],
            dict[str, Any] | None,
            dict[str, str] | None,
        ]
        | None
    ):
        """Claim the accepted socket while holding ``_lifecycle_lock``."""
        if self._connected:
            return None
        if self._disconnect_cleanup_error is not None:
            raise RuntimeError(
                "Telnyx connection cleanup is incomplete; call disconnect() "
                "again before reconnecting"
            ) from self._disconnect_cleanup_error
        if self._socket_consumed:
            if self._socket_close_pending:
                raise RuntimeError(
                    "Telnyx accepted connection has ended; call disconnect() "
                    "to finish socket cleanup"
                )
            raise RuntimeError("Telnyx accepted connection is already closed")
        if not self._socket_close_pending:
            raise RuntimeError("Telnyx accepted connection is already closed")
        self._socket_consumed = True
        self._connection_epoch.bump(self._ws)
        connection = self._connection_epoch.capture()
        self._connected = True
        self._socket_close_pending = True
        self._reset_audio_queue()
        self._client_connected.set()
        pending_start = self._pending_start_message
        pending_claims = self._pending_start_claims
        self._pending_start_message = None
        self._pending_start_claims = None
        return connection, pending_start, pending_claims

    async def _rollback_connect(self, connection: Lease[ServerConnection | None]) -> None:
        """Serialize rollback with a competing disconnect."""
        current = asyncio.current_task()
        async with self._lifecycle_lock:
            self._lifecycle_owner = current
            self._lifecycle_action = "connect"
            try:
                await self._rollback_connect_unlocked(connection)
            finally:
                self._lifecycle_owner = None
                self._lifecycle_action = None

    async def _rollback_connect_unlocked(
        self,
        connection: Lease[ServerConnection | None],
    ) -> None:
        """Roll back one connection lease while holding ``_lifecycle_lock``."""
        if not connection.guard():
            return
        self._connection_epoch.bump(None)
        self._connected = False
        self._client_connected.clear()
        self._receive_task = None
        self._clear_connection_metadata()
        self._enqueue_sentinel()
        try:
            await self._close_socket_for_disconnect()
        except asyncio.CancelledError:
            self._publish_interrupted_disconnect()
            raise
        except Exception as exc:
            self._disconnect_cleanup_error = exc
            logger.debug("Error closing Telnyx WebSocket after connect failure", exc_info=True)
        await self._drain_emit_tasks()

    async def disconnect(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "disconnect":
                return
            raise RuntimeError(
                "TelnyxConnectionTransport.disconnect() cannot run during connect()"
            )
        async with self._lifecycle_lock:
            self._lifecycle_owner = current
            self._lifecycle_action = "disconnect"
            try:
                try:
                    await self._disconnect_unlocked()
                except asyncio.CancelledError:
                    self._publish_interrupted_disconnect()
                    raise
            finally:
                self._lifecycle_owner = None
                self._lifecycle_action = None

    async def _disconnect_unlocked(self) -> None:
        """Disconnect while holding ``_lifecycle_lock``."""
        # Remote EOF clears ``_connected`` in the shared receive finalizer
        # before the owner calls disconnect. Only skip once the connection
        # task and all per-call teardown state have been released.
        if (
            not self._connected
            and self._receive_task is None
            and self._stream_id is None
            and self._call_control_id is None
            and self._call_identity is None
            and self._answered_at is None
            and not self._call_ended_emitted
            and self._pending_start_message is None
            and not self._emit_tasks
            and not self._socket_close_pending
            and self._disconnect_cleanup_error is None
        ):
            return
        self._connection_epoch.bump(None)
        self._connected = False
        self._client_connected.clear()
        receive_task = self._receive_task
        self._receive_task = None
        cleanup_errors: list[Exception] = []
        await self._reap_receive_task_for_disconnect(receive_task)
        self._clear_connection_metadata()
        if self._socket_close_pending:
            try:
                await self._close_socket_for_disconnect()
            except Exception as exc:
                logger.debug("Error closing Telnyx WebSocket", exc_info=True)
                cleanup_errors.append(exc)
        self._enqueue_sentinel()
        try:
            await self._drain_emit_tasks()
        except Exception as exc:
            logger.debug("Error draining Telnyx diagnostic events", exc_info=True)
            cleanup_errors.append(exc)
        self._disconnect_cleanup_error = cleanup_errors[0] if cleanup_errors else None
        if cleanup_errors:
            raise cleanup_errors[0]

    async def _reap_receive_task_for_disconnect(
        self,
        receive_task: asyncio.Task[None] | None,
    ) -> None:
        """Cancel the receive loop without consuming caller cancellation."""
        if receive_task is None or receive_task is asyncio.current_task():
            return
        current = asyncio.current_task()
        cancellation_count = current.cancelling() if current is not None else 0
        if not receive_task.done():
            receive_task.cancel()
        try:
            await receive_task
        except asyncio.CancelledError:
            if current is not None and current.cancelling() > cancellation_count:
                raise
        except Exception:
            logger.debug("Telnyx receive loop failed during disconnect", exc_info=True)
        if current is not None and current.cancelling() > cancellation_count:
            raise asyncio.CancelledError

    async def _close_socket_for_disconnect(self) -> None:
        """Close the accepted socket and clear its retry ledger on success."""
        await self._ws.close()
        self._socket_close_pending = False

    def _publish_interrupted_disconnect(self) -> None:
        """Preserve caller cancellation while retaining unfinished cleanup."""
        self._connected = False
        self._client_connected.clear()
        self._clear_connection_metadata()
        self._enqueue_sentinel()
        self._disconnect_cleanup_error = RuntimeError(
            "Telnyx connection disconnect was interrupted by cancellation"
        )

    def _clear_connection_metadata(self) -> None:
        self._stream_id = None
        self._call_control_id = None
        self._call_identity = None
        self._answered_at = None
        self._call_ended_emitted = False
        self._pending_start_message = None
        self._pending_start_claims = None
        self._diagnostics.reset()
        self._inbound_resampler.reset()

    def _encode_outbound(self, chunk: AudioChunk) -> bytes:
        """Encode one PCM16 chunk into the negotiated wire codec."""
        if self._negotiated_encoding == "PCMU":
            return pcm16_to_mulaw(chunk.data, chunk.format.sample_rate)
        if chunk.format.sample_rate != self._negotiated_sample_rate:
            return resample(
                chunk.data,
                chunk.format.sample_rate,
                self._negotiated_sample_rate,
            )
        return chunk.data

    async def _send_media_frames(self, frames: list[bytes]) -> bool:
        try:
            for frame in frames:
                payload = base64.b64encode(frame).decode("ascii")
                await self._ws.send(json.dumps({"event": "media", "media": {"payload": payload}}))
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send audio: Telnyx disconnected")
            self._stream_id = None
            self._connected = False
            self._client_connected.clear()
            return False

    async def send_audio(self, chunk: AudioChunk) -> bool:
        if self._stream_id is None:
            return False
        encoded = self._coalescer.append(self._encode_outbound(chunk))
        if not encoded:
            return True
        return await self._send_media_frames(encoded)

    async def clear_audio(self) -> None:
        if self._stream_id is None:
            return
        self._coalescer.reset()
        try:
            await self._ws.send(json.dumps({"event": "clear", "stream_id": self._stream_id}))
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot clear audio: Telnyx disconnected")
        await self._expire_pending_marks()

    async def send_mark(self, name: str | None = None) -> str:
        """Send a ``mark``; flush coalesced audio first so ordering holds."""
        if self._stream_id is None:
            logger.debug("Cannot send mark: no active Telnyx stream")
            raise RuntimeError("Cannot send Telnyx mark without an active stream")
        if name is None:
            name = f"mark_{len(self._pending_marks) + 1}_{int(time.monotonic() * 1000) % 10_000}"
            while name in self._pending_marks:
                name = f"{name}_"
        tail = self._coalescer.flush()
        try:
            if tail is not None:
                payload = base64.b64encode(tail).decode("ascii")
                await self._ws.send(json.dumps({"event": "media", "media": {"payload": payload}}))
            await self._ws.send(json.dumps({"event": "mark", "mark": {"name": name}}))
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send mark: Telnyx disconnected")
            self._stream_id = None
            self._connected = False
            self._client_connected.clear()
            raise RuntimeError("Cannot send Telnyx mark: Telnyx disconnected") from None
        self._pending_marks[name] = None
        return name

    async def send_playback_mark(self, name: str | None = None) -> str:
        return await self.send_mark(name=name)

    async def _store_prevalidated_start(self, msg: dict[str, Any]) -> bool | None:
        """Validate and stage a start frame seen before ``connect()``."""
        start = msg.get("start", {})
        if not isinstance(start, dict):
            logger.debug("Ignoring Telnyx start with non-object payload")
            return None
        stream_id = _telnyx_stream_id(msg, start)
        call_control_id = start.get("call_control_id")
        if not isinstance(stream_id, str) or not stream_id:
            logger.warning("Rejecting Telnyx start without stream_id")
            await self._ws.close(4003, "Missing stream_id")
            return False
        if not isinstance(call_control_id, str) or not call_control_id:
            logger.warning("Rejecting Telnyx start without call_control_id")
            await self._ws.close(4003, "Missing call_control_id")
            return False
        if not await self._negotiate_media_format(start.get("media_format")):
            await self._ws.close(4003, "Unsupported media format")
            return False
        token_claims = await _telnyx_stream_token_claims(
            token=handshake_stream_token(
                self._ws,
                parameter=self._config.stream_token_parameter,
            ),
            call_control_id=call_control_id,
            stream_id=stream_id,
            config=self._config,
        )
        if token_claims is None:
            logger.warning("Rejecting Telnyx stream start with missing or invalid stream token")
            await self._ws.close(4003, "Missing or invalid stream token")
            return False
        self._pending_start_message = msg
        self._pending_start_claims = token_claims
        return True

    async def _handle_pre_start_message(self, msg: dict[str, Any]) -> bool | None:
        event = msg.get("event")
        if event == "start":
            return await self._store_prevalidated_start(msg)

        handler = self._MESSAGE_HANDLERS.get(event) if isinstance(event, str) else None
        if handler is None:
            logger.debug("Unknown Telnyx event: %s", event)
            return None
        await handler(self, msg)
        return None

    async def wait_for_start(self, *, timeout_s: float | None = None) -> bool:
        """Read through the first authenticated Telnyx ``start`` message.

        ``serve_telnyx_voice_app`` uses this before creating an EasyCat session
        so invalid media sockets never compile provider configuration. The
        one-time handshake token is consumed here; the accepted ``start`` frame
        is stored and applied during ``connect()`` after Session has attached
        the event bus and caller-identity sink.
        """
        if timeout_s is None:
            return await self._wait_for_start()
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        try:
            async with asyncio.timeout(timeout_s):
                return await self._wait_for_start()
        except TimeoutError:
            await self._ws.close(1008, "Timed out waiting for Telnyx start")
            return False

    async def _wait_for_start(self) -> bool:
        if self._stream_id is not None or self._pending_start_message is not None:
            return True
        if self._connected:
            return self._stream_id is not None

        ws = self._ws
        try:
            async for raw in ws:
                decoded = _decode_telnyx_raw(raw)
                if decoded is None:
                    continue
                msg = _parse_telnyx_message(decoded)
                if msg is None:
                    continue
                result = await self._handle_pre_start_message(msg)
                if result is not None:
                    return result
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("Telnyx media stream disconnected before start")
            if isinstance(exc, websockets.exceptions.ConnectionClosedError):
                self._record_transport_disconnect("telnyx stream closed abnormally before start")
        return False

    async def _receive_loop(self) -> None:
        await self._receive_telnyx_messages(self._ws)

    def version_info(self) -> dict[str, str]:
        return make_version_info("telnyx-connection", "websockets")


__all__ = [
    "STREAM_TOKEN_PARAMETER",
    "TELNYX_PREFERRED_TTS_OUTPUT_FORMAT",
    "StreamTokenContext",
    "StreamTokenStore",
    "TelnyxCodec",
    "TelnyxConnectionTransport",
    "TelnyxStreamTokenStore",
    "TelnyxTransport",
    "TelnyxTransportConfig",
    "handshake_stream_token",
]
