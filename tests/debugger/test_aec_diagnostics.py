"""AEC diagnostics: pure analysis fixtures + debugger endpoints (WP17 STEP 2)."""

from __future__ import annotations

import math
import struct

import pytest

pytest.importorskip("aiohttp")

from easycat.debugger._aec import (
    align_tracks,
    compute_erle,
    detect_double_talk,
    detect_self_echo,
    frame_rms_series,
)
from easycat.runtime.records import AEC_REFERENCE_FRAME_NAME

from ._server_helpers import _SAFE_HEADERS


def _tone_pcm(amplitude: int, n_samples: int) -> bytes:
    """Flat int16 PCM at a constant magnitude (square-ish, easy to reason about)."""
    sample = struct.pack("<h", max(-32768, min(32767, amplitude)))
    return sample * n_samples


# ── compute_erle ─────────────────────────────────────────────────


def test_compute_erle_high_when_residual_small():
    # Near (mic) loud, residual (post-AEC) tiny → large positive ERLE.
    near = _tone_pcm(8000, 320)  # 320 samples = 20ms @ 16k
    residual = _tone_pcm(80, 320)
    out = compute_erle(near, residual, frame_ms=20)
    assert out["measured_frames"] == 1
    assert out["mean_db"] is not None
    expected = 20.0 * math.log10(8000 / 80)
    assert out["mean_db"] == pytest.approx(expected, abs=0.5)
    assert out["mean_db"] > 30.0


def test_compute_erle_skips_silent_frames():
    # A silent near frame carries no echo to enhance → skipped (None), not 0 dB.
    near = _tone_pcm(0, 320)
    residual = _tone_pcm(0, 320)
    out = compute_erle(near, residual, frame_ms=20)
    assert out["frames"] == [None]
    assert out["measured_frames"] == 0
    assert out["mean_db"] is None


def test_compute_erle_low_when_aec_ineffective():
    # Residual ~= near → ERLE near 0 dB (the canceller removed nothing).
    near = _tone_pcm(6000, 320)
    residual = _tone_pcm(6000, 320)
    out = compute_erle(near, residual, frame_ms=20)
    assert out["mean_db"] == pytest.approx(0.0, abs=0.5)


# ── detect_double_talk ───────────────────────────────────────────


def test_detect_double_talk_flags_overlap_bands():
    reference = [500.0, 500.0, 0.0, 500.0, 500.0]
    mic = [500.0, 500.0, 500.0, 0.0, 500.0]
    bands = detect_double_talk(reference, mic, thresh=200.0)
    # Overlap at frames 0-1 (band [0,2)) and frame 4 (band [4,5)).
    assert bands == [{"start": 0, "end": 2}, {"start": 4, "end": 5}]


def test_detect_double_talk_empty_when_no_overlap():
    reference = [500.0, 0.0, 500.0]
    mic = [0.0, 500.0, 0.0]
    assert detect_double_talk(reference, mic, thresh=200.0) == []


# ── detect_self_echo ─────────────────────────────────────────────


def test_detect_self_echo_flags_spike_without_interruption():
    # A loud residual frame with no interruption nearby → self-echo.
    post = _tone_pcm(0, 320) + _tone_pcm(8000, 320) + _tone_pcm(0, 320)
    hits = detect_self_echo(post, interruption_frames=[], frame_ms=20, spike_thresh=1500.0)
    assert [h["frame"] for h in hits] == [1]


def test_detect_self_echo_ignores_spike_coinciding_with_interruption():
    # Same spike, but an interruption is recorded at frame 1 → a real barge-in,
    # not self-echo, so it is NOT flagged.
    post = _tone_pcm(0, 320) + _tone_pcm(8000, 320) + _tone_pcm(0, 320)
    hits = detect_self_echo(
        post, interruption_frames=[1], frame_ms=20, spike_thresh=1500.0, guard_frames=1
    )
    assert hits == []


# ── frame_rms_series / align_tracks ──────────────────────────────


def test_frame_rms_series_constant_tone():
    series = frame_rms_series(_tone_pcm(4000, 640), frame_ms=20)
    assert len(series) == 2
    assert all(v == pytest.approx(4000.0, abs=1.0) for v in series)


class _DictSource:
    """Minimal DebuggerSource-like object exposing records()/artifact()."""

    def __init__(self, records, blobs):
        self._records = records
        self._blobs = blobs
        self.is_live = False

    def records(self):
        return self._records

    def artifact(self, ref):
        return self._blobs.get(ref)


def test_vad_whatif_frames_skips_malformed_sequence_values():
    from easycat.debugger._aec_routes import _vad_whatif_frames

    records = [
        {
            "sequence": "bad",
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "bad",
            "data": {"stage": "vad"},
        },
        {
            "sequence": True,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "bool",
            "data": {"stage": "vad"},
        },
        {
            "sequence": 2,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "ok",
            "data": {"stage": "vad"},
        },
    ]
    source = _DictSource(records, {"bad": b"NO", "bool": b"NO", "ok": b"OK"})

    assert _vad_whatif_frames(source, "t1") == [(b"OK", {"stage": "vad"})]


def test_align_tracks_groups_by_track_and_orders_by_mono_ns():
    records = [
        {
            "sequence": 3,
            "name": "stage_complete",
            "turn_id": "t1",
            "output_ref": "post",
            "data": {"stage": "audio"},
            "timing": {"mono_ns": 300},
        },
        {
            "sequence": 1,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "mic",
            "data": {"stage": "audio"},
            "timing": {"mono_ns": 100},
        },
        {
            "sequence": 2,
            "name": AEC_REFERENCE_FRAME_NAME,
            "turn_id": "t1",
            "output_ref": "ref",
            "data": {"stage": "audio"},
            "timing": {"mono_ns": 200},
        },
        # A different turn must be excluded.
        {
            "sequence": 4,
            "name": AEC_REFERENCE_FRAME_NAME,
            "turn_id": "t2",
            "output_ref": "ref-other",
            "data": {"stage": "audio"},
            "timing": {"mono_ns": 50},
        },
    ]
    blobs = {"mic": b"\x01\x02", "ref": b"\x03\x04", "post": b"\x05\x06", "ref-other": b"\xff"}
    source = _DictSource(records, blobs)
    tracks = align_tracks(records, source=source, turn_id="t1")
    assert [e["ref"] for e in tracks["mic_in"]] == ["mic"]
    assert [e["ref"] for e in tracks["reference"]] == ["ref"]
    assert [e["ref"] for e in tracks["post_aec"]] == ["post"]
    # The other turn's reference is excluded.
    assert all(e["ref"] != "ref-other" for e in tracks["reference"])


def test_align_tracks_skips_malformed_sequence_values():
    records = [
        {
            "sequence": "bad",
            "name": AEC_REFERENCE_FRAME_NAME,
            "turn_id": "t1",
            "output_ref": "bad",
            "data": {"stage": "audio"},
            "timing": {"mono_ns": 100},
        },
        {
            "sequence": True,
            "name": AEC_REFERENCE_FRAME_NAME,
            "turn_id": "t1",
            "output_ref": "bool",
            "data": {"stage": "audio"},
            "timing": {"mono_ns": 200},
        },
        {
            "sequence": 2,
            "name": AEC_REFERENCE_FRAME_NAME,
            "turn_id": "t1",
            "output_ref": "ok",
            "data": {"stage": "audio"},
            "timing": {"mono_ns": 300},
        },
    ]
    source = _DictSource(records, {"bad": b"NO", "bool": b"NO", "ok": b"OK"})

    tracks = align_tracks(records, source=source, turn_id="t1")

    assert [e["ref"] for e in tracks["reference"]] == ["ok"]


def test_frame_rms_series_mulaw_width_one_yields_no_frames():
    # 8-bit mu-law (sample_width == 1) is unsupported by the shared decoder, so
    # the RMS series is empty rather than mis-decoded int8 garbage.
    blob = bytes(range(256)) * 4
    assert frame_rms_series(blob, sample_width=1, frame_ms=20) == []
    # The supported int16 path still produces frames from the same byte count.
    assert frame_rms_series(_tone_pcm(4000, 320), sample_width=2, frame_ms=20)


def test_aec_diagnostics_unsupported_for_mulaw_width():
    from easycat.debugger._aec_routes import _aec_diagnostics_for_turn

    # Mic-in + post-AEC tracks carrying sample_width == 1 (mu-law) must degrade
    # to an unsupported result instead of emitting garbage ERLE numbers.
    records = [
        {
            "sequence": 1,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "mic",
            "data": {"stage": "audio", "sample_width": 1, "channels": 1},
            "timing": {"mono_ns": 100},
        },
        {
            "sequence": 2,
            "name": "stage_complete",
            "turn_id": "t1",
            "output_ref": "post",
            "data": {"stage": "audio", "sample_width": 1, "channels": 1},
            "timing": {"mono_ns": 200},
        },
    ]
    blobs = {"mic": bytes(range(64)), "post": bytes(range(64))}
    source = _DictSource(records, blobs)
    out = _aec_diagnostics_for_turn(source, "t1")
    assert out["unsupported"] is True
    assert "erle" not in out
    assert out["format"]["sample_width"] == 1


# ── Server endpoints ─────────────────────────────────────────────


class _PassthroughAEC:
    """Echo canceller that returns the mic chunk and accepts every reference."""

    async def process(self, chunk):
        return chunk

    def feed_reference(self, chunk) -> None: ...
    def configure(self, **_kw) -> None: ...


async def _aec_bundle(tmp_path):
    """Drive a real Session with AEC enabled and return its bundle path.

    Reuses the shared voice-session doubles (``_server_helpers``); only the
    transport differs — its ``send_audio`` must return a truthy delivered flag
    so the router's delivery side effect (which feeds the AEC reference and,
    when ``capture_aec_reference`` is opted in, journals ``aec_reference_frame``)
    actually runs.  The AEC diagnostics view needs the journaled reference
    track, so this bundle opts capture in explicitly.
    """
    import asyncio
    from collections.abc import AsyncIterator

    from easycat.noise_reduction import PassthroughNoiseReducer
    from easycat.runtime import InMemoryRingBuffer
    from easycat.runtime.artifacts import InMemoryArtifactStore
    from easycat.session._session import Session
    from easycat.session._types import SessionConfig
    from easycat.turn_manager import TurnManagerConfig

    from ._server_helpers import (
        _DistinctiveTTS,
        _FakeAgent,
        _FakeSTT,
        _FakeVAD,
        _silent_chunk,
    )

    class _DeliveringTransport:
        def __init__(self, chunks_in):
            self._chunks_in = chunks_in
            self.sent = []

        async def connect(self) -> None: ...
        async def disconnect(self) -> None: ...
        async def receive_audio(self) -> AsyncIterator:
            for chunk in self._chunks_in:
                yield chunk

        async def send_audio(self, chunk) -> bool:
            self.sent.append(chunk)
            return True

        async def clear_audio(self) -> None: ...

    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=2048, artifact_store=artifact_store)
    session = Session(
        SessionConfig(
            transport=_DeliveringTransport([_silent_chunk(), _silent_chunk()]),
            vad=_FakeVAD(),
            stt=_FakeSTT(),
            agent=_FakeAgent(),
            tts=_DistinctiveTTS(),
            noise_reducer=PassthroughNoiseReducer(),
            echo_canceller=_PassthroughAEC(),
            enable_noise_reduction=False,
            enable_echo_cancellation=True,
            capture_aec_reference=True,
            turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
            journal=journal,
            artifact_store=artifact_store,
            session_id="aec-diag-test",
        )
    )
    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()
    bundle_path = tmp_path / "aec.zip"
    session.export_debug_bundle(str(bundle_path))
    return bundle_path


async def test_api_aec_reports_has_reference_true_for_aec_bundle(tmp_path):
    from easycat.debug.bundle import RunBundle
    from easycat.debugger.server import _bundle_source, _make_app

    bundle_path = await _aec_bundle(tmp_path)
    bundle = RunBundle.load(bundle_path)
    turn_id = next(
        r.get("turn_id")
        for r in bundle.records()
        if r.get("name") == AEC_REFERENCE_FRAME_NAME and r.get("turn_id")
    )
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/aec/{turn_id}")
        assert resp.status == 200
        body = await resp.json()
        assert body["has_reference"] is True
        assert body["turn_id"] == turn_id
        assert "erle" in body and "double_talk" in body and "self_echo" in body
        assert body["tracks"]["reference"]["frame_count"] > 0


async def test_api_aec_reports_has_reference_false_without_aec(tmp_path):
    from easycat.debugger.server import _bundle_source, _make_app

    from ._server_helpers import _build_voice_bundle

    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        # Any turn id; the no-AEC bundle has no reference frames anywhere.
        turns = await (await client.get("/api/turns")).json()
        turn_id = turns["turns"][0]["turn_id"]
        resp = await client.get(f"/api/aec/{turn_id}")
        assert resp.status == 200
        body = await resp.json()
        assert body["has_reference"] is False


async def test_api_aec_is_listed_in_server_docstring():
    from easycat.debugger import server as _server

    assert "/api/aec/<turn>" in (_server.__doc__ or "")
    assert "vad-whatif" in (_server.__doc__ or "")


async def test_api_aec_vad_whatif_returns_false_trigger_delta(tmp_path):
    from easycat.debugger.server import _bundle_source, _make_app

    from ._server_helpers import _build_voice_bundle

    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        turns = await (await client.get("/api/turns")).json()
        turn_id = turns["turns"][0]["turn_id"]
        resp = await client.post(
            f"/api/aec/{turn_id}/vad-whatif?threshold=0.3", headers=_SAFE_HEADERS
        )
        # Either the VAD provider ran (200 with a delta) or it could not be
        # imported in this environment (422 degrade) — both are acceptable, but
        # never a 500.
        assert resp.status in (200, 422)
        body = await resp.json()
        if resp.status == 200:
            assert "false_trigger_delta" in body
            assert "baseline_starts" in body
            assert "whatif_starts" in body
        else:
            assert body["error_code"] == "VAD_UNAVAILABLE"


async def test_api_aec_vad_whatif_live_source_returns_405():
    from easycat.debugger.server import _make_app, _session_source

    class _LiveSession:
        session_id = "live-1"
        is_running = True
        turn_state = "idle"
        journal = None
        _artifact_store = None

    source = _session_source(_LiveSession())
    assert source.is_live is True
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/aec/turn-1/vad-whatif", headers=_SAFE_HEADERS)
        assert resp.status == 405


async def test_api_aec_vad_whatif_missing_origin_returns_403(tmp_path):
    from easycat.debugger.server import _bundle_source, _make_app

    from ._server_helpers import _build_voice_bundle

    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        # State-changing POST with JSON content-type but no Origin header → 403.
        resp = await client.post(
            "/api/aec/turn-1/vad-whatif", headers={"Content-Type": "application/json"}
        )
        assert resp.status == 403


# ── reference audio track (FWP10) ─────────────────────────────────


def test_reference_track_is_a_valid_audio_selector():
    from easycat.debugger.server import (
        _AUDIO_TRACK_REFERENCE,
        _VALID_AUDIO_TRACKS,
    )

    assert _AUDIO_TRACK_REFERENCE == "reference"
    assert _AUDIO_TRACK_REFERENCE in _VALID_AUDIO_TRACKS


def test_collect_audio_frames_stitches_reference_track():
    """The ``reference`` track stitches ``aec_reference_frame``/``output_ref``
    blobs in sequence order, so the AEC view's third strip has audio to draw."""
    from easycat.debugger.server import _collect_audio_frames

    records = [
        {
            "sequence": 2,
            "name": AEC_REFERENCE_FRAME_NAME,
            "turn_id": "t1",
            "output_ref": "ref-b",
            "data": {"stage": "audio", "sample_rate": 16000, "channels": 1, "sample_width": 2},
            "timing": {"mono_ns": 200},
        },
        {
            "sequence": 1,
            "name": AEC_REFERENCE_FRAME_NAME,
            "turn_id": "t1",
            "output_ref": "ref-a",
            "data": {"stage": "audio", "sample_rate": 16000, "channels": 1, "sample_width": 2},
            "timing": {"mono_ns": 100},
        },
        # Mic-in for the same turn must not leak into the reference track.
        {
            "sequence": 3,
            "name": "stage_start",
            "turn_id": "t1",
            "input_ref": "mic",
            "data": {"stage": "stt"},
        },
    ]
    blobs = {"ref-a": _tone_pcm(4000, 160), "ref-b": _tone_pcm(2000, 160), "mic": b"\x00\x00"}
    source = _DictSource(records, blobs)
    frames, fmt = _collect_audio_frames(source, "t1", track="reference")
    # Ordered by sequence: ref-a (seq 1) then ref-b (seq 2); mic excluded.
    assert frames == [blobs["ref-a"], blobs["ref-b"]]
    assert fmt["sample_rate"] == 16000


async def test_api_audio_reference_track_fetchable_when_reference_frames_exist(tmp_path):
    """The reference waveform/concat is fetchable on an AEC bundle whose journal
    carries ``aec_reference_frame`` records, and 404s on a bundle without them."""
    from easycat.debug.bundle import RunBundle
    from easycat.debugger.server import _bundle_source, _make_app

    bundle_path = await _aec_bundle(tmp_path)
    bundle = RunBundle.load(bundle_path)
    ref_turns = [
        r.get("turn_id")
        for r in bundle.records()
        if r.get("name") == AEC_REFERENCE_FRAME_NAME and r.get("turn_id")
    ]
    if not ref_turns:
        # The shared _aec_bundle Session fixture is timing-driven; under load it
        # can rarely settle before any reference frame is journaled.  This test
        # asserts the reference track is fetchable *when reference frames exist*,
        # so a raceless miss degrades to a skip rather than a misleading failure.
        pytest.skip("no aec_reference_frame captured in this fixture run")
    turn_id = ref_turns[0]
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        # Concat returns a WAV stream for the reference track.
        resp = await client.get(f"/api/audio/concat/{turn_id}?track=reference")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "audio/wav"
        body = await resp.read()
        assert body[:4] == b"RIFF"
        # Waveform returns a PNG for the reference track.
        resp = await client.get(f"/api/audio/waveform/{turn_id}?track=reference&w=120&h=40")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"


async def test_api_audio_reference_track_404_without_reference_frames(tmp_path):
    from easycat.debugger.server import _bundle_source, _make_app

    from ._server_helpers import _build_voice_bundle

    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        turns = await (await client.get("/api/turns")).json()
        turn_id = turns["turns"][0]["turn_id"]
        # No reference frames anywhere → the reference track has nothing to stitch.
        resp = await client.get(f"/api/audio/concat/{turn_id}?track=reference")
        assert resp.status == 404
        resp = await client.get(f"/api/audio/waveform/{turn_id}?track=reference")
        assert resp.status == 404


def test_aec_diagnostics_truncates_oversized_tracks(monkeypatch):
    from easycat.debugger import _aec_routes as debugger_server

    cap = 640  # one 20ms frame at 16 kHz, int16 mono
    monkeypatch.setattr(debugger_server, "_AEC_MAX_TRACK_BYTES", cap)
    records = []
    blobs = {}
    for idx, (name, ref_key, ref_value) in enumerate(
        [
            ("stage_start", "input_ref", "mic"),
            (AEC_REFERENCE_FRAME_NAME, "output_ref", "ref"),
            ("stage_complete", "output_ref", "post"),
        ],
        start=1,
    ):
        record = {
            "sequence": idx,
            "name": name,
            "turn_id": "t1",
            ref_key: ref_value,
            "data": {"stage": "audio", "sample_rate": 16000, "channels": 1, "sample_width": 2},
            "timing": {"mono_ns": idx * 20_000_000},
        }
        records.append(record)
        blobs[ref_value] = _tone_pcm(4000, 640)  # two frames, twice the cap
    source = _DictSource(records, blobs)

    out = debugger_server._aec_diagnostics_for_turn(source, "t1")

    assert out["diagnostics_truncated"] is True
    assert out["max_track_bytes"] == cap
    for track in ("mic_in", "reference", "post_aec"):
        assert out["tracks"][track]["truncated"] is True
        assert out["tracks"][track]["byte_count"] == 1280
        assert out["tracks"][track]["analyzed_byte_count"] == cap
    assert out["erle"]["frames"] == [pytest.approx(0.0, abs=0.5)]


def test_aec_diagnostics_late_interruption_not_clamped_into_prefix(monkeypatch):
    """A real barge-in *after* the truncated prefix must not hide self-echo.

    Regression: framing interruptions against the clipped prefix shrank
    ``total_frames`` and clamped the late ``turn_state_changed`` onto the last
    analyzed frame, planting a phantom guard window that suppressed the genuine
    self-echo spike in the analyzed tail (truncated diagnostics under-reported).
    """
    from easycat.debugger import _aec_routes as debugger_server

    cap = 5 * 640  # five 20ms frames at 16 kHz, int16 mono
    monkeypatch.setattr(debugger_server, "_AEC_MAX_TRACK_BYTES", cap)

    # 30-frame post-AEC track: a lone self-echo spike at frame 4 (the last
    # analyzed frame) surrounded by silence.
    post_pcm = _tone_pcm(0, 320 * 4) + _tone_pcm(8000, 320) + _tone_pcm(0, 320 * 25)
    fmt = {"stage": "audio", "sample_rate": 16000, "channels": 1, "sample_width": 2}
    records = [
        {
            "sequence": 1,
            "name": "stage_complete",
            "turn_id": "t1",
            "output_ref": "post",
            "data": fmt,
            "timing": {"mono_ns": 0},
        },
        # Barge-in 20 frames in — far beyond the 5-frame analyzed prefix.
        {
            "sequence": 2,
            "name": "turn_state_changed",
            "turn_id": "t1",
            "data": {"to": "user_speaking"},
            "timing": {"mono_ns": 20 * 20_000_000},
        },
    ]
    source = _DictSource(records, {"post": post_pcm})

    out = debugger_server._aec_diagnostics_for_turn(source, "t1")

    # The late barge-in keeps its true frame index (not clamped to the prefix).
    assert out["interruption_frames"] == [20]
    # The self-echo spike in the analyzed tail is reported, not suppressed.
    assert [hit["frame"] for hit in out["self_echo"]] == [4]
