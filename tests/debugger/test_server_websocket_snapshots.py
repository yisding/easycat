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
    and the WS loop must drive change detection off the cheap O(1)
    ``progress()`` probe — never re-serializing the journal per tick."""
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

    # The WS loop must not materialize the dict list to detect change.
    def _boom():
        raise AssertionError("records() must not be called on the WS hot path")

    source._records_fn = _boom  # type: ignore[attr-defined]

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
            second = (await asyncio.wait_for(ws.receive(), timeout=2.0)).json()
            assert second["type"] == "snapshot"
            assert second["record_count"] == 2


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
