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
