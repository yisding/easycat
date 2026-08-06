"""Direct unit tests for :meth:`CapacityGate.drain`.

These exercise the drain collaborator in isolation (no sockets) and pin the
review-fix contract: the drain OWNS the single graceful stop, escalates to force
on timeout, and — critically — does NOT deadlock against a session that
replicates the real ``Session._stopping`` idempotency guard (where a
``stop(force=True)`` after an in-progress graceful stop is a no-op).
"""

from __future__ import annotations

import asyncio

import pytest

from easycat.server import transports as server_transports
from easycat.server.config import VoiceServerConfig
from easycat.server.transports import CapacityGate
from easycat.server.webrtc_routes import WebRTCRoutes
from easycat.session_manager import SessionManager
from easycat.transports._webrtc_config import WebRTCTransportConfig


class _GracefulSession:
    """A session whose graceful stop completes promptly (no force needed)."""

    def __init__(self) -> None:
        self.graceful = False
        self.forced = False

    async def stop(self, *, force: bool = False) -> None:
        if force:
            self.forced = True
            return
        self.graceful = True


class _HangUntilForceSession:
    """Graceful stop hangs until a force call releases it (no ``_stopping`` guard)."""

    def __init__(self) -> None:
        self.graceful_started = asyncio.Event()
        self.forced = False
        self._release = asyncio.Event()

    async def stop(self, *, force: bool = False) -> None:
        if force:
            self.forced = True
            self._release.set()
            return
        self.graceful_started.set()
        await self._release.wait()


class _GuardedSession:
    """Replicates the real ``Session._stopping`` guard.

    A graceful stop sets ``_stopping`` and hangs forever; a concurrent
    ``stop(force=True)`` is a NO-OP (the real early-return). The drain must
    cancel and reap the graceful task before it can enter the force path.
    """

    def __init__(self) -> None:
        self.graceful_started = asyncio.Event()
        self.force_started = asyncio.Event()
        self.force_path_ran = False
        self._stopping = False
        self._release = asyncio.Event()

    async def stop(self, *, force: bool = False) -> None:
        if self._stopping:
            return
        self._stopping = True
        try:
            if force:
                self.force_path_ran = True
                self.force_started.set()
                return
            self.graceful_started.set()
            await self._release.wait()
        finally:
            self._stopping = False


class _HangEvenInForceSession:
    """A session whose graceful AND forced stop both hang forever.

    The drain's ``force_timeout_s`` must bound the FORCED phase so even this
    pathological session cannot block the caller past the timeout (the task is
    cancelled / abandoned rather than awaited forever).
    """

    def __init__(self) -> None:
        self.graceful_started = asyncio.Event()
        self.force_started = asyncio.Event()
        self._never = asyncio.Event()

    async def stop(self, *, force: bool = False) -> None:
        if force:
            self.force_started.set()
        else:
            self.graceful_started.set()
        await self._never.wait()  # hangs in BOTH paths


class _CancellationResistantForceSession:
    """A forced stop that keeps running after cancellation is requested."""

    def __init__(self) -> None:
        self.force_started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def stop(self, *, force: bool = False) -> None:
        if not force:
            await asyncio.Event().wait()
            return
        self.force_started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release.wait()
        finally:
            self.finished.set()


class _ConcurrentForceSession:
    def __init__(self) -> None:
        self.force_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stop(self, *, force: bool = False) -> None:
        if not force:
            await asyncio.Event().wait()
            return
        self.force_started.set()
        await self.release.wait()


class _ManagedGuardedSession(_GuardedSession):
    async def start(self) -> None:
        return


class _NeverClosedTransport:
    async def wait_closed(self) -> None:
        await asyncio.Event().wait()


def _make_gate(sessions: dict[int, object]) -> CapacityGate[int]:
    gate: CapacityGate[int] = CapacityGate(max_sessions=8)
    for key in sessions:
        assert gate.try_acquire()
        gate.track(key)
    return gate


def _pairs(sessions: dict[int, object]):
    return lambda: list(sessions.items())


async def test_safe_await_ignores_preexisting_cancellation_count() -> None:
    owned = asyncio.create_task(asyncio.Event().wait())
    owned.cancel()
    await asyncio.gather(owned, return_exceptions=True)

    async def reap_after_caught_cancellation() -> int:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert current.cancelling() == 1

        await server_transports._safe_await(owned)
        return current.cancelling()

    caller = asyncio.create_task(reap_after_caught_cancellation())

    assert await caller == 1
    assert owned.cancelled()


async def test_safe_await_preserves_cancellation_pending_at_entry() -> None:
    owned = asyncio.create_task(asyncio.Event().wait())

    async def cancel_before_await() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await server_transports._safe_await(owned)

    caller = asyncio.create_task(cancel_before_await())

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert owned.cancelled()


async def test_safe_await_propagates_new_cancellation_count() -> None:
    owned_started = asyncio.Event()
    awaiting_owned = asyncio.Event()

    async def wait_forever() -> None:
        owned_started.set()
        await asyncio.Event().wait()

    owned = asyncio.create_task(wait_forever())
    await owned_started.wait()

    async def reap_after_caught_cancellation() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert current.cancelling() == 1

        awaiting_owned.set()
        await server_transports._safe_await(owned)

    caller = asyncio.create_task(reap_after_caught_cancellation())
    await awaiting_owned.wait()
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert caller.cancelling() == 2
    assert owned.cancelled()


@pytest.mark.parametrize("max_sessions", [True, 1.5, float("nan"), float("inf"), 0, -1])
def test_capacity_gate_rejects_invalid_session_caps(max_sessions: object) -> None:
    with pytest.raises(ValueError, match="max_sessions"):
        CapacityGate(max_sessions)  # type: ignore[arg-type]


def test_capacity_gate_one_is_an_exact_bound() -> None:
    gate: CapacityGate[int] = CapacityGate(1)
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False


@pytest.mark.parametrize("max_sessions", [True, 1.5, float("nan"), float("inf"), 0, -1])
def test_voice_server_config_rejects_invalid_session_caps(max_sessions: object) -> None:
    with pytest.raises(ValueError, match="max_sessions"):
        VoiceServerConfig(max_sessions=max_sessions)  # type: ignore[arg-type]


def test_voice_server_config_rejects_invalid_drain_mode() -> None:
    with pytest.raises(ValueError, match="drain_mode"):
        VoiceServerConfig(drain_mode="natural")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("drain_timeout_s", True),
        ("drain_timeout_s", -0.1),
        ("drain_timeout_s", float("nan")),
        ("drain_timeout_s", float("inf")),
        ("drain_timeout_s", 10**1000),
        ("force_shutdown_timeout_s", True),
        ("force_shutdown_timeout_s", -0.1),
        ("force_shutdown_timeout_s", float("nan")),
        ("force_shutdown_timeout_s", float("inf")),
        ("force_shutdown_timeout_s", 10**1000),
    ],
)
def test_voice_server_config_rejects_invalid_timeouts(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        VoiceServerConfig(**{field: value})


def test_voice_server_config_preserves_zero_timeout_semantics() -> None:
    config = VoiceServerConfig(drain_timeout_s=0, force_shutdown_timeout_s=0)
    assert config.drain_timeout_s == 0
    assert config.force_shutdown_timeout_s == 0


@pytest.mark.parametrize("timeout", [True, -0.1, float("nan"), float("inf"), 10**1000])
async def test_wait_drained_rejects_invalid_timeout(timeout: object) -> None:
    gate: CapacityGate[int] = CapacityGate(1)
    with pytest.raises(ValueError, match="timeout_s"):
        await gate.wait_drained(timeout_s=timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("poll_interval", [True, 0, -0.1, float("nan"), float("inf"), 10**1000])
async def test_wait_drained_rejects_invalid_poll_interval(poll_interval: object) -> None:
    gate: CapacityGate[int] = CapacityGate(1)
    with pytest.raises(ValueError, match="poll_interval_s"):
        await gate.wait_drained(
            timeout_s=0,
            poll_interval_s=poll_interval,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("timeout", [True, -0.1, float("nan"), float("inf"), 10**1000])
async def test_drain_rejects_invalid_timeouts(timeout: object) -> None:
    gate: CapacityGate[int] = CapacityGate(1)
    with pytest.raises(ValueError, match="drain_timeout_s"):
        await gate.drain(lambda: (), drain_timeout_s=timeout)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="force_timeout_s"):
        await gate.drain(
            lambda: (),
            drain_timeout_s=0,
            force_timeout_s=timeout,  # type: ignore[arg-type]
        )


async def test_drain_stays_graceful_when_stops_finish_in_window() -> None:
    sessions = {1: _GracefulSession(), 2: _GracefulSession()}
    gate = _make_gate(sessions)

    await gate.drain(_pairs(sessions), drain_timeout_s=1.0, force_after=True)

    for s in sessions.values():
        assert isinstance(s, _GracefulSession)
        assert s.graceful is True
        assert s.forced is False  # graceful completed; force never called
    assert gate.active_keys() == ()


async def test_drain_force_escalates_hung_session_after_timeout() -> None:
    s = _HangUntilForceSession()
    sessions: dict[int, object] = {1: s}
    gate = _make_gate(sessions)

    await asyncio.wait_for(
        gate.drain(_pairs(sessions), drain_timeout_s=0.05, force_after=True),
        timeout=2,
    )

    assert s.graceful_started.is_set()
    assert s.forced is True
    assert gate.active_keys() == ()


async def test_drain_does_not_deadlock_against_real_stopping_guard() -> None:
    # The regression: a session that mirrors the real guard. The drain starts the
    # single graceful stop (hangs), times out, cancels it so the guard clears,
    # and then enters the force path.
    s = _GuardedSession()
    sessions: dict[int, object] = {1: s}
    gate = _make_gate(sessions)

    await asyncio.wait_for(
        gate.drain(_pairs(sessions), drain_timeout_s=0.05, force_after=True),
        timeout=2,
    )

    assert s.graceful_started.is_set()
    assert s.force_path_ran is True
    assert gate.active_keys() == ()


async def test_drain_zero_timeout_force_escalates_immediately() -> None:
    s = _HangUntilForceSession()
    sessions: dict[int, object] = {1: s}
    gate = _make_gate(sessions)

    # ``drain_timeout_s <= 0`` (the ``stop(force=True)`` path) collapses the
    # grace window: the session is force-escalated without waiting.
    await asyncio.wait_for(
        gate.drain(_pairs(sessions), drain_timeout_s=0.0, force_after=True),
        timeout=2,
    )

    assert s.forced is True
    assert gate.active_keys() == ()


async def test_wait_drained_observes_natural_untrack_before_timeout() -> None:
    gate: CapacityGate[int] = CapacityGate(max_sessions=2)
    assert gate.try_acquire()
    gate.track(1)

    async def release_later() -> None:
        await asyncio.sleep(0.01)
        gate.untrack(1)
        gate.release()

    task = asyncio.create_task(release_later())
    try:
        assert await gate.wait_drained(timeout_s=1.0)
    finally:
        await task
    assert gate.active_keys() == ()
    assert gate.reserved_count == 0


async def test_wait_drained_returns_false_when_active_session_survives_timeout() -> None:
    gate: CapacityGate[int] = CapacityGate(max_sessions=2)
    assert gate.try_acquire()
    gate.track(1)

    assert await gate.wait_drained(timeout_s=0.0) is False
    assert gate.active_keys() == (1,)
    assert gate.reserved_count == 1


async def test_drain_force_timeout_bounds_a_hung_force_stop() -> None:
    # F4: a session whose force-stop ALSO hangs must not block the drain past
    # ~force_timeout_s. The forced phase (the stop(force=True) call and the
    # follow-on cancel-await) is bounded; the hung task is abandoned.
    s = _HangEvenInForceSession()
    sessions: dict[int, object] = {1: s}
    gate = _make_gate(sessions)

    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.wait_for(
        gate.drain(
            _pairs(sessions),
            drain_timeout_s=0.05,
            force_after=True,
            force_timeout_s=0.1,
        ),
        timeout=2,
    )
    elapsed = loop.time() - start

    assert s.graceful_started.is_set()
    assert s.force_started.is_set()
    # Drain returned promptly despite the force-stop hanging (bounded, not 2s).
    assert elapsed < 1.5
    assert gate.active_keys() == ()


async def test_force_timeout_is_hard_when_stop_resists_cancellation() -> None:
    session = _CancellationResistantForceSession()
    sessions: dict[int, object] = {1: session}
    gate = _make_gate(sessions)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await gate.drain(
        _pairs(sessions),
        drain_timeout_s=0.0,
        force_after=True,
        force_timeout_s=0.05,
    )
    elapsed = loop.time() - started

    assert elapsed < 0.5
    assert session.force_started.is_set()
    assert session.cancel_seen.is_set()
    assert gate.active_keys() == ()

    session.release.set()
    await asyncio.wait_for(session.finished.wait(), timeout=1)
    await asyncio.sleep(0)


async def test_forced_shutdown_runs_all_sessions_concurrently() -> None:
    sessions = {key: _ConcurrentForceSession() for key in range(6)}
    gate = _make_gate(sessions)

    drain_task = asyncio.create_task(
        gate.drain(
            _pairs(sessions),
            drain_timeout_s=0.0,
            force_after=True,
            force_timeout_s=1.0,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(*(session.force_started.wait() for session in sessions.values())),
        timeout=0.2,
    )
    for session in sessions.values():
        session.release.set()
    await asyncio.wait_for(drain_task, timeout=1)

    assert gate.active_keys() == ()


async def test_cancelled_drain_keeps_teardown_owned_and_force_escalates() -> None:
    session = _GuardedSession()
    sessions: dict[int, object] = {1: session}
    gate = _make_gate(sessions)

    drain_task = asyncio.create_task(
        gate.drain(
            _pairs(sessions),
            drain_timeout_s=30.0,
            force_after=True,
            force_timeout_s=1.0,
        )
    )
    await session.graceful_started.wait()
    drain_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain_task

    await asyncio.wait_for(session.force_started.wait(), timeout=1)
    await asyncio.gather(*list(gate._drain_tasks))
    assert gate.active_keys() == ()


@pytest.mark.parametrize("disconnect_error", [None, RuntimeError("peer close failed")])
async def test_cancelled_webrtc_offer_disconnects_and_releases_capacity(
    monkeypatch: pytest.MonkeyPatch,
    disconnect_error: RuntimeError | None,
) -> None:
    import easycat.transports.webrtc as webrtc_module

    class _CancelledOfferTransport:
        instance: _CancelledOfferTransport | None = None

        def __init__(self, _config: object) -> None:
            self.disconnected = False
            type(self).instance = self

        def _prepare_external_signaling(self, _web: object) -> None:
            pass

        async def _handle_offer(self, _request: object) -> object:
            raise asyncio.CancelledError

        async def disconnect(self) -> None:
            self.disconnected = True
            if disconnect_error is not None:
                raise disconnect_error

    monkeypatch.setattr(webrtc_module, "WebRTCTransport", _CancelledOfferTransport)
    gate: CapacityGate[int] = CapacityGate(max_sessions=1)
    routes = WebRTCRoutes(
        WebRTCTransportConfig(static_dir=None),
        auth=None,
        config_factory=lambda _transport: object(),  # type: ignore[arg-type]
        gate=gate,
        manager=SessionManager(),
        runtime_feedback=False,
    )
    routes._web = object()

    with pytest.raises(asyncio.CancelledError):
        await routes.handle_offer(object())

    transport = _CancelledOfferTransport.instance
    assert transport is not None
    assert transport.disconnected is True
    assert gate.reserved_count == 0
    assert gate.try_acquire() is True


async def test_webrtc_offer_crossing_drain_during_negotiation_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.transports.webrtc as webrtc_module

    negotiation_started = asyncio.Event()
    allow_negotiation = asyncio.Event()

    class _OfferResponse:
        status = 200

    class _DrainingResponse:
        def __init__(self, **kwargs: object) -> None:
            self.status = kwargs["status"]

    class _Web:
        Response = _DrainingResponse

    class _PausedOfferTransport:
        instance: _PausedOfferTransport | None = None

        def __init__(self, _config: object) -> None:
            self.disconnected = False
            type(self).instance = self

        def _prepare_external_signaling(self, _web: object) -> None:
            pass

        async def _handle_offer(self, _request: object) -> _OfferResponse:
            negotiation_started.set()
            await allow_negotiation.wait()
            return _OfferResponse()

        async def disconnect(self) -> None:
            self.disconnected = True

    monkeypatch.setattr(webrtc_module, "WebRTCTransport", _PausedOfferTransport)
    gate: CapacityGate[int] = CapacityGate(max_sessions=1)
    manager: SessionManager[int] = SessionManager()
    routes = WebRTCRoutes(
        WebRTCTransportConfig(static_dir=None),
        auth=None,
        config_factory=lambda _transport: _GracefulSession(),  # type: ignore[arg-type]
        gate=gate,
        manager=manager,
        runtime_feedback=False,
    )
    routes._web = _Web()
    routes._auth_reason = lambda _request: "allowed"  # type: ignore[method-assign]
    routes._cors_headers = lambda _request: {}  # type: ignore[method-assign]

    offer = asyncio.create_task(routes.handle_offer(object()))
    await negotiation_started.wait()
    gate.start_draining()
    allow_negotiation.set()

    response = await offer
    transport = _PausedOfferTransport.instance
    assert response.status == 503
    assert transport is not None and transport.disconnected is True
    assert manager.active_keys() == ()
    assert gate.active_keys() == ()
    assert gate.reserved_count == 0


async def test_webrtc_offer_crossing_drain_during_session_start_is_rolled_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.transports.webrtc as webrtc_module

    session_started = asyncio.Event()
    allow_start = asyncio.Event()

    class _StartingSession:
        def __init__(self) -> None:
            self.stopped = False
            self.force_stopped = False

        async def start(self) -> None:
            session_started.set()
            await allow_start.wait()

        async def stop(self, *, force: bool = False) -> None:
            self.stopped = True
            self.force_stopped = force
            if not force:
                await asyncio.Event().wait()

    class _OfferResponse:
        status = 200

    class _DrainingResponse:
        def __init__(self, **kwargs: object) -> None:
            self.status = kwargs["status"]

    class _Web:
        Response = _DrainingResponse

    class _StartedOfferTransport:
        instance: _StartedOfferTransport | None = None

        def __init__(self, _config: object) -> None:
            self.disconnected = False
            type(self).instance = self

        def _prepare_external_signaling(self, _web: object) -> None:
            pass

        async def _handle_offer(self, _request: object) -> _OfferResponse:
            return _OfferResponse()

        async def disconnect(self) -> None:
            self.disconnected = True

    session = _StartingSession()
    monkeypatch.setattr(webrtc_module, "WebRTCTransport", _StartedOfferTransport)
    gate: CapacityGate[int] = CapacityGate(max_sessions=1)
    manager: SessionManager[int] = SessionManager()
    routes = WebRTCRoutes(
        WebRTCTransportConfig(static_dir=None),
        auth=None,
        config_factory=lambda _transport: session,  # type: ignore[arg-type]
        gate=gate,
        manager=manager,
        runtime_feedback=False,
    )
    routes._web = _Web()
    routes._auth_reason = lambda _request: "allowed"  # type: ignore[method-assign]
    routes._cors_headers = lambda _request: {}  # type: ignore[method-assign]

    offer = asyncio.create_task(routes.handle_offer(object()))
    await session_started.wait()
    gate.start_draining()
    allow_start.set()

    response = await asyncio.wait_for(offer, timeout=1)
    transport = _StartedOfferTransport.instance
    assert response.status == 503
    assert session.stopped is True
    assert session.force_stopped is True
    assert transport is not None and transport.disconnected is True
    assert manager.active_keys() == ()
    assert gate.active_keys() == ()
    assert gate.reserved_count == 0


async def test_webrtc_cleanup_and_drain_share_one_force_escalatable_stop() -> None:
    session = _ManagedGuardedSession()
    manager: SessionManager[int] = SessionManager()
    await manager.add(1, session)  # type: ignore[arg-type]
    gate = _make_gate({1: session})
    active: dict[int, object] = {1: session}
    routes = WebRTCRoutes(
        WebRTCTransportConfig(static_dir=None),
        auth=None,
        config_factory=lambda _transport: session,  # type: ignore[arg-type]
        gate=gate,
        manager=manager,
        runtime_feedback=False,
        active_session_objs=active,
    )
    cleanup = routes._start_cleanup_task(
        1,
        _NeverClosedTransport(),  # type: ignore[arg-type]
    )

    assert routes._cleanup_task_scope.tasks() == (cleanup,)

    gate.start_draining()
    drain = asyncio.create_task(
        gate.drain(
            lambda: [(1, session)],
            drain_timeout_s=0.02,
            force_after=True,
            force_timeout_s=None,
            stop_for_key=routes._stop_managed_session,
        )
    )
    await session.graceful_started.wait()
    cleanup_cancel = asyncio.create_task(routes.cancel_cleanup_tasks())
    await asyncio.wait_for(asyncio.gather(drain, cleanup_cancel), timeout=2)

    assert session.force_path_ran is True
    assert manager.get(1) is None
    assert active == {}
    assert gate.active_keys() == ()
    assert gate.reserved_count == 0
    assert routes._cleanup_task_scope.tasks() == ()


@pytest.mark.parametrize("finalizer_fails", [False, True])
async def test_webrtc_cleanup_reports_only_unexpected_results_with_task_name(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    *,
    finalizer_fails: bool,
) -> None:
    routes = WebRTCRoutes(
        WebRTCTransportConfig(static_dir=None),
        auth=None,
        config_factory=lambda _transport: _GracefulSession(),  # type: ignore[arg-type]
        gate=CapacityGate(max_sessions=1),
        manager=SessionManager(),
        runtime_feedback=False,
    )

    async def finalize(_key: int, *, force: bool) -> None:
        if force and finalizer_fails:
            raise RuntimeError("forced finalizer failed")

    monkeypatch.setattr(routes, "_finalize_session_cleanup", finalize)
    cleanup = routes._start_cleanup_task(
        7,
        _NeverClosedTransport(),  # type: ignore[arg-type]
    )

    with caplog.at_level("ERROR", logger="easycat.server.webrtc_routes"):
        await routes.cancel_cleanup_tasks()

    messages = [record.getMessage() for record in caplog.records]
    expected = "WebRTC cleanup task easycat-webrtc-force-cleanup-7 failed"
    if finalizer_fails:
        assert expected in messages
    else:
        assert not any(message.startswith("WebRTC cleanup task ") for message in messages)
    assert cleanup.cancelled()
    assert routes._cleanup_task_scope.tasks() == ()
    assert routes._force_cleanup_task_scope.tasks() == ()


async def test_drain_with_no_active_sessions_is_a_noop() -> None:
    gate: CapacityGate[int] = CapacityGate(max_sessions=4)
    await gate.drain(list, drain_timeout_s=1.0, force_after=True)
    assert gate.active_keys() == ()


async def test_drain_without_force_cancels_pending_graceful_stop() -> None:
    # With ``force_after=False`` a still-pending graceful stop must be cancelled,
    # not awaited forever, so the drain still returns.
    s = _HangUntilForceSession()
    sessions: dict[int, object] = {1: s}
    gate = _make_gate(sessions)

    await asyncio.wait_for(
        gate.drain(_pairs(sessions), drain_timeout_s=0.05, force_after=False),
        timeout=2,
    )

    assert s.graceful_started.is_set()
    assert s.forced is False  # no force escalation requested
    assert gate.active_keys() == ()
