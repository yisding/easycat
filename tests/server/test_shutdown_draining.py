"""Graceful-shutdown, draining, and unified-auth tests for ``VoiceServer``.

These drive a real ``websockets`` client against the co-hosted raw-``/ws``
listener (port 0) and assert the M5 behavior: a graceful ``stop()`` drains an
active session within ``drain_timeout_s``, a hung session is force-escalated, a
draining server rejects new connections, the unified ``AuthPolicy`` guards the
``/ws`` path, and the non-loopback bind guard raises at ``start()``.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

from easycat.server import BearerTokenAuth, VoiceServer, VoiceServerConfig


class _FakeSession:
    """A session whose ``stop`` can optionally block until ``force=True``."""

    def __init__(self, *, hang_until_force: bool = False) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.force_stopped = asyncio.Event()
        self._hang_until_force = hang_until_force
        self._closed = False

    async def start(self) -> None:
        self.started.set()

    async def stop(self, *, force: bool = False) -> None:
        if self._closed:
            return
        if force:
            self.force_stopped.set()
            self.stopped.set()
            self._closed = True
            return
        if self._hang_until_force:
            # Graceful stop hangs; only a forced stop releases it.
            await self.force_stopped.wait()
            self._closed = True
            return
        self.stopped.set()
        self._closed = True


class _GuardedSession:
    """A session-like stub that replicates the real ``Session._stopping`` guard.

    Mirrors ``easycat.session._session.Session.stop`` exactly where it matters:
    once a graceful ``stop`` is in progress, a later ``stop(force=True)`` returns
    immediately as a NO-OP (the ``if self._stopping: return`` early-out). A
    graceful stop here hangs forever — the regression scenario from the review
    where a force-escalation could never preempt an in-progress graceful stop, so
    ``VoiceServer.stop()`` deadlocked. The drain fix must complete ``stop()`` and
    leave the handler task done WITHOUT relying on force preempting an already
    graceful-started stop.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.graceful_started = asyncio.Event()
        self.teardown_completed = asyncio.Event()
        self.force_path_ran = False
        self._stopping = False
        self._closed = False
        self._release = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self, *, force: bool = False) -> None:
        if self._closed or self._stopping:
            # The real guard: a force call after an in-progress graceful stop is
            # a no-op. The force path is therefore never reached here.
            return
        self._stopping = True
        try:
            if force:
                self.force_path_ran = True
            else:
                self.graceful_started.set()
                # Graceful teardown hangs until the drain cancels this coroutine.
                await self._release.wait()
            self._closed = True
            self.teardown_completed.set()
        finally:
            self._stopping = False


class _HangEvenInForceSession:
    """A session whose graceful AND forced stop both hang forever.

    The drain's ``force_shutdown_timeout_s`` bound on the FORCED phase must make
    ``stop()`` return regardless: even a session that resists force-stop cannot
    block teardown past roughly ``force_shutdown_timeout_s``.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.graceful_started = asyncio.Event()
        self.force_started = asyncio.Event()
        self._never = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self, *, force: bool = False) -> None:
        if force:
            self.force_started.set()
        else:
            self.graceful_started.set()
        await self._never.wait()  # hangs in BOTH paths


async def _running_hang_even_in_force_server(
    config: VoiceServerConfig,
) -> tuple[VoiceServer, list[_HangEvenInForceSession]]:
    sessions: list[_HangEvenInForceSession] = []

    def session_factory(_transport: object) -> _HangEvenInForceSession:
        session = _HangEvenInForceSession()
        sessions.append(session)
        return session

    server = VoiceServer(config, session_factory=session_factory)
    await server.start()
    return server, sessions


async def _running_server(
    config: VoiceServerConfig, *, hang: bool = False
) -> tuple[VoiceServer, list[_FakeSession]]:
    sessions: list[_FakeSession] = []

    def session_factory(_transport: object) -> _FakeSession:
        session = _FakeSession(hang_until_force=hang)
        sessions.append(session)
        return session

    server = VoiceServer(config, session_factory=session_factory)
    await server.start()
    return server, sessions


async def _running_guarded_server(
    config: VoiceServerConfig,
) -> tuple[VoiceServer, list[_GuardedSession]]:
    sessions: list[_GuardedSession] = []

    def session_factory(_transport: object) -> _GuardedSession:
        session = _GuardedSession()
        sessions.append(session)
        return session

    server = VoiceServer(config, session_factory=session_factory)
    await server.start()
    return server, sessions


def _ws_url(server: VoiceServer, *, suffix: str = "") -> str:
    address = server.websocket_address
    assert address is not None
    host, port = address
    return f"ws://{host}:{port}{suffix}"


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    async def _loop() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_loop(), timeout=timeout)


# ── graceful drain ───────────────────────────────────────────────────


@pytest.mark.integration_socket
async def test_graceful_stop_drains_active_session_without_force() -> None:
    server, sessions = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=4, drain_timeout_s=2.0)
    )
    async with websockets.connect(_ws_url(server)) as client:
        await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())
        # ``stop`` closes the listener; the client's connection ends, the handler
        # finishes, and the session stops gracefully (NOT force).
        stop_task = asyncio.create_task(server.stop())
        await asyncio.wait_for(client.wait_closed(), timeout=2)
        await asyncio.wait_for(stop_task, timeout=3)
    assert sessions[0].stopped.is_set()
    assert sessions[0].force_stopped.is_set() is False


@pytest.mark.integration_socket
async def test_await_natural_end_drain_leaves_live_websocket_until_client_closes() -> None:
    server, sessions = await _running_server(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            max_sessions=4,
            drain_timeout_s=2.0,
            drain_mode="await_natural_end",
        )
    )
    async with websockets.connect(_ws_url(server)) as client:
        await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())

        stop_task = asyncio.create_task(server.stop())
        await asyncio.sleep(0.1)

        assert stop_task.done() is False
        assert sessions[0].stopped.is_set() is False
        assert client.close_code is None

        await client.close()
        await asyncio.wait_for(stop_task, timeout=3)

    assert sessions[0].stopped.is_set()
    assert sessions[0].force_stopped.is_set() is False


@pytest.mark.integration_socket
async def test_hung_session_is_force_escalated_after_drain_timeout() -> None:
    # A session whose graceful stop hangs must be force-stopped after the (small)
    # drain timeout so teardown cannot block forever.
    server, sessions = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=4, drain_timeout_s=0.2),
        hang=True,
    )
    async with websockets.connect(_ws_url(server)):
        await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())
        await _wait_until(lambda: server._active_sessions == 1)
        # The handler will hang on graceful stop; ``stop`` escalates to force.
        await asyncio.wait_for(server.stop(), timeout=3)
    assert sessions[0].force_stopped.is_set()


@pytest.mark.integration_socket
async def test_await_natural_end_drain_force_escalates_after_timeout() -> None:
    server, sessions = await _running_server(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            max_sessions=4,
            drain_timeout_s=0.1,
            drain_mode="await_natural_end",
        ),
        hang=True,
    )
    async with websockets.connect(_ws_url(server)):
        await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())

        await asyncio.wait_for(server.stop(), timeout=3)

    assert sessions[0].force_stopped.is_set()


@pytest.mark.integration_socket
async def test_hung_guarded_session_does_not_deadlock_stop() -> None:
    # Review regression: with a session that replicates the real ``_stopping``
    # idempotency guard, a graceful stop already in progress turns a later
    # ``stop(force=True)`` into a no-op. The old code let the ``/ws`` handler
    # start that graceful stop, so the drain's force-escalation was a no-op and
    # ``ws_server.wait_closed()`` blocked forever — ``stop()`` deadlocked. The
    # fix makes the drain own the (single) graceful stop and cancel it on
    # timeout, so ``stop()`` completes promptly and the handler task ends.
    server, sessions = await _running_guarded_server(
        VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=4, drain_timeout_s=0.2)
    )
    async with websockets.connect(_ws_url(server)):
        await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())
        await _wait_until(lambda: server._active_sessions == 1)
        # Must NOT hang: bounded well under any unbounded ``wait_closed`` wait.
        await asyncio.wait_for(server.stop(), timeout=5)
    # The drain started the graceful stop and, on timeout, cancelled it before
    # entering the force path after the guard cleared.
    assert sessions[0].graceful_started.is_set()
    assert sessions[0].force_path_ran is True
    assert sessions[0].teardown_completed.is_set()
    assert server._active_sessions == 0
    assert server._ws_handler_task_scope.tasks() == ()


@pytest.mark.integration_socket
async def test_natural_disconnect_near_deadline_remains_force_escalatable() -> None:
    server, sessions = await _running_guarded_server(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            max_sessions=4,
            drain_timeout_s=0.2,
            force_shutdown_timeout_s=1.0,
            drain_mode="await_natural_end",
        )
    )
    async with websockets.connect(_ws_url(server)) as client:
        await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())
        stop_task = asyncio.create_task(server.stop())
        await asyncio.sleep(0.05)

        # Caller hangup starts SessionManager.remove() while the natural-drain
        # deadline is still running. The graceful stop then hangs.
        await client.close()
        await sessions[0].graceful_started.wait()
        await asyncio.wait_for(stop_task, timeout=3)

    # The cancelled remove retained the manager entry, allowing the server's
    # final force sweep to complete teardown after the handler had unwound.
    assert sessions[0].force_path_ran is True
    assert sessions[0].teardown_completed.is_set()
    assert server._manager._sessions == {}
    assert server._active_sessions == 0
    assert server._ws_handler_task_scope.tasks() == ()


@pytest.mark.integration_socket
async def test_force_stop_escalates_immediately() -> None:
    server, sessions = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=4, drain_timeout_s=30.0),
        hang=True,
    )
    async with websockets.connect(_ws_url(server)):
        await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())
        await _wait_until(lambda: server._active_sessions == 1)
        # force=True collapses the 30s drain window to zero.
        await asyncio.wait_for(server.stop(force=True), timeout=2)
    assert sessions[0].force_stopped.is_set()


@pytest.mark.integration_socket
async def test_session_hung_even_in_force_does_not_block_stop() -> None:
    # F4: a session whose force-stop ALSO hangs must not block ``stop()`` past
    # ~force_shutdown_timeout_s. ``force_shutdown_timeout_s`` bounds the forced
    # phase inside the drain. The timed-out session remains fenced for retry
    # because cancellation does not prove teardown completed.
    server, sessions = await _running_hang_even_in_force_server(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            max_sessions=4,
            drain_timeout_s=0.1,
            force_shutdown_timeout_s=0.2,
        )
    )
    async with websockets.connect(_ws_url(server)):
        await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())
        await _wait_until(lambda: server._active_sessions == 1)
        loop = asyncio.get_running_loop()
        start = loop.time()
        # Must fail well under the 4s bound despite the force-stop hanging.
        with pytest.raises(RuntimeError, match="retained 1 session"):
            await asyncio.wait_for(server.stop(), timeout=4)
        elapsed = loop.time() - start
        # The forced phase ran but was bounded; teardown did not block forever
        # and the incomplete session remains behind the drain fence.
        assert sessions[0].graceful_started.is_set()
        assert sessions[0].force_started.is_set()
        assert elapsed < 3.0
        assert server._manager.active_keys()
        assert server._lifecycle_cleanup_error is not None
        assert server._gate.is_draining is True

        sessions[0]._never.set()
        await asyncio.wait_for(server.stop(force=True), timeout=2)

    assert server._manager.active_keys() == ()
    assert server._active_sessions == 0
    assert server._ws_handler_task_scope.tasks() == ()
    assert server._lifecycle_cleanup_error is None


class _HangingWsServer:
    """A raw-ws ``Server`` stub whose ``wait_closed`` never resolves."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.Event().wait()  # never resolves


@pytest.mark.integration_socket
async def test_wait_closed_is_bounded_by_force_shutdown_timeout() -> None:
    # Independent backstop: even if the raw-ws ``Server.wait_closed`` never
    # resolves (a pathological handler that resists cancellation), ``stop()``
    # must return within ``force_shutdown_timeout_s`` rather than block forever.
    # Marked integration_socket: ``start()`` binds the aiohttp TCPSite (loopback).
    server = VoiceServer(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            max_sessions=4,
            drain_timeout_s=0.0,
            force_shutdown_timeout_s=0.2,
            enable_websocket=False,
        ),
        session_factory=lambda _t: _FakeSession(),
    )
    await server.start()
    # Inject a hanging raw-ws server directly (the listener is otherwise off so
    # nothing else binds a port).
    hanging = _HangingWsServer()
    server._ws_server = hanging
    with pytest.raises(RuntimeError, match="cleanup wait cooperatively cancelled"):
        await asyncio.wait_for(server.stop(), timeout=2)
    assert hanging.closed is True
    assert server._ws_server is hanging
    assert "raw-WebSocket listener" not in server._listener_cleanup_tasks
    assert server._listener_cleanup_task_scope.tasks() == ()


# ── draining + capacity ──────────────────────────────────────────────


@pytest.mark.integration_socket
async def test_start_draining_rejects_new_connections() -> None:
    server, sessions = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=4)
    )
    try:
        server._gate.start_draining()
        async with websockets.connect(_ws_url(server)) as client:
            await asyncio.wait_for(client.wait_closed(), timeout=2)
            assert client.close_code == 1013
            assert client.close_reason == "Server is draining"
        assert sessions == []
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_over_capacity_rejection_preserved_after_lift() -> None:
    server, sessions = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, max_sessions=1)
    )
    try:
        async with websockets.connect(_ws_url(server)):
            await _wait_until(lambda: server._active_sessions == 1)
            async with websockets.connect(_ws_url(server)) as extra:
                await asyncio.wait_for(extra.wait_closed(), timeout=2)
                assert extra.close_code == 1013
                assert extra.close_reason == "Server is at the configured session limit"
            assert len(sessions) == 1
            assert server._active_sessions == 1
    finally:
        await server.stop()


# ── unified auth ─────────────────────────────────────────────────────


@pytest.mark.integration_socket
async def test_bearer_auth_rejects_unauthenticated_ws_and_accepts_bearer() -> None:
    config = VoiceServerConfig(
        host="127.0.0.1", port=0, max_sessions=4, auth=BearerTokenAuth(token="sekrit")
    )
    server, sessions = await _running_server(config)
    try:
        # No Authorization header -> rejected (1008 policy violation).
        async with websockets.connect(_ws_url(server)) as anon:
            await asyncio.wait_for(anon.wait_closed(), timeout=2)
            assert anon.close_code == 1008
        assert sessions == []

        # Correct Bearer header -> accepted, a session is created.
        async with websockets.connect(
            _ws_url(server), additional_headers={"Authorization": "Bearer sekrit"}
        ):
            await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())
            await _wait_until(lambda: server._active_sessions == 1)
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_bearer_auth_query_token_rejected_by_default() -> None:
    config = VoiceServerConfig(
        host="127.0.0.1", port=0, max_sessions=4, auth=BearerTokenAuth(token="sekrit")
    )
    server, sessions = await _running_server(config)
    try:
        # Default-OFF: a ``?token=`` query value does not authenticate.
        async with websockets.connect(_ws_url(server, suffix="/?token=sekrit")) as client:
            await asyncio.wait_for(client.wait_closed(), timeout=2)
            assert client.close_code == 1008
        assert sessions == []
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_config_allow_query_token_makes_query_auth_live() -> None:
    # F3: ``config.allow_query_token`` was declared LIVE but never consumed, so
    # a ``?token=`` query was rejected even when the operator opted in. With the
    # config flag threaded onto the policy at start(), the query token now
    # authenticates the handshake (the bundled browser client depends on this).
    config = VoiceServerConfig(
        host="127.0.0.1",
        port=0,
        max_sessions=4,
        auth=BearerTokenAuth(token="sekrit"),
        allow_query_token=True,
    )
    server, sessions = await _running_server(config)
    try:
        async with websockets.connect(_ws_url(server, suffix="/?token=sekrit")):
            await _wait_until(lambda: bool(sessions) and sessions[0].started.is_set())
            await _wait_until(lambda: server._active_sessions == 1)
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_config_allow_query_token_off_keeps_query_auth_rejected() -> None:
    # The default-OFF posture is preserved: leaving ``allow_query_token`` unset
    # keeps a ``?token=`` query rejected even though the policy token is correct.
    config = VoiceServerConfig(
        host="127.0.0.1",
        port=0,
        max_sessions=4,
        auth=BearerTokenAuth(token="sekrit"),
        allow_query_token=False,
    )
    server, sessions = await _running_server(config)
    try:
        async with websockets.connect(_ws_url(server, suffix="/?token=sekrit")) as client:
            await asyncio.wait_for(client.wait_closed(), timeout=2)
            assert client.close_code == 1008
        assert sessions == []
    finally:
        await server.stop()


# ── bind guard at start() ────────────────────────────────────────────


async def test_non_loopback_bind_with_auth_policy_no_token_raises_at_start() -> None:
    # The unified guard now applies to the WS server path: a non-loopback bind
    # with no token and no escape hatch raises before any listener binds.
    server = VoiceServer(
        VoiceServerConfig(host="0.0.0.0", port=0),
        session_factory=lambda _t: _FakeSession(),
    )
    with pytest.raises(ValueError) as exc:
        await server.start()
    assert "0.0.0.0" in str(exc.value)
    assert "unsafe_allow_no_auth" in str(exc.value)


def test_non_loopback_bind_with_unsafe_escape_hatch_passes_guard() -> None:
    # The escape hatch lets a non-loopback bind proceed. Assert the bind guard
    # ``start()`` applies passes for this config WITHOUT opening a public
    # ``0.0.0.0`` listener (which would error in no-socket lanes and trip
    # firewall prompts). The raising side is covered by the start() test above;
    # the actual listener bind is covered by the loopback start tests.
    from easycat.server.auth import enforce_bind_guard

    config = VoiceServerConfig(host="0.0.0.0", port=0, unsafe_allow_no_auth=True)
    # Mirrors VoiceServer.start()'s guard call — must not raise.
    enforce_bind_guard(
        config.host, auth=config.auth, unsafe_allow_no_auth=config.unsafe_allow_no_auth
    )
