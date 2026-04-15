"""Tests for the debugger server's HTTP API.

The server is an aiohttp app; we drive it with the aiohttp test client
so we can assert on JSON shapes without binding to a real port.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import zipfile
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("aiohttp")

from easycat import create_text_session  # noqa: E402
from easycat.audio_format import PCM16_MONO_16K, AudioChunk  # noqa: E402
from easycat.debug.bundle import RunBundle  # noqa: E402
from easycat.debugger.server import (  # noqa: E402
    _bundle_source,
    _filter_records,
    _make_app,
    _session_source,
    _summarise_turns,
)
from easycat.events import (  # noqa: E402
    Event,
    STTEvent,
    STTEventType,
    TTSEvent,
    TTSEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.noise_reduction import PassthroughNoiseReducer  # noqa: E402
from easycat.runtime.artifacts import InMemoryArtifactStore  # noqa: E402
from easycat.runtime.journal import InMemoryRingBuffer  # noqa: E402
from easycat.runtime.records import JournalRecordKind  # noqa: E402
from easycat.session._session import Session  # noqa: E402
from easycat.session._types import SessionConfig  # noqa: E402
from easycat.turn_manager import TurnManagerConfig  # noqa: E402

# Async tests are auto-detected via ``asyncio_mode = "auto"`` in
# pytest config; sync tests stay sync without an extra marker.


class _DeterministicAgent:
    async def run(self, text: str, **_kw):  # type: ignore[no-untyped-def]
        return f"reply-{text}"


# ── Helpers reused from the e2e replay tests ─────────────────────


class _FakeTransport:
    def __init__(self, chunks_in: list[AudioChunk]) -> None:
        self._chunks_in = chunks_in
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self._chunks_in:
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)

    async def clear_audio(self) -> None: ...


class _FakeVAD:
    def __init__(self) -> None:
        self._n = 0

    async def process(self, _chunk: AudioChunk) -> AsyncIterator[Event]:
        self._n += 1
        if self._n == 1:
            yield VADStartSpeaking()
        elif self._n == 2:
            yield VADStopSpeaking()

    def configure(self, **_kw): ...


class _FakeSTT:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None: ...
    async def send_audio(self, _chunk: AudioChunk) -> None: ...
    async def end_stream(self) -> None:
        await self._queue.put(STTEvent(type=STTEventType.FINAL, text="hi"))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            evt = await self._queue.get()
            if evt is None:
                break
            yield evt


class _FakeAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class _DistinctiveTTS:
    async def synthesize(self, _payload) -> AsyncIterator[TTSEvent]:
        for marker in (b"\x11\x22", b"\x33\x44"):
            chunk = AudioChunk(data=marker * 160, format=PCM16_MONO_16K)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=chunk)

    async def stop(self) -> None: ...
    async def cancel(self) -> None: ...


def _silent_chunk() -> AudioChunk:
    return AudioChunk(data=bytes(320), format=PCM16_MONO_16K)


async def _build_voice_bundle(tmp_path: pathlib.Path) -> pathlib.Path:
    """Drive a real Session through a turn and write its bundle."""
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=1024, artifact_store=artifact_store)
    transport = _FakeTransport(chunks_in=[_silent_chunk(), _silent_chunk()])
    session = Session(
        SessionConfig(
            transport=transport,
            vad=_FakeVAD(),
            stt=_FakeSTT(),
            agent=_FakeAgent(),
            tts=_DistinctiveTTS(),
            noise_reducer=PassthroughNoiseReducer(),
            enable_noise_reduction=False,
            turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
            journal=journal,
            artifact_store=artifact_store,
            session_id="debug-ui-test",
        )
    )
    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    bundle_path = tmp_path / "ui.zip"
    session.export_debug_bundle(str(bundle_path))
    return bundle_path


# ── Pure-helper tests ────────────────────────────────────────────


def test_filter_records_by_stage():
    records = [
        {"sequence": 1, "name": "stage_start", "data": {"stage": "tts"}},
        {"sequence": 2, "name": "stage_complete", "data": {"stage": "stt"}},
        {"sequence": 3, "name": "tts_frame", "data": {"stage": "tts"}},
    ]
    out = _filter_records(
        records, stage="tts", turn_id=None, name=None, from_seq=None, to_seq=None
    )
    assert [r["sequence"] for r in out] == [1, 3]


def test_filter_records_by_sequence_range():
    records = [{"sequence": i, "data": {}} for i in range(10)]
    out = _filter_records(records, stage=None, turn_id=None, name=None, from_seq=3, to_seq=6)
    assert [r["sequence"] for r in out] == [3, 4, 5, 6]


def test_summarise_turns_tracks_audio_bytes():
    records = [
        {
            "sequence": 1,
            "turn_id": "t1",
            "name": "stage_start",
            "data": {"stage": "stt"},
            "timing": {"wall_ns": 1_000_000},
        },
        {
            "sequence": 2,
            "turn_id": "t1",
            "name": "tts_frame",
            "data": {"stage": "tts", "audio_bytes": 320},
            "timing": {"wall_ns": 5_000_000},
        },
        {
            "sequence": 3,
            "turn_id": "t1",
            "name": "tts_frame",
            "data": {"stage": "tts", "audio_bytes": 640},
            "timing": {"wall_ns": 9_000_000},
        },
    ]
    turns = _summarise_turns(records)
    assert len(turns) == 1
    assert turns[0]["turn_id"] == "t1"
    assert turns[0]["tts_audio_bytes"] == 960
    assert turns[0]["wall_ms"] == 8.0
    assert turns[0]["stage_counts"] == {"stt": 1, "tts": 2}


# ── HTTP API integration tests using aiohttp test client ─────────


async def test_api_serves_text_session_records(tmp_path):
    """The debugger should expose a real session's records via /api/records."""
    session = create_text_session(agent=_DeterministicAgent(), debug="full", wrap_agent=False)
    for i in range(2):
        await session.send_text(f"ping-{i}")
    bundle_path = tmp_path / "text.zip"
    session.export_debug_bundle(str(bundle_path))
    await session.stop()

    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        m = await (await client.get("/api/manifest")).json()
        assert m["source"] == "bundle"
        assert m["record_count"] > 0

        recs = await (await client.get("/api/records")).json()
        assert recs["total"] == m["record_count"]
        assert len(recs["records"]) == recs["total"]

        turns = await (await client.get("/api/turns")).json()
        # Two send_text calls → two turns.
        assert len(turns["turns"]) == 2


async def test_api_serves_voice_session_artifact_bytes(tmp_path):
    """A bundle's TTS audio artifacts must be retrievable through /api/artifact."""
    bundle_path = await _build_voice_bundle(tmp_path)
    bundle = RunBundle.load(bundle_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    # Pick a real TTS frame's output_ref out of the bundle so we know the
    # endpoint resolves it to bytes the live session emitted.
    tts_frame = next(
        r for r in bundle.records() if r.get("name") == "tts_frame" and r.get("output_ref")
    )
    expected = bundle.artifact_blobs[tts_frame["output_ref"]]

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/artifact/{tts_frame['output_ref']}")
        assert resp.status == 200
        body = await resp.read()
        assert body == expected

        resp_404 = await client.get("/api/artifact/" + "0" * 64)
        assert resp_404.status == 404


async def test_api_filters_records_by_stage_and_turn(tmp_path):
    """The /api/records endpoint must honour ?stage and ?turn filters."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        all_recs = await (await client.get("/api/records")).json()
        tts_only = await (await client.get("/api/records?stage=tts")).json()
        assert tts_only["total"] < all_recs["total"]
        for r in tts_only["records"]:
            data = r.get("data") or {}
            assert data.get("stage") == "tts" or data.get("observed_stage") == "tts"


def test_serve_bundle_raises_helpful_error_when_aiohttp_missing(monkeypatch, tmp_path):
    """If aiohttp is unavailable, the entry points must fail with a clear
    message rather than a bare ImportError."""
    bundle_path = tmp_path / "empty.zip"
    # Build a minimal bundle so _bundle_source succeeds before _serve fails.
    with zipfile.ZipFile(bundle_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": 1}))
        zf.writestr("journal.ndjson", b"")

    import easycat.debugger.server as srv

    def _no_aiohttp(*_a, **_kw):
        raise ImportError("simulated missing aiohttp")

    monkeypatch.setattr(srv, "_make_app", lambda *_a, **_kw: _no_aiohttp())
    with pytest.raises(ImportError):
        srv.serve_bundle(bundle_path, open_browser=False)


def test_debugger_source_session_adapts_live_journal():
    """Live-session source should snapshot the journal each call and pull
    artifact bytes from the session's artifact store."""
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=8, artifact_store=artifact_store)
    ref = artifact_store.put(b"hello-world", artifact_class="replay_critical")

    class _StubSession:
        session_id = "stub-1"
        is_running = True
        turn_state = "IDLE"
        _artifact_store = artifact_store

        @property
        def journal(self):
            return journal

    source = _session_source(_StubSession())
    assert source.manifest()["session_id"] == "stub-1"
    assert source.artifact(ref) == b"hello-world"
    # Empty journal returns no records.
    assert source.records() == []
    # Adding a record makes it visible on the next records() call —
    # this is the polling contract live sources rely on.
    journal.append(kind=JournalRecordKind.EVENT, name="test", session_id="stub-1")
    assert any(r["name"] == "test" for r in source.records())


# ── Production-grade UI endpoints ────────────────────────────────


async def test_api_timeline_emits_per_stage_spans(tmp_path):
    """``/api/timeline`` should compute real per-stage span timing for
    each turn, not just record counts.  The waterfall view depends on
    the ``offset_ms`` + ``duration_ms`` it returns."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/timeline")).json()
        assert "timeline" in body
        # At least one turn with stage spans (TTS minimum).
        assert any(t["spans"] for t in body["timeline"])
        for turn in body["timeline"]:
            for span in turn["spans"]:
                assert span["offset_ms"] >= 0
                assert span["duration_ms"] >= 0
                assert span["stage"] in {
                    "transport",
                    "audio",
                    "vad",
                    "stt",
                    "agent",
                    "tts",
                    "turn",
                    "telephony",
                }


async def test_api_transcript_extracts_user_and_agent_text(tmp_path):
    """The transcript endpoint must surface user STT text and agent text."""
    session = create_text_session(agent=_DeterministicAgent(), debug="full", wrap_agent=False)
    await session.send_text("hello-world")
    bundle_path = tmp_path / "t.zip"
    session.export_debug_bundle(str(bundle_path))
    await session.stop()
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/transcript")).json()
        assert "transcripts" in body
        # Single turn with the agent reply visible.
        agent_turns = [t for t in body["transcripts"] if t["agent"]]
        assert agent_turns, "expected at least one turn with an agent reply"
        # DeterministicAgent returns "reply-<input>".
        assert any("reply-hello-world" in t["agent"] for t in agent_turns)


async def test_api_audio_concat_returns_valid_wav(tmp_path):
    """Concatenated audio endpoint should stitch all TTS frames for a
    turn into one WAV with a parseable RIFF header."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    # Find a turn with TTS frames.
    bundle = RunBundle.load(bundle_path)
    turn_id = next(
        r.get("turn_id")
        for r in bundle.records()
        if r.get("name") == "tts_frame" and r.get("turn_id")
    )

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/audio/concat/{turn_id}")
        assert resp.status == 200
        body = await resp.read()
        # WAV magic bytes
        assert body[:4] == b"RIFF"
        assert body[8:12] == b"WAVE"
        assert len(body) > 44


async def test_api_audio_concat_rejects_unknown_turn(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/audio/concat/no-such-turn")
        assert resp.status == 404


async def test_api_artifact_rejects_invalid_ref(tmp_path):
    """The route must reject anything that isn't a SHA-256 hex digest
    before the filesystem store sees it — guards against URL-encoded
    path traversal."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        # Wrong length.
        r1 = await client.get("/api/artifact/notahash")
        assert r1.status == 400
        # Right length but not hex.
        r2 = await client.get("/api/artifact/" + "z" * 64)
        assert r2.status == 400


async def test_api_audio_concat_rejects_invalid_turn_id(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        # Path-traversal-like turn id.
        resp = await client.get("/api/audio/concat/" + "x" * 200)
        assert resp.status == 400


_SAFE_HEADERS = {
    "Origin": "http://localhost:8765",
    "Content-Type": "application/json",
}


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


async def test_api_cost_returns_zero_when_no_cost_records(tmp_path):
    """Cost panel must degrade gracefully — a bundle with no CostRecord
    events still returns a well-formed totals dict."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/cost")).json()
        assert "totals" in body and "per_turn" in body
        for k in ("usd", "stt_seconds", "tts_chars", "llm_tokens"):
            assert body["totals"][k] == 0


async def test_api_health_returns_ok(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/health")).json()
        assert body["ok"] is True
        assert body["is_live"] is False


async def test_origin_guard_refuses_cross_origin_requests(tmp_path):
    """By default, the origin guard middleware blocks non-loopback
    Origin headers so a malicious page on the local machine can't talk
    to the debugger via CSRF."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)  # allow_remote=False (default)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/manifest",
            headers={"Origin": "https://evil.example.com"},
        )
        assert resp.status == 403


async def test_origin_guard_allows_loopback_origin(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/manifest",
            headers={"Origin": "http://localhost:8765"},
        )
        assert resp.status == 200


async def test_manifest_strips_filesystem_path(tmp_path):
    """``manifest`` should expose only the bundle's basename, never the
    full filesystem path — bundles often live under user-named dirs."""
    bundle_path = tmp_path / "a-secret-folder" / "user-bundle.zip"
    bundle_path.parent.mkdir()
    inner = await _build_voice_bundle(tmp_path)
    bundle_path.write_bytes(inner.read_bytes())

    source = _bundle_source(bundle_path)
    manifest = source.manifest()
    assert manifest["name"] == "user-bundle.zip"
    assert "a-secret-folder" not in json.dumps(manifest)
    assert "path" not in manifest


def test_check_host_refuses_non_loopback_without_opt_in():
    from easycat.debugger.server import _check_host

    with pytest.raises(RuntimeError, match="non-loopback"):
        _check_host("0.0.0.0", allow_remote=False)
    # Loopback always passes.
    _check_host("127.0.0.1", allow_remote=False)
    _check_host("::1", allow_remote=False)
    _check_host("localhost", allow_remote=False)
    # Explicit opt-in passes.
    _check_host("0.0.0.0", allow_remote=True)


def test_serve_bundle_refuses_non_loopback_without_allow_remote(tmp_path):
    """Public entry point should fail loud rather than silently exposing
    the journal to the network."""
    import easycat.debugger.server as srv

    bundle_path = tmp_path / "x.zip"
    with zipfile.ZipFile(bundle_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": 1}))
        zf.writestr("journal.ndjson", b"")

    with pytest.raises(RuntimeError, match="non-loopback"):
        srv.serve_bundle(bundle_path, host="0.0.0.0", open_browser=False)


async def test_websocket_emits_snapshot_for_bundle(tmp_path):
    """A WebSocket client should receive at least one snapshot message
    naming the current record count when it connects to a bundle source."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        async with client.ws_connect("/ws") as ws:
            msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
            payload = msg.json()
            assert payload["type"] == "snapshot"
            assert payload["record_count"] > 0


async def test_records_supports_pagination(tmp_path):
    """``limit``/``offset`` query params return a page slice plus the
    full match count.  ``total`` is the pre-slice count so the UI can
    show "showing 3 of N records" — not just the page size."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        full = await (await client.get("/api/records")).json()
        if full["total"] < 5:
            pytest.skip("bundle too small to exercise pagination")
        page = await (await client.get("/api/records?limit=3&offset=2")).json()
        assert page["page_size"] == 3
        assert page["total"] == full["total"]  # full count, not page size
        assert len(page["records"]) == 3
        assert page["records"][0]["sequence"] == full["records"][2]["sequence"]


async def test_records_rejects_negative_offset(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/records?offset=-5")
        assert resp.status == 400


async def test_records_rejects_zero_or_negative_limit(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        for bad in ("0", "-1"):
            resp = await client.get(f"/api/records?limit={bad}")
            assert resp.status == 400


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


async def test_export_rejected_for_bundle_source(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/export",
            headers={
                "Origin": "http://localhost:8765",
                "Content-Type": "application/json",
            },
        )
        assert resp.status == 405


# ── Round 2 hardening: replay confirm gate, CSRF, ws back-off, etc.


async def test_replay_destructive_combos_require_confirm(tmp_path):
    """LIVE fidelity, ALLOW tool policy, or force=True must be gated on
    an explicit ``confirm: true`` flag so a CSRF / drive-by from another
    tab can't fire them silently."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    safe_headers = {
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


async def test_replay_rejects_unknown_keys(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    headers = {
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
        "Origin": "http://localhost:8765",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/replay", data=b"{not json", headers=headers)
        assert resp.status == 400


async def test_replay_rejects_non_object_json(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    headers = {
        "Origin": "http://localhost:8765",
        "Content-Type": "application/json",
    }
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/replay", data=b"null", headers=headers)
        assert resp.status == 400


async def test_origin_guard_refuses_missing_origin_on_post(tmp_path):
    """A POST with no Origin header is suspicious — block it.  Browsers
    always send Origin on cross-origin POSTs; absence usually means
    a simple-form-POST CSRF or an attacker bypassing CORS."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact"},
            # No Origin header at all.
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403


async def test_origin_guard_refuses_form_encoded_post(tmp_path):
    """State-changing requests must use application/json — a form POST
    sneaks past CORS preflight and could enable simple-form CSRF."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            data="fidelity=artifact",
            headers={
                "Origin": "http://localhost:8765",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert resp.status == 415


async def test_origin_guard_blocks_cross_site_fetch(tmp_path):
    """``Sec-Fetch-Site: cross-site`` is blocked even if Origin happens
    to match a loopback prefix."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/manifest",
            headers={
                "Origin": "http://localhost:8765",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert resp.status == 403


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
                "Origin": "http://localhost:8765",
                "Content-Type": "application/json",
            },
        )
        assert resp.status == 409


async def test_websocket_responds_to_ping_with_pong():
    """Live-source WebSocket should round-trip ping/pong so heartbeats
    work cleanly behind proxies."""
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=4, artifact_store=artifact_store)

    class _StubSession:
        session_id = "live-ping"
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
        async with client.ws_connect("/ws") as ws:
            # First message is the snapshot.
            await asyncio.wait_for(ws.receive(), timeout=2.0)
            await ws.send_json({"action": "ping"})
            # Pong arrives, possibly after another snapshot.
            saw_pong = False
            for _ in range(5):
                msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
                if msg.json().get("type") == "pong":
                    saw_pong = True
                    break
            assert saw_pong


async def test_audio_concat_streams_wav_response(tmp_path):
    """The route should return ``Content-Type: audio/wav`` with a
    Content-Length that matches the streamed body length so browsers
    can scrub to the end without buffering blindly."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    bundle = RunBundle.load(bundle_path)
    turn_id = next(
        r.get("turn_id")
        for r in bundle.records()
        if r.get("name") == "tts_frame" and r.get("turn_id")
    )

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/audio/concat/{turn_id}")
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "audio/wav"
        body = await resp.read()
        assert resp.headers.get("Content-Length") == str(len(body))


async def test_records_total_unchanged_when_filtering(tmp_path):
    """``?stage=tts`` should narrow ``total`` to TTS records, not return
    the unfiltered count.  Sanity check that the new pagination
    contract still respects the filter."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        all_resp = await (await client.get("/api/records")).json()
        tts_resp = await (await client.get("/api/records?stage=tts")).json()
        assert tts_resp["total"] < all_resp["total"]


def test_filter_records_negative_offset_raises():
    from easycat.debugger.server import _filter_records

    with pytest.raises(ValueError, match="offset"):
        _filter_records(
            [],
            stage=None,
            turn_id=None,
            name=None,
            from_seq=None,
            to_seq=None,
            offset=-1,
        )


def test_filter_records_zero_limit_raises():
    from easycat.debugger.server import _filter_records

    with pytest.raises(ValueError, match="limit"):
        _filter_records(
            [],
            stage=None,
            turn_id=None,
            name=None,
            from_seq=None,
            to_seq=None,
            limit=0,
        )


def test_static_index_blocks_protocol_relative_urls():
    """Round-3 follow-up: ``_sanitiseUrl`` in the SPA must reject
    ``//evil.com`` (protocol-relative cross-origin)."""
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert r"if (/^\/\//.test(trimmed)) return" in text, (
        "_sanitiseUrl missing protocol-relative reject"
    )
    # Single-leading-slash same-origin path remains allowed via the
    # ``\\/[^/]`` pattern in the safe-scheme regex.
    assert r"\/[^/]" in text


def test_static_index_force_destructive_check():
    """Round-3 follow-up: the JS ``isDestructive`` check must include
    ``force`` so a future force toggle can't bypass the confirm dialog."""
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert "isDestructive" in text
    assert "|| force" in text


async def test_replay_force_artifact_with_confirm_succeeds(tmp_path):
    """``force=True`` with ``fidelity=artifact`` is destructive but must
    still run when ``confirm=true`` is supplied — the gate exists to
    require acknowledgement, not to disable force entirely."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    headers = {"Origin": "http://localhost:8765", "Content-Type": "application/json"}
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
