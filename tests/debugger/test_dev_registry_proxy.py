"""Dev debugger — best-of-class expansion contract tests.

Covers the lifecycle/correctness spine added on top of the core dev debugger:

* weakref-backed, self-pruning :class:`SessionIndex` (no dead-session leak),
* registration through the ``create_session`` funnel via ``arm_dev_session``
  (so server modes populate the selector) with unregister-on-close,
* the ``_DevDebuggerState`` selection epoch + WS live-follow cursor reset,
* the registry-backed proxy re-pointing every panel on ``/api/dev/select``,
* the cross-session ``/api/dev/overview`` aggregate,
* port-collision-resilient launch (``_find_free_dev_port`` / ``_dev_port``),
* richer ``LiveSessionSummary`` metadata (last_sequence / activity).

All offline: duck-typed fake sessions + fake journals, no live API keys, and
the HTTP cases use the aiohttp test client (skipped cleanly when absent).
"""

from __future__ import annotations

import gc
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest


class _FakeJournal:
    """Minimal JournalView stand-in: dict records + a monotonic sequence."""

    def __init__(self, records: list[dict] | None = None) -> None:
        self._records = records or []
        self.latest_sequence = max((int(r.get("sequence", 0)) for r in self._records), default=0)

    def read(self, start: int | None = None, limit: int | None = None) -> list[dict]:
        rows = self._records
        if start is not None:
            rows = [r for r in rows if int(r.get("sequence", 0)) >= start]
        if limit is not None:
            rows = rows[:limit]
        return list(rows)


class _FakeSession:
    """Duck-typed session for registry/proxy tests (no aiohttp, no live IO)."""

    def __init__(
        self,
        session_id: str = "sess-1",
        *,
        is_running: bool = True,
        turn_state: str = "idle",
        journal: _FakeJournal | None = None,
    ) -> None:
        self.session_id = session_id
        self.is_running = is_running
        self.turn_state = turn_state
        self.journal = journal


# ── Weakref-backed, self-pruning registry ────────────────────────


def test_registry_prunes_after_session_collected():
    from easycat.debugger.session_registry import SessionIndex

    reg = SessionIndex()
    s = _FakeSession("gc-me")
    rid = reg.register(s)
    assert [x.registry_id for x in reg.list()] == [rid]

    del s
    gc.collect()
    # The session has no other owner, so the registry must not pin it alive.
    assert reg.list() == []
    assert reg.get(rid) is None


def test_registry_version_bumps_on_structural_change():
    from easycat.debugger.session_registry import SessionIndex

    reg = SessionIndex()
    held = []  # keep refs so weakref pruning doesn't race the assertions
    v0 = reg.version()
    held.append(_FakeSession("a"))
    rid = reg.register(held[-1])
    assert reg.version() > v0
    v1 = reg.version()
    reg.unregister(rid)
    assert reg.version() > v1


def test_unregister_obj_drops_only_the_matching_entry():
    from easycat.debugger.session_registry import SessionIndex

    reg = SessionIndex()
    a, b = _FakeSession("a"), _FakeSession("b")
    reg.register(a)
    rid_b = reg.register(b)
    reg.unregister_obj(a)
    assert [x.registry_id for x in reg.list()] == [rid_b]


def test_registry_falls_back_to_strong_ref_for_non_weakreffable():
    from easycat.debugger.session_registry import SessionIndex

    class _NoWeak:
        # No __weakref__ slot -> weakref.ref raises TypeError -> strong fallback.
        __slots__ = ("is_running", "journal", "session_id", "turn_state")

        def __init__(self) -> None:
            self.session_id = "noweak"
            self.is_running = True
            self.turn_state = "idle"
            self.journal = None

    reg = SessionIndex()
    rid = reg.register(_NoWeak())  # not held by the test
    gc.collect()
    # The strong fallback keeps it listed (it cannot be weakly tracked/pruned).
    assert [x.registry_id for x in reg.list()] == [rid]
    reg.unregister(rid)
    assert reg.list() == []


def test_registry_concurrent_register_list_prune_is_safe():
    from easycat.debugger.session_registry import SessionIndex

    reg = SessionIndex()
    held: list[_FakeSession] = []

    def worker(i: int) -> None:
        s = _FakeSession(f"s{i}")
        held.append(s)  # keep alive so the final count is deterministic
        reg.register(s)
        reg.list()
        reg.version()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(64)))

    assert len(reg.list()) == 64


def test_summary_surfaces_last_sequence_and_activity():
    from easycat.debugger.session_registry import SessionIndex

    reg = SessionIndex()
    active = _FakeSession("hot", turn_state="agent", journal=_FakeJournal([{"sequence": 7}]))
    idle = _FakeSession("cold", turn_state="idle", journal=_FakeJournal([{"sequence": 2}]))
    reg.register(active)
    reg.register(idle)
    by_id = {s.session_id: s for s in reg.list()}
    assert by_id["hot"].last_sequence == 7
    assert by_id["hot"].activity == "active"
    assert by_id["cold"].activity == "idle"
    # to_dict() carries the new fields for the WS push / selector.
    assert set(by_id["hot"].to_dict()) >= {"last_sequence", "activity"}


# ── Port-collision-resilient launch ──────────────────────────────


def test_find_free_dev_port_skips_an_occupied_port():
    from easycat.debugger import dev as dev_mod

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    taken = sock.getsockname()[1]
    try:
        free = dev_mod._find_free_dev_port("127.0.0.1", taken, 5)
        assert free is not None
        assert free != taken
    finally:
        sock.close()


def test_find_free_dev_port_returns_none_when_range_exhausted():
    from easycat.debugger import dev as dev_mod

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    taken = sock.getsockname()[1]
    try:
        # A 1-wide scan over exactly the occupied port has nowhere to go.
        assert dev_mod._find_free_dev_port("127.0.0.1", taken, 1) is None
    finally:
        sock.close()


def test_dev_port_env_override_bypasses_scan(monkeypatch: pytest.MonkeyPatch):
    from easycat.debugger import dev as dev_mod

    monkeypatch.setenv("EASYCAT_DEV_DEBUGGER_PORT", "9999")
    assert dev_mod._dev_port() == 9999


@pytest.mark.parametrize("raw", ["0", "-1", "70000", "not-a-port"])
def test_dev_port_env_override_rejects_invalid_ports(monkeypatch: pytest.MonkeyPatch, raw: str):
    from easycat.debugger import dev as dev_mod

    monkeypatch.setenv("EASYCAT_DEV_DEBUGGER_PORT", raw)
    monkeypatch.setattr(dev_mod, "_find_free_dev_port", lambda host, start, span: 9876)

    assert dev_mod._dev_port() == 9876


# ── Registration through the create_session funnel (arm_dev_session) ─


@pytest.fixture(autouse=True)
def _isolate_registry_and_latch():
    from easycat.debugger import dev as dev_mod
    from easycat.debugger.session_registry import get_registry

    get_registry().clear()
    dev_mod.reset_launch_state()
    yield
    get_registry().clear()
    dev_mod.reset_launch_state()


def test_arm_dev_session_is_noop_when_not_armed(monkeypatch: pytest.MonkeyPatch):
    from easycat.debugger import dev as dev_mod
    from easycat.debugger.session_registry import list_sessions

    monkeypatch.delenv("EASYCAT_DEV", raising=False)
    assert dev_mod.arm_dev_session(_FakeSession("x")) is None
    assert list_sessions() == []


def test_arm_dev_session_registers_when_env_armed(monkeypatch: pytest.MonkeyPatch):
    from easycat.debugger import dev as dev_mod
    from easycat.debugger.session_registry import list_sessions

    monkeypatch.setenv("EASYCAT_DEV", "1")
    s = _FakeSession("armed")
    rid = dev_mod.arm_dev_session(s)
    assert rid is not None
    assert [x.session_id for x in list_sessions()] == ["armed"]


def test_dev_registry_ui_arms_registration_even_under_pytest(monkeypatch: pytest.MonkeyPatch):
    """Registration is decoupled from the UI launch.

    ``maybe_launch_dev_registry_ui`` returns False under pytest (the UI must
    not open), but it still ARMS registration so per-connection server-mode
    sessions built downstream populate the selector.
    """
    from easycat.debugger import dev as dev_mod
    from easycat.debugger.session_registry import list_sessions

    monkeypatch.delenv("EASYCAT_DEV", raising=False)
    assert dev_mod.maybe_launch_dev_registry_ui(dev=True, launch_ui=False) is False
    s = _FakeSession("downstream")
    assert dev_mod.arm_dev_session(s) is not None
    assert [x.session_id for x in list_sessions()] == ["downstream"]


def test_create_session_registers_after_dev_registry_ui_arms(monkeypatch: pytest.MonkeyPatch):
    """Server-mode sessions built downstream through create_session populate the selector."""
    from easycat import EasyConfig, create_session
    from easycat.debugger import dev as dev_mod
    from easycat.debugger.session_registry import list_sessions
    from easycat.stubs import (
        ScriptedAgent,
        ScriptedSTT,
        ScriptedTransport,
        ScriptedTTS,
        ScriptedVAD,
    )

    monkeypatch.delenv("EASYCAT_DEV", raising=False)
    assert dev_mod.maybe_launch_dev_registry_ui(dev=True, launch_ui=False) is False

    session = create_session(
        EasyConfig(
            stt=ScriptedSTT(),
            tts=ScriptedTTS(),
            vad=ScriptedVAD(),
            transport=ScriptedTransport(),
            agent=ScriptedAgent(),
            debug="off",
        )
    )

    assert [item.session_id for item in list_sessions()] == [session.session_id]


async def test_arm_dev_session_unregisters_on_close(monkeypatch: pytest.MonkeyPatch):
    """With a running loop, the watcher drops the session the moment it closes."""
    import asyncio

    from easycat.debugger import dev as dev_mod
    from easycat.debugger.session_registry import list_sessions
    from easycat.runtime.scope import RuntimeScope

    monkeypatch.setenv("EASYCAT_DEV", "1")

    class _ClosableSession(_FakeSession):
        def __init__(self) -> None:
            super().__init__("closable")
            self._evt = asyncio.Event()
            self._runtime_scope = RuntimeScope(name="debugger-test-session")

        async def wait_closed(self) -> None:
            await self._evt.wait()

        def close(self) -> None:
            self._evt.set()

    s = _ClosableSession()
    dev_mod.arm_dev_session(s)
    assert [x.session_id for x in list_sessions()] == ["closable"]
    watcher_tasks = s._runtime_scope.tasks()
    assert len(watcher_tasks) == 1

    # Re-arming the same registered Session does not create a duplicate
    # lifecycle watcher beneath its root.
    dev_mod.arm_dev_session(s)
    assert s._runtime_scope.tasks() == watcher_tasks

    s.close()
    await asyncio.sleep(0)  # let the watcher run
    await asyncio.sleep(0)
    assert list_sessions() == []
    assert s._runtime_scope.empty
    await s._runtime_scope.close()


# ── Selection epoch (pure + state) ───────────────────────────────


def test_should_reset_live_follow_pure():
    from easycat.debugger.server import _should_reset_live_follow

    assert _should_reset_live_follow(None, 0) is False  # first tick never resets
    assert _should_reset_live_follow(0, 0) is False  # unchanged
    assert _should_reset_live_follow(0, 1) is True  # selection advanced
    assert _should_reset_live_follow(2, 1) is True  # any change resets


def test_selection_epoch_bumps_only_on_real_change():
    from easycat.debugger.server import _DevDebuggerState
    from easycat.debugger.session_registry import SessionIndex

    reg = SessionIndex()
    a, b = _FakeSession("a"), _FakeSession("b")
    rid_a, rid_b = reg.register(a), reg.register(b)
    state = _DevDebuggerState(reg)

    assert state.selection_epoch() == 0
    state.select(rid_a)
    assert state.selection_epoch() == 1
    state.select(rid_a)  # no-op
    assert state.selection_epoch() == 1
    state.select(rid_b)
    assert state.selection_epoch() == 2
    state.select(None)  # clear
    assert state.selection_epoch() == 3


def test_session_overview_stats_pure():
    from easycat.debugger.server import _session_overview_stats

    records = [
        {"sequence": 1, "turn_id": "t1", "name": "stage_start", "wall_ns": 1_000_000_000},
        {"sequence": 2, "turn_id": "t1", "name": "error", "error": True, "wall_ns": 1_050_000_000},
        {"sequence": 3, "turn_id": "t2", "name": "stage_start", "wall_ns": 2_000_000_000},
        {"sequence": 4, "turn_id": "t2", "name": "turn_end", "wall_ns": 2_200_000_000},
    ]
    stats = _session_overview_stats(records)
    assert stats["turn_count"] == 2
    assert stats["error_count"] == 1
    assert stats["last_turn_wall_ms"] == pytest.approx(200.0, abs=1.0)


# ── HTTP API: proxy re-pointing, dev_select matrix, overview ─────

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from ._server_helpers import _SAFE_HEADERS  # noqa: E402


def _dev_app(registry):
    from easycat.debugger.server import _empty_dev_source, _make_app

    return _make_app(_empty_dev_source(), registry=registry)


def _journal_session(session_id: str, records: list[dict]) -> _FakeSession:
    return _FakeSession(session_id, journal=_FakeJournal(records))


async def test_proxy_repoints_every_panel_on_select():
    from easycat.debugger.session_registry import SessionIndex

    reg = SessionIndex()
    a = _journal_session("alpha", [{"sequence": 1, "turn_id": "t1", "name": "a-event"}])
    b = _journal_session("beta", [{"sequence": 1, "turn_id": "t1", "name": "b-event"}])
    rid_a = reg.register(a)
    rid_b = reg.register(b)
    app = _dev_app(reg)

    async with TestClient(TestServer(app)) as client:
        # Two sessions -> nothing auto-selected -> empty proxy source.
        assert (await (await client.get("/api/records")).json())["records"] == []

        await client.post("/api/dev/select", json={"registry_id": rid_a}, headers=_SAFE_HEADERS)
        recs = (await (await client.get("/api/records")).json())["records"]
        assert [r["name"] for r in recs] == ["a-event"]
        manifest = await (await client.get("/api/manifest")).json()
        assert manifest["active_session"] == rid_a and manifest["dev_mode"] is True

        # Switching re-points the same panels at the other session.
        await client.post("/api/dev/select", json={"registry_id": rid_b}, headers=_SAFE_HEADERS)
        recs = (await (await client.get("/api/records")).json())["records"]
        assert [r["name"] for r in recs] == ["b-event"]


async def test_dev_select_validation_matrix():
    from easycat.debugger.session_registry import SessionIndex

    reg = SessionIndex()
    held = _FakeSession("only")
    rid = reg.register(held)
    app = _dev_app(reg)

    async with TestClient(TestServer(app)) as client:
        # Non-JSON body -> 400 (the origin guard requires JSON content-type).
        bad = await client.post("/api/dev/select", data="not json", headers={**_SAFE_HEADERS})
        assert bad.status == 400
        # Non-dict JSON -> 400.
        assert (
            await client.post("/api/dev/select", json=[1, 2], headers=_SAFE_HEADERS)
        ).status == 400
        # Non-string registry_id -> 400.
        assert (
            await client.post("/api/dev/select", json={"registry_id": 5}, headers=_SAFE_HEADERS)
        ).status == 400
        # Unknown id -> 404.
        assert (
            await client.post(
                "/api/dev/select", json={"registry_id": "nope"}, headers=_SAFE_HEADERS
            )
        ).status == 404
        # null clears -> 200.
        ok = await client.post(
            "/api/dev/select", json={"registry_id": None}, headers=_SAFE_HEADERS
        )
        assert ok.status == 200
        # Known id -> 200.
        assert (
            await client.post("/api/dev/select", json={"registry_id": rid}, headers=_SAFE_HEADERS)
        ).status == 200


async def test_dev_sessions_auto_selects_single_session():
    from easycat.debugger.session_registry import SessionIndex

    reg = SessionIndex()
    held = _FakeSession("solo")
    rid = reg.register(held)
    app = _dev_app(reg)
    async with TestClient(TestServer(app)) as client:
        data = await (await client.get("/api/dev/sessions")).json()
        assert data["active_session"] == rid


async def test_dev_overview_aggregates_per_session_and_total():
    from easycat.debugger.session_registry import SessionIndex

    reg = SessionIndex()
    hot = _journal_session(
        "hot",
        [
            {"sequence": 1, "turn_id": "t1", "name": "stage_start", "wall_ns": 1_000_000_000},
            {
                "sequence": 2,
                "turn_id": "t1",
                "name": "error",
                "error": True,
                "wall_ns": 1_010_000_000,
            },
        ],
    )
    hot.turn_state = "agent"  # active
    calm = _journal_session("calm", [{"sequence": 1, "turn_id": "t1", "name": "stage_start"}])
    reg.register(hot)
    reg.register(calm)
    app = _dev_app(reg)

    async with TestClient(TestServer(app)) as client:
        data = await (await client.get("/api/dev/overview")).json()
        assert data["summary"]["sessions_total"] == 2
        assert data["summary"]["errors_total"] == 1
        assert data["summary"]["active_turns"] == 1
        by_id = {s["session_id"]: s for s in data["sessions"]}
        assert by_id["hot"]["error_count"] == 1
        assert by_id["calm"]["error_count"] == 0


async def test_dev_overview_empty_is_200():
    from easycat.debugger.session_registry import SessionIndex

    app = _dev_app(SessionIndex())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/dev/overview")
        assert resp.status == 200
        data = await resp.json()
        assert data["summary"]["sessions_total"] == 0
        assert data["sessions"] == []


async def test_dev_routes_absent_for_plain_source(tmp_path):
    """A non-dev (bundle) app mounts none of the dev registry routes."""
    from easycat.debug.bundle import RunBundle
    from easycat.debugger.server import _bundle_source, _make_app

    from ._server_helpers import _build_voice_bundle

    bundle_path = await _build_voice_bundle(tmp_path)
    RunBundle.load(bundle_path)
    app = _make_app(_bundle_source(bundle_path))
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/dev/sessions")).status == 404
        assert (await client.get("/api/dev/overview")).status == 404
