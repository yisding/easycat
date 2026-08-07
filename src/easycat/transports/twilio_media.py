"""Twilio Media Streams transport.

Handles Twilio's bidirectional WebSocket protocol for real phone calls.
Converts between Twilio's mulaw 8 kHz format and EasyCat's PCM16 format,
and emits DTMF / control events into the Session event bus.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import math
import secrets
import struct
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from typing import Any, ClassVar, cast, get_type_hints

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from easycat._audio_utils import PCM16StreamResampler, resample
from easycat._epoch import Epoch, Lease
from easycat._net import is_loopback_host
from easycat._numeric import is_finite_number
from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import (
    CallAnswered,
    CallEnded,
    EventBus,
    PlaybackMarkAck,
)
from easycat.runtime._event_tasks import RuntimeTaskScope
from easycat.runtime.scope import BackgroundTaskScope, RuntimeScope
from easycat.telephony.dtmf import parse_twilio_dtmf_message
from easycat.transports._base import (
    AudioQueueMixin,
    ServerTransportBase,
    make_version_info,
)
from easycat.transports._limits import DEFAULT_INBOUND_AUDIO_MAX_BYTES

logger = logging.getLogger(__name__)

# Twilio sends/receives mulaw 8 kHz mono.
MULAW_8K = AudioFormat(sample_rate=8000, channels=1, sample_width=1, encoding="mulaw")
TWILIO_PREFERRED_TTS_OUTPUT_FORMAT = PCM16_MONO_8K
_TWILIO_OUTBOUND_TRACKS = {"outbound", "outbound_track"}
_DEGRADED_TWILIO_SEQUENCE_GAP = "twilio_sequence_gap"
_DEGRADED_TWILIO_TIMESTAMP_GAP = "twilio_timestamp_gap"
_TWILIO_MULAW_BYTES_PER_MS = 8
_TWILIO_STREAM_TOKEN_TIME_SCALE = 1_000_000_000
TWILIO_STREAM_TOKEN_PARAMETER = "EasyCatStreamToken"
_TWILIO_RECEIVE_TASK_NAME = "twilio_receive"
_TWILIO_RECEIVE_COHORT = "transport-receive"


def _parse_twilio_message(raw: str) -> dict[str, Any] | None:
    """Parse one Twilio WebSocket message and require a JSON object."""
    try:
        msg = json.loads(raw)
    except (RecursionError, ValueError):
        logger.warning("Ignoring invalid JSON from Twilio")
        return None
    if not isinstance(msg, dict):
        logger.warning("Ignoring non-object JSON from Twilio")
        return None
    return msg


def _decode_twilio_raw(raw: str | bytes) -> str | None:
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("Ignoring non-UTF-8 Twilio message")
        return None


@dataclass(frozen=True)
class _TwilioStreamGrant:
    token: str
    expires_at_ns: int
    claims: tuple[tuple[str, str], ...]


class TwilioStreamTokenStore:
    """Issue and consume signed one-time Twilio ``<Stream>`` tokens.

    The store is intentionally in-memory: the process that emits TwiML also
    consumes the subsequent Media Streams ``start`` event. Apps running multiple
    replicas can provide their own validator via ``TwilioTransportConfig``.
    """

    def __init__(
        self,
        secret: str | bytes | None = None,
        *,
        ttl_s: float = 300.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not is_finite_number(ttl_s) or ttl_s <= 0:
            raise ValueError("ttl_s must be a finite positive number")
        if secret is None:
            secret = secrets.token_urlsafe(32)
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._ttl_s = float(ttl_s)
        self._now = now
        self._pending: dict[str, _TwilioStreamGrant] = {}
        self._idempotent: dict[str, _TwilioStreamGrant] = {}

    def issue(
        self,
        *,
        idempotency_key: str | None = None,
        claims: Mapping[str, str] | None = None,
    ) -> str:
        """Return a signed token accepted by exactly one future ``consume``.

        Reusing an idempotency key returns the original token until its TTL
        expires, even if media preflight already consumed it. Twilio can retry
        the same webhook without receiving additional authorizations.
        """
        self._prune_expired()
        normalized_claims = tuple(
            sorted((str(name), str(value)) for name, value in (claims or {}).items() if value)
        )
        if idempotency_key:
            existing = self._idempotent.get(idempotency_key)
            if existing is not None:
                if existing.claims != normalized_claims:
                    raise ValueError("idempotency_key cannot be reused with different claims")
                return existing.token

        nonce = secrets.token_urlsafe(24)
        expires_at_ns = math.ceil((self._now() + self._ttl_s) * _TWILIO_STREAM_TOKEN_TIME_SCALE)
        payload = f"{nonce}.{expires_at_ns}"
        signature = self._signature(payload)
        token = f"{payload}.{signature}"
        grant = _TwilioStreamGrant(
            token=token,
            expires_at_ns=expires_at_ns,
            claims=normalized_claims,
        )
        self._pending[nonce] = grant
        if idempotency_key:
            self._idempotent[idempotency_key] = grant
        return token

    def issue_parameter(self) -> dict[str, str]:
        """Return the TwiML ``<Parameter>`` mapping for a fresh token."""
        return {TWILIO_STREAM_TOKEN_PARAMETER: self.issue()}

    def consume(self, token: str) -> bool:
        """Validate and consume a token, returning ``False`` on replay/expiry."""
        return self._consume(token, start=None)

    def consume_start(self, context: StreamTokenContext) -> bool:
        """Consume a token only when its bound webhook claims match ``start``."""
        return self._consume(
            context.token,
            start={
                "callSid": context.call_sid,
                "customParameters": context.parameters,
            },
        )

    def _consume(self, token: str, *, start: Mapping[str, Any] | None) -> bool:
        self._prune_expired()
        parts = token.split(".")
        if len(parts) != 3:
            return False
        nonce, expires_text, signature = parts
        try:
            expires_at_ns = int(expires_text)
        except ValueError:
            return False

        payload = f"{nonce}.{expires_at_ns}"
        try:
            matches_signature = hmac.compare_digest(signature, self._signature(payload))
        except TypeError:
            return False
        if not matches_signature:
            return False
        now_ns = math.floor(self._now() * _TWILIO_STREAM_TOKEN_TIME_SCALE)
        if expires_at_ns < now_ns:
            self._pending.pop(nonce, None)
            return False
        grant = self._pending.pop(nonce, None)
        if grant is None or grant.expires_at_ns != expires_at_ns:
            return False
        return _twilio_grant_claims_match(grant.claims, start)

    def _signature(self, payload: str) -> str:
        digest = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _prune_expired(self) -> None:
        now_ns = math.floor(self._now() * _TWILIO_STREAM_TOKEN_TIME_SCALE)
        expired = [nonce for nonce, grant in self._pending.items() if grant.expires_at_ns < now_ns]
        for nonce in expired:
            self._pending.pop(nonce, None)
        expired_keys = [
            key for key, grant in self._idempotent.items() if grant.expires_at_ns < now_ns
        ]
        for key in expired_keys:
            self._idempotent.pop(key, None)


def _twilio_grant_claims_match(
    claims: tuple[tuple[str, str], ...],
    start: Mapping[str, Any] | None,
) -> bool:
    if not claims:
        return True
    if start is None:
        return False
    custom_parameters = start.get("customParameters")
    params = custom_parameters if isinstance(custom_parameters, Mapping) else {}
    for name, expected in claims:
        actual = start.get("callSid") if name == "CallSid" else params.get(name)
        if not isinstance(actual, (str, int)) or isinstance(actual, bool):
            return False
        if str(actual) != expected:
            return False
    return True


@dataclass(frozen=True, slots=True)
class StreamTokenContext:
    """Twilio stream-token validation context from the ``start`` frame."""

    token: str
    call_sid: str | None
    stream_sid: str | None
    parameters: Mapping[str, str]


StreamTokenClaims = Mapping[str, Any]
StreamTokenValidatorResult = bool | StreamTokenClaims | None
StreamTokenValidator = (
    Callable[[str], StreamTokenValidatorResult | Awaitable[StreamTokenValidatorResult]]
    | Callable[
        [StreamTokenContext], StreamTokenValidatorResult | Awaitable[StreamTokenValidatorResult]
    ]
)


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
    context = StreamTokenContext(
        token=token,
        call_sid=start.get("callSid") if isinstance(start.get("callSid"), str) else None,
        stream_sid=stream_sid,
        parameters=parameters,
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
        logger.warning("Twilio stream token validator timed out")
        return None
    except Exception:
        logger.warning("Twilio stream token validator raised", exc_info=True)
        return None


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


def _parse_twilio_int(value: Any) -> int | None:
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


async def _emit_twilio_call_ended(
    event_bus: EventBus | None,
    *,
    call_sid: str | None,
    answered_at: float | None,
    call_identity: Any | None,
    session_id: str | None,
) -> None:
    if event_bus is None or call_sid is None:
        return
    duration = None
    if answered_at is not None:
        duration = max(0.0, time.monotonic() - answered_at)
    await event_bus.emit(
        CallEnded(
            call_sid=call_sid,
            duration_s=duration,
            number=call_identity.caller_number if call_identity is not None else None,
            session_id=session_id,
        )
    )


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
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        logger.warning("Ignoring non-UTF-8 Twilio message")
                        continue
                await self._handle_message(raw)
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
            mulaw_data = base64.b64decode(payload)
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
        await _emit_twilio_call_ended(
            self._event_bus,
            call_sid=self._call_sid,
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
        if (
            not is_loopback_host(self._config.host)
            and self._config.stream_token_validator is None
            and not self._config.unsafe_allow_no_auth
        ):
            raise ValueError(
                "TwilioTransportConfig.stream_token_validator is required when "
                "binding Twilio media to a non-loopback host; pass "
                "unsafe_allow_no_auth=True only for an intentionally unauthenticated listener"
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


# ── Audio conversion helpers ──────────────────────────────────────


def mulaw_to_pcm16(mulaw_data: bytes, target_rate: int = 16000) -> bytes:
    """Convert mulaw 8 kHz audio to PCM16 at ``target_rate``."""
    pcm_8k = _mulaw_decode(mulaw_data)
    if target_rate == 8000:
        return pcm_8k
    return resample(pcm_8k, 8000, target_rate)


def pcm16_to_mulaw(pcm_data: bytes, source_rate: int = 16000) -> bytes:
    """Convert PCM16 at ``source_rate`` to mulaw 8 kHz."""
    if source_rate != 8000:
        pcm_data = resample(pcm_data, source_rate, 8000)
    return _mulaw_encode(pcm_data)


_MULAW_BIAS = 0x84
_MULAW_CLIP = 32635


def _mulaw_decode(mulaw_data: bytes) -> bytes:
    """Decode G.711 mu-law bytes into PCM16 little-endian bytes."""
    if not mulaw_data:
        return b""
    return struct.pack(f"<{len(mulaw_data)}h", *map(_MULAW_DECODE_LUT.__getitem__, mulaw_data))


def _mulaw_encode(pcm_data: bytes) -> bytes:
    """Encode PCM16 little-endian bytes into G.711 mu-law bytes."""
    if len(pcm_data) % 2 != 0:
        pcm_data = pcm_data[:-1]
    if not pcm_data:
        return b""
    count = len(pcm_data) // 2
    return bytes(map(_MULAW_ENCODE_LUT.__getitem__, struct.unpack(f"<{count}H", pcm_data)))


def _mulaw_decode_sample(value: int) -> int:
    """Decode a single mu-law byte into a signed PCM16 sample."""
    value = (~value) & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    sample = ((mantissa << 3) + _MULAW_BIAS) << exponent
    sample -= _MULAW_BIAS
    if sign:
        sample = -sample
    return sample


def _mulaw_encode_sample(sample: int) -> int:
    """Encode a signed PCM16 sample into a mu-law byte."""
    if sample < 0:
        sign = 0x80
        sample = -sample
    else:
        sign = 0x00

    sample = min(sample, _MULAW_CLIP)

    sample += _MULAW_BIAS
    exponent = 7
    exp_mask = 0x4000
    while exponent > 0 and (sample & exp_mask) == 0:
        exponent -= 1
        exp_mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


# Table-driven G.711 codec. The tables are precomputed from the reference
# per-sample formulas above, so table-driven output is byte-identical to the
# per-sample loops while avoiding per-sample Python work on the hot audio path.
_MULAW_DECODE_LUT: tuple[int, ...] = tuple(_mulaw_decode_sample(i) for i in range(256))
_MULAW_ENCODE_LUT: bytes = bytes(
    _mulaw_encode_sample(s if s < 32768 else s - 65536) for s in range(65536)
)


class TwilioConnectionTransport(_TwilioProtocolMixin, AudioQueueMixin):
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

    def __init__(
        self,
        ws: ServerConnection,
        *,
        event_bus: EventBus | None = None,
        config: TwilioTransportConfig | None = None,
    ) -> None:
        self._ws = ws
        resolved_config = config or TwilioTransportConfig()
        # AudioQueueMixin preserves a constructor-injected event bus while it
        # initializes the queue and diagnostics machinery.
        self._event_bus = event_bus
        self._receive_task: asyncio.Task[None] | None = None
        self._receive_tasks = RuntimeTaskScope(
            owner_label="twilio-connection-receive",
            member_name=_TWILIO_RECEIVE_TASK_NAME,
            cohort=_TWILIO_RECEIVE_COHORT,
            logger=logger,
            failure_message="Twilio receive loop failed",
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
        self._lifecycle_tasks = BackgroundTaskScope(name="twilio-connection-lifecycle")
        self._socket_consumed = False
        # The accepted socket remains cleanup-owned until close succeeds.
        # Public connected state and receive metadata may already be cleared
        # when cancellation/failure interrupts disconnect, so keep an explicit
        # retry ledger instead of relying on those fields for admission.
        self._socket_close_pending = True
        self._disconnect_cleanup_error: Exception | None = None
        # Serialize socket ownership transitions. ``connect`` releases this
        # lock while dispatching a deferred CallAnswered event so an observer
        # can still initiate disconnect; its final publish phase reacquires the
        # lock and rejects a generation invalidated by that disconnect.
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_owner: asyncio.Task[Any] | None = None
        self._lifecycle_action: str | None = None
        self._init_audio_queue(
            resolved_config.max_pending_chunks,
            resolved_config.max_pending_bytes,
        )
        self._init_twilio_protocol(resolved_config, event_bus)

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
                "TwilioConnectionTransport.connect() cannot run during disconnect()"
            )
        leader = False
        connect_task: asyncio.Task[None] | None = None
        async with self._lifecycle_lock:
            connect_task = self._connect_task
            leader = connect_task is None or connect_task.done()
            if leader:
                connect_task = self._lifecycle_tasks.create_task(
                    "twilio-connection-connect",
                    self._connect_transaction(),
                    log_errors=False,
                )
                self._connect_task = connect_task
        if connect_task is None:
            raise RuntimeError("Twilio connection transaction was not initialized")
        if leader:
            # Cancellation of the initiating caller cancels the shared
            # transaction so partial startup rolls back just as it did before
            # connect became single-flight.
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
                        # A disconnect may have completed while an observer was
                        # running. Remove metadata published by that stale
                        # observer before reporting the invalidated connect.
                        self._clear_connection_metadata()
                        self._enqueue_sentinel()
                        raise ConnectionError("Twilio transport disconnected during connect")
                    receive_task = self._receive_tasks.create_task(
                        self._receive_loop(),
                        task_name="twilio-connection-receive",
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
                "Twilio connection cleanup is incomplete; call disconnect() "
                "again before reconnecting"
            ) from self._disconnect_cleanup_error
        if self._socket_consumed:
            if self._socket_close_pending:
                raise RuntimeError(
                    "Twilio accepted connection has ended; call disconnect() "
                    "to finish socket cleanup"
                )
            raise RuntimeError("Twilio accepted connection is already closed")
        if not self._socket_close_pending:
            raise RuntimeError("Twilio accepted connection is already closed")
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
            logger.debug("Error closing Twilio WebSocket after connect failure", exc_info=True)
        await self._drain_emit_tasks()

    async def disconnect(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "disconnect":
                return
            raise RuntimeError(
                "TwilioConnectionTransport.disconnect() cannot run during connect()"
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
            and self._stream_sid is None
            and self._call_sid is None
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
                logger.debug("Error closing Twilio WebSocket", exc_info=True)
                cleanup_errors.append(exc)
        self._enqueue_sentinel()
        try:
            await self._drain_emit_tasks()
        except Exception as exc:
            logger.debug("Error draining Twilio diagnostic events", exc_info=True)
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
            logger.debug("Twilio receive loop failed during disconnect", exc_info=True)
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
            "Twilio connection disconnect was interrupted by cancellation"
        )

    def _clear_connection_metadata(self) -> None:
        self._stream_sid = None
        self._call_sid = None
        self._call_identity = None
        self._answered_at = None
        self._call_ended_emitted = False
        self._pending_start_message = None
        self._pending_start_claims = None
        self._diagnostics.reset()
        self._inbound_resampler.reset()

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

    async def send_playback_mark(self, name: str | None = None) -> str:
        return await self.send_mark(name=name)

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

    async def _handle_pre_start_message(self, msg: dict[str, Any]) -> bool | None:
        event = msg.get("event")
        if event == "start":
            return await self._store_prevalidated_start(msg)

        handler = self._MESSAGE_HANDLERS.get(event) if isinstance(event, str) else None
        if handler is None:
            logger.debug("Unknown Twilio event: %s", event)
            return None
        await handler(self, msg)
        return None

    async def wait_for_start(self, *, timeout_s: float | None = None) -> bool:
        """Read through the first authenticated Twilio ``start`` message.

        ``serve_twilio_voice_app`` uses this before creating an EasyCat session
        so invalid media sockets never compile provider configuration. The
        one-time token is consumed here; the accepted ``start`` frame is stored
        and applied during ``connect()`` after Session has attached the event
        bus and caller-identity sink.
        """
        if timeout_s is None:
            return await self._wait_for_start()
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        try:
            async with asyncio.timeout(timeout_s):
                return await self._wait_for_start()
        except TimeoutError:
            await self._ws.close(1008, "Timed out waiting for Twilio start")
            return False

    async def _wait_for_start(self) -> bool:
        if self._stream_sid is not None or self._pending_start_message is not None:
            return True
        if self._connected:
            return self._stream_sid is not None

        ws = self._ws
        try:
            async for raw in ws:
                decoded = _decode_twilio_raw(raw)
                if decoded is None:
                    continue
                msg = _parse_twilio_message(decoded)
                if msg is None:
                    continue
                result = await self._handle_pre_start_message(msg)
                if result is not None:
                    return result
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("Twilio Media Streams disconnected before start")
            if isinstance(exc, websockets.exceptions.ConnectionClosedError):
                self._record_transport_disconnect("twilio stream closed abnormally before start")
        return False

    async def _receive_loop(self) -> None:
        await self._receive_twilio_messages(self._ws)

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
