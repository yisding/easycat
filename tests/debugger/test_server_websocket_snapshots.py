from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

import asyncio

from easycat.debugger.server import _bundle_source, _make_app, _session_source
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.runtime.records import JournalRecordKind

from ._server_helpers import _build_voice_bundle


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


async def test_websocket_live_source_pushes_on_growth_without_serializing():
    """A live source should push a fresh snapshot when the journal grows,
    and the WS loop must drive change *detection* off the cheap O(1)
    ``progress()`` probe — ``records()`` is only ever materialised to build
    the follow-now batch *after* a sequence advance, never per tick."""
    from easycat.runtime import JournalView

    journal = InMemoryRingBuffer(capacity=32)

    class _StubSession:
        session_id = "ws-live"
        is_running = True
        turn_state = "IDLE"
        _artifact_store = None

        @property
        def journal(self):
            return JournalView(journal)

    source = _session_source(_StubSession())

    # Wrap the real serializer to count calls: the WS loop must detect growth
    # via ``progress()`` and only serialize records when the sequence advances.
    real_records = source._records_fn
    calls = {"n": 0}

    def _counting_records():
        calls["n"] += 1
        return real_records()

    source._records_fn = _counting_records  # type: ignore[attr-defined]

    journal.append(kind=JournalRecordKind.EVENT, name="first", session_id="s")
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        async with client.ws_connect("/ws") as ws:
            first = (await asyncio.wait_for(ws.receive(), timeout=2.0)).json()
            assert first["type"] == "snapshot"
            assert first["record_count"] == 1
            # Grow the journal; the 500ms poll should surface the new count.
            journal.append(kind=JournalRecordKind.EVENT, name="second", session_id="s")
            # Drain frames until the next snapshot (a records batch may precede
            # it) and assert the count caught up.
            saw_snapshot_2 = False
            for _ in range(4):
                frame = (await asyncio.wait_for(ws.receive(), timeout=2.0)).json()
                if frame["type"] == "snapshot" and frame["record_count"] == 2:
                    saw_snapshot_2 = True
                    break
            assert saw_snapshot_2
    # ``records()`` was called at most once per growth (two snapshots), never
    # on the idle change-detection probe.
    assert calls["n"] <= 2


async def test_websocket_pushes_only_new_records_batch_on_growth():
    """On a sequence advance the WS loop pushes a capped, only-new
    ``{"type": "records"}`` batch alongside the snapshot."""
    from easycat.runtime import JournalView

    journal = InMemoryRingBuffer(capacity=64)

    class _StubSession:
        session_id = "ws-batch"
        is_running = True
        turn_state = "IDLE"
        _artifact_store = None

        @property
        def journal(self):
            return JournalView(journal)

    source = _session_source(_StubSession())
    journal.append(kind=JournalRecordKind.EVENT, name="first", session_id="s")
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        async with client.ws_connect("/ws") as ws:
            # Collect the initial snapshot + records batch for the seed record.
            seen = []
            for _ in range(2):
                seen.append((await asyncio.wait_for(ws.receive(), timeout=2.0)).json())
            batch = next((m for m in seen if m["type"] == "records"), None)
            assert batch is not None
            assert [r["sequence"] for r in batch["records"]] == [1]
            assert batch["from_seq"] == 1
            assert batch["to_seq"] == 1

            # Grow by two records; the next batch must carry ONLY the new ones.
            journal.append(kind=JournalRecordKind.EVENT, name="second", session_id="s")
            journal.append(kind=JournalRecordKind.EVENT, name="third", session_id="s")
            next_batch = None
            for _ in range(5):
                frame = (await asyncio.wait_for(ws.receive(), timeout=2.0)).json()
                if frame["type"] == "records":
                    next_batch = frame
                    break
            assert next_batch is not None
            assert [r["sequence"] for r in next_batch["records"]] == [2, 3]
            assert next_batch["from_seq"] == 2
            assert next_batch["to_seq"] == 3


async def test_records_since_caps_and_advances_cursor():
    """``_records_since`` returns only records past the cursor, capped, and
    reports the new high-water mark."""
    from easycat.debugger.server import DebuggerSource, _records_since

    records = [{"sequence": i, "name": f"e{i}"} for i in range(1, 11)]
    source = DebuggerSource(
        label="t",
        _records_fn=lambda: records,
        _artifact_fn=lambda ref: None,
        _manifest_fn=lambda: {},
    )

    batch, cursor = _records_since(source, after_seq=3, cap=4)
    assert [r["sequence"] for r in batch] == [4, 5, 6, 7]
    assert cursor == 7

    # Nothing new past the tail leaves the cursor unchanged.
    empty, cursor2 = _records_since(source, after_seq=10, cap=4)
    assert empty == []
    assert cursor2 == 10


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
