"""Lifecycle + boundary tests for the M4 :class:`VoiceServer` skeleton.

These cover: ``start``/``serve``/``stop`` teardown spanning both listener
kinds, the ``run()``-is-sole-``asyncio.run``-owner rule, ``from_app`` lifting
only the config_factory (not ``WebSocketSessionServerConfig`` defaults),
``from_manifest`` raising ``NotImplementedError``, and the M4 no-planner /
no-metric boundary guard.
"""

from __future__ import annotations

import asyncio
import sys

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
    app = VoiceApp(agent=object())
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
    app = VoiceApp(agent=object())
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
