"""Telnyx Call Control v2 webhook verification and command payload helpers.

Covers the three protocol surfaces EasyCat needs for first-class Telnyx
support:

- Ed25519 webhook signature verification over ``{timestamp}|{raw_body}``
  (headers ``telnyx-signature-ed25519`` / ``telnyx-timestamp``, 5-minute
  replay window). ``cryptography`` is imported lazily so the module imports
  without the optional dependency installed.
- Webhook envelope parsing into EasyCat's neutral call-lifecycle events
  (:class:`~easycat.events.CallInitiated`, ``CallRinging``, ``CallAnswered``,
  ``CallEnded``, ``CallFailed``), answering-machine detection results, and
  ``streaming.failed`` degradations.
- Answer/dial payload builders carrying the bidirectional media-stream
  parameters, plus the ``client_state`` base64 round-trip used to correlate
  calls back to the webhook that initiated them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from typing import Any, Literal

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallInitiated,
    CallRinging,
    Event,
    TransportDegraded,
    VoicemailDetected,
)
from easycat.telephony._install import TELNYX_INSTALL_HINT

logger = logging.getLogger(__name__)

TELNYX_WEBHOOK_SIGNATURE_HEADER = "telnyx-signature-ed25519"
TELNYX_WEBHOOK_TIMESTAMP_HEADER = "telnyx-timestamp"
TELNYX_DEFAULT_REPLAY_WINDOW_S = 300.0

# Bidirectional media-stream defaults (L16 @ 16 kHz matches the internal bus).
TELNYX_STREAM_TRACK = "inbound_track"
TELNYX_STREAM_MODE = "rtp"
TELNYX_L16_CODEC = "L16"
TELNYX_PCMU_CODEC = "PCMU"
TELNYX_L16_SAMPLING_RATE = 16000
TELNYX_PCMU_SAMPLING_RATE = 8000

TelnyxAnsweringMachineDetection = Literal[
    "premium", "detect", "detect_beep", "detect_words", "greeting_end", "disabled"
]

_AMD_DETECTION_EVENT_TYPES = frozenset(
    {
        "call.machine.detection.ended",
        "call.machine.premium.detection.ended",
    }
)
_AMD_GREETING_EVENT_TYPES = frozenset(
    {
        "call.machine.greeting.ended",
        "call.machine.premium.greeting.ended",
    }
)

_HANGUP_FAILURE_CAUSES = frozenset(
    {
        "busy",
        "no_answer",
        "call_rejected",
        "error",
        "timeout",
    }
)


def _ed25519_public_key_type() -> Any:
    """Return Ed25519PublicKey, importing lazily."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - exercised via hint text
        raise ImportError(TELNYX_INSTALL_HINT) from exc
    return Ed25519PublicKey


def verify_telnyx_webhook_signature(
    *,
    payload: bytes | str,
    signature: str | None,
    timestamp: str | None,
    public_key: str,
    tolerance_s: float = TELNYX_DEFAULT_REPLAY_WINDOW_S,
    now: Any | None = None,
) -> bool:
    """Verify a Telnyx webhook's Ed25519 signature and replay window.

    Telnyx signs ``{timestamp}|{raw_body}`` with the portal's Ed25519 private
    key; the base64 signature arrives in ``telnyx-signature-ed25519`` beside
    the unix-second ``telnyx-timestamp``. Signatures older than *tolerance_s*
    (default five minutes) are rejected to bound replays.

    Args:
        payload: The exact raw request body bytes received.
        signature: Base64 ``telnyx-signature-ed25519`` header value.
        timestamp: ``telnyx-timestamp`` header value.
        public_key: Base64 Ed25519 public key from the Telnyx portal.
        tolerance_s: Allowed clock skew / replay window in seconds.
        now: Injectable clock (seconds since epoch) for tests.
    """
    if not signature or not timestamp or not public_key:
        return False
    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    try:
        ts_seconds = int(str(timestamp).strip())
    except ValueError:
        return False
    clock = now if now is not None else time.time
    try:
        current = float(clock())
    except (TypeError, ValueError):
        return False
    if abs(current - ts_seconds) > tolerance_s:
        logger.warning("Rejecting Telnyx webhook outside the replay window")
        return False

    Ed25519PublicKey = _ed25519_public_key_type()

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        provided_signature = base64.b64decode(signature.strip(), validate=True)
        key.verify(provided_signature, f"{timestamp}|".encode() + payload)
    except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
        return False
    return True


def parse_telnyx_webhook(
    body: bytes | str | Mapping[str, Any],
) -> dict[str, Any] | None:
    """Parse one Telnyx webhook body and require its minimal envelope.

    Returns ``None`` for non-JSON bodies, non-object envelopes, or missing
    ``event_type`` — callers treat that as an unauthenticated-looking drop.
    """
    if isinstance(body, Mapping):
        envelope: Any = body
    else:
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("Ignoring non-UTF-8 Telnyx webhook body")
                return None
        try:
            envelope = json.loads(body)
        except (RecursionError, ValueError):
            logger.warning("Ignoring invalid JSON Telnyx webhook body")
            return None
    if not isinstance(envelope, dict):
        logger.warning("Ignoring non-object Telnyx webhook")
        return None
    data = envelope.get("data")
    if isinstance(data, dict):
        envelope = data
    event_type = envelope.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        logger.warning("Ignoring Telnyx webhook without event_type")
        return None
    return envelope


def telnyx_webhook_idempotency_key(envelope: Mapping[str, Any]) -> str | None:
    """Return a stable retry key for one Telnyx webhook delivery.

    Uses the delivery ``id`` when present, falling back to a digest of the
    event type plus occurred-at so retries of the same event collapse while
    distinct deliveries stay independent.
    """
    event_id = envelope.get("id")
    if isinstance(event_id, str) and event_id:
        return event_id
    event_type = envelope.get("event_type")
    occurred_at = envelope.get("occurred_at")
    material = f"{event_type}\0{occurred_at}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _webhook_payload(envelope: Mapping[str, Any]) -> dict[str, Any]:
    payload = envelope.get("payload")
    return payload if isinstance(payload, dict) else {}


def _call_sid_from(envelope: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    for source in (payload, envelope):
        value = source.get("call_control_id")
        if isinstance(value, str) and value:
            return value
    return None


def encode_client_state(state: Mapping[str, Any]) -> str:
    """Encode ``client_state`` claims as Telnyx's base64 JSON blob."""
    return base64.b64encode(json.dumps(dict(state)).encode("utf-8")).decode("ascii")


def decode_client_state(raw: Any) -> dict[str, Any]:
    """Decode a ``client_state`` blob; malformed input yields an empty dict."""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        decoded = json.loads(base64.b64decode(raw, validate=True).decode("utf-8"))
    except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
        return {}
    return decoded if isinstance(decoded, dict) else {}


def build_stream_parameters(
    *,
    stream_url: str,
    client_state: Mapping[str, Any] | None = None,
    codec: str = TELNYX_L16_CODEC,
    sampling_rate: int = TELNYX_L16_SAMPLING_RATE,
    stream_track: str = TELNYX_STREAM_TRACK,
    send_silence_when_idle: bool = True,
) -> dict[str, Any]:
    """Return the shared bidirectional media-stream parameter block.

    L16 @ 16 kHz is the default because it matches EasyCat's internal
    ``PCM16_MONO_16K`` bus exactly; PCMU @ 8 kHz is the supported fallback.
    """
    if codec == TELNYX_PCMU_CODEC:
        sampling_rate = TELNYX_PCMU_SAMPLING_RATE
    elif codec != TELNYX_L16_CODEC:
        raise ValueError("codec must be 'L16' or 'PCMU'")
    parameters: dict[str, Any] = {
        "stream_url": stream_url,
        "stream_track": stream_track,
        "stream_bidirectional_mode": TELNYX_STREAM_MODE,
        "stream_bidirectional_codec": codec,
        "stream_bidirectional_sampling_rate": sampling_rate,
        "send_silence_when_idle": send_silence_when_idle,
    }
    if client_state is not None:
        parameters["client_state"] = encode_client_state(client_state)
    return parameters


def build_answer_payload(
    *,
    stream_url: str,
    client_state: Mapping[str, Any] | None = None,
    codec: str = TELNYX_L16_CODEC,
    sampling_rate: int = TELNYX_L16_SAMPLING_RATE,
    command_id: str | None = None,
    send_silence_when_idle: bool = True,
) -> dict[str, Any]:
    """Build the ``POST /v2/calls/{call_control_id}/actions/answer`` body."""
    payload = build_stream_parameters(
        stream_url=stream_url,
        client_state=client_state,
        codec=codec,
        sampling_rate=sampling_rate,
        send_silence_when_idle=send_silence_when_idle,
    )
    if command_id:
        payload["command_id"] = command_id
    return payload


def build_dial_payload(
    *,
    to: str,
    from_: str,
    connection_id: str,
    stream_url: str,
    client_state: Mapping[str, Any] | None = None,
    codec: str = TELNYX_L16_CODEC,
    sampling_rate: int = TELNYX_L16_SAMPLING_RATE,
    answering_machine_detection: TelnyxAnsweringMachineDetection | None = None,
    command_id: str | None = None,
    timeout_secs: int | None = None,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    """Build the ``POST /v2/calls`` (Dial) body with media-stream parameters."""
    if not connection_id:
        raise ValueError("connection_id must be non-empty")
    payload: dict[str, Any] = {
        "to": to,
        "from": from_,
        "connection_id": connection_id,
    }
    payload.update(
        build_stream_parameters(
            stream_url=stream_url,
            client_state=client_state,
            codec=codec,
            sampling_rate=sampling_rate,
        )
    )
    if answering_machine_detection is not None:
        payload["answering_machine_detection"] = answering_machine_detection
    if command_id:
        payload["command_id"] = command_id
    if timeout_secs is not None:
        payload["timeout_secs"] = timeout_secs
    if webhook_url:
        payload["webhook_url"] = webhook_url
    return payload


def parse_telnyx_call_event(
    envelope: Mapping[str, Any],
    *,
    session_id: str | None = None,
) -> Event | None:
    """Map one Telnyx webhook envelope onto a neutral EasyCat event.

    Handles the call lifecycle (``call.initiated/answered/hangup``),
    answering-machine detection results, and ``streaming.failed``. Returns
    ``None`` for unsupported or unparseable event types.
    """
    event_type = envelope.get("event_type")
    payload = _webhook_payload(envelope)

    if event_type == "call.initiated":
        call_control_id = _call_sid_from(envelope, payload)
        if call_control_id is None:
            logger.warning("Ignoring Telnyx call.initiated without call_control_id")
            return None
        return CallInitiated(
            call_sid=call_control_id,
            to=str(payload.get("to", "")),
            from_=str(payload.get("from", "")),
            session_id=session_id,
        )

    if event_type == "call.ringing":
        call_control_id = _call_sid_from(envelope, payload)
        if call_control_id is None:
            return None
        return CallRinging(call_sid=call_control_id, session_id=session_id)

    if event_type == "call.answered":
        call_control_id = _call_sid_from(envelope, payload)
        if call_control_id is None:
            return None
        return CallAnswered(call_sid=call_control_id, session_id=session_id)

    if event_type == "call.hangup":
        return _parse_hangup_event(payload, envelope, session_id=session_id)

    if event_type in _AMD_DETECTION_EVENT_TYPES or event_type in _AMD_GREETING_EVENT_TYPES:
        return _parse_amd_event(event_type, payload, envelope, session_id=session_id)

    if event_type == "streaming.failed":
        return _parse_streaming_failed(payload, envelope, session_id=session_id)

    return None


def _parse_amd_event(
    event_type: str,
    payload: Mapping[str, Any],
    envelope: Mapping[str, Any],
    *,
    session_id: str | None,
) -> VoicemailDetected:
    call_control_id = _call_sid_from(envelope, payload)
    result = (
        _amd_result(str(payload.get("result", "")))
        if event_type in _AMD_DETECTION_EVENT_TYPES
        else "machine"
    )
    return VoicemailDetected(
        result=result,
        source="detector",
        call_sid=call_control_id or "",
        session_id=session_id,
    )


def _parse_streaming_failed(
    payload: Mapping[str, Any],
    envelope: Mapping[str, Any],
    *,
    session_id: str | None,
) -> TransportDegraded:
    reason = str(payload.get("failure_reason") or payload.get("reason") or "unknown")
    return TransportDegraded(
        provider="telnyx",
        reason="streaming_failed",
        detail=f"code={payload.get('failure_code', '')} {reason}".strip(),
        fatal=True,
        session_id=session_id,
    )


def _parse_hangup_event(
    payload: Mapping[str, Any],
    envelope: Mapping[str, Any],
    *,
    session_id: str | None,
) -> CallEnded | CallFailed | None:
    call_control_id = _call_sid_from(envelope, payload)
    if call_control_id is None:
        logger.warning("Ignoring Telnyx call.hangup without call_control_id")
        return None
    cause = str(payload.get("hangup_cause", ""))
    sip_code_raw = payload.get("sip_code")
    sip_code: int | None = None
    if isinstance(sip_code_raw, int) and not isinstance(sip_code_raw, bool):
        sip_code = sip_code_raw
    elif isinstance(sip_code_raw, str) and sip_code_raw.strip().isdigit():
        sip_code = int(sip_code_raw)
    duration = payload.get("call_duration")
    duration_s = (
        float(duration)
        if isinstance(duration, int | float) and not isinstance(duration, bool)
        else None
    )

    if cause in _HANGUP_FAILURE_CAUSES:
        return CallFailed(
            call_sid=call_control_id,
            reason=cause or "failed",
            sip_code=sip_code,
            number=str(payload.get("from", "")),
            session_id=session_id,
        )
    return CallEnded(
        call_sid=call_control_id,
        duration_s=duration_s,
        disposition=cause or "completed",
        number=str(payload.get("from", "")),
        session_id=session_id,
    )


def _amd_result(raw_result: str) -> Literal["human", "machine", "unknown"]:
    token = raw_result.strip().lower()
    if token.startswith("human"):
        return "human"
    if token.startswith("machine") or token in {"fax", "beep"}:
        return "machine"
    return "unknown"


__all__ = [
    "TELNYX_L16_CODEC",
    "TELNYX_L16_SAMPLING_RATE",
    "TELNYX_PCMU_CODEC",
    "TELNYX_PCMU_SAMPLING_RATE",
    "TELNYX_STREAM_MODE",
    "TELNYX_STREAM_TRACK",
    "TELNYX_WEBHOOK_SIGNATURE_HEADER",
    "TELNYX_WEBHOOK_TIMESTAMP_HEADER",
    "TelnyxAnsweringMachineDetection",
    "build_answer_payload",
    "build_dial_payload",
    "build_stream_parameters",
    "decode_client_state",
    "encode_client_state",
    "parse_telnyx_call_event",
    "parse_telnyx_webhook",
    "telnyx_webhook_idempotency_key",
    "verify_telnyx_webhook_signature",
]
