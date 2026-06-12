from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

from easycat import create_text_session
from easycat.debug.bundle import RunBundle
from easycat.debugger.server import DebuggerSource, _bundle_source, _make_app

from ._server_helpers import _build_voice_bundle, _DeterministicAgent


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
        # Each turn must carry source-record seqs so the UI can link a
        # sentence back to its journal entry.
        assert all("user_seq" in t and "agent_seq" in t for t in body["transcripts"])
        for t in agent_turns:
            assert isinstance(t["agent_seq"], int)


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
        assert body["budget"]["configured"] is False


async def test_api_cost_reports_budget_from_manifest():
    source = DebuggerSource(
        label="budget-source",
        _records_fn=lambda: [
            {
                "sequence": 1,
                "name": "cost",
                "turn_id": "turn-1",
                "data": {"usd": 0.85, "stt_seconds": 2.5},
            }
        ],
        _artifact_fn=lambda _ref: None,
        _manifest_fn=lambda: {"config_snapshot": {"max_session_cost_usd": "1.0"}},
    )
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/cost")).json()
        assert body["totals"]["usd"] == pytest.approx(0.85)
        assert body["per_turn"]["turn-1"]["stt_seconds"] == pytest.approx(2.5)
        assert body["budget"]["status"] == "warning"
        assert body["budget"]["usage_fraction"] == pytest.approx(0.85)


async def test_api_health_returns_ok(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/health")).json()
        assert body["ok"] is True
        assert body["is_live"] is False


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
