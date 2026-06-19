"""Neo M13 — dev debugger mode (Workstream A: always-available dev timeline).

Covers the dev opt-in (``EASYCAT_DEV`` / ``VoiceApp(dev=True)``), the
process-local session registry, the registry-backed debugger API additions
(``/api/dev/sessions``, ``/api/budgets``, ``/api/dev/promote``), and — most
importantly — the R7 PURELY-ADDITIVE invariant: the dev opt-in adds a fresh
launch trigger but must NOT weaken the existing
``debug="full"``-alone-never-autolaunches guarantee.
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


def test_registry_prunes_stopped_sessions():
    """A per-connection server mode registers a session per connection; a stopped
    session (``Session._closed``) is pruned on the next list/get so the registry
    self-cleans in fan-out modes without an explicit unregister at teardown."""
    from easycat.debugger.session_registry import (
        get_registry,
        list_sessions,
        register_session,
    )

    live = _FakeSession("live")
    stopped = _FakeSession("stopped")
    register_session(live)
    stopped_id = register_session(stopped)

    # Simulate teardown: Session.stop() -> _close() flips ``_closed``.
    stopped._closed = True

    summaries = {s.session_id for s in list_sessions()}
    assert summaries == {"live"}
    # The pruned entry is gone from the backing map and from get().
    assert get_registry().get(stopped_id) is None


def test_dev_session_observer_registers_only_when_opted_in():
    """The fan-out ``on_session`` observer registers each session when dev mode
    is opted in, and is ``None`` (a no-op for the serve helpers) otherwise."""
    from easycat.debugger.dev import dev_session_observer
    from easycat.debugger.session_registry import list_sessions

    assert dev_session_observer(dev=False) is None

    observer = dev_session_observer(dev=True)
    assert observer is not None
    session = _FakeSession("fanned-out")
    observer(session)
    assert {s.session_id for s in list_sessions()} == {"fanned-out"}


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
    sid = registry.register(_FakeSession("live-1"), label="first")
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


async def test_budgets_route_uses_shared_report():
    """``/api/budgets`` evaluates configured budgets via build_budget_report."""
    from easycat.budgets import LatencyBudget
    from easycat.debugger.server import _empty_dev_source, _make_app
    from easycat.debugger.session_registry import SessionRegistry

    class _ConfigObservability:
        latency_budget = (LatencyBudget(stage="total_ms", max_ms=100.0),)
        max_session_cost_usd = None

    class _Config:
        observability = _ConfigObservability()

    class _BudgetSession(_FakeSession):
        def __init__(self) -> None:
            super().__init__("budget-1")
            self._config = _Config()

            class _Journal:
                latest_sequence = 1

                def read(self, *_a, **_k):
                    return [
                        {
                            "sequence": 1,
                            "name": "latency_metric",
                            "turn_id": "t1",
                            "data": {"stage": "total_ms", "value": 250.0},
                        }
                    ]

            self.journal = _Journal()

    registry = SessionRegistry()
    registry.register(_BudgetSession())
    app = _make_app(_empty_dev_source(), registry=registry)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/budgets")
        assert resp.status == 200
        report = await resp.json()
        assert report["evaluated_latency_budgets"] == 1
        # 250ms observed exceeds the 100ms total_ms budget.
        assert report["passed"] is False
        assert any(v["stage"] == "total_ms" for v in report["violations"])


async def test_budgets_route_passes_without_budgets():
    """No configured budgets → an empty passing report, never a 500."""
    from easycat.debugger.session_registry import SessionRegistry

    registry = SessionRegistry()
    registry.register(_FakeSession("plain"))
    app = _dev_app(registry)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/budgets")
        assert resp.status == 200
        report = await resp.json()
        assert report["passed"] is True
        assert report["evaluated_latency_budgets"] == 0


async def test_dev_promote_route_uses_hardened_path(monkeypatch: pytest.MonkeyPatch):
    """``/api/dev/promote`` routes through the M11 hardened promote function."""
    import easycat.debugger.server as server_mod
    from easycat.debugger.session_registry import SessionRegistry

    registry = SessionRegistry()
    registry.register(_FakeSession("promote-1"))
    app = _dev_app(registry)

    seen: dict[str, object] = {}

    def _fake_promote_active_turn(session, turn_id, **kwargs):
        seen["turn_id"] = turn_id
        seen.update(kwargs)
        return 200, {
            "out": "tests/test_x.py",
            "bundle": "tests/test_x.bundle",
            "turn_id": turn_id,
            "redacted": not kwargs["allow_pii"],
            "include_audio": kwargs["include_audio"],
            "assert_on": kwargs["assert_on"],
        }

    monkeypatch.setattr(server_mod, "_promote_active_turn", _fake_promote_active_turn)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/dev/promote",
            json={"turn_id": "t1", "out": "tests/test_x.py"},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        # Redact-by-default, audio excluded by default, hash assertion default.
        assert seen["allow_pii"] is False
        assert seen["include_audio"] is False
        assert seen["assert_on"] == "hash"
        assert data["redacted"] is True


async def test_dev_promote_requires_turn_id():
    from easycat.debugger.session_registry import SessionRegistry

    registry = SessionRegistry()
    registry.register(_FakeSession("promote-2"))
    app = _dev_app(registry)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/dev/promote", json={"out": "tests/x.py"}, headers=_SAFE_HEADERS
        )
        assert resp.status == 400


@pytest.mark.parametrize(
    "bad_out",
    [
        "/etc/cron.d/evil.py",  # absolute → escapes the project
        "../evil.py",  # parent traversal → escapes cwd
        "tests/../../evil.py",  # normalizes to outside cwd
        "tests/x.txt",  # not a .py regression test
    ],
)
async def test_dev_promote_rejects_unsafe_out_path(monkeypatch: pytest.MonkeyPatch, bad_out: str):
    """A client-controlled ``out`` that escapes the project or is not a ``.py``
    file is rejected with 400 — never written. Promotion emits executable code,
    so an unconfined path would be an arbitrary file/code-write primitive."""
    import easycat.debugger.server as server_mod
    from easycat.debugger.session_registry import SessionRegistry

    registry = SessionRegistry()
    registry.register(_FakeSession("promote-3"))
    app = _dev_app(registry)

    # If confinement fails to fire, this would be invoked — assert it is NOT.
    def _must_not_run(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("unsafe out path reached the promote writer")

    monkeypatch.setattr(server_mod, "_promote_active_turn", _must_not_run)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/dev/promote",
            json={"turn_id": "t1", "out": bad_out},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 400


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
        # /api/budgets is mounted everywhere (degrades to empty report).
        assert (await client.get("/api/budgets")).status == 200
