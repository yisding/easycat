"""Unit tests for the shared persistent MultiContextWSManager."""

from __future__ import annotations

import asyncio
import json

import pytest

from easycat.tts._multi_context_ws import (
    MultiContextAdapter,
    MultiContextWSManager,
)


class FakeMultiContextWS:
    """Fake multi-context ReconnectingWebSocket.

    ``script`` is the list of frames recv_iter yields. ``on_reconnect`` (the
    manager's hook) is fired after ``reconnect_after`` frames when set.
    """

    def __init__(
        self,
        script=None,
        *,
        on_reconnect=None,
        reconnect_after=None,
        fail_send_at=None,
    ):
        self._script = list(script or [])
        self._on_reconnect = on_reconnect
        self._reconnect_after = reconnect_after
        self.sent: list[str] = []
        self._send_count = 0
        self._fail_send_at = fail_send_at or set()
        self.closed = False
        self._gate = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def send(self, message: str) -> None:
        self._send_count += 1
        if self._send_count in self._fail_send_at:
            raise RuntimeError("send failed")
        self.sent.append(message)

    async def recv_iter(self):
        for i, frame in enumerate(self._script):
            yield frame
            if self._reconnect_after is not None and i + 1 == self._reconnect_after:
                result = self._on_reconnect()
                if asyncio.iscoroutine(result):
                    await result
        # Hold the reader open until the manager cancels us, mirroring a live
        # socket that has not been server-closed.
        await self._gate.wait()

    async def close(self) -> None:
        self.closed = True
        self._gate.set()


def _chunk(ctx_id: str, key: str = "context_id", *, done: bool = False) -> str:
    return json.dumps({key: ctx_id, "type": "chunk", "done": done})


def _default_parse(frame):
    """Parse a raw JSON wire frame to a dict, or None (mirrors providers)."""
    if not isinstance(frame, str):
        return None
    try:
        parsed = json.loads(frame)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _make_adapter(ws, **overrides) -> MultiContextAdapter:
    defaults = dict(
        connect_factory=lambda _hook: ws,
        parse_frame=_default_parse,
        # route_key receives the *parsed* object now (parse happens once).
        route_key=lambda d: d.get("context_id") if isinstance(d, dict) else None,
        context_cancel_frames=lambda cid: [json.dumps({"context_id": cid, "cancel": True})],
        on_context_replay=lambda _id: None,
        socket_close_frames=lambda: [],
        on_global_frame=lambda _f: None,
        context_queue_maxsize=64,
    )
    defaults.update(overrides)
    return MultiContextAdapter(**defaults)


class TestMultiContextWSManager:
    async def test_fresh_uuid_per_open_context(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx1 = await mgr.open_context()
        ctx2 = await mgr.open_context()
        assert ctx1.context_id != ctx2.context_id
        assert len(ctx1.context_id) >= 32
        await mgr.aclose()

    async def test_demux_routes_by_context_id(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()
        # Script frames addressed to ctx after it is registered.
        ws._script = [_chunk(ctx.context_id), _chunk(ctx.context_id, done=True)]
        # Restart the reader against the now-populated script.
        await mgr._cancel_background_tasks()
        ws._gate = asyncio.Event()
        mgr._reader_task = asyncio.create_task(mgr._reader_loop())

        frames = []
        async for frame in ctx.frames():
            frames.append(frame)
            if frames[-1].get("done"):
                break
        assert len(frames) == 2
        await mgr.aclose()

    async def test_camelcase_route_key(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(
            _make_adapter(
                ws,
                route_key=lambda d: d.get("contextId") if isinstance(d, dict) else None,
            )
        )
        ctx = await mgr.open_context()
        ws._script = [json.dumps({"contextId": ctx.context_id, "isFinal": True})]
        await mgr._cancel_background_tasks()
        ws._gate = asyncio.Event()
        mgr._reader_task = asyncio.create_task(mgr._reader_loop())

        got = []
        async for frame in ctx.frames():
            got.append(frame)
            if got[-1].get("isFinal"):
                break
        assert got[0]["contextId"] == ctx.context_id
        await mgr.aclose()

    async def test_no_cross_talk_between_contexts(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx_a = await mgr.open_context()
        ctx_b = await mgr.open_context()
        ws._script = [
            _chunk(ctx_a.context_id),
            _chunk(ctx_b.context_id),
            _chunk(ctx_a.context_id, done=True),
            _chunk(ctx_b.context_id, done=True),
        ]
        await mgr._cancel_background_tasks()
        ws._gate = asyncio.Event()
        mgr._reader_task = asyncio.create_task(mgr._reader_loop())

        a_frames = []
        async for frame in ctx_a.frames():
            a_frames.append(frame)
            if a_frames[-1].get("done"):
                break
        b_frames = []
        async for frame in ctx_b.frames():
            b_frames.append(frame)
            if b_frames[-1].get("done"):
                break

        assert all(f["context_id"] == ctx_a.context_id for f in a_frames)
        assert all(f["context_id"] == ctx_b.context_id for f in b_frames)
        await mgr.aclose()

    async def test_ensure_socket_clears_failed_socket_so_next_open_retries(self):
        class FailingConnectWS(FakeMultiContextWS):
            async def connect(self) -> None:
                raise RuntimeError("connect failed")

        good = FakeMultiContextWS()
        sockets: list[FakeMultiContextWS] = [FailingConnectWS(), good]
        adapter = _make_adapter(good, connect_factory=lambda _hook: sockets.pop(0))
        mgr = MultiContextWSManager(adapter)

        # First open: the initial connect fails and must NOT leave a dead socket.
        with pytest.raises(RuntimeError, match="connect failed"):
            await mgr.open_context()
        assert mgr._ws is None

        # Second open reconnects a fresh (good) socket instead of early-returning.
        ctx = await mgr.open_context()
        assert ctx is not None
        assert mgr._ws is good
        await mgr.aclose()

    async def test_concurrent_cold_callers_await_one_initial_connect(self):
        class SlowConnectWS(FakeMultiContextWS):
            def __init__(self) -> None:
                super().__init__()
                self.connect_calls = 0
                self.connect_entered = asyncio.Event()
                self.allow_connect = asyncio.Event()

            async def connect(self) -> None:
                self.connect_calls += 1
                self.connect_entered.set()
                await self.allow_connect.wait()

        ws = SlowConnectWS()
        mgr = MultiContextWSManager(_make_adapter(ws))

        warmup = asyncio.create_task(mgr.warmup())
        await ws.connect_entered.wait()
        first_context = asyncio.create_task(mgr.open_context())
        second_context = asyncio.create_task(mgr.open_context())
        await asyncio.sleep(0)

        assert not first_context.done()
        assert not second_context.done()
        assert ws.connect_calls == 1

        ws.allow_connect.set()
        await warmup
        ctx1 = await first_context
        ctx2 = await second_context

        assert ctx1 is not None
        assert ctx2 is not None
        assert ws.connect_calls == 1
        await mgr.aclose()

    async def test_backpressure_delivers_done_under_full_queue(self):
        # maxsize=1 forces the reader to block on put when the consumer is slow;
        # every frame (incl. the terminal done) must still be delivered, never
        # silently dropped.
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws, context_queue_maxsize=1))
        ctx = await mgr.open_context()
        ws._script = [_chunk(ctx.context_id) for _ in range(4)] + [
            _chunk(ctx.context_id, done=True)
        ]
        await mgr._cancel_background_tasks()
        ws._gate = asyncio.Event()
        mgr._reader_task = asyncio.create_task(mgr._reader_loop())

        async def _collect() -> list[dict]:
            out = []
            async for frame in ctx.frames():
                out.append(frame)
                await asyncio.sleep(0)  # yield so the reader has to backpressure
                if out[-1].get("done"):
                    break
            return out

        frames = await asyncio.wait_for(_collect(), timeout=2.0)
        assert len(frames) == 5
        assert frames[-1]["done"] is True
        await mgr.aclose()

    async def test_finish_context_delivers_terminal_when_queue_full(self):
        # If the queue is full at teardown, the terminal must still land (the
        # buffered frames are drained to make room) so frames() cannot hang.
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws, context_queue_maxsize=1))
        ctx = await mgr.open_context()
        ctx.queue.put_nowait(_chunk(ctx.context_id))  # fill to capacity
        assert ctx.queue.full()

        mgr.finish_context(ctx)

        async def _drain() -> list:
            return [f async for f in ctx.frames()]

        # Returns promptly (terminal delivered); buffered frame was drained.
        got = await asyncio.wait_for(_drain(), timeout=1.0)
        assert got == []
        await mgr.aclose()

    async def test_send_lock_serializes_concurrent_sends(self):
        order: list[str] = []

        class RecordingWS(FakeMultiContextWS):
            async def send(self, message: str) -> None:
                order.append(f"start:{message}")
                await asyncio.sleep(0)
                order.append(f"end:{message}")
                self.sent.append(message)

        ws = RecordingWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        await mgr.open_context()

        await asyncio.gather(
            mgr._send_frames(["A"]),
            mgr._send_frames(["B"]),
        )
        # Each send must complete before the next begins (no interleaving).
        assert order in (
            ["start:A", "end:A", "start:B", "end:B"],
            ["start:B", "end:B", "start:A", "end:A"],
        )
        await mgr.aclose()

    async def test_on_reconnect_replays_live_armed_contexts(self):
        ws = FakeMultiContextWS()
        replayed: list[str] = []
        mgr = MultiContextWSManager(
            _make_adapter(ws, on_context_replay=lambda cid: replayed.append(cid))
        )
        armed = await mgr.open_context()
        await mgr.open_context()  # unarmed: never sent -> pending_frames None
        cancelled = await mgr.open_context()

        await mgr.send(armed, [json.dumps({"context_id": armed.context_id, "transcript": "hi"})])
        cancelled.cancelled = True

        ws.sent.clear()
        await mgr._on_reconnect()

        assert replayed == []
        assert len(ws.sent) == 1
        assert json.loads(ws.sent[0])["context_id"] == armed.context_id
        await armed.queue.put({"context_id": armed.context_id, "type": "chunk"})
        frame = await anext(armed.frames())
        assert frame["type"] == "chunk"
        assert replayed == [armed.context_id]
        await mgr.aclose()

    async def test_reconnect_reset_waits_for_buffered_frames_under_backpressure(self):
        ws = FakeMultiContextWS()
        trace: list[str] = []
        mgr = MultiContextWSManager(
            _make_adapter(
                ws,
                on_context_replay=lambda _cid: trace.append("reset"),
                context_queue_maxsize=1,
            )
        )
        ctx = await mgr.open_context()
        await mgr.send(ctx, [json.dumps({"context_id": ctx.context_id, "transcript": "hi"})])
        ctx.queue.put_nowait({"context_id": ctx.context_id, "phase": "pre-drop"})

        replay = asyncio.create_task(mgr._on_reconnect())
        await asyncio.sleep(0)

        assert not replay.done()
        assert trace == []

        frames = ctx.frames()
        pre_drop = await anext(frames)
        trace.append(pre_drop["phase"])
        await replay

        replayed_frame = asyncio.create_task(anext(frames))
        await asyncio.sleep(0)
        assert trace == ["pre-drop", "reset"]
        await ctx.queue.put({"context_id": ctx.context_id, "phase": "replayed"})
        replayed = await replayed_frame
        trace.append(replayed["phase"])

        assert trace == ["pre-drop", "reset", "replayed"]
        await mgr.aclose()

    async def test_cancel_context_sends_cancel_without_socket_close(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()
        await mgr.cancel_context(ctx)

        assert not ws.closed
        cancel = [json.loads(s) for s in ws.sent]
        assert any(c.get("cancel") is True for c in cancel)
        assert ctx.cancelled
        await mgr.aclose()

    async def test_cancel_send_failure_falls_back_to_socket_close(self):
        ws = FakeMultiContextWS(fail_send_at={1})
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()
        await mgr.cancel_context(ctx)

        assert ws.closed
        assert mgr._ws is None
        # Next open_context reconnects a fresh socket.
        ws2 = FakeMultiContextWS()
        mgr._adapter = _make_adapter(ws2)
        new_ctx = await mgr.open_context()
        assert mgr._ws is ws2
        assert new_ctx.context_id != ctx.context_id
        await mgr.aclose()

    async def test_aclose_sends_close_frames_and_cancels_tasks(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(
            _make_adapter(ws, socket_close_frames=lambda: [json.dumps({"close_socket": True})])
        )
        await mgr.open_context()
        reader = mgr._reader_task

        await mgr.aclose()

        assert any(json.loads(s).get("close_socket") for s in ws.sent)
        assert ws.closed
        assert reader.done()
        # Idempotent.
        await mgr.aclose()

    async def test_reconnect_budget_exhaustion_surfaces_error(self):
        # recv_iter ends mid-utterance (no gate wait) simulating budget
        # exhaustion / unexpected server close while a context is live: this is
        # a truncation, so frames() must raise (not end cleanly) so the provider
        # surfaces a real failure — matching the one-shot path.
        class EndingWS(FakeMultiContextWS):
            async def recv_iter(self):
                for frame in self._script:
                    yield frame
                # No gate wait -> stream ends.

        ws = EndingWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()
        # Reader started with empty script -> ends immediately and finishes ctx.
        await asyncio.sleep(0.01)

        with pytest.raises(ConnectionError):
            async for _ in ctx.frames():
                pass
        assert mgr._ws is None
        await mgr.aclose()

    async def test_non_object_json_frame_does_not_kill_reader(self):
        # A valid-but-non-object JSON frame (bare number / quoted keepalive) is
        # parsed to None by parse_frame and dropped — it must NOT reach
        # on_global_frame, raise, or tear down the shared socket.
        ws = FakeMultiContextWS()
        globals_seen: list = []
        mgr = MultiContextWSManager(
            _make_adapter(ws, on_global_frame=lambda f: globals_seen.append(f))
        )
        ctx = await mgr.open_context()
        ws._script = ["123", '"pong"', "[]", _chunk(ctx.context_id, done=True)]
        await mgr._cancel_background_tasks()
        ws._gate = asyncio.Event()
        mgr._reader_task = asyncio.create_task(mgr._reader_loop())

        frames = []
        async for frame in ctx.frames():
            frames.append(frame)
            if frames[-1].get("done"):
                break
        # The stray non-object frames were dropped (parse_frame → None); the
        # socket survived and the real frame was still delivered.
        assert frames and frames[-1]["done"] is True
        assert globals_seen == []
        assert mgr._ws is ws  # reader still alive, socket not torn down
        await mgr.aclose()

    async def test_on_global_frame_raising_does_not_kill_reader(self):
        # Even if a provider's on_global_frame raises on a routable-less (global)
        # frame, the reader must survive (the call is guarded), keep the socket,
        # and keep delivering frames to live contexts.
        ws = FakeMultiContextWS()

        def _boom(_parsed):
            raise RuntimeError("on_global_frame boom")

        mgr = MultiContextWSManager(_make_adapter(ws, on_global_frame=_boom))
        ctx = await mgr.open_context()
        # A valid dict with no context_id -> routed to on_global_frame (raises),
        # then a real frame for ctx.
        ws._script = [json.dumps({"type": "error"}), _chunk(ctx.context_id, done=True)]
        await mgr._cancel_background_tasks()
        ws._gate = asyncio.Event()
        mgr._reader_task = asyncio.create_task(mgr._reader_loop())

        frames = []
        async for frame in ctx.frames():
            frames.append(frame)
            if frames[-1].get("done"):
                break
        assert frames and frames[-1]["done"] is True
        assert mgr._ws is ws  # reader survived the raising global handler
        await mgr.aclose()

    async def test_buffered_terminal_survives_socket_close(self):
        # F1 edge: a COMPLETED utterance whose terminal (done) is still buffered
        # when the socket closes must NOT be reported as an error or lose its
        # tail — the consumer drains the buffered done and completes cleanly.
        class EndingWS(FakeMultiContextWS):
            async def recv_iter(self):
                for frame in self._script:
                    yield frame
                # stream ends right after the buffered frames (clean close)

        ws = EndingWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()
        ws._script = [_chunk(ctx.context_id), _chunk(ctx.context_id, done=True)]
        await mgr._cancel_background_tasks()
        ws._gate = asyncio.Event()
        mgr._reader_task = asyncio.create_task(mgr._reader_loop())
        await asyncio.sleep(0.01)  # let the reader buffer both frames then end

        # Both frames (incl. the terminal done) are delivered; no error raised.
        frames = []
        async for frame in ctx.frames():
            frames.append(frame)
            if frames[-1].get("done"):
                break
        assert len(frames) == 2
        assert frames[-1]["done"] is True
        await mgr.aclose()

    async def test_deliberate_aclose_ends_frames_without_error(self):
        # A deliberate aclose() is NOT a failure: live contexts end cleanly.
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()
        await mgr.aclose()
        frames = [f async for f in ctx.frames()]
        assert frames == []
        assert ctx.error is None


@pytest.mark.asyncio
async def test_open_context_after_aclose_raises():
    ws = FakeMultiContextWS()
    mgr = MultiContextWSManager(_make_adapter(ws))
    await mgr.aclose()
    with pytest.raises(RuntimeError):
        await mgr.open_context()
