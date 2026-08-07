from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

from easycat import create_text_session
from easycat.debugger.server import _bundle_source, _make_app, _session_source
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore

from ._server_helpers import _SAFE_HEADERS, _build_voice_bundle, _DeterministicAgent


async def test_api_replay_runs_against_bundle(tmp_path):
    """``POST /api/replay`` should invoke the bundle's replay runner and
    return a structured result with fidelity_label and frame_count."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay", json={"fidelity": "artifact"}, headers=_SAFE_HEADERS
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["fidelity_label"] == "artifact"
        assert body["frame_count"] > 0
        assert body["side_effecting"] is False


async def test_api_replay_runs_off_event_loop_thread(tmp_path):
    """Synchronous wall pacing and stage replay must not block aiohttp."""
    import threading

    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    original_replay = source._replay_fn
    assert original_replay is not None
    event_loop_thread = threading.current_thread()
    replay_threads = []

    def _record_thread(**kwargs):
        replay_threads.append(threading.current_thread())
        return original_replay(**kwargs)

    source._replay_fn = _record_thread
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay", json={"fidelity": "artifact"}, headers=_SAFE_HEADERS
        )
        assert resp.status == 200

    assert replay_threads
    assert replay_threads[0] is not event_loop_thread


async def test_api_replay_rejected_for_live_sessions():
    """Live-session sources don't have a bundle to replay; the endpoint
    must respond with 405, not crash."""
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=4, artifact_store=artifact_store)

    class _StubSession:
        session_id = "live-1"
        is_running = True
        turn_state = "IDLE"
        _artifact_store = artifact_store

        @property
        def journal(self):
            return journal

    source = _session_source(_StubSession())
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay", json={"fidelity": "artifact"}, headers=_SAFE_HEADERS
        )
        assert resp.status == 405


async def test_export_endpoint_returns_zip_for_live_session():
    """``POST /api/export`` should bundle a live session and return a ZIP."""
    session = create_text_session(agent=_DeterministicAgent(), debug="full", wrap_agent=False)
    await session.send_text("export-me")
    source = _session_source(session)
    from easycat.debugger import server as srv

    source._export_fn = lambda: srv._bundle_zip_from_session(session)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    try:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/export", headers=_SAFE_HEADERS)
            assert resp.status == 200
            body = await resp.read()
            # ZIP magic.
            assert body[:2] == b"PK"
    finally:
        await session.stop()


async def test_export_turn_returns_single_turn_slice(tmp_path):
    """``POST /api/export?turn=<id>`` returns a ZIP whose journal has only
    that turn — the SPA "Save as test case" flow."""
    import io
    import zipfile

    from easycat.debugger import server as srv

    session = create_text_session(agent=_DeterministicAgent(), debug="full", wrap_agent=False)
    await session.send_text("slice-me")
    # Resolve the turn id the session recorded.
    turn_ids = sorted({r.turn_id for r in session.journal.read() if getattr(r, "turn_id", None)})
    assert turn_ids, "session should have recorded at least one turn"
    turn_id = turn_ids[0]

    source = _session_source(session)
    source._export_turn_fn = lambda tid: srv._turn_bundle_zip_from_session(session, tid)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    try:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/export?turn={turn_id}", headers=_SAFE_HEADERS)
            assert resp.status == 200
            body = await resp.read()
            assert body[:2] == b"PK"
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                journal = zf.read("journal.ndjson").decode("utf-8")
            import json as _json

            turns = {_json.loads(line)["turn_id"] for line in journal.splitlines() if line.strip()}
            assert turns == {turn_id}
    finally:
        await session.stop()


async def test_export_turn_unknown_turn_returns_404(tmp_path):
    from easycat.debugger import server as srv

    session = create_text_session(agent=_DeterministicAgent(), debug="full", wrap_agent=False)
    await session.send_text("hi")
    source = _session_source(session)
    source._export_turn_fn = lambda tid: srv._turn_bundle_zip_from_session(session, tid)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    try:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/export?turn=turn-does-not-exist", headers=_SAFE_HEADERS)
            assert resp.status == 404
    finally:
        await session.stop()


async def test_export_turn_invalid_turn_id_returns_400(tmp_path):
    session = create_text_session(agent=_DeterministicAgent(), debug="full", wrap_agent=False)
    await session.send_text("hi")
    source = _session_source(session)
    from easycat.debugger import server as srv

    source._export_turn_fn = lambda tid: srv._turn_bundle_zip_from_session(session, tid)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    try:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/export?turn=" + "x" * 200, headers=_SAFE_HEADERS)
            assert resp.status == 400
    finally:
        await session.stop()


async def test_export_rejected_for_bundle_source(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/export",
            headers={
                "Host": "localhost:8765",
                "Origin": "http://localhost:8765",
                "Content-Type": "application/json",
            },
        )
        assert resp.status == 405


async def test_replay_destructive_combos_require_confirm(tmp_path):
    """LIVE fidelity, ALLOW tool policy, or force=True must be gated on
    an explicit ``confirm: true`` flag so a CSRF / drive-by from another
    tab can't fire them silently."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    safe_headers = {
        "Host": "localhost:8765",
        "Origin": "http://localhost:8765",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        for body in (
            {"fidelity": "live"},
            {"tool_policy": "allow"},
            {"fidelity": "artifact", "force": True},
        ):
            resp = await client.post("/api/replay", json=body, headers=safe_headers)
            assert resp.status == 409, f"expected 409 for body {body}, got {resp.status}"
            data = await resp.json()
            assert data["destructive"] is True

        # With confirm=true the request proceeds (force still fires the
        # ARTIFACT path, which works since we don't check provider versions).
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact", "force": True, "confirm": True},
            headers=safe_headers,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["destructive"] is True


async def test_replay_destructive_confirm_must_be_literal_true(tmp_path):
    """Truthy JSON values like ``"false"`` must not satisfy confirmation."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    headers = {
        "Host": "localhost:8765",
        "Origin": "http://localhost:8765",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        for confirm in ("true", "false", 1):
            resp = await client.post(
                "/api/replay",
                json={"fidelity": "artifact", "force": True, "confirm": confirm},
                headers=headers,
            )
            assert resp.status == 409


async def test_replay_force_must_be_literal_boolean(tmp_path):
    """Truthy JSON values like ``"false"`` or ``1`` must not enable force."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    headers = {
        "Host": "localhost:8765",
        "Origin": "http://localhost:8765",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        for force in ("true", "false", 1):
            resp = await client.post(
                "/api/replay",
                json={"fidelity": "artifact", "force": force},
                headers=headers,
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["error_code"] == "BAD_REQUEST"
            assert "force must be a boolean" in body["message"]

        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact", "force": False},
            headers=headers,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["destructive"] is False


async def test_replay_rejects_unknown_keys(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    headers = {
        "Host": "localhost:8765",
        "Origin": "http://localhost:8765",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact", "rm_rf": "/"},
            headers=headers,
        )
        assert resp.status == 400


async def test_replay_rejects_malformed_json(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    headers = {
        "Host": "localhost:8765",
        "Origin": "http://localhost:8765",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/replay", data=b"{not json", headers=headers)
        assert resp.status == 400


async def test_replay_rejects_oversized_json_integer(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            data=b'{"from_sequence":' + b"9" * 5000 + b"}",
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 400


async def test_replay_rejects_non_object_json(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    headers = {
        "Host": "localhost:8765",
        "Origin": "http://localhost:8765",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/replay", data=b"null", headers=headers)
        assert resp.status == 400


async def test_export_without_journal_returns_409(tmp_path):
    """Live session with debug='off' (no journal) can't export — endpoint
    must return 409, not crash."""
    artifact_store = InMemoryArtifactStore()

    class _StubSession:
        session_id = "no-journal"
        is_running = False
        turn_state = "IDLE"
        _artifact_store = artifact_store
        journal = None

    source = _session_source(_StubSession())

    # Wire the export function so the endpoint can call it.
    from easycat.debugger import server as srv

    source._export_fn = lambda: srv._bundle_zip_from_session(_StubSession())
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/export",
            headers={
                "Host": "localhost:8765",
                "Origin": "http://localhost:8765",
                "Content-Type": "application/json",
            },
        )
        assert resp.status == 409


async def test_replay_force_artifact_with_confirm_succeeds(tmp_path):
    """``force=True`` with ``fidelity=artifact`` is destructive but must
    still run when ``confirm=true`` is supplied — the gate exists to
    require acknowledgement, not to disable force entirely."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    headers = {
        "Host": "localhost:8765",
        "Origin": "http://localhost:8765",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact", "force": True, "confirm": True},
            headers=headers,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["destructive"] is True
        assert body["fidelity_label"] == "artifact"


async def test_api_replay_returns_serialized_frames(tmp_path):
    """``POST /api/replay`` should return the frame list, with bytes
    blobs stripped (raw bytes can't go through JSON) and refs preserved
    so the UI can fetch artifacts on demand from ``/api/artifact/{ref}``."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay", json={"fidelity": "artifact"}, headers=_SAFE_HEADERS
        )
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body["frames"], list)
        assert len(body["frames"]) == body["frame_count"]
        assert body["total_frames"] >= body["frame_count"]
        assert body["frames_truncated"] is False
        for frame in body["frames"]:
            # Bytes blobs must be stripped; refs survive.
            assert "input_blob" not in frame
            assert "output_blob" not in frame
            for required in ("sequence", "stage", "kind", "name", "data"):
                assert required in frame
            assert isinstance(frame["input_blob_size"], int)
            assert isinstance(frame["output_blob_size"], int)


async def test_api_replay_accepts_window_and_stage_filter(tmp_path):
    """Window keys (``from_sequence``/``to_sequence``) and ``stage_filter``
    should reach the runner.  When filtered to one stage, every frame
    must report that stage."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact", "stage_filter": ["tts"]},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["frames"], "tts stage should produce at least one frame"
        for frame in body["frames"]:
            assert frame["stage"] == "tts"


async def test_api_replay_rejects_unknown_stage(tmp_path):
    """An unknown stage in ``stage_filter`` must return 400 with a
    ``BAD_REQUEST`` error_code rather than silently producing no frames."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact", "stage_filter": ["bogus_stage"]},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["error_code"] == "BAD_REQUEST"
        assert "bogus_stage" in body["message"]


async def test_api_replay_rejects_invalid_timing(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact", "timing": "not-a-timing-mode"},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["error_code"] == "BAD_REQUEST"
        assert "timing" in body["message"]


@pytest.mark.parametrize(
    ("name", "value"),
    [("fidelity", []), ("tool_policy", {})],
)
async def test_api_replay_rejects_unhashable_enum_controls(tmp_path, name, value):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={name: value},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["error_code"] == "BAD_REQUEST"
        assert name in body["message"]


async def test_api_replay_returns_structured_version_mismatch(tmp_path):
    """When the runner raises :class:`ProviderVersionMismatchError`, the
    handler must return a 409 with ``error_code`` and a ``mismatches``
    detail array — not a stringified message."""
    from easycat.runtime.replay import (
        ProviderVersionMismatchError,
        VersionMismatch,
    )

    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)

    def _raise(**_kwargs):
        raise ProviderVersionMismatchError(
            "openai: bundle='1.0' installed='2.0' (MISMATCH)",
            error_code="PROVIDER_VERSION_MISMATCH",
            mismatches=(
                VersionMismatch(
                    provider="openai",
                    bundle_version="1.0",
                    installed_version="2.0",
                    code="MISMATCH",
                ),
            ),
        )

    source._replay_fn = _raise
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay", json={"fidelity": "artifact"}, headers=_SAFE_HEADERS
        )
        assert resp.status == 409
        body = await resp.json()
        assert body["error_code"] == "PROVIDER_VERSION_MISMATCH"
        assert body["details"]["mismatches"] == [
            {
                "provider": "openai",
                "bundle_version": "1.0",
                "installed_version": "2.0",
                "code": "MISMATCH",
            }
        ]


async def test_api_replay_returns_structured_replay_error(tmp_path):
    """:class:`ReplayError` (non-committable boundary) must surface the
    requested sequence and nearest committable checkpoints so the UI can
    render snap-to-checkpoint buttons."""
    from easycat.runtime.replay import ReplayError

    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)

    def _raise(**_kwargs):
        raise ReplayError(
            "Replay start sequence 7 is not a committable boundary.",
            requested_sequence=7,
            nearest_committable_before=5,
            nearest_committable_after=10,
            stage="agent",
        )

    source._replay_fn = _raise
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact", "from_sequence": 7},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 409
        body = await resp.json()
        assert body["error_code"] == "REPLAY_NON_COMMITTABLE"
        assert body["details"] == {
            "requested_sequence": 7,
            "nearest_committable_before": 5,
            "nearest_committable_after": 10,
            "stage": "agent",
        }


async def test_api_replay_returns_structured_divergence_error(tmp_path):
    """Stage output mismatches retain EASYCAT_E403 and digest details."""
    from easycat.runtime.replay import ReplayDivergenceError

    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)

    def _raise(**_kwargs):
        raise ReplayDivergenceError(
            "stage='agent', turn_id='turn-1'",
            stage="agent",
            turn_id="turn-1",
            expected_digest="expected",
            actual_digest="actual",
            requested_sequence=7,
        )

    source._replay_fn = _raise
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact"},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 409
        body = await resp.json()
        assert body["error_code"] == "EASYCAT_E403"
        assert body["details"] == {
            "requested_sequence": 7,
            "stage": "agent",
            "turn_id": "turn-1",
            "expected_digest": "expected",
            "actual_digest": "actual",
        }


async def test_api_replay_returns_structured_side_effect_blocked(tmp_path):
    """:class:`ReplaySideEffectBlocked` becomes ``REPLAY_SIDE_EFFECT_BLOCKED``
    so the UI can hint at switching tool policy."""
    from easycat.runtime.replay import ReplaySideEffectBlocked

    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)

    def _raise(**_kwargs):
        raise ReplaySideEffectBlocked("tool 'send_email' blocked at sequence 42")

    source._replay_fn = _raise
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay", json={"fidelity": "artifact"}, headers=_SAFE_HEADERS
        )
        assert resp.status == 409
        body = await resp.json()
        assert body["error_code"] == "REPLAY_SIDE_EFFECT_BLOCKED"
        assert "send_email" in body["message"]


async def test_api_manifest_includes_replay_entry_points(tmp_path):
    """Bundle manifests must surface ``replay_entry_points`` so the UI
    can populate a checkpoint-snap picker.  Entries serialise the four
    fields the UI cares about; live-session manifests carry an empty list
    for shape symmetry."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/manifest")
        assert resp.status == 200
        body = await resp.json()
        assert "replay_entry_points" in body
        assert isinstance(body["replay_entry_points"], list)
        for entry in body["replay_entry_points"]:
            assert set(entry.keys()) == {"sequence", "stage", "unit_id", "checkpoint_id"}
            assert entry["checkpoint_id"] == f"cp_{entry['sequence']}"


async def test_api_manifest_session_has_empty_replay_entry_points():
    """Live session sources expose ``replay_entry_points: []`` so the UI
    can read the field unconditionally."""
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=4, artifact_store=artifact_store)

    class _StubSession:
        session_id = "live-1"
        is_running = True
        turn_state = "IDLE"
        _artifact_store = artifact_store

        @property
        def journal(self):
            return journal

    source = _session_source(_StubSession())
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/manifest")
        assert resp.status == 200
        body = await resp.json()
        assert body["replay_entry_points"] == []


async def test_api_replay_caps_frame_count(tmp_path, monkeypatch):
    """Frame payload is capped at ``_REPLAY_FRAME_LIMIT`` so a giant
    bundle can't blow the response.  When the cap fires, ``frames``
    is truncated, ``frames_truncated`` is True, and ``total_frames``
    reports the full count."""
    from easycat.debugger import _sources

    # ``_REPLAY_FRAME_LIMIT`` physically lives in ``_sources`` (QS3); patch the
    # module the replay closure reads it from, not the ``server`` facade alias.
    monkeypatch.setattr(_sources, "_REPLAY_FRAME_LIMIT", 3)

    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay", json={"fidelity": "artifact"}, headers=_SAFE_HEADERS
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["frames_truncated"] is True
        assert body["frame_count"] == 3
        assert body["total_frames"] > 3
        assert len(body["frames"]) == 3
