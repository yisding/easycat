"""Outbound call management: status callback parsing and call placement."""

from __future__ import annotations

__all__ = [
    "OutboundCallClient",
    "OutboundCallManager",
    "OutboundCallManagerState",
    "TelnyxOutboundClient",
    "TwilioRestOutboundClient",
    "emit_call_status",
    "emit_telnyx_call_event",
    "parse_call_status_callback",
]

import asyncio
import logging
import math
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from easycat._concurrency import shielded_cleanup
from easycat._epoch import Epoch, Lease
from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallInitiated,
    CallRinging,
    Event,
    EventBus,
    VoicemailDetected,
)
from easycat.telephony._install import TELEPHONY_INSTALL_HINT
from easycat.telephony.compliance import dnc_is_on_dnc
from easycat.telephony.voicemail import TWILIO_AMD_MAP, VoicemailResult

if TYPE_CHECKING:
    from easycat.telephony.compliance import DNCStore

logger = logging.getLogger(__name__)

# SIP response codes indicating call blocking (FCC March 2026 mandate).
_SIP_BLOCK_REASONS: dict[int, str] = {
    603: "declined",
    607: "blocked_unwanted",
    608: "blocked_rejected",
}
_CALL_STATUSES: frozenset[str] = frozenset(
    {
        "initiated",
        "ringing",
        "in-progress",
        "completed",
        "busy",
        "no-answer",
        "failed",
        "canceled",
    }
)

# Reason strings that indicate carrier/callee blocking (for number_health).
BLOCK_REASONS: frozenset[str] = frozenset({"blocked_unwanted", "blocked_rejected"})


def _get_call_sid(params: dict[str, Any]) -> str | None:
    call_sid = params.get("CallSid")
    if not isinstance(call_sid, str) or not call_sid:
        return None
    return call_sid


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _voicemail_detected_event(
    result: VoicemailResult,
    call_sid: str,
    *,
    session_id: str | None = None,
) -> VoicemailDetected:
    """Build the AMD classification event correlated to a Twilio call SID."""
    return VoicemailDetected(result=result, call_sid=call_sid, session_id=session_id)


def parse_call_status_callback(
    params: dict[str, Any],
    *,
    session_id: str | None = None,
) -> CallInitiated | CallRinging | CallAnswered | CallEnded | CallFailed | None:
    """Parse a Twilio status callback into a call lifecycle event.

    Returns ``None`` for missing/unknown statuses.
    """
    status = params.get("CallStatus")
    if status is None:
        return None
    if status not in _CALL_STATUSES:
        return None

    call_sid = _get_call_sid(params)
    if call_sid is None:
        logger.warning("Ignoring Twilio status callback without CallSid: status=%r", status)
        return None

    if status == "initiated":
        return CallInitiated(
            call_sid=call_sid,
            to=params.get("To", ""),
            from_=params.get("From", ""),
            session_id=session_id,
        )

    if status == "ringing":
        return CallRinging(call_sid=call_sid, session_id=session_id)

    if status == "in-progress":
        return CallAnswered(
            call_sid=call_sid,
            answered_by=params.get("AnsweredBy"),
            session_id=session_id,
        )

    if status == "completed":
        duration_s = _parse_float(params.get("CallDuration"))
        if duration_s is None:
            duration_s = _parse_float(params.get("Duration"))
        return CallEnded(
            call_sid=call_sid,
            duration_s=duration_s,
            disposition="completed",
            number=params.get("From", ""),
            session_id=session_id,
        )

    if status in {"busy", "no-answer", "failed", "canceled"}:
        sip_code = _parse_int(params.get("SipResponseCode"))
        reason = _SIP_BLOCK_REASONS.get(sip_code, status) if sip_code else status
        return CallFailed(
            call_sid=call_sid,
            reason=reason,
            sip_code=sip_code,
            number=params.get("From", ""),
            session_id=session_id,
        )

    return None


async def emit_call_status(
    params: dict[str, Any],
    event_bus: EventBus,
    *,
    session_id: str | None = None,
) -> CallInitiated | CallRinging | CallAnswered | CallEnded | CallFailed | None:
    """Parse a Twilio status callback and emit the resulting event.

    When the callback is an ``in-progress`` status that includes an
    ``AnsweredBy`` field (Twilio async AMD), a ``VoicemailDetected`` event
    is also emitted so the call state machine can classify the call without
    requiring a separate ``emit_twilio_amd()`` call.
    """
    event = parse_call_status_callback(params, session_id=session_id)
    if event is not None:
        await event_bus.emit(event)
        # Emit AMD classification when AnsweredBy is present on the answered callback.
        if isinstance(event, CallAnswered) and event.answered_by:
            amd_result = TWILIO_AMD_MAP.get(event.answered_by.lower())
            if amd_result is not None:
                await event_bus.emit(
                    _voicemail_detected_event(
                        amd_result,
                        event.call_sid,
                        session_id=session_id,
                    )
                )
    return event


class OutboundCallManagerState(Enum):
    IDLE = "idle"
    ACTIVE = "active"


# ── Outbound call client seam ────────────────────────────────────


class OutboundCallUpdater(Protocol):
    """Per-call resource the manager drives to complete a live call."""

    def update(self, **kwargs: Any) -> Any: ...


class OutboundCallsResource(Protocol):
    """Provider calls collection: create a call, address one by SID."""

    def create(self, **kwargs: Any) -> Any: ...

    def __call__(self, call_sid: str) -> OutboundCallUpdater: ...


@runtime_checkable
class OutboundCallClient(Protocol):
    """Provider boundary used by :class:`OutboundCallManager`.

    Mirrors the Twilio SDK resource surface the manager offloads to worker
    threads (``calls.create(...)`` / ``calls(sid).update(status=...)``).
    Implementations adapt other providers (e.g. Telnyx Call Control v2) onto
    this shape; ``create`` must return an object exposing ``sid``.
    """

    @property
    def calls(self) -> OutboundCallsResource: ...


class TwilioRestOutboundClient:
    """Default :class:`OutboundCallClient` backed by the Twilio REST SDK."""

    def __init__(self, account_sid: str, auth_token: str) -> None:
        try:
            from twilio.rest import Client as TwilioClient
        except ImportError:
            raise ImportError(
                "The 'twilio' package is required for OutboundCallManager. "
                + TELEPHONY_INSTALL_HINT
            ) from None
        self._sdk_client = TwilioClient(account_sid, auth_token)

    @property
    def calls(self) -> Any:
        return self._sdk_client.calls

    async def close(self) -> None:
        return None


_TELNYX_AMD_BY_TWILIO_MODE = {
    "DetectMessageEnd": "greeting_end",
    "Enable": "detect",
    "Disabled": "disabled",
}

_NATIVE_TELNYX_AMD_MODES = frozenset(
    {"premium", "detect", "detect_beep", "detect_words", "greeting_end", "disabled"}
)


def telnyx_dial_payload_from_create_kwargs(
    create_kwargs: dict[str, Any],
    *,
    connection_id: str,
    webhook_url: str = "",
) -> dict[str, Any]:
    """Translate the manager's Twilio-shaped create kwargs to a Dial body.

    Only the provider-shared subset is translated (destination, caller,
    AMD mode, webhook target, stream parameters when present); Twilio-only
    media-stream/transcription keys are ignored.
    """
    from easycat.telephony.telnyx import build_stream_parameters

    if not connection_id:
        raise ValueError("connection_id must be non-empty")
    payload: dict[str, Any] = {
        "to": create_kwargs["to"],
        "from": create_kwargs["from_"],
        "connection_id": connection_id,
    }
    machine_detection = str(create_kwargs.get("machine_detection", ""))
    amd_mode = _TELNYX_AMD_BY_TWILIO_MODE.get(machine_detection)
    if amd_mode is None and machine_detection.lower() in _NATIVE_TELNYX_AMD_MODES:
        amd_mode = machine_detection.lower()
    if amd_mode:
        payload["answering_machine_detection"] = amd_mode
    stream_url = create_kwargs.get("stream_url")
    if isinstance(stream_url, str) and stream_url:
        payload.update(build_stream_parameters(stream_url=stream_url))
    effective_webhook_url = webhook_url or create_kwargs.get("status_callback")
    if isinstance(effective_webhook_url, str) and effective_webhook_url:
        payload["webhook_url"] = effective_webhook_url
    return payload


class _TelnyxCreatedCall:
    """Minimal created-call carrier matching the Twilio SDK's ``sid`` shape."""

    __slots__ = ("sid",)

    def __init__(self, sid: str) -> None:
        self.sid = sid


class TelnyxOutboundClient:
    """:class:`OutboundCallClient` adapter over Telnyx Call Control v2.

    The sync ``calls`` surface bridges into the async
    :class:`~easycat.telephony.telnyx_client.TelnyxCallControlClient` because
    the manager invokes it inside ``asyncio.to_thread`` workers with no running
    loop; each command runs on a short-lived loop with a dedicated client so
    no aiohttp session spans loops. Prefer the native ``dial``/``hangup``
    coroutines when calling from async code directly.
    """

    def __init__(
        self,
        api_key: str,
        *,
        connection_id: str = "",
        webhook_url: str = "",
        base_url: str | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._connection_id = connection_id
        self._webhook_url = webhook_url
        self._base_url = base_url
        self._client_factory = client_factory
        self.calls = _TelnyxCallsResource(self)

    def _fresh_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from easycat.telephony.telnyx_client import TELNYX_API_BASE_URL, TelnyxCallControlClient

        return TelnyxCallControlClient(
            self._api_key,
            base_url=self._base_url or TELNYX_API_BASE_URL,
        )

    async def dial(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = self._fresh_client()
        try:
            return await client.dial(payload)
        finally:
            await client.close()

    async def hangup(self, call_control_id: str) -> dict[str, Any]:
        client = self._fresh_client()
        try:
            return await client.hangup(call_control_id)
        finally:
            await client.close()

    async def close(self) -> None:
        return None

    async def _dial_translated(self, create_kwargs: dict[str, Any]) -> _TelnyxCreatedCall:
        payload = telnyx_dial_payload_from_create_kwargs(
            create_kwargs,
            connection_id=self._connection_id,
            webhook_url=self._webhook_url,
        )
        response = await self.dial(payload)
        data = response.get("data") if isinstance(response, dict) else None
        call_control_id = str(data.get("call_control_id", "")) if isinstance(data, dict) else ""
        return _TelnyxCreatedCall(call_control_id)


class _TelnyxCallsResource:
    def __init__(self, owner: TelnyxOutboundClient) -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> _TelnyxCreatedCall:
        return asyncio.run(self._owner._dial_translated(kwargs))

    def __call__(self, call_control_id: str) -> _TelnyxCallUpdater:
        return _TelnyxCallUpdater(self._owner, call_control_id)


class _TelnyxCallUpdater:
    """Adapt ``calls(sid).update(...)`` onto the Telnyx Call Control surface.

    Only the subset the manager actually invokes is supported. Any other
    keyword raises immediately so Twilio-shaped callers get a clear signal
    rather than a silent no-op or unintended hangup.
    """

    def __init__(self, owner: TelnyxOutboundClient, call_control_id: str) -> None:
        self._owner = owner
        self._call_control_id = call_control_id

    def update(self, **kwargs: Any) -> dict[str, Any]:
        status = kwargs.pop("status", None)
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(
                f"Telnyx calls().update() does not support: {unsupported}. "
                f"Only status='completed' is translated to a hangup command."
            )
        if status is not None and status != "completed":
            raise ValueError(
                f"Telnyx calls().update() only supports status='completed'; got {status!r}"
            )
        return asyncio.run(self._owner.hangup(self._call_control_id))


async def emit_telnyx_call_event(
    envelope: dict[str, Any],
    event_bus: EventBus,
    *,
    session_id: str | None = None,
) -> Event | None:
    """Parse a Telnyx webhook envelope and emit the resulting neutral event.

    Returns the emitted event, or ``None`` when the envelope carries no
    supported call event. AMD results are already mapped by
    :func:`easycat.telephony.telnyx.parse_telnyx_call_event`.
    """
    from easycat.telephony.telnyx import parse_telnyx_call_event

    event = parse_telnyx_call_event(envelope, session_id=session_id)
    if event is not None:
        await event_bus.emit(event)
    return event


class OutboundCallManager:
    """Orchestrates placing outbound calls via the Twilio REST API.

    Requires the ``twilio`` Python package (``uv add 'easycat[telephony]'``;
    from the EasyCat repo, use ``uv sync --extra telephony --group dev``).

    Optional pre-call gates (plugged in after construction by
    ``_create_outbound_helpers`` or the caller directly):

    - ``dnc_list`` — a :class:`~easycat.telephony.compliance.DNCList`
      that rejects numbers on the internal Do Not Call list.
    - ``compliance_check`` — a callable ``(to) -> bool`` that returns
      ``False`` when the call should be blocked (e.g. calling hours).
    - ``retry_strategy`` — a :class:`~easycat.telephony.retry.RetryStrategy`
      used by app code to decide whether to retry after a
      :class:`CallFailed`.
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        from_number: str,
        amd_mode: str = "DetectMessageEnd",
        async_amd: bool = True,
        amd_timeout: int = 30,
        speech_threshold: int = 2400,
        speech_end_threshold: int = 1200,
        silence_timeout: int = 5000,
        enable_realtime_transcription: bool = True,
        twilio_account_sid: str = "",
        twilio_auth_token: str = "",
        status_callback_url: str = "",
        twiml_url: str = "",
        session_id: str | None = None,
        client: OutboundCallClient | None = None,
    ) -> None:
        if client is None:
            # Lazy-import twilio at instantiation time.
            client = TwilioRestOutboundClient(twilio_account_sid, twilio_auth_token)

            if not twilio_account_sid or not twilio_auth_token:
                raise ValueError(
                    "twilio_account_sid and twilio_auth_token are required for OutboundCallManager"
                )

        self._event_bus = event_bus
        self._session_id = session_id
        self._from_number = from_number
        self._amd_mode = amd_mode
        self._async_amd = async_amd
        self._amd_timeout = amd_timeout
        self._speech_threshold = speech_threshold
        self._speech_end_threshold = speech_end_threshold
        self._silence_timeout = silence_timeout
        self._enable_realtime_transcription = enable_realtime_transcription
        self._status_callback_url = status_callback_url
        self._twiml_url = twiml_url
        self._client: OutboundCallClient = client
        self._state = OutboundCallManagerState.IDLE
        self._active_call_sid: str | None = None
        self._owned_call_sids: set[str] = set()
        # Calls created by an invalidated placement whose immediate provider
        # completion failed remain manager-owned across stop/start. Keeping
        # this separate from the live lifecycle's owned calls lets ``stop``
        # retain its normal state-reset semantics without losing retry
        # authority for a billable stale call.
        self._pending_cleanup_call_sids: set[str] = set()
        # Provider-created calls being completed after an invalidated or
        # cancelled placement remain placement-owned until REST cleanup and
        # failure-event dispatch both settle. Terminal callbacks observed
        # during that window prevent failed cleanup from resurrecting a call
        # the provider has already declared terminal.
        self._reconciling_call_sids: set[str] = set()
        self._terminal_reconciliation_call_sids: set[str] = set()
        # ``place_call`` reports reconciliation failure with a synthetic
        # CallFailed event. Track its exact identity so the manager's own
        # subscriber does not mistake it for a provider terminal callback.
        self._synthetic_failure_event_ids: set[int] = set()
        self._started = False
        # Synchronous lifecycle methods cannot acquire ``_place_call_lock``.
        # Advance an epoch on every real start/stop transition instead, so an
        # in-flight REST create can detect that the lifecycle which authorized
        # it no longer exists before publishing its SID.
        self._lifecycle_epoch: Epoch[None] = Epoch(None)
        # Serialize the full eligibility -> REST create -> activation
        # transaction.  ``place_call`` awaits both DNC storage and the Twilio
        # thread offload, so a state check alone lets concurrent callers both
        # observe IDLE and originate separate billable calls before either one
        # records its SID.
        self._place_call_lock = asyncio.Lock()

        # Optional plug-ins assigned after construction (see docstring).
        self.dnc_list: DNCStore | None = None
        self.compliance_check: Callable[[str], bool] | None = None
        self.retry_strategy: Any | None = None

    @property
    def state(self) -> OutboundCallManagerState:
        return self._state

    @property
    def active_call_sid(self) -> str | None:
        return self._active_call_sid

    def set_session_id(self, session_id: str) -> None:
        """Attach the owning Session correlation ID to lifecycle events."""
        self._session_id = session_id

    def start(self) -> None:
        if self._started:
            return
        self._lifecycle_epoch.bump(None)
        self._event_bus.subscribe(CallRinging, self._on_call_ringing)
        self._event_bus.subscribe(CallAnswered, self._on_call_answered)
        self._event_bus.subscribe(CallEnded, self._on_call_ended)
        self._event_bus.subscribe(CallFailed, self._on_call_failed)
        self._started = True
        self._state = OutboundCallManagerState.IDLE
        self._active_call_sid = None

    def stop(self) -> None:
        # Invalidate an in-flight placement before resetting visible state.
        # ``place_call`` retains ownership of its uncancellable REST worker and
        # will immediately complete any SID returned for this stale epoch.
        self._lifecycle_epoch.bump(None)
        if self._started:
            self._event_bus.unsubscribe(CallRinging, self._on_call_ringing)
            self._event_bus.unsubscribe(CallAnswered, self._on_call_answered)
            self._event_bus.unsubscribe(CallEnded, self._on_call_ended)
            self._event_bus.unsubscribe(CallFailed, self._on_call_failed)
        self._state = OutboundCallManagerState.IDLE
        self._active_call_sid = None
        self._owned_call_sids.clear()
        self._started = False

    async def hangup_call(self, call_sid: str | None = None) -> None:
        """Hang up an active Twilio call without making ``stop()`` block on REST I/O."""
        target_call_sid = call_sid or self._active_call_sid
        if not target_call_sid:
            return
        error, cancellation = await self._complete_call_owned(target_call_sid)
        if error is not None:
            if cancellation is not None:
                raise cancellation from error
            raise error
        self._clear_active_call(target_call_sid)
        if cancellation is not None:
            raise cancellation

    async def hangup_owned_call(self, call_sid: str) -> None:
        """Hang up ``call_sid`` only when this manager created it."""
        if (
            call_sid not in self._owned_call_sids
            and call_sid not in self._pending_cleanup_call_sids
        ):
            return
        await self.hangup_call(call_sid)

    async def place_call(self, to: str) -> str:
        """Place an outbound call and return the call SID.

        Pre-call gates:

        - Numbers on :attr:`dnc_list` raise ``ValueError`` immediately.
        - ``compliance_check(to) is False`` raises ``ValueError``.  Use
          this to wire :func:`~easycat.telephony.compliance.check_calling_hours`
          or your own timezone / DNC-vendor lookup.
        """
        async with self._place_call_lock:
            (
                call_sid,
                cancellation,
                create_error,
                lifecycle_error,
                stale_cleanup_error,
                placement,
            ) = await self._place_call_transaction(to)

        if lifecycle_error is not None:
            assert call_sid is not None
            return await self._finish_stale_call_placement(
                call_sid=call_sid,
                error=lifecycle_error,
                cleanup_error=stale_cleanup_error,
                cancellation=cancellation,
            )

        return await self._finish_call_placement(
            to=to,
            call_sid=call_sid if create_error is None else None,
            cancellation=cancellation,
            create_error=create_error,
            placement=placement,
        )

    async def _place_call_transaction(
        self,
        to: str,
    ) -> tuple[
        str | None,
        asyncio.CancelledError | None,
        Exception | None,
        RuntimeError | None,
        Exception | None,
        Lease[None],
    ]:
        """Run eligibility, provider creation, and epoch reconciliation under the lock."""
        self._ensure_can_place_call()
        placement = self._lifecycle_epoch.capture()
        await self._check_pre_call_gates(to)
        if not self._placement_is_current(placement):
            # A stop while awaiting a DNC store invalidates the transaction
            # before it reaches Twilio, so no provider reconciliation is needed.
            # Keep this lifecycle error in the create_error tuple slot:
            # place_call must route it through _finish_call_placement instead
            # of the stale-call path, which requires a provider call SID.
            return None, None, self._lifecycle_changed_error(), None, None, placement

        # ``asyncio.to_thread`` cannot stop its worker when the awaiting
        # coroutine is cancelled. Retain placement ownership until it settles.
        call, cancellation, create_error = await self._create_call_owned(
            self._build_create_kwargs(to)
        )
        if create_error is not None:
            return None, cancellation, create_error, None, None, placement
        assert call is not None
        try:
            call_sid = call.sid
            if not isinstance(call_sid, str) or not call_sid:
                raise ValueError("Twilio call creation returned an empty call SID")
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            return None, cancellation, exc, None, None, placement

        if self._placement_is_current(placement) and cancellation is None:
            self._owned_call_sids.add(call_sid)
            self._set_active_call(call_sid)
            return call_sid, cancellation, None, None, None, placement

        self._reconciling_call_sids.add(call_sid)
        stale_cleanup_error, cleanup_cancellation = await self._complete_call_owned(call_sid)
        cancellation = cancellation or cleanup_cancellation
        if self._placement_is_current(placement):
            lifecycle_error = self._placement_cancelled_error(
                call_sid=call_sid,
                cleanup_error=stale_cleanup_error,
            )
        else:
            lifecycle_error = self._lifecycle_changed_error(
                call_sid=call_sid,
                cleanup_error=stale_cleanup_error,
            )
        if (
            stale_cleanup_error is not None
            and call_sid not in self._terminal_reconciliation_call_sids
        ):
            # Keep retry ownership without reactivating the current lifecycle.
            self._pending_cleanup_call_sids.add(call_sid)
        return (
            call_sid,
            cancellation,
            None,
            lifecycle_error,
            stale_cleanup_error,
            placement,
        )

    def _ensure_can_place_call(self) -> None:
        if not self._started:
            raise RuntimeError("OutboundCallManager must be started before placing calls")
        if (
            self._state is not OutboundCallManagerState.IDLE
            or self._active_call_sid is not None
            or self._owned_call_sids
            or self._pending_cleanup_call_sids
            or self._reconciling_call_sids
        ):
            raise RuntimeError("OutboundCallManager already has an active call or pending cleanup")

    async def _check_pre_call_gates(self, to: str) -> None:
        if self.dnc_list is not None and await dnc_is_on_dnc(self.dnc_list, to):
            raise ValueError(f"Refusing to call {to!r}: on DNC list")
        if self.compliance_check is not None and not self.compliance_check(to):
            raise ValueError(
                f"Refusing to call {to!r}: blocked by compliance_check "
                "(e.g. outside allowed calling hours)"
            )

    def _build_create_kwargs(self, to: str) -> dict[str, Any]:
        create_kwargs: dict[str, Any] = {
            "to": to,
            "from_": self._from_number,
            "url": self._twiml_url,
            "machine_detection": self._amd_mode,
            "async_amd": str(self._async_amd).lower(),
            "machine_detection_timeout": self._amd_timeout,
            "machine_detection_speech_threshold": self._speech_threshold,
            "machine_detection_speech_end_threshold": self._speech_end_threshold,
            "machine_detection_silence_timeout": self._silence_timeout,
        }
        if self._enable_realtime_transcription:
            create_kwargs["transcription"] = True
            create_kwargs["transcription_track"] = "inbound_track"
        if self._status_callback_url:
            create_kwargs["status_callback"] = self._status_callback_url
            create_kwargs["status_callback_event"] = [
                "initiated",
                "ringing",
                "answered",
                "completed",
            ]
        return create_kwargs

    def _placement_is_current(self, placement: Lease[None]) -> bool:
        return self._started and placement.guard()

    @staticmethod
    def _lifecycle_changed_error(
        *,
        call_sid: str | None = None,
        cleanup_error: Exception | None = None,
    ) -> RuntimeError:
        message = "Outbound call placement was invalidated by a manager lifecycle change"
        if call_sid is not None:
            message += f" after Twilio created call {call_sid}"
        if cleanup_error is not None:
            message += f"; immediate hangup failed: {cleanup_error}"
        return RuntimeError(message)

    @staticmethod
    def _placement_cancelled_error(
        *,
        call_sid: str,
        cleanup_error: Exception | None = None,
    ) -> RuntimeError:
        message = f"Outbound call placement was cancelled after Twilio created call {call_sid}"
        if cleanup_error is not None:
            message += f"; immediate hangup failed: {cleanup_error}"
        return RuntimeError(message)

    async def _create_call_owned(
        self,
        create_kwargs: dict[str, Any],
    ) -> tuple[Any | None, asyncio.CancelledError | None, Exception | None]:
        """Await one uncancellable REST worker while retaining placement ownership."""
        settlement = await shielded_cleanup(
            lambda: asyncio.to_thread(self._client.calls.create, **create_kwargs)
        )
        cancellation = asyncio.CancelledError() if settlement.cancellation_requests else None
        if settlement.error is None:
            return settlement.result, cancellation, None
        if isinstance(settlement.error, Exception):
            return None, cancellation, settlement.error
        raise settlement.error

    async def _complete_call_owned(
        self,
        call_sid: str,
    ) -> tuple[Exception | None, asyncio.CancelledError | None]:
        """Complete a Twilio call without abandoning its REST worker."""
        settlement = await shielded_cleanup(
            lambda: asyncio.to_thread(
                self._client.calls(call_sid).update,
                status="completed",
            )
        )
        cancellation = asyncio.CancelledError() if settlement.cancellation_requests else None
        if settlement.error is not None:
            if isinstance(settlement.error, Exception):
                return settlement.error, cancellation
            raise settlement.error
        return None, cancellation

    async def _finish_stale_call_placement(
        self,
        *,
        call_sid: str,
        error: RuntimeError,
        cleanup_error: Exception | None,
        cancellation: asyncio.CancelledError | None,
    ) -> str:
        """Report a stale placement, then preserve the original caller outcome."""
        dispatch_error: Exception | None = None
        failure_event = CallFailed(
            call_sid=call_sid,
            reason=str(error),
            session_id=self._session_id,
        )
        failure_event_id = id(failure_event)
        self._synthetic_failure_event_ids.add(failure_event_id)
        try:
            await self._event_bus.emit(failure_event)
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            dispatch_error = exc
        finally:
            self._synthetic_failure_event_ids.discard(failure_event_id)
            terminal_observed = call_sid in self._terminal_reconciliation_call_sids
            self._terminal_reconciliation_call_sids.discard(call_sid)
            self._reconciling_call_sids.discard(call_sid)
            if terminal_observed:
                # A provider terminal callback is authoritative even when the
                # redundant REST completion failed.
                self._pending_cleanup_call_sids.discard(call_sid)
        if cancellation is not None:
            raise cancellation from (dispatch_error or error)
        if dispatch_error is not None:
            raise error from dispatch_error
        raise error from cleanup_error

    async def _finish_call_placement(
        self,
        *,
        to: str,
        call_sid: str | None,
        cancellation: asyncio.CancelledError | None,
        create_error: Exception | None,
        placement: Lease[None],
    ) -> str:
        """Dispatch placement events after unlocking, then propagate the caller outcome."""
        # EventBus dispatch is inline and async handlers are awaited. Never hold
        # the non-reentrant placement lock across dispatch: a handler that tries
        # another placement must reach the active-call check and fail promptly,
        # not deadlock waiting on its own dispatch stack.
        if create_error is not None:
            await self._event_bus.emit(
                CallFailed(
                    call_sid="",
                    reason=str(create_error),
                    session_id=self._session_id,
                )
            )
            if cancellation is not None:
                raise cancellation
            raise create_error

        assert call_sid is not None
        # Treat the whole initiated-dispatch window as reconciliation-owned.
        # A provider terminal callback can arrive while an async subscriber is
        # still blocked.  If dispatch is then cancelled, remembering that
        # terminal outcome prevents a redundant REST completion failure from
        # resurrecting already-ended call ownership.
        self._reconciling_call_sids.add(call_sid)
        try:
            await self._event_bus.emit(
                CallInitiated(
                    call_sid=call_sid,
                    to=to,
                    from_=self._from_number,
                    session_id=self._session_id,
                )
            )
        except asyncio.CancelledError as exc:
            return await self._finish_dispatch_failed_call(
                call_sid=call_sid,
                error=RuntimeError("CallInitiated dispatch was cancelled"),
                cancellation=exc,
            )
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            return await self._finish_dispatch_failed_call(
                call_sid=call_sid,
                error=exc,
                cancellation=cancellation,
            )
        if not self._placement_is_current(placement):
            return await self._finish_dispatch_failed_call(
                call_sid=call_sid,
                error=self._lifecycle_changed_error(call_sid=call_sid),
                cancellation=cancellation,
            )
        self._terminal_reconciliation_call_sids.discard(call_sid)
        self._reconciling_call_sids.discard(call_sid)
        if cancellation is not None:
            raise cancellation
        return call_sid

    async def _finish_dispatch_failed_call(
        self,
        *,
        call_sid: str,
        error: Exception,
        cancellation: asyncio.CancelledError | None,
    ) -> str:
        """Reconcile an activated call whose initiated-event dispatch failed."""
        self._reconciling_call_sids.add(call_sid)
        cleanup_error, cleanup_cancellation = await self._complete_call_owned(call_sid)
        cancellation = cancellation or cleanup_cancellation
        terminal_observed = call_sid in self._terminal_reconciliation_call_sids
        self._clear_active_call(call_sid)
        if cleanup_error is not None and not terminal_observed:
            self._pending_cleanup_call_sids.add(call_sid)

        if cleanup_error is None:
            failure_reason = f"{error}; Twilio call {call_sid} was completed"
        else:
            failure_reason = (
                f"{error}; immediate hangup of Twilio call {call_sid} failed: {cleanup_error}"
            )
        failure_event = CallFailed(
            call_sid=call_sid,
            reason=failure_reason,
            session_id=self._session_id,
        )
        failure_event_id = id(failure_event)
        self._synthetic_failure_event_ids.add(failure_event_id)
        secondary_dispatch_error: Exception | None = None
        try:
            await self._event_bus.emit(failure_event)
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            secondary_dispatch_error = exc
        finally:
            self._synthetic_failure_event_ids.discard(failure_event_id)
            terminal_observed = (
                terminal_observed or call_sid in self._terminal_reconciliation_call_sids
            )
            self._terminal_reconciliation_call_sids.discard(call_sid)
            self._reconciling_call_sids.discard(call_sid)
            if terminal_observed:
                self._pending_cleanup_call_sids.discard(call_sid)

        if cancellation is not None:
            raise cancellation from error
        if cleanup_error is not None:
            reconciliation_error = RuntimeError(failure_reason)
            if secondary_dispatch_error is not None:
                reconciliation_error.add_note(
                    f"CallFailed dispatch also failed: {secondary_dispatch_error}"
                )
            raise error from reconciliation_error
        raise error

    def _set_active_call(self, call_sid: str) -> None:
        if not call_sid:
            return
        if self._active_call_sid not in (None, call_sid):
            return
        self._active_call_sid = call_sid
        self._state = OutboundCallManagerState.ACTIVE

    def _clear_active_call(self, call_sid: str) -> None:
        self._owned_call_sids.discard(call_sid)
        self._pending_cleanup_call_sids.discard(call_sid)
        if self._active_call_sid is not None and call_sid != self._active_call_sid:
            return
        self._active_call_sid = None
        self._state = OutboundCallManagerState.IDLE

    def _record_terminal_call(self, call_sid: str) -> None:
        if call_sid in self._reconciling_call_sids:
            self._terminal_reconciliation_call_sids.add(call_sid)
        self._clear_active_call(call_sid)

    async def _on_call_ringing(self, event: CallRinging) -> None:
        if event.call_sid in self._owned_call_sids:
            self._set_active_call(event.call_sid)

    async def _on_call_answered(self, event: CallAnswered) -> None:
        if event.call_sid in self._owned_call_sids:
            self._set_active_call(event.call_sid)

    async def _on_call_ended(self, event: CallEnded) -> None:
        self._record_terminal_call(event.call_sid)

    async def _on_call_failed(self, event: CallFailed) -> None:
        if id(event) in self._synthetic_failure_event_ids:
            return
        self._record_terminal_call(event.call_sid)
