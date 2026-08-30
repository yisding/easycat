"""Twilio Media Streams transport.

Handles Twilio's bidirectional WebSocket protocol for real phone calls.
Converts between Twilio's mulaw 8 kHz format and EasyCat's PCM16 format,
and emits DTMF / control events into the Session event bus.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from typing import Any, ClassVar

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from easycat._audio_utils import PCM16StreamResampler
from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import (
    CallAnswered,
    EventBus,
    PlaybackMarkAck,
)
from easycat.telephony._stream_tokens import (
    STREAM_TOKEN_PARAMETER,
    StreamTokenContext,
    StreamTokenStore,
)
from easycat.telephony.dtmf import parse_twilio_dtmf_message
from easycat.transports._base import (
    ServerTransportBase,
    make_version_info,
)
from easycat.transports._g711 import (
    _MULAW_DECODE_LUT,
    _MULAW_ENCODE_LUT,
    _mulaw_decode,
    _mulaw_decode_sample,
    _mulaw_encode,
    _mulaw_encode_sample,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)
from easycat.transports._limits import DEFAULT_INBOUND_AUDIO_MAX_BYTES
from easycat.transports._telephony_media import (
    StreamTokenValidator,
    TelephonyConnectionTransportBase,
    decode_telephony_raw,
    emit_call_ended,
    enforce_media_bind_auth,
    parse_telephony_message,
    run_stream_token_validation,
)
from easycat.transports._telephony_media import parse_wire_int as _parse_twilio_int

# Shared telephony codecs and stream tokens moved to ``easycat.transports._g711``
# and ``easycat.telephony._stream_tokens``; these names stay importable from here.
__all__ = [
    "STREAM_TOKEN_PARAMETER",
    "TWILIO_STREAM_TOKEN_PARAMETER",
    "_MULAW_DECODE_LUT",
    "_MULAW_ENCODE_LUT",
    "StreamTokenContext",
    "StreamTokenStore",
    "TwilioStreamTokenStore",
    "_mulaw_decode",
    "_mulaw_decode_sample",
    "_mulaw_encode",
    "_mulaw_encode_sample",
    "mulaw_to_pcm16",
    "pcm16_to_mulaw",
]

logger = logging.getLogger(__name__)

# Twilio sends/receives mulaw 8 kHz mono.
MULAW_8K = AudioFormat(sample_rate=8000, channels=1, sample_width=1, encoding="mulaw")
TWILIO_PREFERRED_TTS_OUTPUT_FORMAT = PCM16_MONO_8K
_TWILIO_OUTBOUND_TRACKS = {"outbound", "outbound_track"}
_DEGRADED_TWILIO_SEQUENCE_GAP = "twilio_sequence_gap"
_DEGRADED_TWILIO_TIMESTAMP_GAP = "twilio_timestamp_gap"
_TWILIO_MULAW_BYTES_PER_MS = 8
TWILIO_STREAM_TOKEN_PARAMETER = STREAM_TOKEN_PARAMETER

TwilioStreamTokenStore = StreamTokenStore


def _parse_twilio_message(raw: str) -> dict[str, Any] | None:
    """Parse one Twilio WebSocket message and require a JSON object."""
    return parse_telephony_message(raw, provider="Twilio")


@dataclass
class TwilioTransportConfig:
    """Configuration for :class:`TwilioTransport`."""

    preferred_tts_output_format: ClassVar[AudioFormat] = TWILIO_PREFERRED_TTS_OUTPUT_FORMAT

    host: str = "127.0.0.1"
    port: int = 8766
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200
    # Legacy validators receive the raw token string. Annotating the accepted
    # parameter as StreamTokenContext explicitly opts into start-frame context.
    stream_token_validator: StreamTokenValidator | None = None
    stream_token_parameter: str = TWILIO_STREAM_TOKEN_PARAMETER
    stream_token_validation_timeout_s: float = 5.0
    unsafe_allow_no_auth: bool = False
    max_pending_bytes: int = DEFAULT_INBOUND_AUDIO_MAX_BYTES

    def __post_init__(self) -> None:
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


def twilio_websocket_signature_process_request(
    auth_token: str,
    websocket_url: str,
) -> Callable[[ServerConnection, Request], Response | None]:
    """Build a handshake hook that authenticates Twilio before session setup.

    Twilio doesn't support query parameters in ``<Stream url>``; custom
    parameters arrive only after the WebSocket upgrade in the ``start`` frame.
    Media Streams does sign the initial handshake with ``X-Twilio-Signature``,
    so validating that header against the exact public ``wss://`` URL is the
    supported pre-upgrade authentication boundary. The existing one-time
    start-frame token remains a second, independent check.
    """
    from easycat.telephony.twiml import validate_twilio_webhook_signature

    def process_request(_ws: ServerConnection, request: Request) -> Response | None:
        signatures = request.headers.get_all("X-Twilio-Signature")
        signature = signatures[0] if len(signatures) == 1 else None
        if validate_twilio_webhook_signature(
            auth_token=auth_token,
            url=websocket_url,
            params=[],
            signature=signature,
        ):
            return None
        body = b"Missing or invalid Twilio signature.\n"
        return Response(
            HTTPStatus.UNAUTHORIZED.value,
            HTTPStatus.UNAUTHORIZED.phrase,
            Headers(
                [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                ]
            ),
            body,
        )

    return process_request


def _parse_twilio_start_identity(
    start: dict[str, Any],
    call_sid: str | None,
    *,
    excluded_parameter_names: set[str] | None = None,
) -> tuple[Any, str, str]:
    """Build a CallIdentity from Twilio ``start.customParameters``."""
    from easycat.session._types import CallDirection, CallIdentity

    params: dict[str, str] = {}
    raw_params = start.get("customParameters") or {}
    if isinstance(raw_params, dict):
        for key, value in raw_params.items():
            if isinstance(key, str) and (
                isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))
            ):
                params[key] = str(value)
    for name in excluded_parameter_names or set():
        params.pop(name, None)

    direction_raw = _clean_twilio_parameter(_pop_twilio_aliases(params, "Direction", "direction"))
    direction_token = direction_raw.strip().lower()
    direction: CallDirection
    if direction_token.startswith("outbound"):
        direction = "outbound"
    elif direction_token.startswith("inbound") or not direction_token:
        direction = "inbound"
    else:
        direction = "unknown"

    from_number = _clean_twilio_parameter(_pop_twilio_aliases(params, "From", "from"))
    to_number = _clean_twilio_parameter(_pop_twilio_aliases(params, "To", "to"))
    if direction == "outbound":
        caller = to_number
        called = from_number
    else:
        caller = from_number
        called = to_number
    display_name = _clean_twilio_parameter(
        _pop_twilio_aliases(params, "CallerName", "caller_name")
    )
    city = _clean_twilio_parameter(_pop_twilio_aliases(params, "FromCity", "from_city"))
    state = _clean_twilio_parameter(_pop_twilio_aliases(params, "FromState", "from_state"))
    zip_code = _clean_twilio_parameter(_pop_twilio_aliases(params, "FromZip", "from_zip"))
    country = _clean_twilio_parameter(_pop_twilio_aliases(params, "FromCountry", "from_country"))

    identity = CallIdentity(
        caller_number=caller,
        called_number=called,
        direction=direction,
        display_name=display_name or None,
        call_sid=call_sid,
        city=city or None,
        state=state or None,
        zip_code=zip_code or None,
        country=country or None,
        custom_fields=params,
    )
    return identity, caller, called


def _pop_twilio_aliases(params: dict[str, str], primary: str, alias: str) -> str:
    primary_value = params.pop(primary, "")
    alias_value = params.pop(alias, "")
    return primary_value or alias_value


def _clean_twilio_parameter(value: Any) -> str:
    """Return a Twilio parameter value, ignoring unsubstituted templates."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("{{") and text.endswith("}}"):
        return ""
    return text


def _stream_token_parameters(start: dict[str, Any]) -> dict[str, str] | None:
    raw_params = start.get("customParameters") or {}
    if not isinstance(raw_params, dict):
        return None
    parameters: dict[str, str] = {}
    for key, value in raw_params.items():
        if isinstance(key, str) and (
            isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))
        ):
            parameters[key] = str(value)
    return parameters


async def _twilio_stream_token_claims(
    start: dict[str, Any],
    config: TwilioTransportConfig,
    *,
    stream_sid: str | None = None,
) -> dict[str, str] | None:
    validator = config.stream_token_validator
    if validator is None:
        return {}
    parameters = _stream_token_parameters(start)
    if parameters is None:
        return None
    token = parameters.get(config.stream_token_parameter)
    if not isinstance(token, str) or not token:
        return None
    call_sid = start.get("callSid")
    context = StreamTokenContext(
        token=token,
        call_sid=call_sid if isinstance(call_sid, str) else None,
        stream_sid=stream_sid,
        parameters=parameters,
    )
    return await run_stream_token_validation(
        validator=validator,
        context=context,
        token_parameter=config.stream_token_parameter,
        validation_timeout_s=config.stream_token_validation_timeout_s,
        provider="Twilio",
    )


async def _twilio_stream_token_valid(start: dict[str, Any], config: TwilioTransportConfig) -> bool:
    return await _twilio_stream_token_claims(start, config) is not None


def _twilio_start_stream_sid(
    msg: Mapping[str, Any],
    start: Mapping[str, Any],
) -> tuple[str | None, bool]:
    """Return the canonical stream SID and whether duplicate fields agree."""
    top_stream_sid = msg.get("streamSid")
    top_stream_sid = top_stream_sid if isinstance(top_stream_sid, str) and top_stream_sid else None
    nested_stream_sid = start.get("streamSid")
    nested_stream_sid = (
        nested_stream_sid if isinstance(nested_stream_sid, str) and nested_stream_sid else None
    )
    return top_stream_sid or nested_stream_sid, (
        top_stream_sid is None or nested_stream_sid is None or top_stream_sid == nested_stream_sid
    )


def _mulaw_duration_ms(mulaw_data: bytes) -> int:
    if not mulaw_data:
        return 0
    return max(1, round(len(mulaw_data) / _TWILIO_MULAW_BYTES_PER_MS))


def _observe_twilio_sequence_gap(
    msg: dict[str, Any],
    *,
    previous_sequence: int | None,
    emit_degraded: Callable[..., None],
) -> int | None:
    sequence = _parse_twilio_int(msg.get("sequenceNumber"))
    if sequence is None:
        return None
    if previous_sequence is not None and sequence != previous_sequence + 1:
        emit_degraded(
            _DEGRADED_TWILIO_SEQUENCE_GAP,
            (
                f"streamSid={msg.get('streamSid')!s} expected sequenceNumber "
                f"{previous_sequence + 1}, got {sequence}"
            ),
        )
    return sequence


def _observe_twilio_timestamp_gap(
    media: dict[str, Any],
    *,
    stream_sid: str | None,
    previous_timestamp_ms: int | None,
    previous_duration_ms: int | None,
    emit_degraded: Callable[..., None],
) -> int | None:
    timestamp_ms = _parse_twilio_int(media.get("timestamp"))
    if timestamp_ms is None:
        return None
    if (
        previous_timestamp_ms is not None
        and previous_duration_ms is not None
        and previous_duration_ms > 0
    ):
        expected_timestamp_ms = previous_timestamp_ms + previous_duration_ms
        if timestamp_ms != expected_timestamp_ms:
            emit_degraded(
                _DEGRADED_TWILIO_TIMESTAMP_GAP,
                (
                    f"streamSid={stream_sid!s} expected media timestamp "
                    f"{expected_timestamp_ms}ms, got {timestamp_ms}ms"
                ),
            )
    return timestamp_ms


class _TwilioStreamDiagnostics:
    def __init__(self, emit_degraded: Callable[..., None]) -> None:
        self._emit_degraded = emit_degraded
        self._last_sequence_number: int | None = None
        self._last_media_timestamp_ms: int | None = None
        self._last_media_duration_ms: int | None = None

    def reset(self) -> None:
        self._last_sequence_number = None
        self._last_media_timestamp_ms = None
        self._last_media_duration_ms = None

    def start(self, msg: dict[str, Any]) -> None:
        self.reset()
        self._last_sequence_number = _parse_twilio_int(msg.get("sequenceNumber"))

    def observe_sequence(self, msg: dict[str, Any]) -> None:
        self._last_sequence_number = _observe_twilio_sequence_gap(
            msg,
            previous_sequence=self._last_sequence_number,
            emit_degraded=self._emit_degraded,
        )

    def observe_media_timestamp(
        self,
        media: dict[str, Any],
        *,
        stream_sid: str | None,
        mulaw_data: bytes,
    ) -> None:
        duration_ms = _mulaw_duration_ms(mulaw_data)
        self._last_media_timestamp_ms = _observe_twilio_timestamp_gap(
            media,
            stream_sid=stream_sid,
            previous_timestamp_ms=self._last_media_timestamp_ms,
            previous_duration_ms=self._last_media_duration_ms,
            emit_degraded=self._emit_degraded,
        )
        self._last_media_duration_ms = duration_ms


def _accepted_twilio_media(
    msg: dict[str, Any],
    *,
    active_stream_sid: str | None,
) -> dict[str, Any] | None:
    """Return the Twilio media payload only when the frame belongs to the inbound stream."""
    if active_stream_sid is None:
        logger.debug("Ignoring Twilio media before start")
        return None

    stream_sid = msg.get("streamSid")
    if stream_sid != active_stream_sid:
        logger.debug(
            "Ignoring Twilio media for streamSid=%s while active streamSid=%s",
            stream_sid,
            active_stream_sid,
        )
        return None

    media = msg.get("media", {})
    if not isinstance(media, dict):
        logger.debug("Ignoring Twilio media frame with non-object media payload")
        return None

    track = media.get("track", "")
    if track in _TWILIO_OUTBOUND_TRACKS:
        logger.debug("Ignoring Twilio outbound media track: %s", track)
        return None

    return media


def _is_active_twilio_stream_event(
    msg: dict[str, Any],
    *,
    active_stream_sid: str | None,
    event_name: str,
) -> bool:
    """Return True only when a Twilio control event belongs to the active stream."""
    if active_stream_sid is None:
        logger.debug("Ignoring Twilio %s before start", event_name)
        return False

    stream_sid = msg.get("streamSid")
    if stream_sid != active_stream_sid:
        logger.debug(
            "Ignoring Twilio %s for streamSid=%s while active streamSid=%s",
            event_name,
            stream_sid,
            active_stream_sid,
        )
        return False

    return True


async def _emit_parsed_twilio_dtmf(
    msg: dict[str, Any],
    event_bus: EventBus | None,
    *,
    session_id: str | None,
) -> None:
    event = parse_twilio_dtmf_message(msg)
    if event is None:
        logger.debug("Ignoring Twilio DTMF with invalid payload")
        return
    if event_bus is not None:
        await event_bus.emit(replace(event, session_id=session_id))


class _TwilioProtocolMixin:
    """Shared Twilio Media Streams inbound routing + handlers.

    Both :class:`TwilioTransport` (a :class:`ServerTransportBase` that accepts
    and rejects extra connections) and :class:`TwilioConnectionTransport` (a
    :class:`AudioQueueMixin` wrapping one injected connection) speak the exact
    same Media Streams wire protocol. This mixin owns the single copy of the
    inbound routing (``_handle_message``) and its handlers
    (``_handle_start``/``_handle_media``/``_handle_dtmf``), the
    once-only ``CallEnded`` emitter, the shared read-only accessors, and the
    reconnect-race-guarded finally cleanup (``_finalize_after_receive``).

    Two hooks capture the only real divergence:

    * ``_current_ws()`` — the active :class:`ServerConnection` (or ``None``).
    * ``_reset_connection_state()`` — per-class ownership reset run inside the
      guarded finally (server nulls ``self._ws``; connection clears
      ``self._connected``).

    The outbound send/mark/clear encoders stay per-class because their
    ``ConnectionClosed`` error-path resets model genuinely different lifecycles.
    """

    # Base-provided members this mixin relies on (declared for readers/type
    # checkers; supplied by ServerTransportBase / AudioQueueMixin at runtime).
    _emit_degraded: Any
    _record_transport_disconnect: Any
    _enqueue_chunk: Any
    _enqueue_sentinel: Any
    _client_connected: Any
    _diagnostics: _TwilioStreamDiagnostics
    _event_bus: EventBus | None
    _easycat_session_id: str | None
    _config: TwilioTransportConfig
    _audio_format: AudioFormat
    _stream_sid: str | None
    _call_sid: str | None
    _call_identity: Any | None
    _identity_sink: Any
    _answered_at: float | None
    _call_ended_emitted: bool
    _mark_counter: int
    _inbound_resampler: PCM16StreamResampler

    # ── Per-class hooks ───────────────────────────────────────────

    def _init_twilio_protocol(
        self,
        config: TwilioTransportConfig,
        event_bus: EventBus | None,
    ) -> None:
        """Initialize state shared by both Twilio transport lifecycles.

        Queue/server ownership must be initialized by the concrete transport
        before this method runs so ``_emit_degraded`` is ready for diagnostics.
        """
        self._config = config
        self._audio_format = config.audio_format
        self._event_bus = event_bus
        self._stream_sid = None
        self._call_sid = None
        self._call_identity = None
        self._identity_sink = None
        self._answered_at = None
        self._call_ended_emitted = False
        self._diagnostics = _TwilioStreamDiagnostics(self._emit_degraded)
        self._mark_counter = 0
        self._inbound_resampler = PCM16StreamResampler(self._audio_format.sample_rate)

    def _current_ws(self) -> ServerConnection | None:
        """Return the active Twilio WebSocket connection (or ``None``)."""
        raise NotImplementedError

    def _reset_connection_state(self) -> None:
        """Reset per-class ownership state inside the guarded finally."""
        raise NotImplementedError

    # ── Read-only accessors ───────────────────────────────────────

    @property
    def request(self) -> Any | None:
        """Accepted Twilio Media Streams WebSocket handshake request, when available."""
        return getattr(self._current_ws(), "request", None)

    @property
    def call_identity(self) -> Any | None:
        """Latest :class:`CallIdentity` parsed from the Twilio start event."""
        return self._call_identity

    @property
    def transport_kind(self) -> str:
        return "telephony"

    def bind_identity_sink(self, sink: Any) -> None:
        """Register a callback that receives every identity update.

        Used by :func:`easycat.config.create_session` to bridge the
        Twilio ``<Stream>`` ``customParameters`` (``From``, ``To``,
        ``CallerName`` …) onto the session's
        :attr:`~easycat.session._session.Session.call_identity` without
        making Session depend on the transport directly.
        """
        self._identity_sink = sink

    @property
    def stream_sid(self) -> str | None:
        return self._stream_sid

    @property
    def call_sid(self) -> str | None:
        return self._call_sid

    # ── Inbound routing ───────────────────────────────────────────

    async def _receive_twilio_messages(self, ws: ServerConnection) -> None:
        """Drive one Twilio receive stream and perform guarded cleanup."""
        try:
            async for raw in ws:
                decoded = decode_telephony_raw(raw, provider="Twilio")
                if decoded is None:
                    continue
                await self._handle_message(decoded)
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("Twilio Media Streams disconnected")
            if isinstance(exc, websockets.exceptions.ConnectionClosedError):
                self._record_transport_disconnect("twilio stream closed abnormally")
        finally:
            await self._finalize_after_receive(ws)

    async def _finalize_after_receive(self, ws: ServerConnection) -> None:
        """Run the reconnect-race-guarded finally cleanup for a receive driver.

        Only tears down when this handler still owns the active connection
        (``_current_ws() is ws``) or when the slot has already been cleared by a
        send-path error and no newer client has claimed it
        (``_current_ws() is None``). A newer client owning the slot is left
        untouched — the prerequisite #19 reconnect-race guard, kept in the
        single shared copy.
        """
        if self._current_ws() is ws or self._current_ws() is None:
            await self._emit_call_ended_once()
            tail = self._inbound_resampler.finish()
            if tail:
                self._enqueue_chunk(
                    AudioChunk(data=tail, format=self._audio_format),
                    context="Twilio",
                )
            self._reset_connection_state()
            self._client_connected.clear()
            self._stream_sid = None
            self._call_sid = None
            self._answered_at = None
            self._diagnostics.reset()
            self._enqueue_sentinel()
        # else: a newer client owns the connection -> leave it alone.

    async def _handle_message(self, raw: str) -> None:
        """Route a Twilio JSON message to the appropriate handler."""
        msg = _parse_twilio_message(raw)
        if msg is None:
            return

        event = msg.get("event")
        handler = self._MESSAGE_HANDLERS.get(event) if isinstance(event, str) else None
        if handler is None:
            logger.debug("Unknown Twilio event: %s", event)
            return
        await handler(self, msg)

    async def _handle_connected(self, msg: dict[str, Any]) -> None:
        logger.debug("Twilio connected event: protocol=%s", msg.get("protocol"))

    async def _validated_start_stream_sid(
        self,
        msg: Mapping[str, Any],
        start: Mapping[str, Any],
    ) -> str | None:
        """Return a usable start-frame stream SID, closing malformed streams."""
        stream_sid, stream_sid_valid = _twilio_start_stream_sid(msg, start)
        if stream_sid is None:
            logger.warning("Rejecting Twilio start without streamSid")
            ws = self._current_ws()
            if ws is not None:
                await ws.close(4003, "Missing streamSid")
            return None
        if not stream_sid_valid:
            logger.warning("Rejecting Twilio start with conflicting streamSid values")
            ws = self._current_ws()
            if ws is not None:
                await ws.close(4003, "Conflicting streamSid")
            return None
        return stream_sid

    async def _accept_start(
        self,
        msg: dict[str, Any],
        *,
        token_prevalidated: bool,
        prevalidated_claims: dict[str, str] | None = None,
    ) -> bool:
        """Extract stream metadata from the ``start`` message.

        Twilio's Media Streams ``start`` payload carries streamSid /
        callSid plus anything you pass through as ``<Parameter>``
        children of the TwiML ``<Stream>``.  The convention — emitted
        by :func:`twiml_connect_stream` — is to forward actual webhook
        values for ``Direction``, ``From``, ``To``, ``CallerName`` *and*
        Twilio's geographic fields (``FromCity``, ``FromState``,
        ``FromZip``, ``FromCountry``) so the voice pipeline sees who is
        on the far end without a secondary Lookup API round-trip:

        .. code-block:: xml

            <Stream url="wss://…">
              <Parameter name="Direction" value="inbound"/>
              <Parameter name="From" value="+15551234567"/>
              <Parameter name="To" value="+15557654321"/>
              <Parameter name="CallerName" value="Alice Example"/>
              <Parameter name="FromCity" value="SAN FRANCISCO"/>
              <Parameter name="FromState" value="CA"/>
              <Parameter name="FromZip" value="94105"/>
              <Parameter name="FromCountry" value="US"/>
            </Stream>

        This method also emits a :class:`~easycat.events.CallAnswered`
        event so observers get a consistent inbound + outbound
        lifecycle.
        """
        start = msg.get("start", {})
        if not isinstance(start, dict):
            logger.debug("Ignoring Twilio start with non-object payload")
            return False
        stream_sid = await self._validated_start_stream_sid(msg, start)
        if stream_sid is None:
            return False
        token_claims = (
            prevalidated_claims
            if token_prevalidated
            else await _twilio_stream_token_claims(
                start,
                self._config,
                stream_sid=stream_sid,
            )
        )
        if token_claims is None:
            logger.warning("Rejecting Twilio stream start with missing or invalid stream token")
            ws = self._current_ws()
            if ws is not None:
                await ws.close(4003, "Missing or invalid stream token")
            return False
        self._inbound_resampler.reset()
        self._stream_sid = stream_sid
        self._call_sid = start.get("callSid")
        self._answered_at = time.monotonic()
        self._call_ended_emitted = False
        self._diagnostics.start(msg)
        identity, caller, called = _parse_twilio_start_identity(
            start,
            self._call_sid,
            excluded_parameter_names={self._config.stream_token_parameter},
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

        if self._event_bus is not None and self._call_sid:
            await self._event_bus.emit(
                CallAnswered(
                    call_sid=self._call_sid,
                    answered_by="human",
                    session_id=self._easycat_session_id,
                )
            )

        logger.info(
            "Twilio stream started: streamSid=%s callSid=%s from=%s to=%s",
            self._stream_sid,
            self._call_sid,
            caller,
            called,
        )
        return True

    async def _handle_start(self, msg: dict[str, Any]) -> None:
        await self._accept_start(msg, token_prevalidated=False)

    async def _handle_media(self, msg: dict[str, Any]) -> None:
        """Decode mulaw audio from a ``media`` message and enqueue as PCM16."""
        media = _accepted_twilio_media(msg, active_stream_sid=self._stream_sid)
        if media is None:
            return
        self._diagnostics.observe_sequence(msg)
        payload = media.get("payload", "")
        if not payload:
            return

        try:
            mulaw_data = base64.b64decode(payload, validate=True)
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.warning("Ignoring Twilio media frame with invalid base64 payload")
            return
        self._diagnostics.observe_media_timestamp(
            media,
            stream_sid=self._stream_sid,
            mulaw_data=mulaw_data,
        )
        pcm_data = self._inbound_resampler.process(_mulaw_decode(mulaw_data), 8000)

        if pcm_data:
            chunk = AudioChunk(data=pcm_data, format=self._audio_format)
            self._enqueue_chunk(chunk, context="Twilio")

    async def _handle_stop(self, msg: dict[str, Any]) -> None:
        if not _is_active_twilio_stream_event(
            msg,
            active_stream_sid=self._stream_sid,
            event_name="stop",
        ):
            return
        self._diagnostics.observe_sequence(msg)
        logger.info("Twilio stream stopped (streamSid=%s)", self._stream_sid)
        tail = self._inbound_resampler.finish()
        if tail:
            self._enqueue_chunk(
                AudioChunk(data=tail, format=self._audio_format),
                context="Twilio",
            )
        # Mirror the outbound call manager lifecycle for inbound calls.
        await self._emit_call_ended_once()
        self._stream_sid = None
        self._call_sid = None
        self._answered_at = None
        self._diagnostics.reset()
        self._enqueue_sentinel()

    async def _handle_mark(self, msg: dict[str, Any]) -> None:
        if not _is_active_twilio_stream_event(
            msg,
            active_stream_sid=self._stream_sid,
            event_name="mark",
        ):
            return
        self._diagnostics.observe_sequence(msg)
        mark = msg.get("mark")
        if not isinstance(mark, dict):
            logger.debug("Ignoring Twilio mark with non-object payload")
            return
        mark_name = mark.get("name")
        if not isinstance(mark_name, str) or not mark_name:
            logger.debug("Ignoring Twilio mark with invalid name")
            return
        logger.debug("Twilio mark acknowledged: %s", mark_name)
        if self._event_bus is not None:
            await self._event_bus.emit(
                PlaybackMarkAck(mark_name=mark_name, session_id=self._easycat_session_id)
            )

    async def _emit_call_ended_once(self) -> None:
        if self._call_ended_emitted or self._call_sid is None:
            return
        self._call_ended_emitted = True
        await emit_call_ended(
            self._event_bus,
            call_id=self._call_sid,
            answered_at=self._answered_at,
            call_identity=self._call_identity,
            session_id=self._easycat_session_id,
        )

    async def _handle_dtmf(self, msg: dict[str, Any]) -> None:
        """Emit a DTMF event for the pressed digit."""
        if _is_active_twilio_stream_event(
            msg,
            active_stream_sid=self._stream_sid,
            event_name="dtmf",
        ):
            self._diagnostics.observe_sequence(msg)
            await _emit_parsed_twilio_dtmf(
                msg,
                self._event_bus,
                session_id=self._easycat_session_id,
            )

    _MessageHandler = Callable[["_TwilioProtocolMixin", dict[str, Any]], Awaitable[None]]
    _MESSAGE_HANDLERS: ClassVar[dict[str, _MessageHandler]] = {
        "connected": _handle_connected,
        "start": _handle_start,
        "media": _handle_media,
        "dtmf": _handle_dtmf,
        "stop": _handle_stop,
        "mark": _handle_mark,
    }


class TwilioTransport(_TwilioProtocolMixin, ServerTransportBase):
    """Transport for Twilio Media Streams bidirectional WebSocket.

    Implements the ``Transport`` protocol from :mod:`easycat.providers`.

    Twilio message types handled:
      - ``connected`` — initial connection acknowledgement
      - ``start``     — stream metadata (streamSid, callSid, tracks, etc.)
      - ``media``     — base64-encoded mulaw 8 kHz audio
      - ``stop``      — stream ended
      - ``mark``      — playback mark acknowledgement
      - ``dtmf``      — DTMF digit pressed by caller

    DTMF digits are emitted into the provided ``EventBus`` so downstream
    consumers can handle them.  Audio is converted from mulaw 8 kHz to the internal PCM16
    format (default 16 kHz) on ingest, and back on egress.
    """

    _transport_name = "Twilio"
    # Telephony policy: leave EasyCat-side echo cancellation off by default.
    # Twilio's PSTN/SIP path handles line echo upstream, and the 8 kHz mulaw
    # mono stream has no reliable local reference signal for software AEC.
    # Declared explicitly so the choice is intentional, not a getattr fallback.
    default_echo_cancellation_enabled = False
    # Captured audio is inbound-only: ``_accepted_twilio_media`` drops every
    # ``outbound``/``outbound_track`` frame at ingest before it reaches STT, so
    # transcripts derived from this transport are safely the callee's speech.
    # Session wiring stamps this label onto STTFinal/STTPartial events that the
    # STT provider leaves unlabeled, letting telephony classifiers (e.g. the
    # outbound voicemail-pickup guard) trust the inbound track in production.
    inbound_stt_track = "inbound"
    # Outbound Twilio media must be 8 kHz mulaw. Ask TTS for 8 kHz PCM16
    # so send_audio only performs the final companding step.
    preferred_tts_output_format: ClassVar[AudioFormat] = TWILIO_PREFERRED_TTS_OUTPUT_FORMAT

    def __init__(
        self,
        config: TwilioTransportConfig | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        resolved_config = config or TwilioTransportConfig()
        super().__init__(
            host=resolved_config.host,
            port=resolved_config.port,
            max_pending_chunks=resolved_config.max_pending_chunks,
            max_pending_bytes=resolved_config.max_pending_bytes,
        )
        self._init_twilio_protocol(resolved_config, event_bus)

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Start the media listener after enforcing a safe public bind."""
        enforce_media_bind_auth(
            host=self._config.host,
            provider_label="Twilio",
            config_class_name="TwilioTransportConfig",
            stream_token_validator=self._config.stream_token_validator,
            unsafe_allow_no_auth=self._config.unsafe_allow_no_auth,
        )
        await super().connect()

    def _current_ws(self) -> ServerConnection | None:
        return self._ws

    def _reset_connection_state(self) -> None:
        self._ws = None

    async def disconnect(self) -> None:
        """Disconnect Twilio and stop the server."""
        await super().disconnect()
        self._stream_sid = None
        self._call_sid = None
        self._call_identity = None
        self._call_ended_emitted = False
        self._diagnostics.reset()
        self._inbound_resampler.reset()

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Convert a PCM16 chunk to mulaw 8 kHz and send to Twilio."""
        ws = self._ws
        if ws is None or self._stream_sid is None:
            return False

        mulaw_data = pcm16_to_mulaw(chunk.data, chunk.format.sample_rate)
        payload = base64.b64encode(mulaw_data).decode("ascii")

        message = json.dumps(
            {
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": payload},
            }
        )
        try:
            await ws.send(message)
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send audio: Twilio disconnected")
            # Emit the dead call's ``CallEnded`` BEFORE releasing the slot:
            # once ``self._ws`` is cleared a replacement connection can claim
            # it, and the old receive handler's reconnect-race-guarded finally
            # (``_finalize_after_receive``) then skips its emit — a later
            # ``start`` would overwrite the old call ids and observers would
            # never see the previous call end. The once-only flag makes the
            # finally's emit a no-op afterwards.
            await self._emit_call_ended_once()
            # Mirror the connection-variant / WebSocket transports: clear
            # the live-client state so ``has_client``/``is_connected``/
            # ``wait_for_client`` stop reporting a peer once the socket drops.
            # The ``_handle_connection`` finally also performs this cleanup, so
            # this is belt-and-suspenders for symmetry across socket transports.
            self._ws = None
            self._stream_sid = None
            self._client_connected.clear()
            return False

    # ── Mark support ──────────────────────────────────────────────

    async def send_mark(self, name: str | None = None) -> str:
        """Send a ``mark`` message so Twilio acknowledges playback position.

        Returns the mark name used (auto-generated if not provided).
        """
        ws = self._ws
        if ws is None or self._stream_sid is None:
            logger.debug("Cannot send mark: no active Twilio stream")
            raise RuntimeError("Cannot send Twilio mark without an active stream")

        if name is None:
            self._mark_counter += 1
            name = f"mark_{self._mark_counter}"

        message = json.dumps(
            {
                "event": "mark",
                "streamSid": self._stream_sid,
                "mark": {"name": name},
            }
        )
        try:
            await ws.send(message)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send mark: Twilio disconnected")
            # Same ordering as ``send_audio``: emit the dead call's
            # ``CallEnded`` before releasing the slot to a replacement
            # connection (see the reconnect-race note there).
            await self._emit_call_ended_once()
            self._ws = None
            self._stream_sid = None
            self._client_connected.clear()
            raise RuntimeError("Cannot send Twilio mark: Twilio disconnected") from None
        return name

    async def send_playback_mark(self, name: str | None = None) -> str:
        """Compatibility wrapper for generic playback-mark capability."""
        return await self.send_mark(name=name)

    async def clear_audio(self) -> None:
        """Send a ``clear`` message to discard queued outbound audio on Twilio's side."""
        ws = self._ws
        if ws is None or self._stream_sid is None:
            return

        message = json.dumps(
            {
                "event": "clear",
                "streamSid": self._stream_sid,
            }
        )
        try:
            await ws.send(message)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot clear audio: Twilio disconnected")

    # ── Twilio WebSocket handler ──────────────────────────────────

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """Handle the Twilio Media Streams WebSocket connection."""
        if self._ws is not None:
            logger.warning("Rejecting additional Twilio connection")
            await ws.close(4000, "Only one stream at a time")
            return

        self._ws = ws
        self._client_connected.set()
        logger.info("Twilio Media Streams connected")
        await self._receive_twilio_messages(ws)

    def version_info(self) -> dict[str, str]:
        return make_version_info("twilio", "websockets")


class TwilioConnectionTransport(_TwilioProtocolMixin, TelephonyConnectionTransportBase):
    """Twilio Media Streams transport for one accepted WebSocket connection."""

    # Telephony policy: see ``TwilioTransport.default_echo_cancellation_enabled``.
    # PSTN echo is handled upstream and the mulaw stream lacks a local reference
    # signal, so EasyCat-side AEC defaults off — declared explicitly, not via
    # getattr fallback.
    default_echo_cancellation_enabled = False
    # Inbound-only capture: see ``TwilioTransport.inbound_stt_track``.
    inbound_stt_track = "inbound"
    # Outbound Twilio media must be 8 kHz mulaw. Ask TTS for 8 kHz PCM16
    # so send_audio only performs the final companding step.
    preferred_tts_output_format: ClassVar[AudioFormat] = TWILIO_PREFERRED_TTS_OUTPUT_FORMAT
    _PROVIDER_LABEL = "Twilio"

    def __init__(
        self,
        ws: ServerConnection,
        *,
        event_bus: EventBus | None = None,
        config: TwilioTransportConfig | None = None,
    ) -> None:
        resolved_config = config or TwilioTransportConfig()
        super().__init__(
            ws,
            event_bus=event_bus,
            max_pending_chunks=resolved_config.max_pending_chunks,
            max_pending_bytes=resolved_config.max_pending_bytes,
        )
        self._init_twilio_protocol(resolved_config, event_bus)

    # ── Shared-lifecycle hooks ────────────────────────────────────

    def _current_ws(self) -> ServerConnection | None:
        return self._ws

    def _reset_connection_state(self) -> None:
        self._connected = False
        if self._connection_epoch.capture().value is self._ws:
            self._connection_epoch.bump(None)

    def _has_accepted_stream(self) -> bool:
        return self._stream_sid is not None

    def _has_active_call_state(self) -> bool:
        return (
            self._stream_sid is not None
            or self._call_sid is not None
            or self._call_identity is not None
            or self._answered_at is not None
            or self._call_ended_emitted
        )

    def _clear_call_refs(self) -> None:
        self._stream_sid = None
        self._call_sid = None

    async def _run_receive_loop(self) -> None:
        await self._receive_twilio_messages(self._ws)

    async def send_audio(self, chunk: AudioChunk) -> bool:
        if self._stream_sid is None:
            return False
        mulaw_data = pcm16_to_mulaw(chunk.data, chunk.format.sample_rate)
        payload = base64.b64encode(mulaw_data).decode("ascii")
        message = json.dumps(
            {
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": payload},
            }
        )
        try:
            await self._ws.send(message)
            return True
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send audio: Twilio disconnected")
            self._stream_sid = None
            self._connected = False
            self._client_connected.clear()
            return False

    async def clear_audio(self) -> None:
        if self._stream_sid is None:
            return
        try:
            await self._ws.send(json.dumps({"event": "clear", "streamSid": self._stream_sid}))
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot clear audio: Twilio disconnected")

    async def send_mark(self, name: str | None = None) -> str:
        if self._stream_sid is None:
            logger.debug("Cannot send mark: no active Twilio stream")
            raise RuntimeError("Cannot send Twilio mark without an active stream")
        if name is None:
            self._mark_counter += 1
            name = f"mark_{self._mark_counter}"
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "event": "mark",
                        "streamSid": self._stream_sid,
                        "mark": {"name": name},
                    }
                )
            )
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send mark: Twilio disconnected")
            self._stream_sid = None
            self._connected = False
            self._client_connected.clear()
            raise RuntimeError("Cannot send Twilio mark: Twilio disconnected") from None
        return name

    async def _store_prevalidated_start(self, msg: dict[str, Any]) -> bool | None:
        start = msg.get("start", {})
        if not isinstance(start, dict):
            logger.debug("Ignoring Twilio start with non-object payload")
            return None
        stream_sid = await self._validated_start_stream_sid(msg, start)
        if stream_sid is None:
            return False
        token_claims = await _twilio_stream_token_claims(
            start,
            self._config,
            stream_sid=stream_sid,
        )
        if token_claims is None:
            logger.warning("Rejecting Twilio stream start with missing or invalid stream token")
            await self._ws.close(4003, "Missing or invalid stream token")
            return False
        self._pending_start_message = msg
        self._pending_start_claims = token_claims
        return True

    def version_info(self) -> dict[str, str]:
        return make_version_info("twilio-connection", "websockets")


# ── TwiML helpers ─────────────────────────────────────────────────


def twiml_connect_stream(
    websocket_url: str,
    *,
    track: str = "both",
    status_callback_url: str | None = None,
    parameters: dict[str, str] | None = None,
    stream_token: str | None = None,
    forward_caller_id: bool = False,
) -> str:
    """Generate TwiML ``<Connect><Stream>`` XML for bidirectional streaming.

    Parameters
    ----------
    websocket_url:
        The ``wss://`` URL of the EasyCat Twilio transport server.
    track:
        Which audio tracks to stream (``inbound``, ``outbound``, or ``both``).
    status_callback_url:
        Optional URL for Twilio to POST call status updates.
    parameters:
        Extra ``<Parameter>`` children to attach to the stream.  Pass
        actual values from the Twilio webhook request, e.g.
        ``{"From": form["From"], "To": form["To"]}``.  Twilio forwards
        generated TwiML parameter values verbatim, so ``"{{From}}"``
        placeholders are not substituted for Python-generated XML.
    stream_token:
        Optional signed one-time token to pass as
        ``EasyCatStreamToken``. Pair this with
        ``TwilioTransportConfig(stream_token_validator=store.consume)``.
    forward_caller_id:
        Kept as a compatibility assertion.  When ``True``, at least one
        caller-ID parameter must be supplied explicitly in ``parameters``;
        no placeholder values are generated.
    """
    from xml.sax.saxutils import quoteattr

    status_attr = ""
    if status_callback_url:
        status_attr = f" statusCallback={quoteattr(status_callback_url)}"

    merged: dict[str, str] = dict(parameters or {})
    if stream_token is not None:
        merged[TWILIO_STREAM_TOKEN_PARAMETER] = stream_token
    if forward_caller_id:
        identity_names = {
            "From",
            "To",
            "CallerName",
            "FromCity",
            "FromState",
            "FromZip",
            "FromCountry",
            "from",
            "to",
            "caller_name",
            "from_city",
            "from_state",
            "from_zip",
            "from_country",
        }
        if not any(
            _clean_twilio_parameter(value)
            for name, value in merged.items()
            if name in identity_names
        ):
            raise ValueError(
                "forward_caller_id=True requires explicit caller-ID values in "
                "parameters; Twilio does not substitute {{From}} placeholders "
                "inside generated TwiML."
            )

    if not merged:
        stream = (
            f"    <Stream url={quoteattr(websocket_url)} track={quoteattr(track)}{status_attr} />"
        )
    else:
        param_lines = "\n".join(
            f"      <Parameter name={quoteattr(str(name))} value={quoteattr(str(value))}/>"
            for name, value in merged.items()
        )
        stream = (
            f"    <Stream url={quoteattr(websocket_url)} "
            f"track={quoteattr(track)}{status_attr}>\n"
            f"{param_lines}\n"
            "    </Stream>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f"{stream}\n"
        "  </Connect>\n"
        "</Response>"
    )


def twiml_stream(
    websocket_url: str,
    *,
    track: str = "inbound_track",
    parameters: dict[str, str] | None = None,
    stream_token: str | None = None,
) -> str:
    """Generate TwiML ``<Start><Stream>`` XML for one-way streaming.

    Parameters
    ----------
    websocket_url:
        The ``wss://`` URL of the EasyCat Twilio transport server.
    track:
        Which track to stream (``inbound_track`` or ``outbound_track``).
    parameters:
        Extra ``<Parameter>`` children to attach to the stream.
    stream_token:
        Optional signed one-time token to pass as
        ``EasyCatStreamToken``.
    """
    from xml.sax.saxutils import quoteattr

    merged: dict[str, str] = dict(parameters or {})
    if stream_token is not None:
        merged[TWILIO_STREAM_TOKEN_PARAMETER] = stream_token
    if not merged:
        stream = f"    <Stream url={quoteattr(websocket_url)} track={quoteattr(track)} />"
    else:
        param_lines = "\n".join(
            f"      <Parameter name={quoteattr(str(name))} value={quoteattr(str(value))}/>"
            for name, value in merged.items()
        )
        stream = (
            f"    <Stream url={quoteattr(websocket_url)} track={quoteattr(track)}>\n"
            f"{param_lines}\n"
            "    </Stream>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Start>\n"
        f"{stream}\n"
        "  </Start>\n"
        '  <Pause length="60" />\n'
        "</Response>"
    )
