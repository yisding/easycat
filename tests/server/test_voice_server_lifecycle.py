"""Lifecycle + boundary tests for the M4 :class:`VoiceServer` skeleton.

These cover: ``start``/``serve``/``stop`` teardown spanning both listener
kinds, the ``run()``-is-sole-``asyncio.run``-owner rule, ``from_app`` lifting
only the config_factory (not ``WebSocketSessionServerConfig`` defaults),
``from_manifest`` raising ``NotImplementedError``, and the M4 no-planner /
no-metric boundary guard.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from easycat.server import VoiceServer, VoiceServerConfig
from easycat.voice_app import VoiceApp


class _FakeSession:
    """Minimal stand-in for an EasyCat ``Session`` (start/stop only)."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self, *, force: bool = False) -> None:
        self.stopped.set()


def _idle_server(**kwargs: object) -> VoiceServer:
    config = VoiceServerConfig(host="127.0.0.1", port=0, **kwargs)  # type: ignore[arg-type]
    return VoiceServer(config, session_factory=lambda _t: _FakeSession())


@pytest.mark.parametrize("close_fails", [False, True])
async def test_active_websocket_close_reports_only_failures_with_task_name(
    caplog: pytest.LogCaptureFixture,
    *,
    close_fails: bool,
) -> None:
    task_names: list[str] = []

    class _Connection:
        async def close(self, *, code: int, reason: str) -> None:
            assert code == 1001
            assert reason == "Server is draining"
            current = asyncio.current_task()
            assert current is not None
            task_names.append(current.get_name())
            if close_fails:
                raise RuntimeError("connection close failed")

    server = _idle_server(
        enable_websocket=False,
        enable_webrtc=False,
        force_shutdown_timeout_s=0.1,
    )
    server._ws_connections[41] = _Connection()

    with caplog.at_level(logging.ERROR, logger="easycat.server.voice_server"):
        await server._close_active_ws_connections()

    expected = "VoiceServer raw-WebSocket close task easycat-raw-ws-close-41 failed"
    messages = [record.getMessage() for record in caplog.records]
    if close_fails:
        assert expected in messages
    else:
        assert not any(
            message.startswith("VoiceServer raw-WebSocket close task ") for message in messages
        )
    assert task_names == ["easycat-raw-ws-close-41"]
    assert server._ws_close_task_scope.tasks() == ()


def test_late_websocket_close_releases_scope_before_cross_loop_reuse() -> None:
    server = _idle_server(
        enable_websocket=False,
        enable_webrtc=False,
        force_shutdown_timeout_s=0.01,
    )

    async def first_close() -> None:
        release = asyncio.Event()

        class _LateConnection:
            async def close(self, *, code: int, reason: str) -> None:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()

        server._ws_connections[1] = _LateConnection()
        await server._close_active_ws_connections()
        late_tasks = server._ws_close_task_scope.tasks()
        assert len(late_tasks) == 1
        release.set()
        await asyncio.gather(*late_tasks)
        for _ in range(20):
            if server._ws_close_task_scope.scope is None:
                break
            await asyncio.sleep(0)
        assert server._ws_close_task_scope.scope is None

    async def second_close() -> None:
        server._ws_connections.clear()
        server._ws_connections[2] = AsyncMock()
        await server._close_active_ws_connections()

    asyncio.run(first_close())
    asyncio.run(second_close())


def test_listener_cleanup_releases_scope_before_cross_loop_reuse() -> None:
    server = _idle_server(enable_websocket=False, enable_webrtc=False)
    cleanup = AsyncMock()

    async def cleanup_once(stage: str) -> None:
        errors: list[Exception] = []
        assert await server._attempt_bounded_listener_cleanup(stage, cleanup, errors)
        assert errors == []

    asyncio.run(cleanup_once("first listener"))
    asyncio.run(cleanup_once("second listener"))

    assert cleanup.await_count == 2


@pytest.mark.parametrize("handler_cleanup_fails", [False, True])
async def test_cancel_websocket_handlers_reports_only_cleanup_failures(
    caplog: pytest.LogCaptureFixture,
    *,
    handler_cleanup_fails: bool,
) -> None:
    started = asyncio.Event()

    async def handler() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            if handler_cleanup_fails:
                raise RuntimeError("handler cleanup failed") from exc
            raise

    server = _idle_server(enable_websocket=False, enable_webrtc=False)
    task = asyncio.create_task(handler(), name="easycat-raw-ws-handler-41")
    server._ws_handler_task_scope.adopt_task(task)
    await started.wait()

    with caplog.at_level(logging.ERROR, logger="easycat.server.voice_server"):
        await server._cancel_ws_handler_tasks()

    expected = "VoiceServer raw-WebSocket handler task easycat-raw-ws-handler-41 failed"
    messages = [record.getMessage() for record in caplog.records]
    if handler_cleanup_fails:
        assert expected in messages
    else:
        assert not any(
            message.startswith("VoiceServer raw-WebSocket handler task ") for message in messages
        )
    assert server._ws_handler_task_scope.tasks() == ()


async def test_cancel_websocket_handlers_reports_failure_after_hard_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def handler() -> None:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
        raise RuntimeError("late handler cleanup failed")

    server = _idle_server(enable_websocket=False, enable_webrtc=False)
    task = asyncio.create_task(handler(), name="easycat-raw-ws-handler-41")
    server._ws_handler_task_scope.adopt_task(task)
    await started.wait()

    with caplog.at_level(logging.ERROR, logger="easycat.server.voice_server"):
        await server._cancel_ws_handler_tasks(timeout_s=0.01)
        await cancellation_seen.wait()
        release.set()
        with pytest.raises(RuntimeError, match="late handler cleanup failed"):
            await task
        await asyncio.sleep(0)
        await asyncio.gather(
            *tuple(server._ws_handler_task_scope._release_tasks),
            return_exceptions=True,
        )

    assert "VoiceServer raw-WebSocket handler task easycat-raw-ws-handler-41 failed" in caplog.text
    assert "late handler cleanup failed" in caplog.text
    assert server._ws_handler_task_scope.tasks() == ()
    assert server._ws_handler_task_scope.scope is None


# ── start/serve/stop ─────────────────────────────────────────────────


@pytest.mark.integration_socket
async def test_start_binds_both_listeners_and_stop_closes_both() -> None:
    server = _idle_server()
    await server.start()
    try:
        assert server.http_address is not None
        assert server.websocket_address is not None
        health = await server.health()
        assert health.route_stack_ready is True
    finally:
        await server.stop()

    # ``stop`` closed both listener kinds and dropped the references.
    assert server._site is None
    assert server._runner is None
    assert server._ws_server is None
    assert server.http_address is None
    assert server.websocket_address is None


@pytest.mark.integration_socket
async def test_start_is_idempotent() -> None:
    server = _idle_server()
    await server.start()
    first_http = server.http_address
    try:
        await server.start()  # second call is a no-op, not a re-bind
        assert server.http_address == first_http
    finally:
        await server.stop()


async def test_start_rolls_back_http_listener_when_websocket_bind_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.server.voice_server as voice_server_module

    app = SimpleNamespace(router=SimpleNamespace(add_get=Mock()))
    runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
    site = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    web = SimpleNamespace(
        Application=Mock(return_value=app),
        AppRunner=Mock(return_value=runner),
        TCPSite=Mock(return_value=site),
    )
    monkeypatch.setattr(
        voice_server_module,
        "require_module",
        lambda *_args, **_kwargs: web,
    )
    monkeypatch.setattr(
        voice_server_module,
        "metrics_middleware",
        lambda _server: object(),
    )

    server = _idle_server(enable_webrtc=False)
    websocket_start = AsyncMock(side_effect=OSError("websocket port busy"))
    monkeypatch.setattr(server, "_start_websocket_listener", websocket_start)

    with pytest.raises(OSError, match="websocket port busy"):
        await server.start()

    runner.setup.assert_awaited_once()
    site.start.assert_awaited_once()
    websocket_start.assert_awaited_once()
    site.stop.assert_awaited_once()
    runner.cleanup.assert_awaited_once()
    assert server._runner is None
    assert server._site is None
    assert server._ws_server is None
    assert server._webrtc_routes is None
    assert server._started is False
    assert server._gate.is_draining is False

    # A caller may still unconditionally stop in its own finally block.
    await server.stop(force=True)
    site.stop.assert_awaited_once()
    runner.cleanup.assert_awaited_once()


async def test_start_preserves_bind_failure_when_listener_rollback_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.server.voice_server as voice_server_module

    app = SimpleNamespace(router=SimpleNamespace(add_get=Mock()))
    runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
    rollback_error = RuntimeError("site rollback failed")
    site = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(side_effect=rollback_error))
    web = SimpleNamespace(
        Application=Mock(return_value=app),
        AppRunner=Mock(return_value=runner),
        TCPSite=Mock(return_value=site),
    )
    monkeypatch.setattr(voice_server_module, "require_module", lambda *_a, **_kw: web)
    monkeypatch.setattr(voice_server_module, "metrics_middleware", lambda _server: object())

    server = _idle_server(enable_webrtc=False)
    monkeypatch.setattr(
        server,
        "_start_websocket_listener",
        AsyncMock(side_effect=OSError("websocket port busy")),
    )

    with pytest.raises(OSError, match="websocket port busy") as exc_info:
        await server.start()

    assert exc_info.value.__cause__ is rollback_error
    site.stop.assert_awaited_once()
    runner.cleanup.assert_awaited_once()
    assert server._site is site
    assert server._runner is None
    assert server._started is False
    assert server._lifecycle_cleanup_error is rollback_error


async def test_start_retains_runner_when_setup_and_runner_rollback_both_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.server.voice_server as voice_server_module

    app = SimpleNamespace(router=SimpleNamespace(add_get=Mock()))
    rollback_error = RuntimeError("runner rollback failed")
    runner = SimpleNamespace(
        setup=AsyncMock(side_effect=OSError("runner setup failed")),
        cleanup=AsyncMock(side_effect=rollback_error),
    )
    web = SimpleNamespace(
        Application=Mock(return_value=app),
        AppRunner=Mock(return_value=runner),
        TCPSite=Mock(),
    )
    monkeypatch.setattr(voice_server_module, "require_module", lambda *_a, **_kw: web)
    monkeypatch.setattr(voice_server_module, "metrics_middleware", lambda _server: object())
    server = _idle_server(enable_websocket=False, enable_webrtc=False)

    with pytest.raises(OSError, match="runner setup failed") as exc_info:
        await server.start()

    assert exc_info.value.__cause__ is rollback_error
    assert server._runner is runner
    assert server._site is None
    assert server._lifecycle_cleanup_error is rollback_error
    with pytest.raises(RuntimeError, match="previous teardown cleanup is incomplete"):
        await server.start()

    runner.cleanup = AsyncMock()
    await server.stop(force=True)
    runner.cleanup.assert_awaited_once()
    assert server._runner is None
    assert server._lifecycle_cleanup_error is None


async def test_start_bounds_hung_http_rollback_and_retains_retry_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.server.voice_server as voice_server_module

    release_cleanup = asyncio.Event()

    async def block_runner_cleanup() -> None:
        await release_cleanup.wait()

    app = SimpleNamespace(router=SimpleNamespace(add_get=Mock()))
    runner = SimpleNamespace(
        setup=AsyncMock(side_effect=OSError("runner setup failed")),
        cleanup=AsyncMock(side_effect=block_runner_cleanup),
    )
    web = SimpleNamespace(
        Application=Mock(return_value=app),
        AppRunner=Mock(return_value=runner),
        TCPSite=Mock(),
    )
    monkeypatch.setattr(voice_server_module, "require_module", lambda *_a, **_kw: web)
    monkeypatch.setattr(voice_server_module, "metrics_middleware", lambda _server: object())
    server = _idle_server(
        enable_websocket=False,
        enable_webrtc=False,
        force_shutdown_timeout_s=0.01,
    )

    with pytest.raises(OSError, match="runner setup failed") as exc_info:
        await asyncio.wait_for(server.start(), timeout=0.5)

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert server._runner is runner
    assert server._site is None
    assert server._gate.is_draining is True
    assert isinstance(server._lifecycle_cleanup_error, RuntimeError)

    release_cleanup.set()
    await asyncio.wait_for(server.stop(force=True), timeout=0.5)

    assert runner.cleanup.await_count == 1
    assert server._runner is None
    assert server._gate.is_draining is False
    assert server._lifecycle_cleanup_error is None


async def test_cancelled_internal_startup_rollback_records_retry_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.server.voice_server as voice_server_module

    rollback_started = asyncio.Event()

    async def block_site_stop() -> None:
        rollback_started.set()
        await asyncio.Event().wait()

    app = SimpleNamespace(router=SimpleNamespace(add_get=Mock()))
    runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
    site = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(side_effect=block_site_stop),
    )
    web = SimpleNamespace(
        Application=Mock(return_value=app),
        AppRunner=Mock(return_value=runner),
        TCPSite=Mock(return_value=site),
    )
    monkeypatch.setattr(voice_server_module, "require_module", lambda *_a, **_kw: web)
    monkeypatch.setattr(voice_server_module, "metrics_middleware", lambda _server: object())
    server = _idle_server(enable_webrtc=False)
    monkeypatch.setattr(
        server,
        "_start_websocket_listener",
        AsyncMock(side_effect=OSError("websocket port busy")),
    )

    starting = asyncio.create_task(server.start())
    await rollback_started.wait()
    starting.cancel()
    with pytest.raises(OSError, match="websocket port busy") as exc_info:
        await starting

    assert isinstance(exc_info.value.__cause__, asyncio.CancelledError)
    assert server._site is site
    assert server._runner is runner
    assert server._started is False
    assert server._gate.is_draining is True
    assert isinstance(server._lifecycle_cleanup_error, RuntimeError)

    site.stop = AsyncMock()
    await server.stop(force=True)
    assert server._site is None
    assert server._runner is None
    assert server._lifecycle_cleanup_error is None


async def test_concurrent_starts_publish_one_listener_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.server.voice_server as voice_server_module

    setup_started = asyncio.Event()
    release_setup = asyncio.Event()
    app = SimpleNamespace(router=Mock())
    runner = SimpleNamespace(cleanup=AsyncMock())

    async def setup() -> None:
        setup_started.set()
        await release_setup.wait()

    runner.setup = AsyncMock(side_effect=setup)
    site = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    web = SimpleNamespace(
        Application=Mock(return_value=app),
        AppRunner=Mock(return_value=runner),
        TCPSite=Mock(return_value=site),
    )
    monkeypatch.setattr(voice_server_module, "require_module", lambda *_a, **_kw: web)
    monkeypatch.setattr(voice_server_module, "metrics_middleware", lambda _server: object())

    server = _idle_server(enable_websocket=False, enable_webrtc=False)
    first = asyncio.create_task(server.start())
    await setup_started.wait()
    second = asyncio.create_task(server.start())
    await asyncio.sleep(0)

    web.AppRunner.assert_called_once()
    release_setup.set()
    await asyncio.gather(first, second)
    await server.stop(force=True)

    web.AppRunner.assert_called_once()
    web.TCPSite.assert_called_once_with(runner, "127.0.0.1", 0)
    runner.cleanup.assert_awaited_once()
    site.stop.assert_awaited_once()


async def test_stop_waits_for_in_progress_start_then_cleans_same_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.server.voice_server as voice_server_module

    setup_started = asyncio.Event()
    release_setup = asyncio.Event()
    app = SimpleNamespace(router=Mock())
    runner = SimpleNamespace(cleanup=AsyncMock())

    async def setup() -> None:
        setup_started.set()
        await release_setup.wait()

    runner.setup = AsyncMock(side_effect=setup)
    site = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    web = SimpleNamespace(
        Application=Mock(return_value=app),
        AppRunner=Mock(return_value=runner),
        TCPSite=Mock(return_value=site),
    )
    monkeypatch.setattr(voice_server_module, "require_module", lambda *_a, **_kw: web)
    monkeypatch.setattr(voice_server_module, "metrics_middleware", lambda _server: object())

    server = _idle_server(enable_websocket=False, enable_webrtc=False)
    starting = asyncio.create_task(server.start())
    await setup_started.wait()
    stopping = asyncio.create_task(server.stop(force=True))
    await asyncio.sleep(0)

    assert not stopping.done()
    release_setup.set()
    await asyncio.gather(starting, stopping)

    web.AppRunner.assert_called_once()
    web.TCPSite.assert_called_once()
    runner.cleanup.assert_awaited_once()
    site.stop.assert_awaited_once()
    assert server._runner is None
    assert server._site is None
    assert server._started is False


async def test_cleanup_failure_finishes_stop_and_rejects_queued_start_until_retry() -> None:
    server = _idle_server(enable_websocket=False, enable_webrtc=False)
    site_stop_started = asyncio.Event()
    release_site_stop = asyncio.Event()

    async def fail_site_stop() -> None:
        site_stop_started.set()
        await release_site_stop.wait()
        raise RuntimeError("site cleanup failed")

    site = SimpleNamespace(stop=AsyncMock(side_effect=fail_site_stop))
    runner = SimpleNamespace(cleanup=AsyncMock())
    server._site = site
    server._runner = runner
    server._started = True

    stopping = asyncio.create_task(server.stop(force=True))
    await site_stop_started.wait()
    queued_start = asyncio.create_task(server.start())
    await asyncio.sleep(0)
    assert not queued_start.done()

    release_site_stop.set()
    with pytest.raises(RuntimeError, match="site cleanup failed"):
        await stopping
    with pytest.raises(RuntimeError, match="previous teardown cleanup is incomplete"):
        await queued_start

    # Teardown continues after the failed site stage, publishes truthful
    # lifecycle flags, and keeps only the failed resource reference for retry.
    runner.cleanup.assert_awaited_once()
    assert server._runner is None
    assert server._site is site
    assert server._started is False
    assert server._gate.is_draining is True

    site.stop = AsyncMock()
    await server.stop(force=True)
    site.stop.assert_awaited_once()
    assert server._site is None
    assert server._lifecycle_cleanup_error is None
    assert server._gate.is_draining is False


async def test_listener_cleanup_timeout_keeps_drain_fence_and_drains_sessions() -> None:
    """A stuck listener stop must not strand sessions or reopen admission."""
    server = _idle_server(
        enable_websocket=False,
        enable_webrtc=False,
        force_shutdown_timeout_s=0.01,
    )
    stop_started = asyncio.Event()
    release_site_stop = asyncio.Event()

    async def stop_site() -> None:
        stop_started.set()
        await release_site_stop.wait()

    site = SimpleNamespace(stop=AsyncMock(side_effect=stop_site))
    runner = SimpleNamespace(cleanup=AsyncMock())
    session = _FakeSession()
    key = 23
    server._site = site
    server._runner = runner
    server._started = True
    server._manager._sessions[key] = session
    server._active_session_objs[key] = session
    assert server._gate.try_acquire()
    server._gate.track(key)

    with pytest.raises(RuntimeError, match="HTTP site stop did not finish"):
        await asyncio.wait_for(server.stop(force=True), timeout=0.5)

    assert stop_started.is_set()
    listener_task = server._listener_cleanup_tasks["HTTP site stop"]
    assert listener_task.get_name() == "easycat-voice-server-listener-cleanup-http-site-stop"
    assert server._listener_cleanup_task_scope.tasks() == (listener_task,)
    assert session.stopped.is_set()
    # AppRunner.cleanup() stops its registered sites. Do not invoke it while
    # the retained site.stop() call is still pending, or aiohttp receives two
    # concurrent stop operations for the same site.
    runner.cleanup.assert_not_awaited()
    assert server._runner is runner
    assert server._site is site
    assert server._gate.is_draining is True
    assert server._gate.try_acquire() is False
    assert isinstance(server._lifecycle_cleanup_error, RuntimeError)

    # The retry observes the already-running site cleanup rather than starting
    # a second concurrent stop call. Once it finishes, normal reuse is safe.
    release_site_stop.set()
    await asyncio.wait_for(server.stop(force=True), timeout=0.5)

    assert site.stop.await_count == 1
    runner.cleanup.assert_awaited_once()
    assert server._site is None
    assert server._runner is None
    assert server._gate.is_draining is False
    assert server._lifecycle_cleanup_error is None
    assert server._listener_cleanup_task_scope.tasks() == ()


async def test_raw_websocket_listener_timeout_retries_same_owned_waiter() -> None:
    server = _idle_server(
        enable_websocket=False,
        enable_webrtc=False,
        force_shutdown_timeout_s=0.01,
    )
    cancel_seen = asyncio.Event()
    release_wait = asyncio.Event()

    class _CancellationResistantListener:
        def __init__(self) -> None:
            self.wait_calls = 0

        def close(self, close_connections: bool = True) -> None:
            pass

        async def wait_closed(self) -> None:
            self.wait_calls += 1
            while not release_wait.is_set():
                try:
                    await release_wait.wait()
                except asyncio.CancelledError:
                    cancel_seen.set()

    listener = _CancellationResistantListener()
    server._ws_server = listener
    server._started = True

    with pytest.raises(RuntimeError, match="raw-WebSocket listener did not close"):
        await server.stop(force=True)

    await asyncio.wait_for(cancel_seen.wait(), timeout=1)
    waiter = server._listener_cleanup_tasks["raw-WebSocket listener"]
    assert waiter.get_name() == ("easycat-voice-server-listener-cleanup-raw-websocket-listener")
    assert server._listener_cleanup_task_scope.tasks() == (waiter,)

    with pytest.raises(RuntimeError, match="raw-WebSocket listener did not close"):
        await server.stop(force=True)
    assert listener.wait_calls == 1

    release_wait.set()
    await server.stop(force=True)

    assert listener.wait_calls == 1
    assert server._ws_server is None
    assert "raw-WebSocket listener" not in server._listener_cleanup_tasks
    assert server._listener_cleanup_task_scope.tasks() == ()


async def test_failed_session_hard_sweep_blocks_restart_and_retains_retry_ownership(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSession(_FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.fail_stop = True
            self.stop_calls: list[bool] = []

        async def stop(self, *, force: bool = False) -> None:
            self.stop_calls.append(force)
            if self.fail_stop:
                raise RuntimeError("session teardown failed")
            await super().stop(force=force)

    server = _idle_server(enable_websocket=False, enable_webrtc=False)
    session = FailingSession()
    key = 17
    server._started = True
    server._manager._sessions[key] = session
    server._active_session_objs[key] = session
    assert server._gate.try_acquire()
    server._gate.track(key)
    sweep_task_names: list[str] = []
    stop_all = server._manager.stop_all

    async def named_stop_all(*, force: bool = False) -> object:
        current = asyncio.current_task()
        assert current is not None
        sweep_task_names.append(current.get_name())
        return await stop_all(force=force)

    monkeypatch.setattr(server._manager, "stop_all", named_stop_all)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="retained 1 session"):
        await server.stop(force=True)

    assert "VoiceServer hard sweep failed to stop 1 of 1 session" in caplog.text
    assert "session teardown failed" in caplog.text
    assert server._manager.get(key) is session
    assert server._active_session_objs == {key: session}
    assert sweep_task_names == ["easycat-voice-server-session-sweep"]
    assert server._session_sweep_task_scope.tasks() == ()
    assert isinstance(server._lifecycle_cleanup_error, RuntimeError)
    with pytest.raises(RuntimeError, match="previous teardown cleanup is incomplete"):
        await server.start()

    session.fail_stop = False
    await server.stop(force=True)

    assert session.stopped.is_set()
    assert server._manager.get(key) is None
    assert server._active_session_objs == {}
    assert server._lifecycle_cleanup_error is None


async def test_hard_sweep_timeout_keeps_named_task_owned_until_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _idle_server(
        enable_websocket=False,
        enable_webrtc=False,
        force_shutdown_timeout_s=0.01,
    )
    server._started = True
    sweep_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_sweep = asyncio.Event()

    async def stop_all(*, force: bool = False) -> object:
        assert force is True
        sweep_started.set()
        while not release_sweep.is_set():
            try:
                await release_sweep.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
        return object()

    monkeypatch.setattr(server._manager, "stop_all", stop_all)

    with pytest.raises(RuntimeError, match="SessionManager.stop_all did not finish"):
        await asyncio.wait_for(server.stop(force=True), timeout=0.5)

    assert sweep_started.is_set()
    assert cancellation_seen.is_set()
    tasks = server._session_sweep_task_scope.tasks()
    assert len(tasks) == 1
    sweep_task = tasks[0]
    assert sweep_task.get_name() == "easycat-voice-server-session-sweep"

    release_sweep.set()
    await asyncio.wait_for(sweep_task, timeout=0.5)
    await server._session_sweep_task_scope.release_standalone_if_empty()
    assert server._session_sweep_task_scope.tasks() == ()


async def test_cancelled_stop_publishes_retryable_stopped_state_before_reraising() -> None:
    server = _idle_server(enable_websocket=False, enable_webrtc=False)
    site_stop_started = asyncio.Event()
    release_site_stop = asyncio.Event()

    async def stop_site() -> None:
        site_stop_started.set()
        await release_site_stop.wait()

    site = SimpleNamespace(stop=AsyncMock(side_effect=stop_site))
    runner = SimpleNamespace(cleanup=AsyncMock())
    server._site = site
    server._runner = runner
    server._started = True

    stopping = asyncio.create_task(server.stop(force=True))
    await site_stop_started.wait()
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert server._started is False
    assert server._gate.is_draining is True
    assert server._site is site
    assert server._runner is runner
    assert isinstance(server._lifecycle_cleanup_error, RuntimeError)
    with pytest.raises(RuntimeError, match="previous teardown cleanup is incomplete"):
        await server.start()

    site.stop = AsyncMock()
    await server.stop(force=True)
    site.stop.assert_awaited_once()
    runner.cleanup.assert_awaited_once()
    assert server._site is None
    assert server._runner is None
    assert server._lifecycle_cleanup_error is None


@pytest.mark.integration_socket
async def test_stop_then_start_resets_draining_and_recovers_readiness() -> None:
    # Regression: ``stop`` drains by flipping the shared gate's draining flag.
    # Reusing the same instance via ``start`` must reset it, or the restarted
    # server binds its listeners but rejects every new connection as "draining"
    # and never reports ready.
    server = _idle_server()
    await server.start()
    await server.stop()
    assert server._gate.is_draining is False  # cleared by the drain reset

    await server.start()  # reuse the same instance
    try:
        assert server._gate.is_draining is False
        health = await server.health()
        assert health.draining is False
        assert health.route_stack_ready is True
        assert health.is_ready() is True
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_serve_runs_until_stop_event_and_does_not_call_asyncio_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``serve`` must be the async verb and never own the loop. We are already
    # inside a pytest-asyncio loop here; assert ``asyncio.run`` is never called.
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("serve() must not call asyncio.run")

    monkeypatch.setattr(asyncio, "run", _boom)

    stop_event = asyncio.Event()
    server = _idle_server()
    task = asyncio.create_task(server.serve(stop_event))
    # Let it start.
    await asyncio.sleep(0)
    for _ in range(100):
        if server.http_address is not None:
            break
        await asyncio.sleep(0.01)
    assert server.http_address is not None

    stop_event.set()
    await asyncio.wait_for(task, timeout=2)
    assert server._site is None  # stop() ran in the finally


def test_run_is_the_sole_asyncio_run_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``run()`` is the only method that calls ``asyncio.run``. Patch it to
    # observe the single call and avoid actually spinning a loop.
    calls: list[object] = []

    def _fake_run(coro: object) -> None:
        calls.append(coro)
        coro.close()  # type: ignore[union-attr]

    monkeypatch.setattr(asyncio, "run", _fake_run)
    server = _idle_server()
    server.run()
    assert len(calls) == 1


# ── from_app / from_manifest ─────────────────────────────────────────


def test_from_app_applies_server_policy_not_websocket_defaults() -> None:
    # A mounted VoiceApp contributes ONLY its config_factory. VoiceServerConfig
    # owns process policy: max_sessions=64 / port=8080 (NOT the
    # WebSocketSessionServerConfig 10/8765 defaults).
    # A per-connection ``config_factory`` is the valid mounted-app input: a live
    # ``agent=object()`` is rejected by VoiceApp's per-connection guard (a built
    # collaborator cannot be reused across connections).
    app = VoiceApp(config_factory=lambda _transport: object())
    server = VoiceServer.from_app(app)
    assert server.config.max_sessions == 64
    assert server.config.port == 8080
    assert server._session_factory is not None


def test_from_app_lifts_the_apps_config_factory() -> None:
    sentinel_transport = object()
    seen: list[object] = []

    def factory(transport: object) -> _FakeSession:
        seen.append(transport)
        return _FakeSession()

    app = VoiceApp(config_factory=factory)
    server = VoiceServer.from_app(app)

    # The lifted session_factory routes through the app's config_factory. The
    # app's websocket factory returns the config_factory verbatim, and
    # ``create_session`` is only reached for an EasyConfig — here the factory
    # returns a Session-like object, so it is returned as-is.
    result = server._build_session(sentinel_transport)
    assert isinstance(result, _FakeSession)
    assert seen == [sentinel_transport]


def test_from_app_honors_explicit_config() -> None:
    # A per-connection ``config_factory`` is the valid mounted-app input: a live
    # ``agent=object()`` is rejected by VoiceApp's per-connection guard (a built
    # collaborator cannot be reused across connections).
    app = VoiceApp(config_factory=lambda _transport: object())
    config = VoiceServerConfig(max_sessions=3, port=0)
    server = VoiceServer.from_app(app, config)
    assert server.config is config
    assert server.config.max_sessions == 3


def test_from_manifest_missing_file_raises_coded_error() -> None:
    # M6a implements ``from_manifest``; a missing file surfaces the coded
    # discovery error (EASYCAT_E601), not ``NotImplementedError``.
    from easycat.errors import EasyCatError

    with pytest.raises(EasyCatError) as exc_info:
        VoiceServer.from_manifest("/nonexistent/easycat.toml")
    assert exc_info.value.code == "EASYCAT_E601"


def test_from_manifest_builds_server_with_server_policy(tmp_path: object) -> None:
    # ``from_manifest`` maps ``[server]`` to VoiceServerConfig process policy and
    # builds a per-connection session_factory from the selected profile.
    from pathlib import Path

    manifest = Path(str(tmp_path)) / "easycat.toml"
    manifest.write_text(
        "[server]\n"
        'host = "127.0.0.1"\n'
        "port = 9099\n"
        "max_sessions = 7\n"
        "\n"
        "[voice.default]\n"
        'transport = "websocket"\n',
        encoding="utf-8",
    )
    server = VoiceServer.from_manifest(manifest)
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 9099
    assert server.config.max_sessions == 7
    assert server.config.auth is None  # no [server] auth configured
    assert server._session_factory is not None


def test_from_manifest_factory_binds_accepted_transport(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The per-connection factory must drive the already-negotiated connection
    # transport, NOT the standalone transport the manifest shortcut would build
    # (otherwise each accepted client opens a second listener/peer).
    from pathlib import Path

    # A bare websocket profile (no stt/tts) needs a key to construct EasyConfig.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    manifest = Path(str(tmp_path)) / "easycat.toml"
    manifest.write_text(
        '[server]\nhost = "127.0.0.1"\nport = 9099\n\n[voice.default]\ntransport = "websocket"\n',
        encoding="utf-8",
    )
    server = VoiceServer.from_manifest(manifest)
    assert server._session_factory is not None

    sentinel_transport = object()
    config = server._session_factory(sentinel_transport)
    assert config.transport is sentinel_transport


# ── M4 boundary guards ───────────────────────────────────────────────


def test_importing_server_does_not_import_planner_or_project() -> None:
    # Assert the boundary: importing ``easycat.server`` triggers no planner
    # (M6b) or project (M6a) import — ``from_manifest`` pulls ``easycat.project``
    # lazily, only when called. Run in a FRESH subprocess so the check observes a
    # true module-load (not leftover ``sys.modules`` state from sibling tests)
    # AND does not mutate this process's module identity (re-importing
    # ``easycat.server.*`` in-process would split class identity for later tests).
    import subprocess

    code = (
        "import sys; import easycat.server; "
        "leaked = sorted(n for n in sys.modules "
        "if n.startswith('easycat.planning') or n.startswith('easycat.project')); "
        "print(leaked)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout


def test_observability_registers_the_five_server_metric_names() -> None:
    # M8 crosses the M4 boundary: ``easycat._observability`` now registers exactly
    # the five ``easycat.server.*`` names (with their expected kinds) and the three
    # new low-cardinality labels. Registration lives at module level in
    # ``_observability.py`` (imported regardless of ``import easycat.server``), so
    # the names/labels are present after import.
    import easycat._observability as observability
    import easycat.server  # noqa: F401

    server_metric_names = {
        name: kind
        for name, kind in observability.METRIC_DEFINITIONS.items()
        if name.startswith("easycat.server.")
    }
    assert server_metric_names == {
        "easycat.server.requests.total": "counter",
        "easycat.server.request.duration": "histogram",
        "easycat.server.sessions.rejected.total": "counter",
        "easycat.server.connections.active": "observable_gauge",
        "easycat.server.draining": "observable_gauge",
    }
    server_labels = {
        key
        for key in observability.LOW_CARDINALITY_ATTRIBUTE_KEYS
        if key in {"easycat.server_state", "easycat.auth_result", "easycat.route"}
    }
    assert server_labels == {"easycat.server_state", "easycat.auth_result", "easycat.route"}
