from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

from easycat import create_text_session
from easycat.debug.bundle import RunBundle
from easycat.debugger import _audio
from easycat.debugger import server as debugger_server
from easycat.debugger._audio import _coerce_frames_to_format
from easycat.debugger._sources import DebuggerSource, _bundle_source
from easycat.debugger.server import (
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


async def test_api_serves_issue_rollup(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        payload = await (await client.get("/api/issues")).json()
        assert set(payload) == {"issues", "summary", "total"}
        assert payload["total"] == len(payload["issues"])
        assert {"error", "warning", "info"} <= set(payload["summary"])


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


async def test_api_records_regex_search_matches(tmp_path):
    """``/api/records?regex=1&q=`` must regex-filter (search runs off the loop)."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        # Plain substring baseline for the same needle.
        plain = await (await client.get("/api/records?q=tts&limit=10000")).json()
        # ``t.s`` regex-matches ``tts`` (and any t-?-s run); the offloaded
        # asyncio.to_thread scan must return the same kind of hits as the
        # inline path did, with annotated ``_match_fields``.
        regex = await (await client.get("/api/records?regex=1&q=t.s&limit=10000")).json()
        assert regex["total"] >= plain["total"] > 0
        assert regex["scan_truncated"] is False
        for r in regex["records"]:
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
        _manifest_fn=dict,
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


async def test_api_audio_concat_mic_unsupported_width_returns_415(tmp_path):
    """A real 8-bit (mu-law telephony) mic capture must surface an explicit 415.

    The unsupported ``sample_width == 1`` is preserved through format collection
    so the route reports an unsupported format, rather than silently rewriting
    it to the 16-bit default — which would drop every blob and 404 instead.
    """
    records = [
        {
            "sequence": 1,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "a",
            "data": {"stage": "stt", "sample_rate": 8000, "channels": 1, "sample_width": 1},
        },
    ]
    source = _audio_source(records, {"a": b"\x55" * 160})
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/audio/concat/t1?track=mic")
        assert resp.status == 415
        payload = await resp.json()
        assert payload["unsupported"] is True
        assert payload["format"]["sample_width"] == 1


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


async def test_api_audio_waveform_accepts_pcm_at_memory_limit(monkeypatch):
    monkeypatch.setattr(debugger_server, "_WAVEFORM_MAX_PCM_BYTES", 4)
    records = [
        {
            "sequence": sequence,
            "name": "tts_frame",
            "turn_id": "t1",
            "output_ref": ref,
            "data": {"sample_rate": 16000, "channels": 1, "sample_width": 2},
        }
        for sequence, ref in enumerate(("a", "b"), start=1)
    ]
    app = _make_app(_audio_source(records, {"a": b"\x00\x00", "b": b"\x01\x00"}))
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/audio/waveform/t1?w=2&h=2")

    assert resp.status == 200


async def test_api_audio_waveform_rejects_pcm_over_memory_limit(monkeypatch):
    monkeypatch.setattr(debugger_server, "_WAVEFORM_MAX_PCM_BYTES", 4)
    records = [
        {
            "sequence": sequence,
            "name": "tts_frame",
            "turn_id": "t1",
            "output_ref": ref,
            "data": {"sample_rate": 16000, "channels": 1, "sample_width": 2},
        }
        for sequence, ref in enumerate(("a", "b"), start=1)
    ]
    app = _make_app(_audio_source(records, {"a": b"\x00\x00", "b": b"\x01\x00\x02\x00"}))
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/audio/waveform/t1")
        payload = await resp.json()

    assert resp.status == 413
    assert payload == {
        "error_code": "WAVEFORM_AUDIO_TOO_LARGE",
        "message": "Waveform audio exceeds the in-memory rendering limit",
        "max_pcm_bytes": 4,
    }


async def test_api_audio_waveform_rejects_pcm_expanded_over_memory_limit(monkeypatch):
    monkeypatch.setattr(debugger_server, "_WAVEFORM_MAX_PCM_BYTES", 4)

    def expand_after_coercion(frames, fmt, *, strict):
        _ = frames, fmt, strict
        return [b"\x00" * 5], 0

    monkeypatch.setattr(debugger_server, "_coerce_frames_to_format", expand_after_coercion)
    records = [
        {
            "sequence": 1,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "a",
            "data": {
                "stage": "stt",
                "sample_rate": 16000,
                "channels": 1,
                "sample_width": 2,
            },
        }
    ]
    app = _make_app(_audio_source(records, {"a": b"\x00" * 4}))
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/audio/waveform/t1?track=mic")
        payload = await resp.json()

    assert resp.status == 413
    assert payload == {
        "error_code": "WAVEFORM_AUDIO_TOO_LARGE",
        "message": "Waveform audio exceeds the in-memory rendering limit",
        "max_pcm_bytes": 4,
    }


async def test_api_audio_waveform_mic_unsupported_width_returns_415(tmp_path):
    """The waveform route must also 415 on an unsupported 8-bit telephony capture."""
    records = [
        {
            "sequence": 1,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "a",
            "data": {"stage": "stt", "sample_rate": 8000, "channels": 1, "sample_width": 1},
        },
    ]
    source = _audio_source(records, {"a": b"\x55" * 160})
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/audio/waveform/t1?track=mic")
        assert resp.status == 415
        payload = await resp.json()
        assert payload["unsupported"] is True


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


def test_collect_audio_frames_drops_untrusted_resample_metadata(monkeypatch):
    """Malicious bundle metadata must not reach a resampler."""
    records = [
        {
            "sequence": 1,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "a",
            "data": {
                "stage": "stt",
                "sample_rate": float("inf"),
                "channels": 1,
                "sample_width": 2,
            },
        },
        {
            "sequence": 2,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "b",
            "data": {"stage": "stt", "sample_rate": 1, "channels": 1, "sample_width": 2},
        },
    ]
    source = _audio_source(records, {"a": b"\x00\x00", "b": b"\x00\x00"})

    def fail_ratecv(*_args, **_kwargs):
        raise AssertionError("untrusted metadata reached numpy resampler")

    monkeypatch.setattr(_audio, "_audioop", None)
    monkeypatch.setattr(_audio, "_np", object())
    monkeypatch.setattr(_audio, "_np_ratecv", fail_ratecv)

    blobs, fmt = _collect_audio_frames(source, "t1", track="mic")

    assert blobs == []
    assert fmt == {"sample_rate": 16000, "channels": 1, "sample_width": 2}


def test_collect_audio_frames_skips_malformed_sequence_values():
    records = [
        {
            "sequence": "bad",
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "bad",
            "data": {"stage": "stt", "sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
        {
            "sequence": True,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "bool",
            "data": {"stage": "stt", "sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
        {
            "sequence": 2,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "ok",
            "data": {"stage": "stt", "sample_rate": 16000, "channels": 1, "sample_width": 2},
        },
    ]
    source = _audio_source(records, {"bad": b"NO", "bool": b"NO", "ok": b"OK"})

    blobs, fmt = _collect_audio_frames(source, "t1", track="mic")

    assert blobs == [b"OK"]
    assert fmt == {"sample_rate": 16000, "channels": 1, "sample_width": 2}


def test_np_ratecv_rejects_oversized_resampled_output(monkeypatch):
    """The numpy fallback bounds output bytes before allocating interpolation arrays."""
    if _audio._np is None:
        pytest.skip("numpy fallback is unavailable")
    monkeypatch.setattr(_audio, "_AUDIO_MAX_CONVERTED_FRAME_BYTES", 4)

    with pytest.raises(ValueError, match="exceeds debugger size limit"):
        _audio._np_ratecv(b"\x00\x00\x00\x00", 2, 1, 1_000, 2_000)


def test_coerce_frames_to_format_rejects_oversized_audioop_resample(monkeypatch):
    """The shared coercion path bounds output bytes before audioop.ratecv."""

    class FailingAudioop:
        def ratecv(self, *_args, **_kwargs):
            raise AssertionError("audioop.ratecv should not receive oversized input")

    monkeypatch.setattr(_audio, "_AUDIO_MAX_CONVERTED_FRAME_BYTES", 4)
    monkeypatch.setattr(_audio, "_audioop", FailingAudioop())
    monkeypatch.setattr(_audio, "_np", None)

    fmt = {"sample_rate": 16000, "channels": 1, "sample_width": 2}
    frames = [
        (1, b"\x00\x00\x00\x00", {"sample_rate": 8000, "channels": 1, "sample_width": 2}),
    ]

    with pytest.raises(ValueError, match="exceeds debugger size limit"):
        _coerce_frames_to_format(frames, fmt, strict=False)


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
        _manifest_fn=dict,
    )
    pcm, fmt = _collect_concat_pcm(source, "t1", track="tts")
    assert pcm == b"\x01\x02\x03\x04"
    assert fmt == {"sample_rate": 16000, "channels": 1, "sample_width": 2}
    # Unknown turn → empty pcm, empty fmt.
    empty_pcm, empty_fmt = _collect_concat_pcm(source, "nope", track="tts")
    assert empty_pcm == b"" and empty_fmt == {}


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


async def test_records_rejects_non_integer_range_and_pagination_values(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        for name in ("from", "to", "limit", "offset"):
            resp = await client.get(f"/api/records?{name}=invalid")
            assert resp.status == 400
            assert await resp.text() == "from/to/limit/offset must be integers"


async def test_records_rejects_negative_offset(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/records?offset=-5")
        assert resp.status == 400
        assert await resp.text() == "invalid query parameters"


async def test_records_rejects_zero_or_negative_limit(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        for bad in ("0", "-1"):
            resp = await client.get(f"/api/records?limit={bad}")
            assert resp.status == 400
            assert await resp.text() == "invalid query parameters"


async def test_records_search_rejects_negative_offset(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/records?q=tts&offset=-5")
        assert resp.status == 400


async def test_records_search_rejects_zero_or_negative_limit(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        for bad in ("0", "-1"):
            resp = await client.get(f"/api/records?q=tts&limit={bad}")
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
    if _audio._audioop is None:  # pragma: no cover - 3.13+ path
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


def test_coerce_frames_to_format_lenient_drops_unsafe_resample_ratio():
    """Mic coercion must not hand attacker-controlled extreme ratios to audioop."""
    fmt = {"sample_rate": 16000, "channels": 1, "sample_width": 2}
    frames = [
        (1, b"\x00" * 320, {"sample_rate": 16000, "channels": 1, "sample_width": 2}),
        (2, b"\x02" * 4, {"sample_rate": 1, "channels": 1, "sample_width": 2}),
    ]
    blobs, dropped = _coerce_frames_to_format(frames, fmt, strict=False)
    assert blobs == [frames[0][1]]
    assert dropped == 1


def test_coerce_frames_to_format_lenient_skips_when_no_helper(monkeypatch):
    """When no audio helper is available, a format mismatch is skipped, not raised."""
    monkeypatch.setattr(_audio, "_audioop", None)
    monkeypatch.setattr(_audio, "_np", None)
    fmt = {"sample_rate": 16000, "channels": 1, "sample_width": 2}
    frames = [
        (1, b"\x00" * 320, {"sample_rate": 16000, "channels": 1, "sample_width": 2}),
        (2, b"\x02" * 320, {"sample_rate": 8000, "channels": 1, "sample_width": 2}),
    ]
    blobs, dropped = _coerce_frames_to_format(frames, fmt, strict=False)
    assert blobs == [frames[0][1]]
    assert dropped == 1


def test_coerce_frames_to_format_lenient_resamples_with_numpy(monkeypatch):
    """Lenient (mic) path resamples a differing rate via numpy when audioop is absent."""
    if _audio._np is None:  # pragma: no cover
        pytest.skip("numpy unavailable")
    monkeypatch.setattr(_audio, "_audioop", None)
    fmt = {"sample_rate": 16000, "channels": 1, "sample_width": 2}
    frames = [
        (1, b"\x00" * 320, {"sample_rate": 16000, "channels": 1, "sample_width": 2}),
        (2, b"\x02" * 320, {"sample_rate": 8000, "channels": 1, "sample_width": 2}),
    ]
    blobs, dropped = _coerce_frames_to_format(frames, fmt, strict=False)
    assert dropped == 0
    assert len(blobs) == 2
    assert len(blobs[1]) > len(frames[1][1])


def test_np_pcm_dtype_uses_little_endian_specs(monkeypatch):
    """Multi-byte PCM artifacts are little-endian even on big-endian hosts."""
    requested: list[str] = []

    class FakeNumpy:
        def dtype(self, spec: str) -> str:
            requested.append(spec)
            return f"dtype:{spec}"

    monkeypatch.setattr(_audio, "_np", FakeNumpy())

    assert _audio._np_pcm_dtype(1) == "dtype:int8"
    assert _audio._np_pcm_dtype(2) == "dtype:<i2"
    assert _audio._np_pcm_dtype(4) == "dtype:<i4"
    assert requested == ["int8", "<i2", "<i4"]


def test_np_tomono_uses_wide_sum_for_int32_peak_values():
    """The numpy fallback must not overflow before averaging int32 stereo samples."""
    if _audio._np is None:  # pragma: no cover
        pytest.skip("numpy unavailable")
    import struct

    peak = 2_147_483_647
    data = struct.pack("<ii", peak, peak)

    mono = _audio._np_tomono(data, 4)

    assert struct.unpack("<i", mono) == (peak,)


def test_collect_audio_frames_mic_falls_back_from_unsafe_target_format():
    """The target WAV format is bounded before using untrusted mic metadata."""
    records = [
        {
            "sequence": 1,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "a",
            "data": {"stage": "stt", "sample_rate": 10_000_000, "channels": 1, "sample_width": 2},
        },
        {
            "sequence": 2,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "b",
            "data": {"stage": "stt", "sample_rate": 1, "channels": 1, "sample_width": 2},
        },
    ]
    source = DebuggerSource(
        label="mic-source",
        _records_fn=lambda: records,
        _artifact_fn=lambda ref: {"a": b"\x00\x00", "b": b"\x01" * 4}.get(ref),
        _manifest_fn=dict,
    )
    frames, fmt = _collect_audio_frames(source, "t1", track="mic")
    assert frames == []
    assert fmt == {"sample_rate": 16000, "channels": 1, "sample_width": 2}


def test_collect_audio_frames_mic_preserves_unsupported_width():
    """An 8-bit telephony capture keeps its width (rate stays bounded) so the
    route can return 415 instead of dropping every blob and 404'ing."""
    records = [
        {
            "sequence": 1,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "a",
            "data": {"stage": "stt", "sample_rate": 8000, "channels": 1, "sample_width": 1},
        },
    ]
    source = DebuggerSource(
        label="mic-source",
        _records_fn=lambda: records,
        _artifact_fn=lambda ref: {"a": b"\x55" * 160}.get(ref),
        _manifest_fn=dict,
    )
    frames, fmt = _collect_audio_frames(source, "t1", track="mic")
    # The matching frame survives so the route reaches its unsupported-width
    # branch; the width is preserved rather than rewritten to the 16-bit default.
    assert frames == [b"\x55" * 160]
    assert fmt == {"sample_rate": 8000, "channels": 1, "sample_width": 1}


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
        _manifest_fn=dict,
    )
    frames, fmt = _collect_audio_frames(source, "t1", track="mic")
    assert frames == [b"AA", b"BB"]  # seq 1 then seq 5, vad/tts excluded
    assert fmt == {"sample_rate": 16000, "channels": 1, "sample_width": 2}
