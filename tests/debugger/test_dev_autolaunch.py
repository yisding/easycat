"""Neo M13 — dev debugger mode (Workstream A: always-available dev timeline).

Covers the dev opt-in (``EASYCAT_DEV`` / ``VoiceApp(dev=True)``), the
process-local session registry, the registry-backed debugger API additions
(``/api/dev/sessions``, ``/api/dev/select``), and — most importantly — the R7
PURELY-ADDITIVE invariant: the dev opt-in adds a fresh launch trigger but must
NOT weaken the existing ``debug="full"``-alone-never-autolaunches guarantee.
"""

from __future__ import annotations

import pytest


class _FakeSession:
    """Minimal duck-typed session for registry/hook tests (no aiohttp)."""

    def __init__(self, session_id: str = "sess-1", *, is_running: bool = True) -> None:
        self.session_id = session_id
        self.is_running = is_running
        self.turn_state = "idle"
        self.journal = None


@pytest.fixture(autouse=True)
def _isolate_registry_and_latch():
    """Each test gets a clean process registry and a reset launch latch."""
    from easycat.debugger import dev as dev_mod
    from easycat.debugger.session_registry import get_registry

    get_registry().clear()
    dev_mod.reset_launch_state()
    yield
    get_registry().clear()
    dev_mod.reset_launch_state()


# ── Session registry ─────────────────────────────────────────────


def test_register_and_list_sessions_round_trips():
    from easycat.debugger.session_registry import (
        list_sessions,
        register_session,
        unregister_session,
    )

    a = _FakeSession("sess-a")
    b = _FakeSession("sess-b")
    id_a = register_session(a, label="alpha")
    id_b = register_session(b)

    summaries = {s.registry_id: s for s in list_sessions()}
    assert set(summaries) == {id_a, id_b}
    assert summaries[id_a].label == "alpha"
    assert summaries[id_b].session_id == "sess-b"
    assert summaries[id_a].is_running is True

    unregister_session(id_a)
    assert [s.registry_id for s in list_sessions()] == [id_b]


def test_register_same_session_is_idempotent():
    from easycat.debugger.session_registry import list_sessions, register_session

    a = _FakeSession("sess-a")
    first = register_session(a)
    second = register_session(a)
    assert first == second
    assert len(list_sessions()) == 1


# ── Dev opt-in invokes the launch hook exactly once ──────────────


def _force_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake an interactive, non-CI terminal so the dev guards pass."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)


def test_dev_opt_in_invokes_launch_hook_once_per_process(monkeypatch: pytest.MonkeyPatch):
    """``dev=True`` registers sessions and fires the launch hook exactly once.

    CI is non-interactive, so we assert the launch HOOK is invoked (mocked),
    not that a browser literally opens.
    """
    from easycat.debugger import dev as dev_mod
    from easycat.debugger.session_registry import list_sessions

    _force_interactive(monkeypatch)
    launches: list[int] = []
    monkeypatch.setattr(dev_mod, "_launch_dev_ui", lambda *, port: launches.append(port))

    s1 = _FakeSession("s1")
    s2 = _FakeSession("s2")
    dev_mod.maybe_launch_dev_debugger(s1, dev=True)
    dev_mod.maybe_launch_dev_debugger(s2, dev=True)

    # Both sessions registered, but the UI launch hook fired exactly once.
    assert len(launches) == 1
    assert {s.session_id for s in list_sessions()} == {"s1", "s2"}


def test_dev_opt_in_via_env_var(monkeypatch: pytest.MonkeyPatch):
    from easycat.debugger import dev as dev_mod

    _force_interactive(monkeypatch)
    monkeypatch.setenv("EASYCAT_DEV", "1")
    launches: list[int] = []
    monkeypatch.setattr(dev_mod, "_launch_dev_ui", lambda *, port: launches.append(port))

    dev_mod.maybe_launch_dev_debugger(_FakeSession(), dev=False)
    assert len(launches) == 1


def test_dev_registry_ui_launch_hook_fires_once(monkeypatch: pytest.MonkeyPatch):
    """The server-mode eager launch (no session) also fires the hook once."""
    from easycat.debugger import dev as dev_mod

    _force_interactive(monkeypatch)
    launches: list[int] = []
    monkeypatch.setattr(dev_mod, "_launch_dev_ui", lambda *, port: launches.append(port))

    assert dev_mod.maybe_launch_dev_registry_ui(dev=True) is True
    assert dev_mod.maybe_launch_dev_registry_ui(dev=True) is False
    assert len(launches) == 1


# ── R7: purely additive — debug="full" alone never launches ──────


def test_debug_full_alone_does_not_invoke_dev_launch(monkeypatch: pytest.MonkeyPatch):
    """R7 regression: ``debug="full"`` with NO dev opt-in does NOT launch.

    The dev opt-in is purely additive over the ``_autolaunch.py`` guard. With
    neither ``EASYCAT_DEV`` nor ``dev=True``, the dev launch hook must never
    fire — even in an interactive terminal — while the dev opt-in DOES fire it.
    """
    from easycat.debugger import dev as dev_mod

    _force_interactive(monkeypatch)
    monkeypatch.delenv("EASYCAT_DEV", raising=False)
    launches: list[int] = []
    monkeypatch.setattr(dev_mod, "_launch_dev_ui", lambda *, port: launches.append(port))

    # debug="full" is modeled by the absence of any dev opt-in: no env, dev=False.
    result = dev_mod.maybe_launch_dev_debugger(_FakeSession(), dev=False)
    assert result is None
    assert launches == []

    # The dev opt-in, by contrast, DOES invoke the launch hook.
    dev_mod.maybe_launch_dev_debugger(_FakeSession(), dev=True)
    assert len(launches) == 1


def test_autolaunch_guard_still_blocks_debug_full(monkeypatch: pytest.MonkeyPatch):
    """The existing ``_autolaunch.py`` guarantee is unchanged by dev mode.

    Mirrors ``tests/test_dx_helpers.py``: ``maybe_launch_debugger_ui`` with no
    ``EASYCAT_DEBUGGER_AUTOLAUNCH`` opt-in still no-ops, proving the dev path
    did not relax the original guard.
    """
    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[object] = []
    monkeypatch.setattr(
        "easycat.debugger.serve_session",
        lambda session, **kw: calls.append(session),
        raising=False,
    )
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_AUTOLAUNCH", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)

    maybe_launch_debugger_ui(session=object())
    assert calls == []


def test_dev_no_op_under_ci(monkeypatch: pytest.MonkeyPatch):
    from easycat.debugger import dev as dev_mod

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
    launches: list[int] = []
    monkeypatch.setattr(dev_mod, "_launch_dev_ui", lambda *, port: launches.append(port))

    assert dev_mod.maybe_launch_dev_debugger(_FakeSession(), dev=True) is None
    assert launches == []


# ── VoiceApp wiring ──────────────────────────────────────────────


def test_voice_app_dev_defaults_to_durable_debugging():
    """``VoiceApp(dev=True)`` defaults the forwarded preset to debug='full'."""
    from easycat.voice_app import VoiceApp

    app = VoiceApp(agent="echo", dev=True)
    assert app._forwardable_config_kwargs().get("debug") == "full"

    # An explicit debug wins over the dev default.
    app2 = VoiceApp(agent="echo", debug="light", dev=True)
    assert app2._forwardable_config_kwargs().get("debug") == "light"

    # Without dev, no debug default is injected.
    app3 = VoiceApp(agent="echo")
    assert "debug" not in app3._forwardable_config_kwargs()


def test_voice_app_build_local_session_arms_dev_hook(monkeypatch: pytest.MonkeyPatch):
    """Local-mode session construction fires the dev hook with the app's flag."""
    from easycat.voice_app import VoiceApp

    captured: list[bool] = []
    monkeypatch.setattr(
        "easycat.debugger.dev.maybe_launch_dev_debugger",
        lambda session, *, dev: captured.append(dev),
    )
    fake = _FakeSession()
    monkeypatch.setattr("easycat.config.create_session", lambda config: fake)
    monkeypatch.setattr(VoiceApp, "_local_config", lambda self, **kw: object())

    VoiceApp(agent="echo", dev=True)._build_local_session()
    assert captured == [True]


def test_serve_cli_dev_mode_flag(monkeypatch: pytest.MonkeyPatch):
    from easycat.cli import serve as serve_mod

    monkeypatch.setenv("EASYCAT_DEV", "1")
    assert serve_mod._dev_mode_enabled() is True
    monkeypatch.setenv("EASYCAT_DEV", "0")
    assert serve_mod._dev_mode_enabled() is False
    monkeypatch.delenv("EASYCAT_DEV", raising=False)
    assert serve_mod._dev_mode_enabled() is False


# ── Debugger API: dev routes ─────────────────────────────────────

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from ._server_helpers import _SAFE_HEADERS  # noqa: E402


def _dev_app(registry):
    from easycat.debugger.server import _empty_dev_source, _make_app

    return _make_app(_empty_dev_source(), registry=registry)


async def test_dev_sessions_route_lists_and_selects():
    from easycat.debugger.session_registry import SessionRegistry

    registry = SessionRegistry()
    live = _FakeSession("live-1")  # held: the registry only weakly references it
    sid = registry.register(live, label="first")
    app = _dev_app(registry)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/dev/sessions")
        assert resp.status == 200
        data = await resp.json()
        assert [s["registry_id"] for s in data["sessions"]] == [sid]
        # Single session auto-selects so the panels show data immediately.
        assert data["active_session"] == sid

        # Selecting an unknown session 404s; selecting the known one works.
        bad = await client.post(
            "/api/dev/select", json={"registry_id": "nope"}, headers=_SAFE_HEADERS
        )
        assert bad.status == 404
        ok = await client.post("/api/dev/select", json={"registry_id": sid}, headers=_SAFE_HEADERS)
        assert ok.status == 200
        assert (await ok.json())["active_session"] == sid


async def test_dev_routes_absent_for_plain_source(tmp_path):
    """A non-dev (bundle) app does NOT mount the dev registry routes."""
    from easycat.debug.bundle import RunBundle
    from easycat.debugger.server import _bundle_source, _make_app

    from ._server_helpers import _build_voice_bundle

    bundle_path = await _build_voice_bundle(tmp_path)
    RunBundle.load(bundle_path)
    app = _make_app(_bundle_source(bundle_path))

    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/dev/sessions")).status == 404
        assert (await client.post("/api/dev/select", json={}, headers=_SAFE_HEADERS)).status == 404
