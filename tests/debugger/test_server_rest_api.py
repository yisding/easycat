from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

from easycat import create_text_session
from easycat.debug.bundle import RunBundle
from easycat.debugger import server as _server
from easycat.debugger.server import (
    DebuggerSource,
    _bundle_source,
    _coerce_frames_to_format,
    _collect_audio_frames,
    _collect_concat_pcm,
    _make_app,
)

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


async def test_api_records_full_text_search_filters(tmp_path):
    """``/api/records?q=`` must full-text filter and stay a subset of all records."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        all_recs = await (await client.get("/api/records?limit=10000")).json()
        # ``tts`` appears in the stage/name of TTS records but not every record.
        hits = await (await client.get("/api/records?q=tts&limit=10000")).json()
        assert 0 < hits["total"] <= all_recs["total"]
        assert hits["scan_truncated"] is False
        for r in hits["records"]:
            assert r.get("_match_fields")


async def test_api_records_rejects_invalid_regex(tmp_path):
    """``/api/records?regex=1&q=[`` must return 400, not a 500."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/records?regex=1&q=%5B")
        assert resp.status == 400
        body = await resp.text()
        assert "invalid regex" in body


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


async def test_api_issues_serves_severity_rollup(tmp_path):
    """``/api/issues`` serves the ``{issues, summary, total}`` rollup."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/issues")).json()
        assert set(body) == {"issues", "summary", "total"}
        assert set(body["summary"]) == {"error", "warning", "info"}
        assert body["total"] == len(body["issues"])


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


async def test_api_audio_concat_mic_track_returns_caller_wav(tmp_path):
    """``?track=mic`` stitches the STT stage's captured input into a WAV."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    bundle = RunBundle.load(bundle_path)
    turn_id = next(
        r.get("turn_id")
        for r in bundle.records()
        if r.get("name") == "stage_start"
        and (r.get("data") or {}).get("stage") == "stt"
        and r.get("input_ref")
        and r.get("turn_id")
    )

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/audio/concat/{turn_id}?track=mic")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "audio/wav"
        body = await resp.read()
        assert body[:4] == b"RIFF"
        assert body[8:12] == b"WAVE"
        assert len(body) > 44


async def test_api_audio_concat_rejects_unknown_track(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        turn_id = next(
            r.get("turn_id")
            for r in RunBundle.load(bundle_path).records()
            if r.get("name") == "tts_frame" and r.get("turn_id")
        )
        resp = await client.get(f"/api/audio/concat/{turn_id}?track=bogus")
        assert resp.status == 400


def _audio_source(records, blobs):
    """Build a synthetic ``DebuggerSource`` over in-memory records/artifacts."""
    return DebuggerSource(
        label="audio-source",
        _records_fn=lambda: records,
        _artifact_fn=lambda ref: blobs.get(ref),
        _manifest_fn=lambda: {},
    )


async def test_api_audio_concat_tts_format_mismatch_raises_409(tmp_path):
    """The strict TTS track must refuse to splice differing PCM formats."""
    records = [
        {
            "sequence": 1,
            "name": "tts_frame",
            "turn_id": "t1",
            "output_ref": "a",
            "data": {"sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
        {
            "sequence": 2,
            "name": "tts_frame",
            "turn_id": "t1",
            "output_ref": "b",
            "data": {"sample_rate": 24000, "channels": 1, "sample_width": 2},
        },
    ]
    source = _audio_source(records, {"a": b"\x00" * 320, "b": b"\x01" * 320})
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/audio/concat/t1")
        assert resp.status == 409


async def test_api_audio_concat_mic_skips_format_mismatch_without_raising(tmp_path):
    """The lenient mic track drops format-mismatched blobs instead of 409.

    The first frame matches the chosen format; the second has a different
    sample width (no audioop conversion path), so it is silently skipped and
    the response is still a valid WAV built from the surviving frame.
    """
    records = [
        {
            "sequence": 1,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "a",
            "data": {"stage": "stt", "sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
        {
            "sequence": 2,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "b",
            "data": {"stage": "stt", "sample_rate": 16000, "channels": 1, "sample_width": 1},
        },
    ]
    source = _audio_source(records, {"a": b"\x00" * 320, "b": b"\x01" * 160})
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/audio/concat/t1?track=mic")
        assert resp.status == 200
        body = await resp.read()
        assert body[:4] == b"RIFF"
        # Only the first (matching) frame survives: 44-byte header + 320 bytes.
        assert len(body) == 44 + 320


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


async def test_api_audio_waveform_returns_png(tmp_path):
    """``GET /api/audio/waveform/{turn}?track=tts`` returns an image/png."""
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
        resp = await client.get(f"/api/audio/waveform/{turn_id}?track=tts&w=600&h=80")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.headers["Cache-Control"] == "no-store"
        body = await resp.read()
        assert body[:8] == _PNG_SIGNATURE
        # IHDR chunk must parse with the requested dimensions.
        import struct

        width, height = struct.unpack(">II", body[16:24])
        assert (width, height) == (600, 80)


async def test_api_audio_waveform_mic_track(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    bundle = RunBundle.load(bundle_path)
    turn_id = next(
        r.get("turn_id")
        for r in bundle.records()
        if r.get("name") == "stage_start"
        and (r.get("data") or {}).get("stage") == "stt"
        and r.get("input_ref")
        and r.get("turn_id")
    )
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/audio/waveform/{turn_id}?track=mic&w=120&h=40")
        assert resp.status == 200
        body = await resp.read()
        assert body[:8] == _PNG_SIGNATURE


async def test_api_audio_waveform_rejects_unknown_turn(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/audio/waveform/no-such-turn")
        assert resp.status == 404


async def test_api_audio_waveform_rejects_invalid_turn_id(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/audio/waveform/" + "x" * 200)
        assert resp.status == 400


async def test_api_audio_waveform_rejects_invalid_track(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        turn_id = next(
            r.get("turn_id")
            for r in RunBundle.load(bundle_path).records()
            if r.get("name") == "tts_frame" and r.get("turn_id")
        )
        resp = await client.get(f"/api/audio/waveform/{turn_id}?track=bogus")
        assert resp.status == 400


async def test_api_audio_waveform_clamps_dimensions(tmp_path):
    """Out-of-range ``w``/``h`` are clamped, not rejected, so a hostile or
    fat-fingered query can't allocate an enormous canvas."""
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
        resp = await client.get(f"/api/audio/waveform/{turn_id}?w=99999&h=99999")
        assert resp.status == 200
        body = await resp.read()
        import struct

        width, height = struct.unpack(">II", body[16:24])
        assert (width, height) == (2000, 400)


def test_collect_concat_pcm_joins_frames():
    """``_collect_concat_pcm`` returns one joined PCM blob plus the format."""
    records = [
        {
            "sequence": 1,
            "name": "tts_frame",
            "turn_id": "t1",
            "output_ref": "a",
            "data": {"sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
        {
            "sequence": 2,
            "name": "tts_frame",
            "turn_id": "t1",
            "output_ref": "b",
            "data": {"sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
    ]
    blobs = {"a": b"\x01\x02", "b": b"\x03\x04"}
    source = DebuggerSource(
        label="concat-source",
        _records_fn=lambda: records,
        _artifact_fn=lambda ref: blobs.get(ref),
        _manifest_fn=lambda: {},
    )
    pcm, fmt = _collect_concat_pcm(source, "t1", track="tts")
    assert pcm == b"\x01\x02\x03\x04"
    assert fmt == {"sample_rate": 16000, "channels": 1, "sample_width": 2}
    # Unknown turn → empty pcm, empty fmt.
    empty_pcm, empty_fmt = _collect_concat_pcm(source, "nope", track="tts")
    assert empty_pcm == b"" and empty_fmt == {}


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


def test_coerce_frames_to_format_strict_raises_on_mismatch():
    """The strict (TTS) path raises ValueError on a format mismatch."""
    fmt = {"sample_rate": 16000, "channels": 1, "sample_width": 2}
    frames = [
        (1, b"\x00" * 320, {"sample_rate": 16000, "channels": 1, "sample_width": 2}),
        (2, b"\x01" * 320, {"sample_rate": 24000, "channels": 1, "sample_width": 2}),
    ]
    with pytest.raises(ValueError):
        _coerce_frames_to_format(frames, fmt, strict=True)


def test_coerce_frames_to_format_lenient_resamples_with_audioop():
    """Lenient (mic) path resamples a differing rate when audioop is present."""
    if _server._audioop is None:  # pragma: no cover - 3.13+ path
        pytest.skip("audioop unavailable on this interpreter")
    fmt = {"sample_rate": 16000, "channels": 1, "sample_width": 2}
    frames = [
        (1, b"\x00" * 320, {"sample_rate": 16000, "channels": 1, "sample_width": 2}),
        (2, b"\x02" * 320, {"sample_rate": 8000, "channels": 1, "sample_width": 2}),
    ]
    blobs, dropped = _coerce_frames_to_format(frames, fmt, strict=False)
    # Both frames survive: the second is upsampled (8k -> 16k) rather than
    # dropped, so we get two blobs and the resampled one is longer.
    assert dropped == 0
    assert len(blobs) == 2
    assert len(blobs[1]) > len(frames[1][1])


def test_coerce_frames_to_format_lenient_skips_when_audioop_missing(monkeypatch):
    """When audioop is unavailable, a width mismatch is skipped, not raised."""
    monkeypatch.setattr(_server, "_audioop", None)
    fmt = {"sample_rate": 16000, "channels": 1, "sample_width": 2}
    frames = [
        (1, b"\x00" * 320, {"sample_rate": 16000, "channels": 1, "sample_width": 2}),
        (2, b"\x01" * 160, {"sample_rate": 16000, "channels": 1, "sample_width": 1}),
    ]
    blobs, dropped = _coerce_frames_to_format(frames, fmt, strict=False)
    assert blobs == [frames[0][1]]
    assert dropped == 1


def test_collect_audio_frames_mic_selects_stt_stage_start():
    """The mic track picks STT stage_start input_ref frames, ordered by seq."""
    records = [
        {
            "sequence": 5,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "b",
            "data": {"stage": "stt", "sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
        {
            "sequence": 1,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "a",
            "data": {"stage": "stt", "sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
        # A non-STT stage_start and a tts_frame must be ignored on the mic track.
        {
            "sequence": 2,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "vad",
            "data": {"stage": "vad", "sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
        {
            "sequence": 3,
            "name": "tts_frame",
            "turn_id": "t1",
            "output_ref": "tts",
            "data": {"sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
    ]
    blobs = {"a": b"AA", "b": b"BB", "vad": b"VV", "tts": b"TT"}
    source = DebuggerSource(
        label="mic-source",
        _records_fn=lambda: records,
        _artifact_fn=lambda ref: blobs.get(ref),
        _manifest_fn=lambda: {},
    )
    frames, fmt = _collect_audio_frames(source, "t1", track="mic")
    assert frames == [b"AA", b"BB"]  # seq 1 then seq 5, vad/tts excluded
    assert fmt == {"sample_rate": 16000, "channels": 1, "sample_width": 2}
