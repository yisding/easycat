"""Provider-neutral telephony media-stream machinery.

Single shared home for logic that every carrier media transport (Twilio
Media Streams, Telnyx Call Control) needs identically:

- JSON message parse/decode with provider-labelled diagnostics,
- stream-token validator plumbing (legacy string validators and
  context-aware validators share one invocation path),
- the once-only ``CallEnded`` emitter,
- the non-loopback bind auth guard for server transports, and
- :class:`TelephonyConnectionTransportBase`, the accepted-socket lifecycle
  machine (connect transaction with rollback, disconnect ledger,
  cancellation reaping, ``wait_for_start`` preflight).

Per-provider wire differences (framing field names, codecs, auth transport)
stay in :mod:`easycat.transports.twilio_media` /
:mod:`easycat.transports.telnyx_media`. This module owns only what is truly
common so a lifecycle fix lands once instead of twice.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar, cast, get_type_hints

import websockets
from websockets.asyncio.server import ServerConnection

from easycat._audio_utils import PCM16StreamResampler
from easycat._epoch import Epoch, Lease
from easycat._net import is_loopback_host
from easycat.events import CallEnded, EventBus
from easycat.runtime._event_tasks import RuntimeTaskScope
from easycat.runtime.scope import BackgroundTaskScope, RuntimeScope
from easycat.telephony._stream_tokens import StreamTokenContext
from easycat.transports._base import AudioQueueMixin
from easycat.transports._limits import DEFAULT_INBOUND_AUDIO_MAX_BYTES

logger = logging.getLogger(__name__)

_TELEPHONY_RECEIVE_COHORT = "transport-receive"

StreamTokenClaims = Mapping[str, Any]
StreamTokenValidatorResult = bool | StreamTokenClaims | None
StreamTokenValidator = (
    Callable[[str], StreamTokenValidatorResult | Awaitable[StreamTokenValidatorResult]]
    | Callable[
        [StreamTokenContext], StreamTokenValidatorResult | Awaitable[StreamTokenValidatorResult]
    ]
)


def decode_telephony_raw(raw: str | bytes, *, provider: str) -> str | None:
    """Decode one inbound WebSocket frame to text, dropping non-UTF-8 input."""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("Ignoring non-UTF-8 %s message", provider)
        return None


def parse_telephony_message(raw: str, *, provider: str) -> dict[str, Any] | None:
    """Parse one telephony WebSocket message and require a JSON object."""
    try:
        msg = json.loads(raw)
    except (RecursionError, ValueError):
        logger.warning("Ignoring invalid JSON from %s", provider)
        return None
    if not isinstance(msg, dict):
        logger.warning("Ignoring non-object JSON from %s", provider)
        return None
    return msg


def parse_wire_int(value: Any) -> int | None:
    """Coerce a wire sequence number or timestamp that may arrive as a string."""
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


async def run_stream_token_validation(
    *,
    validator: StreamTokenValidator | None,
    context: StreamTokenContext,
    token_parameter: str,
    validation_timeout_s: float,
    provider: str,
) -> dict[str, str] | None:
    """Invoke a stream-token validator and normalize its result to claims.

    Returns ``{}`` when no validator is configured, ``None`` when validation
    fails, times out, or raises, and otherwise the coerced claims minus the
    token parameter itself.
    """
    if validator is None:
        return {}
    token = context.token
    try:
        async with asyncio.timeout(validation_timeout_s):
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
            claims.pop(token_parameter, None)
        return claims
    except TimeoutError:
        logger.warning("%s stream token validator timed out", provider)
        return None
    except Exception:
        logger.warning("%s stream token validator raised", provider, exc_info=True)
        return None


async def emit_call_ended(
    event_bus: EventBus | None,
    *,
    call_id: str | None,
    answered_at: float | None,
    call_identity: Any | None,
    session_id: str | None,
) -> None:
    """Emit the once-per-call ``CallEnded`` event when a call id is known."""
    if event_bus is None or call_id is None:
        return
    duration = None
    if answered_at is not None:
        duration = max(0.0, time.monotonic() - answered_at)
    await event_bus.emit(
        CallEnded(
            call_sid=call_id,
            duration_s=duration,
            number=call_identity.caller_number if call_identity is not None else None,
            session_id=session_id,
        )
    )


def enforce_media_bind_auth(
    *,
    host: str,
    provider_label: str,
    config_class_name: str,
    stream_token_validator: StreamTokenValidator | None,
    unsafe_allow_no_auth: bool,
) -> None:
    """Reject a public media bind without a stream-token validator."""
    if not is_loopback_host(host) and stream_token_validator is None and not unsafe_allow_no_auth:
        raise ValueError(
            f"{config_class_name}.stream_token_validator is required when "
            f"binding {provider_label} media to a non-loopback host; pass "
            "unsafe_allow_no_auth=True only for an intentionally unauthenticated listener"
        )


class TelephonyConnectionTransportBase(AudioQueueMixin):
    """Accepted-socket telephony media transport: one shared lifecycle machine.

    Wraps exactly one injected WebSocket connection and owns everything that
    is identical across carriers: single-flight :meth:`connect` transactions
    with startup rollback, serialized disconnect with an interrupted-cleanup
    retry ledger, receive-task reaping that never consumes caller
    cancellation, reconnect-race-guarded cleanup, and ``wait_for_start``
    preflight so invalid media sockets never compile provider configuration.

    Concrete classes mix in a per-provider protocol mixin for the wire
    protocol and implement these hooks:

    * ``_has_accepted_stream()`` — a start frame was accepted on this socket.
    * ``_has_active_call_state()`` — any per-call teardown state remains.
    * ``_clear_call_refs()`` — clear the per-provider stream/call identifiers.
    * ``_run_receive_loop()`` — drive the protocol receive stream.
    * ``_store_prevalidated_start(msg)`` — validate/stage a pre-connect start.
    * ``send_audio`` / ``send_mark`` / ``clear_audio`` — outbound wire paths.
    """

    _PROVIDER_LABEL: ClassVar[str]

    # Provided by the per-provider protocol mixin at runtime; declared here
    # so the shared lifecycle can reset them without knowing field names.
    _diagnostics: Any
    _inbound_resampler: PCM16StreamResampler
    _MESSAGE_HANDLERS: ClassVar[dict[str, Any]]
    _call_identity: Any | None
    _answered_at: float | None
    _call_ended_emitted: bool

    def __init__(
        self,
        ws: ServerConnection,
        *,
        event_bus: EventBus | None = None,
        max_pending_chunks: int = 200,
        max_pending_bytes: int = DEFAULT_INBOUND_AUDIO_MAX_BYTES,
    ) -> None:
        lower = self._PROVIDER_LABEL.lower()
        self._ws = ws
        # AudioQueueMixin preserves a constructor-injected event bus while it
        # initializes the queue and diagnostics machinery.
        self._event_bus = event_bus
        self._receive_task: asyncio.Task[None] | None = None
        self._receive_tasks = RuntimeTaskScope(
            owner_label=f"{lower}-connection-receive",
            member_name=f"{lower}_receive",
            cohort=_TELEPHONY_RECEIVE_COHORT,
            logger=logger,
            failure_message=f"{self._PROVIDER_LABEL} receive loop failed",
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
        self._lifecycle_tasks = BackgroundTaskScope(name=f"{lower}-connection-lifecycle")
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
        self._init_audio_queue(max_pending_chunks, max_pending_bytes)

    # ── Per-provider hooks ────────────────────────────────────────

    def _has_accepted_stream(self) -> bool:
        """Return True once a start frame has been accepted on this socket."""
        raise NotImplementedError

    def _has_active_call_state(self) -> bool:
        """Return True while any per-call teardown state remains."""
        raise NotImplementedError

    def _clear_call_refs(self) -> None:
        """Clear the per-provider stream/call identifiers."""
        raise NotImplementedError

    async def _run_receive_loop(self) -> None:
        """Drive the provider receive stream over the accepted socket."""
        raise NotImplementedError

    async def _accept_start(
        self,
        msg: dict[str, Any],
        *,
        token_prevalidated: bool,
        prevalidated_claims: dict[str, str] | None = None,
    ) -> bool:
        """Apply a staged or live ``start`` frame; return False to reject."""
        raise NotImplementedError

    async def send_mark(self, name: str | None = None) -> str:
        """Send a playback-position mark over the wire."""
        raise NotImplementedError

    async def _store_prevalidated_start(self, msg: dict[str, Any]) -> bool | None:
        """Validate and stage a start frame seen before ``connect()``."""
        raise NotImplementedError

    # ── Shared runtime-scope plumbing ─────────────────────────────

    def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach receive and event work to the owning transport scope."""
        super().set_runtime_scope(parent, name=name)
        scope = self._emit_scope
        assert scope is not None
        self._receive_tasks.bind(scope)

    # ── Connect transaction ───────────────────────────────────────

    async def connect(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._connect_task is current:
            return
        if current is not None and self._lifecycle_owner is current:
            raise RuntimeError(f"{type(self).__name__}.connect() cannot run during disconnect()")
        leader = False
        connect_task: asyncio.Task[None] | None = None
        async with self._lifecycle_lock:
            connect_task = self._connect_task
            leader = connect_task is None or connect_task.done()
            if leader:
                connect_task = self._lifecycle_tasks.create_task(
                    f"{self._PROVIDER_LABEL.lower()}-connection-connect",
                    self._connect_transaction(),
                    log_errors=False,
                )
                self._connect_task = connect_task
        if connect_task is None:
            raise RuntimeError(
                f"{self._PROVIDER_LABEL} connection transaction was not initialized"
            )
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
                        raise ConnectionError(
                            f"{self._PROVIDER_LABEL} transport disconnected during connect"
                        )
                    receive_task = self._receive_tasks.create_task(
                        self._run_receive_loop(),
                        task_name=f"{self._PROVIDER_LABEL.lower()}-connection-receive",
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
        label = self._PROVIDER_LABEL
        if self._connected:
            return None
        if self._disconnect_cleanup_error is not None:
            raise RuntimeError(
                f"{label} connection cleanup is incomplete; call disconnect() "
                "again before reconnecting"
            ) from self._disconnect_cleanup_error
        if self._socket_consumed:
            if self._socket_close_pending:
                raise RuntimeError(
                    f"{label} accepted connection has ended; call disconnect() "
                    "to finish socket cleanup"
                )
            raise RuntimeError(f"{label} accepted connection is already closed")
        if not self._socket_close_pending:
            raise RuntimeError(f"{label} accepted connection is already closed")
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
            logger.debug(
                "Error closing %s WebSocket after connect failure",
                self._PROVIDER_LABEL,
                exc_info=True,
            )
        await self._drain_emit_tasks()

    # ── Disconnect ────────────────────────────────────────────────

    async def disconnect(self) -> None:
        current = asyncio.current_task()
        if current is not None and self._lifecycle_owner is current:
            if self._lifecycle_action == "disconnect":
                return
            raise RuntimeError(f"{type(self).__name__}.disconnect() cannot run during connect()")
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
            and not self._has_active_call_state()
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
                logger.debug("Error closing %s WebSocket", self._PROVIDER_LABEL, exc_info=True)
                cleanup_errors.append(exc)
        self._enqueue_sentinel()
        try:
            await self._drain_emit_tasks()
        except Exception as exc:
            logger.debug(
                "Error draining %s diagnostic events", self._PROVIDER_LABEL, exc_info=True
            )
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
            logger.debug(
                "%s receive loop failed during disconnect",
                self._PROVIDER_LABEL,
                exc_info=True,
            )
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
            f"{self._PROVIDER_LABEL} connection disconnect was interrupted by cancellation"
        )

    def _clear_connection_metadata(self) -> None:
        """Reset every per-call field, including staged pre-connect state."""
        self._clear_call_refs()
        self._call_identity = None
        self._answered_at = None
        self._call_ended_emitted = False
        self._pending_start_message = None
        self._pending_start_claims = None
        self._diagnostics.reset()
        self._inbound_resampler.reset()

    # ── Pre-start preflight ───────────────────────────────────────

    async def _handle_pre_start_message(self, msg: dict[str, Any]) -> bool | None:
        """Route a pre-connect message; ``start`` staging decides the verdict."""
        event = msg.get("event")
        if event == "start":
            return await self._store_prevalidated_start(msg)

        handler = self._MESSAGE_HANDLERS.get(event) if isinstance(event, str) else None
        if handler is None:
            logger.debug("Unknown %s event: %s", self._PROVIDER_LABEL, event)
            return None
        await handler(self, msg)
        return None

    async def wait_for_start(self, *, timeout_s: float | None = None) -> bool:
        """Read through the first authenticated ``start`` message.

        The multi-call telephony servers use this before creating an EasyCat
        session so invalid media sockets never compile provider configuration.
        The one-time token is consumed here; the accepted ``start`` frame is
        stored and applied during ``connect()`` after Session has attached the
        event bus and caller-identity sink.
        """
        if timeout_s is None:
            return await self._wait_for_start()
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        try:
            async with asyncio.timeout(timeout_s):
                return await self._wait_for_start()
        except TimeoutError:
            await self._ws.close(1008, f"Timed out waiting for {self._PROVIDER_LABEL} start")
            return False

    async def _wait_for_start(self) -> bool:
        if self._has_accepted_stream() or self._pending_start_message is not None:
            return True
        if self._connected:
            return self._has_accepted_stream()

        ws = self._ws
        try:
            async for raw in ws:
                decoded = decode_telephony_raw(raw, provider=self._PROVIDER_LABEL)
                if decoded is None:
                    continue
                msg = parse_telephony_message(decoded, provider=self._PROVIDER_LABEL)
                if msg is None:
                    continue
                result = await self._handle_pre_start_message(msg)
                if result is not None:
                    return result
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("%s media stream disconnected before start", self._PROVIDER_LABEL)
            if isinstance(exc, websockets.exceptions.ConnectionClosedError):
                reason = f"{self._PROVIDER_LABEL.lower()} stream closed abnormally before start"
                self._record_transport_disconnect(reason)
        return False

    # ── Generic playback-mark capability ──────────────────────────

    async def send_playback_mark(self, name: str | None = None) -> str:
        """Compatibility wrapper for generic playback-mark capability."""
        return await self.send_mark(name=name)


__all__ = [
    "TelephonyConnectionTransportBase",
]
