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
import json
import logging
import secrets
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

import websockets
from websockets.asyncio.server import ServerConnection

from easycat._audio_utils import resample
from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import (
    CallAnswered,
    CallEnded,
    EventBus,
    PlaybackMarkAck,
)
from easycat.telephony.dtmf import parse_twilio_dtmf_message
from easycat.transports._base import AudioQueueMixin, ServerTransportBase

logger = logging.getLogger(__name__)

# Twilio sends/receives mulaw 8 kHz mono.
MULAW_8K = AudioFormat(sample_rate=8000, channels=1, sample_width=1, encoding="mulaw")
TWILIO_PREFERRED_TTS_OUTPUT_FORMAT = PCM16_MONO_8K
_TWILIO_OUTBOUND_TRACKS = {"outbound", "outbound_track"}
_DEGRADED_TWILIO_SEQUENCE_GAP = "twilio_sequence_gap"
_DEGRADED_TWILIO_TIMESTAMP_GAP = "twilio_timestamp_gap"
_TWILIO_MULAW_BYTES_PER_MS = 8
TWILIO_STREAM_TOKEN_PARAMETER = "EasyCatStreamToken"


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
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if secret is None:
            secret = secrets.token_urlsafe(32)
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._ttl_s = ttl_s
        self._now = now
        self._pending: dict[str, int] = {}

    def issue(self) -> str:
        """Return a signed token accepted by exactly one future ``consume``."""
        self._prune_expired()
        nonce = secrets.token_urlsafe(24)
        expires_at = int(self._now() + self._ttl_s)
        payload = f"{nonce}.{expires_at}"
        signature = self._signature(payload)
        self._pending[nonce] = expires_at
        return f"{payload}.{signature}"

    def issue_parameter(self) -> dict[str, str]:
        """Return the TwiML ``<Parameter>`` mapping for a fresh token."""
        return {TWILIO_STREAM_TOKEN_PARAMETER: self.issue()}

    def consume(self, token: str) -> bool:
        """Validate and consume a token, returning ``False`` on replay/expiry."""
        self._prune_expired()
        parts = token.split(".")
        if len(parts) != 3:
            return False
        nonce, expires_text, signature = parts
        try:
            expires_at = int(expires_text)
        except ValueError:
            return False

        payload = f"{nonce}.{expires_at}"
        try:
            matches_signature = hmac.compare_digest(signature, self._signature(payload))
        except TypeError:
            return False
        if not matches_signature:
            return False
        if expires_at < self._now():
            self._pending.pop(nonce, None)
            return False
        pending_expires_at = self._pending.pop(nonce, None)
        return pending_expires_at == expires_at

    def _signature(self, payload: str) -> str:
        digest = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _prune_expired(self) -> None:
        now = self._now()
        expired = [nonce for nonce, expires_at in self._pending.items() if expires_at < now]
        for nonce in expired:
            self._pending.pop(nonce, None)


@dataclass
class TwilioTransportConfig:
    """Configuration for :class:`TwilioTransport`."""

    preferred_tts_output_format: ClassVar[AudioFormat] = TWILIO_PREFERRED_TTS_OUTPUT_FORMAT

    host: str = "0.0.0.0"
    port: int = 8766
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200
    stream_token_validator: Callable[[str], bool] | None = None
    stream_token_parameter: str = TWILIO_STREAM_TOKEN_PARAMETER


def _parse_twilio_start_identity(
    start: dict[str, Any],
    call_sid: str | None,
    *,
    excluded_parameter_names: set[str] | None = None,
) -> tuple[Any, str, str]:
    """Build a CallIdentity from Twilio ``start.customParameters``."""
    from easycat.session._types import CallIdentity

    params: dict[str, str] = {}
    raw_params = start.get("customParameters") or {}
    if isinstance(raw_params, dict):
        for key, value in raw_params.items():
            if isinstance(key, str) and isinstance(value, (str, int)):
                params[key] = str(value)
    for name in excluded_parameter_names or set():
        params.pop(name, None)

    direction_raw = _clean_twilio_parameter(
        params.pop("Direction", "") or params.pop("direction", "")
    )
    direction_token = direction_raw.strip().lower()
    if direction_token.startswith("outbound"):
        direction = "outbound"
    elif direction_token.startswith("inbound") or not direction_token:
        direction = "inbound"
    else:
        direction = "unknown"

    from_number = _clean_twilio_parameter(params.pop("From", "") or params.pop("from", ""))
    to_number = _clean_twilio_parameter(params.pop("To", "") or params.pop("to", ""))
    if direction == "outbound":
        caller = to_number
        called = from_number
    else:
        caller = from_number
        called = to_number
    display_name = _clean_twilio_parameter(
        params.pop("CallerName", None) or params.pop("caller_name", None)
    )
    city = _clean_twilio_parameter(params.pop("FromCity", "") or params.pop("from_city", ""))
    state = _clean_twilio_parameter(params.pop("FromState", "") or params.pop("from_state", ""))
    zip_code = _clean_twilio_parameter(params.pop("FromZip", "") or params.pop("from_zip", ""))
    country = _clean_twilio_parameter(
        params.pop("FromCountry", "") or params.pop("from_country", "")
    )

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


def _clean_twilio_parameter(value: Any) -> str:
    """Return a Twilio parameter value, ignoring unsubstituted templates."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("{{") and text.endswith("}}"):
        return ""
    return text


def _twilio_stream_token_valid(start: dict[str, Any], config: TwilioTransportConfig) -> bool:
    validator = config.stream_token_validator
    if validator is None:
        return True
    raw_params = start.get("customParameters") or {}
    if not isinstance(raw_params, dict):
        return False
    token = raw_params.get(config.stream_token_parameter)
    if not isinstance(token, str) or not token:
        return False
    try:
        return bool(validator(token))
    except Exception:
        logger.warning("Twilio stream token validator raised", exc_info=True)
        return False


def _parse_twilio_int(value: Any) -> int | None:
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
) -> None:
    event = parse_twilio_dtmf_message(msg)
    if event is None:
        logger.debug("Ignoring Twilio DTMF with invalid payload")
        return
    if event_bus is not None:
        await event_bus.emit(event)


class TwilioTransport(ServerTransportBase):
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
        self._config = config or TwilioTransportConfig()
        super().__init__(
            host=self._config.host,
            port=self._config.port,
            max_pending_chunks=self._config.max_pending_chunks,
        )
        self._audio_format = self._config.audio_format
        self._event_bus = event_bus

        self._stream_sid: str | None = None
        self._call_sid: str | None = None
        self._call_identity: Any | None = None
        # Optional sink populated by Session wiring so the caller ID
        # extracted from the ``<Stream>`` customParameters flows through
        # to ``session.call_identity`` without the app doing plumbing.
        self._identity_sink: Any = None
        self._answered_at: float | None = None
        self._call_ended_emitted = False
        self._diagnostics = _TwilioStreamDiagnostics(self._emit_degraded)

        self._mark_counter = 0

    # ── Transport protocol ────────────────────────────────────────

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

    async def disconnect(self) -> None:
        """Disconnect Twilio and stop the server."""
        await super().disconnect()
        self._stream_sid = None
        self._call_sid = None
        self._call_identity = None
        self._call_ended_emitted = False
        self._diagnostics.reset()

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
            return name or ""

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

        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        logger.warning("Ignoring non-UTF-8 Twilio message")
                        continue
                await self._handle_message(raw)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Twilio Media Streams disconnected")
        finally:
            await self._emit_call_ended_once()
            self._ws = None
            self._client_connected.clear()
            self._stream_sid = None
            self._call_sid = None
            self._answered_at = None
            self._diagnostics.reset()
            self._enqueue_sentinel()

    async def _handle_message(self, raw: str) -> None:
        """Route a Twilio JSON message to the appropriate handler."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid JSON from Twilio")
            return
        if not isinstance(msg, dict):
            logger.warning("Ignoring non-object JSON from Twilio")
            return

        event = msg.get("event", "")
        if event == "connected":
            logger.debug("Twilio connected event: protocol=%s", msg.get("protocol"))
        elif event == "start":
            await self._handle_start(msg)
        elif event == "media":
            await self._handle_media(msg)
        elif event == "stop":
            if not _is_active_twilio_stream_event(
                msg,
                active_stream_sid=self._stream_sid,
                event_name="stop",
            ):
                return
            self._diagnostics.observe_sequence(msg)
            logger.info("Twilio stream stopped (streamSid=%s)", self._stream_sid)
            # Emit the inbound-direction mirror of the outbound call
            # manager's ``CallEnded`` event so observers like
            # ``CallDispositionTracker`` and ``NumberHealthMonitor``
            # see the same lifecycle regardless of direction.
            await self._emit_call_ended_once()
            # Explicitly end the current audio stream so receive_audio() can terminate.
            self._stream_sid = None
            self._call_sid = None
            self._answered_at = None
            self._diagnostics.reset()
            self._enqueue_sentinel()
        elif event == "mark":
            if not _is_active_twilio_stream_event(
                msg,
                active_stream_sid=self._stream_sid,
                event_name="mark",
            ):
                return
            self._diagnostics.observe_sequence(msg)
            mark = msg.get("mark", {})
            if not isinstance(mark, dict):
                logger.debug("Ignoring Twilio mark with non-object payload")
                return
            mark_name = mark.get("name", "")
            logger.debug("Twilio mark acknowledged: %s", mark_name)
            if mark_name and self._event_bus is not None:
                await self._event_bus.emit(PlaybackMarkAck(mark_name=mark_name))
        elif event == "dtmf":
            await self._handle_dtmf(msg)
        else:
            logger.debug("Unknown Twilio event: %s", event)

    async def _handle_start(self, msg: dict[str, Any]) -> None:
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
            return
        if not _twilio_stream_token_valid(start, self._config):
            logger.warning("Rejecting Twilio stream start with missing or invalid stream token")
            if self._ws is not None:
                await self._ws.close(4003, "Missing or invalid stream token")
            return
        self._stream_sid = msg.get("streamSid") or start.get("streamSid")
        self._call_sid = start.get("callSid")
        self._answered_at = time.monotonic()
        self._call_ended_emitted = False
        self._diagnostics.start(msg)
        identity, caller, called = _parse_twilio_start_identity(
            start,
            self._call_sid,
            excluded_parameter_names={self._config.stream_token_parameter},
        )
        self._call_identity = identity
        if self._identity_sink is not None:
            try:
                self._identity_sink(identity)
            except Exception:
                logger.debug("Identity sink raised on start", exc_info=True)

        if self._event_bus is not None and self._call_sid:
            await self._event_bus.emit(CallAnswered(call_sid=self._call_sid, answered_by="human"))

        logger.info(
            "Twilio stream started: streamSid=%s callSid=%s from=%s to=%s",
            self._stream_sid,
            self._call_sid,
            caller,
            called,
        )

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
        except Exception:
            logger.warning("Ignoring Twilio media frame with invalid base64 payload")
            return
        self._diagnostics.observe_media_timestamp(
            media,
            stream_sid=self._stream_sid,
            mulaw_data=mulaw_data,
        )
        pcm_data = mulaw_to_pcm16(mulaw_data, self._audio_format.sample_rate)

        chunk = AudioChunk(data=pcm_data, format=self._audio_format)
        self._enqueue_chunk(chunk, context="Twilio")

    async def _emit_call_ended_once(self) -> None:
        if self._call_ended_emitted:
            return
        self._call_ended_emitted = True
        await _emit_twilio_call_ended(
            self._event_bus,
            call_sid=self._call_sid,
            answered_at=self._answered_at,
            call_identity=self._call_identity,
        )

    async def _handle_dtmf(self, msg: dict[str, Any]) -> None:
        """Emit a DTMF event for the pressed digit."""
        if _is_active_twilio_stream_event(
            msg,
            active_stream_sid=self._stream_sid,
            event_name="dtmf",
        ):
            self._diagnostics.observe_sequence(msg)
            await _emit_parsed_twilio_dtmf(msg, self._event_bus)

    # ── Properties ────────────────────────────────────────────────

    @property
    def stream_sid(self) -> str | None:
        return self._stream_sid

    @property
    def call_sid(self) -> str | None:
        return self._call_sid

    def version_info(self) -> dict[str, str]:
        try:
            from importlib.metadata import version

            ws_ver = version("websockets")
        except Exception:
            ws_ver = "unknown"
        return {
            "provider": "twilio",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": ws_ver,
        }


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
    out = bytearray(len(mulaw_data) * 2)
    for i, value in enumerate(mulaw_data):
        sample = _mulaw_decode_sample(value)
        out[i * 2 : i * 2 + 2] = sample.to_bytes(2, "little", signed=True)
    return bytes(out)


def _mulaw_encode(pcm_data: bytes) -> bytes:
    """Encode PCM16 little-endian bytes into G.711 mu-law bytes."""
    if len(pcm_data) % 2 != 0:
        pcm_data = pcm_data[:-1]
    out = bytearray(len(pcm_data) // 2)
    for i, (sample,) in enumerate(struct.iter_unpack("<h", pcm_data)):
        out[i] = _mulaw_encode_sample(sample)
    return bytes(out)


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

    if sample > _MULAW_CLIP:
        sample = _MULAW_CLIP

    sample += _MULAW_BIAS
    exponent = 7
    exp_mask = 0x4000
    while exponent > 0 and (sample & exp_mask) == 0:
        exponent -= 1
        exp_mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


class TwilioConnectionTransport(AudioQueueMixin):
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
        self._config = config or TwilioTransportConfig()
        self._audio_format = self._config.audio_format
        self._event_bus = event_bus
        self._stream_sid: str | None = None
        self._call_sid: str | None = None
        self._call_identity: Any | None = None
        self._identity_sink: Any = None
        self._answered_at: float | None = None
        self._call_ended_emitted = False
        self._mark_counter = 0
        self._receive_task: asyncio.Task[None] | None = None
        self._init_audio_queue(self._config.max_pending_chunks)
        self._diagnostics = _TwilioStreamDiagnostics(self._emit_degraded)

    @property
    def call_identity(self) -> Any | None:
        """Latest :class:`CallIdentity` parsed from the Twilio start event."""
        return self._call_identity

    @property
    def transport_kind(self) -> str:
        return "telephony"

    def bind_identity_sink(self, sink: Any) -> None:
        """Register a callback that receives every identity update."""
        self._identity_sink = sink

    async def connect(self) -> None:
        if self._connected:
            return
        self._connected = True
        self._reset_audio_queue()
        self._client_connected.set()
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        self._client_connected.clear()
        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        self._receive_task = None
        self._stream_sid = None
        self._call_sid = None
        self._call_identity = None
        self._answered_at = None
        self._call_ended_emitted = False
        self._diagnostics.reset()
        try:
            await self._ws.close()
        except Exception:
            logger.debug("Error closing Twilio WebSocket", exc_info=True)
        self._enqueue_sentinel()
        await self._drain_emit_tasks()

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
            return name or ""
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
        return name

    async def send_playback_mark(self, name: str | None = None) -> str:
        return await self.send_mark(name=name)

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        logger.warning("Ignoring non-UTF-8 Twilio message")
                        continue
                await self._handle_message(raw)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Twilio Media Streams disconnected")
        finally:
            await self._emit_call_ended_once()
            self._connected = False
            self._client_connected.clear()
            self._stream_sid = None
            self._call_sid = None
            self._answered_at = None
            self._diagnostics.reset()
            self._enqueue_sentinel()

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid JSON from Twilio")
            return
        if not isinstance(msg, dict):
            logger.warning("Ignoring non-object JSON from Twilio")
            return

        event = msg.get("event", "")
        if event == "start":
            await self._handle_start(msg)
        elif event == "media":
            await self._handle_media(msg)
        elif event == "stop":
            if not _is_active_twilio_stream_event(
                msg,
                active_stream_sid=self._stream_sid,
                event_name="stop",
            ):
                return
            self._diagnostics.observe_sequence(msg)
            await self._emit_call_ended_once()
            self._stream_sid = None
            self._call_sid = None
            self._answered_at = None
            self._diagnostics.reset()
            self._enqueue_sentinel()
        elif event == "mark":
            if not _is_active_twilio_stream_event(
                msg,
                active_stream_sid=self._stream_sid,
                event_name="mark",
            ):
                return
            self._diagnostics.observe_sequence(msg)
            mark = msg.get("mark", {})
            if not isinstance(mark, dict):
                logger.debug("Ignoring Twilio mark with non-object payload")
                return
            mark_name = mark.get("name", "")
            if mark_name and self._event_bus is not None:
                await self._event_bus.emit(PlaybackMarkAck(mark_name=mark_name))
        elif event == "dtmf":
            await self._handle_dtmf(msg)

    async def _handle_start(self, msg: dict[str, Any]) -> None:
        start = msg.get("start", {})
        if not isinstance(start, dict):
            logger.debug("Ignoring Twilio start with non-object payload")
            return
        if not _twilio_stream_token_valid(start, self._config):
            logger.warning(
                "Rejecting Twilio connection stream start with missing or invalid stream token"
            )
            await self._ws.close(4003, "Missing or invalid stream token")
            return
        self._stream_sid = msg.get("streamSid") or start.get("streamSid")
        self._call_sid = start.get("callSid")
        self._answered_at = time.monotonic()
        self._call_ended_emitted = False
        self._diagnostics.start(msg)
        identity, caller, called = _parse_twilio_start_identity(
            start,
            self._call_sid,
            excluded_parameter_names={self._config.stream_token_parameter},
        )
        self._call_identity = identity
        if self._identity_sink is not None:
            try:
                self._identity_sink(identity)
            except Exception:
                logger.debug("Identity sink raised on start", exc_info=True)

        if self._event_bus is not None and self._call_sid:
            await self._event_bus.emit(CallAnswered(call_sid=self._call_sid, answered_by="human"))

        logger.info(
            "Twilio connection stream started: streamSid=%s callSid=%s from=%s to=%s",
            self._stream_sid,
            self._call_sid,
            caller,
            called,
        )

    async def _handle_media(self, msg: dict[str, Any]) -> None:
        media = _accepted_twilio_media(msg, active_stream_sid=self._stream_sid)
        if media is None:
            return
        self._diagnostics.observe_sequence(msg)
        payload = media.get("payload", "")
        if not payload:
            return
        try:
            mulaw_data = base64.b64decode(payload)
        except Exception:
            logger.warning("Ignoring Twilio media frame with invalid base64 payload")
            return
        self._diagnostics.observe_media_timestamp(
            media,
            stream_sid=self._stream_sid,
            mulaw_data=mulaw_data,
        )
        pcm_data = mulaw_to_pcm16(mulaw_data, self._audio_format.sample_rate)
        chunk = AudioChunk(data=pcm_data, format=self._audio_format)
        self._enqueue_chunk(chunk, context="Twilio")

    async def _emit_call_ended_once(self) -> None:
        if self._call_ended_emitted:
            return
        self._call_ended_emitted = True
        await _emit_twilio_call_ended(
            self._event_bus,
            call_sid=self._call_sid,
            answered_at=self._answered_at,
            call_identity=self._call_identity,
        )

    async def _handle_dtmf(self, msg: dict[str, Any]) -> None:
        if _is_active_twilio_stream_event(
            msg,
            active_stream_sid=self._stream_sid,
            event_name="dtmf",
        ):
            self._diagnostics.observe_sequence(msg)
            await _emit_parsed_twilio_dtmf(msg, self._event_bus)

    @property
    def stream_sid(self) -> str | None:
        return self._stream_sid

    @property
    def call_sid(self) -> str | None:
        return self._call_sid

    def version_info(self) -> dict[str, str]:
        try:
            from importlib.metadata import version

            ws_ver = version("websockets")
        except Exception:
            ws_ver = "unknown"
        return {
            "provider": "twilio-connection",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": ws_ver,
        }


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
