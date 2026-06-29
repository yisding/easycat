"""Tests for the ``/api/annotate`` + ``/api/annotations`` sidecar endpoints."""

from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

from easycat.debug.annotations import load_annotations, sidecar_path
from easycat.debugger.server import _bundle_source, _make_app, _session_source
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore

from ._server_helpers import _SAFE_HEADERS, _build_voice_bundle


async def _first_turn_id(client) -> str:
    turns = await (await client.get("/api/turns")).json()
    assert turns["turns"], "voice bundle should produce at least one turn"
    return turns["turns"][0]["turn_id"]


async def test_bundle_manifest_advertises_annotate(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        m = await (await client.get("/api/manifest")).json()
        assert m["supports_annotate"] is True
        # The real on-disk path must never leak into the browser manifest.
        for value in m.values():
            assert ".annotations.json" not in str(value)
            assert str(bundle_path) not in str(value)


async def test_annotate_writes_sidecar_and_round_trips(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    bundle_bytes_before = bundle_path.read_bytes()
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        turn_id = await _first_turn_id(client)
        resp = await client.post(
            "/api/annotate",
            json={
                "turn_id": turn_id,
                "passed": False,
                "failure_type": "tts_cutoff",
                "score": 2,
                "notes": "cut off mid-sentence",
            },
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["turn_id"] == turn_id
        assert body["annotation"]["failure_type"] == "tts_cutoff"
        assert body["annotation"]["score"] == 2

        # GET /api/annotations round-trips the persisted map.
        got = await (await client.get("/api/annotations")).json()
        assert turn_id in got["annotations"]
        assert got["annotations"][turn_id]["passed"] is False
        assert got["annotations"][turn_id]["notes"] == "cut off mid-sentence"

    # The sidecar landed next to the bundle; the bundle ZIP is untouched.
    assert sidecar_path(bundle_path).exists()
    assert bundle_path.read_bytes() == bundle_bytes_before
    on_disk = load_annotations(bundle_path)
    assert on_disk[turn_id]["failure_type"] == "tts_cutoff"


async def test_annotations_get_empty_when_no_sidecar(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        got = await (await client.get("/api/annotations")).json()
        assert got == {"annotations": {}}


async def test_annotate_rejects_bad_turn_id(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/annotate",
            json={"turn_id": "../etc/passwd", "passed": True},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["error_code"] == "BAD_REQUEST"


async def test_annotate_rejects_turn_id_not_in_bundle(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/annotate",
            json={"turn_id": "syntactically-valid-but-missing", "passed": True},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["error_code"] == "BAD_REQUEST"
        assert "does not exist" in body["message"]
        assert not sidecar_path(bundle_path).exists()


async def test_annotate_rejects_bad_failure_type(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        turn_id = await _first_turn_id(client)
        resp = await client.post(
            "/api/annotate",
            json={"turn_id": turn_id, "failure_type": "not_a_real_type"},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 400


async def test_annotate_rejects_out_of_band_score(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        turn_id = await _first_turn_id(client)
        for bad in (0, 6):
            resp = await client.post(
                "/api/annotate",
                json={"turn_id": turn_id, "score": bad},
                headers=_SAFE_HEADERS,
            )
            assert resp.status == 400, bad


async def test_annotate_rejected_for_live_sessions():
    """Live-session sources have no on-disk sidecar; annotate must 405."""
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
        m = await (await client.get("/api/manifest")).json()
        assert m["supports_annotate"] is False

        resp = await client.post(
            "/api/annotate",
            json={"turn_id": "t1", "passed": True},
            headers=_SAFE_HEADERS,
        )
        assert resp.status == 405

        resp_get = await client.get("/api/annotations")
        assert resp_get.status == 405
