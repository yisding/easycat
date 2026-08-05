"""Unit tests for the shared persistent MultiContextWSManager."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from easycat._concurrency import RuntimeSupervisor
from easycat.runtime.scope import RuntimeScope, RuntimeScopeState
from easycat.tts import _multi_context_ws as multi_context_ws_module
from easycat.tts._multi_context_ws import (
    _CLOSE_FINALIZER,
    _READER_TASK,
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
    defaults = {
        "connect_factory": lambda _hook: ws,
        "parse_frame": _default_parse,
        # route_key receives the *parsed* object now (parse happens once).
        "route_key": lambda d: d.get("context_id") if isinstance(d, dict) else None,
        "context_cancel_frames": lambda cid: [json.dumps({"context_id": cid, "cancel": True})],
        "on_context_replay": lambda _id: None,
        "socket_close_frames": list,
        "on_global_frame": lambda _f: None,
        "context_queue_maxsize": 64,
    }
    defaults.update(overrides)
    return MultiContextAdapter(**defaults)


@pytest.mark.parametrize("maxsize", [0, -1, True, 1.5])
def test_adapter_rejects_unbounded_or_invalid_context_queue(maxsize: object) -> None:
    with pytest.raises(ValueError, match="context_queue_maxsize"):
        _make_adapter(FakeMultiContextWS(), context_queue_maxsize=maxsize)


def test_adapter_accepts_minimum_bounded_context_queue() -> None:
    adapter = _make_adapter(FakeMultiContextWS(), context_queue_maxsize=1)
    assert adapter.context_queue_maxsize == 1


class TestMultiContextWSManager:
    async def test_standalone_manager_closes_its_runtime_scope(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        scope = mgr._runtime_scope

        assert mgr._owns_runtime_scope is True
        assert scope.parent is None

        await mgr.aclose()

        assert scope.state is RuntimeScopeState.CLOSED

    async def test_attached_manager_leaves_parent_owned_scope_open(self):
        supervisor = RuntimeSupervisor(capacity=1)
        root = RuntimeScope.create_root(
            name="test-root",
            root_id="test:tts-manager",
            supervisor=supervisor,
            survivor_capacity=1,
        )
        provider_scope = root.create_child("tts-provider-runtime")
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(
            _make_adapter(ws),
            runtime_scope=provider_scope,
        )

        assert mgr._runtime_scope is provider_scope
        assert mgr._owns_runtime_scope is False

        await mgr.aclose()

        assert provider_scope.state is RuntimeScopeState.OPEN
        await root.close()

    async def test_attached_root_finalizer_closes_socket_before_reader_cohort(self):
        supervisor = RuntimeSupervisor(capacity=1)
        root = RuntimeScope.create_root(
            name="test-root",
            root_id="test:tts-manager-close",
            supervisor=supervisor,
            survivor_capacity=1,
        )
        provider_scope = root.create_child("tts-provider-runtime")
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(
            _make_adapter(ws),
            runtime_scope=provider_scope,
        )
        await mgr.connect()
        reader = mgr._reader_task

        state = await root.close(phases=(_CLOSE_FINALIZER, "tts-receive"))

        assert state is RuntimeScopeState.CLOSED
        assert ws.closed is True
        assert reader is not None and reader.done()
        assert mgr.runtime_cleanup_complete is True

    async def test_connect_warms_socket_without_opening_context(self):
        ws = FakeMultiContextWS()
        connect_calls = 0

        def _connect_factory(_hook):
            nonlocal connect_calls
            connect_calls += 1
            return ws

        mgr = MultiContextWSManager(_make_adapter(ws, connect_factory=_connect_factory))
        await mgr.connect()
        await mgr.connect()

        assert connect_calls == 1
        assert mgr._ws is ws
        assert mgr._contexts == {}
        assert mgr._reader_task in mgr._runtime_scope.tasks(_READER_TASK)
        assert mgr._runtime_scope.cohorts(force=False) == ("tts-receive",)
        assert mgr._runtime_scope.cohorts(force=True) == ("tts-receive",)
        await mgr.aclose()

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

    async def test_finish_context_drops_late_frame_without_stalling_successor(self):
        # A full finished-context queue can have a late frame waiting in the
        # reader when provider teardown runs. That late put must lose to done,
        # otherwise the shared reader stays blocked and strands later contexts.
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws, context_queue_maxsize=1))
        finished = await mgr.open_context()
        successor = await mgr.open_context()
        ws._script = [
            _chunk(finished.context_id, done=True),
            _chunk(finished.context_id),
            _chunk(successor.context_id, done=True),
        ]
        await mgr._cancel_background_tasks()
        ws._gate = asyncio.Event()
        mgr._reader_task = asyncio.create_task(mgr._reader_loop())

        assert (await anext(finished.frames()))["done"] is True
        mgr.finish_context(finished)

        successor_frame = await asyncio.wait_for(anext(successor.frames()), timeout=1)
        assert successor_frame["done"] is True
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

    async def test_on_reconnect_primer_does_not_reacquire_send_lock(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()
        frames = [json.dumps({"context_id": ctx.context_id, "transcript": "hi"})]
        await mgr.send(ctx, frames)
        ws.sent.clear()

        async with mgr._send_lock:
            await asyncio.wait_for(mgr._on_reconnect(), timeout=1)

        assert ws.sent == frames
        await mgr.aclose()

    async def test_on_reconnect_propagates_live_context_replay_failure(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()
        frames = [json.dumps({"context_id": ctx.context_id, "transcript": "hi"})]
        await mgr.send(ctx, frames)
        ws._fail_send_at.add(2)

        with pytest.raises(RuntimeError, match="send failed"):
            await mgr._on_reconnect()

        assert mgr._contexts[ctx.context_id] is ctx
        assert not ctx.done.is_set()
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
        sibling = await mgr.open_context()
        await mgr.cancel_context(ctx)

        assert ws.closed
        assert mgr._ws is None
        assert sibling.done.is_set()
        with pytest.raises(ConnectionError, match="context cancellation failed"):
            await anext(sibling.frames())
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

    async def test_aclose_bounds_wedged_graceful_close_frame(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import easycat.tts._multi_context_ws as multi_context_module

        send_started = asyncio.Event()

        class WedgedSendWS(FakeMultiContextWS):
            async def send(self, message: str) -> None:
                _ = message
                send_started.set()
                await asyncio.Future()

        monkeypatch.setattr(multi_context_module, "_SOCKET_CLOSE_SEND_TIMEOUT", 0.01)
        ws = WedgedSendWS()
        mgr = MultiContextWSManager(
            _make_adapter(ws, socket_close_frames=lambda: [json.dumps({"close_socket": True})])
        )
        await mgr.open_context()

        await asyncio.wait_for(mgr.aclose(), timeout=1)

        assert send_started.is_set()
        assert ws.closed

    async def test_aclose_retains_fail_once_socket_and_retries_exact_owner(self):
        ws = FakeMultiContextWS()
        close_error = RuntimeError("socket close failed")
        ws.close = AsyncMock(side_effect=[close_error, None])  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()
        reader = mgr._reader_task

        with pytest.raises(RuntimeError, match="socket close failed"):
            await mgr.aclose()

        assert mgr._closed is True
        assert mgr._ws is None
        assert mgr._pending_socket_close is ws
        assert mgr._socket_close_error is close_error
        assert reader is not None and reader.done()
        assert ctx.done.is_set()
        assert mgr._contexts == {}

        await mgr.aclose()
        assert mgr._pending_socket_close is None
        assert mgr._socket_close_error is None
        assert ws.close.await_count == 2

        # Successful close remains idempotent.
        await mgr.aclose()
        assert ws.close.await_count == 2

    async def test_concurrent_aclose_callers_share_one_close_failure_then_retry(self):
        ws = FakeMultiContextWS()
        close_entered = asyncio.Event()
        release_close = asyncio.Event()
        close_error = RuntimeError("socket close failed")

        async def fail_blocked_close() -> None:
            close_entered.set()
            await release_close.wait()
            raise close_error

        ws.close = AsyncMock(side_effect=fail_blocked_close)  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))
        await mgr.connect()

        leader = asyncio.create_task(mgr.aclose())
        await close_entered.wait()
        follower = asyncio.create_task(mgr.aclose())
        await asyncio.sleep(0)

        assert not leader.done()
        assert not follower.done()
        assert ws.close.await_count == 1

        release_close.set()
        outcomes = await asyncio.gather(leader, follower, return_exceptions=True)

        assert outcomes == [close_error, close_error]
        assert mgr._pending_socket_close is ws
        assert mgr._socket_close_error is close_error
        assert ws.close.await_count == 1

        # A later call starts a new transaction and retries this exact owner.
        ws.close.side_effect = None
        await mgr.aclose()
        assert mgr._pending_socket_close is None
        assert mgr._socket_close_error is None
        assert ws.close.await_count == 2

    async def test_aclose_converts_internal_child_cancellation_and_retries_owner(self):
        ws = FakeMultiContextWS()
        close_error = asyncio.CancelledError("socket close cancelled internally")
        ws.close = AsyncMock(side_effect=[close_error, None])  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))
        await mgr.connect()
        caller = asyncio.current_task()

        assert caller is not None
        assert caller.cancelling() == 0
        with pytest.raises(RuntimeError, match="cleanup was cancelled internally") as raised:
            await mgr.aclose()

        assert caller.cancelling() == 0
        assert raised.value.__cause__ is close_error
        assert mgr._pending_socket_close is ws
        assert mgr._socket_close_error is close_error
        assert ws.close.await_count == 1

        await mgr.aclose()
        assert mgr._pending_socket_close is None
        assert mgr._socket_close_error is None
        assert ws.close.await_count == 2

    async def test_cancelled_aclose_waiter_does_not_cancel_shared_close(self):
        ws = FakeMultiContextWS()
        close_entered = asyncio.Event()
        release_close = asyncio.Event()

        async def blocked_close() -> None:
            close_entered.set()
            await release_close.wait()
            ws.closed = True

        ws.close = AsyncMock(side_effect=blocked_close)  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))
        await mgr.connect()

        leader = asyncio.create_task(mgr.aclose())
        await close_entered.wait()
        follower = asyncio.create_task(mgr.aclose())
        await asyncio.sleep(0)
        follower.cancel()

        with pytest.raises(asyncio.CancelledError):
            await follower
        assert follower.cancelling() == 1
        assert not leader.done()
        assert mgr._close_owner_task is not None
        assert not mgr._close_owner_task.done()

        release_close.set()
        await leader
        assert ws.closed
        assert ws.close.await_count == 1

    async def test_close_wait_preserves_cancellation_pending_at_entry(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        await mgr.connect()

        async def cancel_before_await() -> None:
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
            await mgr.aclose()

        caller = asyncio.create_task(cancel_before_await())

        with pytest.raises(asyncio.CancelledError):
            await caller
        assert mgr._close_owner_task is None
        assert ws.closed is False

        await mgr.aclose()
        assert ws.closed is True

    async def test_cancel_fallback_closes_before_joining_wedged_sender(self, monkeypatch):
        monkeypatch.setattr(multi_context_ws_module, "_CANCEL_SEND_TIMEOUT", 0.01)
        ws = FakeMultiContextWS()
        send_entered = asyncio.Event()
        release_send = asyncio.Event()

        async def blocked_send(_frame: str) -> None:
            send_entered.set()
            await release_send.wait()

        async def close_and_unblock_send() -> None:
            ws.closed = True
            ws._gate.set()
            release_send.set()

        ws.send = AsyncMock(side_effect=blocked_send)  # type: ignore[method-assign]
        ws.close = AsyncMock(side_effect=close_and_unblock_send)  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()

        sending = asyncio.create_task(mgr.send(ctx, ["request"]))
        await send_entered.wait()
        cancelling = asyncio.create_task(mgr.cancel_context(ctx))

        await asyncio.wait_for(cancelling, timeout=1)
        with pytest.raises(RuntimeError, match="is closing"):
            await asyncio.wait_for(sending, timeout=1)

        assert ws.closed
        assert ws.close.await_count == 1
        assert ctx.done.is_set()

    async def test_cancel_fallback_does_not_mask_reader_failure_while_waiting_for_connect(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()
        lock_held = asyncio.Event()
        release_lock = asyncio.Event()

        async def hold_connect_lock() -> None:
            async with mgr._connect_lock:
                lock_held.set()
                await release_lock.wait()

        holder = asyncio.create_task(hold_connect_lock())
        await lock_held.wait()
        closing = asyncio.create_task(mgr._close_socket_only())
        await asyncio.sleep(0)

        assert mgr._closing is False
        ws._gate.set()
        assert mgr._reader_task is not None
        await mgr._reader_task
        assert isinstance(ctx.error, ConnectionError)

        release_lock.set()
        await asyncio.wait_for(asyncio.gather(holder, closing), timeout=1)

    async def test_cancel_fallback_and_aclose_do_not_double_close_socket(self):
        ws = FakeMultiContextWS(fail_send_at={1})
        close_entered = asyncio.Event()
        release_close = asyncio.Event()

        async def blocked_close() -> None:
            close_entered.set()
            await release_close.wait()
            ws.closed = True

        ws.close = AsyncMock(side_effect=blocked_close)  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()

        cancelling = asyncio.create_task(mgr.cancel_context(ctx))
        await close_entered.wait()
        closing = asyncio.create_task(mgr.aclose())
        await asyncio.sleep(0)

        assert mgr._closed is True
        assert not cancelling.done()
        assert not closing.done()

        release_close.set()
        await asyncio.gather(cancelling, closing)

        assert ws.closed
        assert ws.close.await_count == 1
        assert mgr._closing is True
        assert mgr._ws is None

    async def test_aclose_is_reentrant_from_owned_socket_close(self):
        ws = FakeMultiContextWS()
        mgr = MultiContextWSManager(_make_adapter(ws))
        await mgr.connect()

        async def reentrant_close() -> None:
            await mgr.aclose()
            ws.closed = True

        ws.close = AsyncMock(side_effect=reentrant_close)  # type: ignore[method-assign]

        await asyncio.wait_for(mgr.aclose(), timeout=1)

        assert ws.closed
        assert ws.close.await_count == 1

    async def test_aclose_waits_for_blocked_connect_and_prevents_late_reader(self):
        ws = FakeMultiContextWS()
        connect_entered = asyncio.Event()
        release_connect = asyncio.Event()

        async def blocked_connect() -> None:
            connect_entered.set()
            await release_connect.wait()

        async def record_close() -> None:
            ws.closed = True

        ws.connect = AsyncMock(side_effect=blocked_connect)  # type: ignore[method-assign]
        ws.close = AsyncMock(side_effect=record_close)  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))

        connecting = asyncio.create_task(mgr.connect())
        await connect_entered.wait()
        closing = asyncio.create_task(mgr.aclose())
        await asyncio.sleep(0)

        assert mgr._closed is True
        assert not connecting.done()
        assert not closing.done()

        release_connect.set()
        with pytest.raises(RuntimeError, match="closed during connect"):
            await connecting
        await closing
        await asyncio.sleep(0)

        assert ws.closed
        assert ws.connect.await_count == 1
        assert ws.close.await_count == 1
        assert mgr._ws is None
        assert mgr._reader_task is None
        with pytest.raises(RuntimeError, match="is closed"):
            await mgr.open_context()

    async def test_aclose_waits_for_admitted_send_before_context_and_socket_teardown(self):
        ws = FakeMultiContextWS()
        send_entered = asyncio.Event()
        release_send = asyncio.Event()

        async def blocked_send(_frame: str) -> None:
            send_entered.set()
            await release_send.wait()

        ws.send = AsyncMock(side_effect=blocked_send)  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()

        sending = asyncio.create_task(mgr.send(ctx, ["request"]))
        await send_entered.wait()
        closing = asyncio.create_task(mgr.aclose())
        await asyncio.sleep(0)

        assert mgr._closed is True
        assert not sending.done()
        assert not closing.done()
        assert ctx.done.is_set() is False
        assert ws.closed is False

        release_send.set()
        with pytest.raises(RuntimeError, match="is closing"):
            await asyncio.wait_for(sending, timeout=1)
        await asyncio.wait_for(closing, timeout=1)

        assert ws.closed
        assert ctx.done.is_set()
        assert ctx.pending_frames is None
        assert ctx.context_id not in mgr._contexts

    async def test_send_queued_before_aclose_rechecks_admission_inside_send_lock(self):
        ws = FakeMultiContextWS()
        first_send_entered = asyncio.Event()
        release_first_send = asyncio.Event()

        async def blocked_first_send(_frame: str) -> None:
            first_send_entered.set()
            await release_first_send.wait()

        ws.send = AsyncMock(side_effect=blocked_first_send)  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()

        admitted = asyncio.create_task(mgr.send(ctx, ["first"]))
        await first_send_entered.wait()
        queued = asyncio.create_task(mgr.send(ctx, ["second"]))
        await asyncio.sleep(0)
        closing = asyncio.create_task(mgr.aclose())
        await asyncio.sleep(0)

        assert mgr._closed is True
        assert not admitted.done()
        assert not queued.done()
        assert not closing.done()

        release_first_send.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(admitted, queued, return_exceptions=True),
            timeout=1,
        )
        await asyncio.wait_for(closing, timeout=1)

        assert all(isinstance(outcome, RuntimeError) for outcome in outcomes)
        assert all("is closing" in str(outcome) for outcome in outcomes)
        assert ws.send.await_count == 1
        assert ctx.pending_frames is None
        assert ctx.done.is_set()

    async def test_aclose_always_failing_socket_remains_retry_owned(self):
        ws = FakeMultiContextWS()
        ws.close = AsyncMock(side_effect=RuntimeError("socket close always fails"))  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))
        await mgr.connect()

        for expected_count in (1, 2):
            with pytest.raises(RuntimeError, match="socket close always fails"):
                await mgr.aclose()
            assert mgr._pending_socket_close is ws
            assert ws.close.await_count == expected_count

    async def test_cancel_fallback_blocks_replacement_until_failed_close_retries(self):
        ws = FakeMultiContextWS(fail_send_at={1})
        close_error = RuntimeError("cancel fallback close failed")
        ws.close = AsyncMock(side_effect=[close_error, None])  # type: ignore[method-assign]
        mgr = MultiContextWSManager(_make_adapter(ws))
        ctx = await mgr.open_context()

        with pytest.raises(RuntimeError, match="cancel fallback close failed"):
            await mgr.cancel_context(ctx)

        assert ctx.done.is_set()
        assert mgr._pending_socket_close is ws

        replacement = FakeMultiContextWS()
        replacement_factory_calls = 0

        def replacement_factory(_hook):
            nonlocal replacement_factory_calls
            replacement_factory_calls += 1
            return replacement

        mgr._adapter = _make_adapter(
            replacement,
            connect_factory=replacement_factory,
        )
        new_ctx = await mgr.open_context()

        assert ws.close.await_count == 2
        assert replacement_factory_calls == 1
        assert mgr._ws is replacement
        assert new_ctx.context_id != ctx.context_id
        await mgr.aclose()

    async def test_failed_connect_keeps_primary_and_blocks_replacement_until_cleanup(self):
        connect_error = RuntimeError("initial connect failed")
        cleanup_one = RuntimeError("rollback close one")
        cleanup_two = RuntimeError("rollback close two")
        failed = FakeMultiContextWS()
        failed.connect = AsyncMock(side_effect=connect_error)  # type: ignore[method-assign]
        failed.close = AsyncMock(side_effect=[cleanup_one, cleanup_two, None])  # type: ignore[method-assign]
        replacement = FakeMultiContextWS()
        candidates = [failed, replacement]
        factory_calls = 0

        def factory(_hook):
            nonlocal factory_calls
            candidate = candidates[factory_calls]
            factory_calls += 1
            return candidate

        mgr = MultiContextWSManager(_make_adapter(failed, connect_factory=factory))

        with pytest.raises(RuntimeError, match="initial connect failed") as first:
            await mgr.connect()
        assert first.value.__cause__ is cleanup_one
        assert mgr._pending_socket_close is failed
        assert factory_calls == 1

        with pytest.raises(RuntimeError, match="cleanup is incomplete") as second:
            await mgr.connect()
        assert second.value.__cause__ is cleanup_two
        assert mgr._pending_socket_close is failed
        assert factory_calls == 1

        await mgr.connect()
        assert failed.close.await_count == 3
        assert mgr._pending_socket_close is None
        assert mgr._ws is replacement
        assert factory_calls == 2
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
