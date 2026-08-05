"""Tests for ReconnectingWebSocket wrapper."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import websockets.exceptions
import websockets.frames

from easycat.events import EventBus, ReconnectAttempt, ReconnectFailure, ReconnectSuccess
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket, connect_until_stopped


class FakeWSConnection:
    """Mock websockets ClientConnection."""

    def __init__(self):
        self.close_code = None
        self._messages: list[str | bytes] = []
        self._sent: list[str | bytes] = []
        self.close = AsyncMock()

    async def send(self, msg: str | bytes) -> None:
        self._sent.append(msg)

    async def recv(self) -> str | bytes:
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for msg in self._messages:
            yield msg


class TestReconnectConfig:
    def test_defaults(self):
        config = ReconnectConfig()
        assert config.max_retries == 3
        assert config.base_delay == 1.0
        assert config.max_delay == 30.0
        assert config.backoff_factor == 2.0
        assert config.jitter_factor == 0.5
        assert config.extra_headers == {}

    def test_custom(self):
        config = ReconnectConfig(
            max_retries=5,
            base_delay=0.5,
            jitter_factor=0.0,
            extra_headers={"Authorization": "Bearer test"},
        )
        assert config.max_retries == 5
        assert config.jitter_factor == 0.0
        assert config.extra_headers["Authorization"] == "Bearer test"

    def test_unlimited_retries(self):
        config = ReconnectConfig(max_retries=-1)
        assert config.max_retries == -1

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("max_retries", -2, "max_retries"),
            ("max_retries", True, "max_retries"),
            ("base_delay", 0.0, "base_delay"),
            ("base_delay", -0.1, "base_delay"),
            ("max_delay", 0.0, "max_delay"),
            ("backoff_factor", 0.99, "backoff_factor"),
            ("jitter_factor", -0.01, "jitter_factor"),
            ("jitter_factor", 1.01, "jitter_factor"),
            ("jitter_factor", True, "jitter_factor"),
            ("base_delay", float("nan"), "base_delay"),
            ("base_delay", float("inf"), "base_delay"),
            ("max_delay", float("nan"), "max_delay"),
            ("max_delay", float("inf"), "max_delay"),
            ("backoff_factor", float("nan"), "backoff_factor"),
            ("backoff_factor", float("inf"), "backoff_factor"),
            ("jitter_factor", float("nan"), "jitter_factor"),
            ("jitter_factor", float("inf"), "jitter_factor"),
            ("base_delay", 10**1000, "base_delay"),
            ("max_delay", 10**1000, "max_delay"),
            ("backoff_factor", 10**1000, "backoff_factor"),
            ("jitter_factor", 10**1000, "jitter_factor"),
        ],
    )
    def test_invalid_retry_policy_rejected(self, field: str, value: object, message: str):
        with pytest.raises(ValueError, match=message):
            ReconnectConfig(**{field: value})

    def test_max_delay_must_cover_base_delay(self):
        with pytest.raises(ValueError, match="max_delay"):
            ReconnectConfig(base_delay=10.0, max_delay=5.0)


class _ConnectUntilStoppedClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.connected = False
        self.connect_cancelled = False
        self.closed = False

    async def connect(self) -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.connect_cancelled = True
            raise
        self.connected = True

    async def close(self) -> None:
        self.closed = True


async def test_connect_until_stopped_returns_true_when_connect_finishes() -> None:
    client = _ConnectUntilStoppedClient()
    client.release.set()

    assert await connect_until_stopped(client, asyncio.Event()) is True  # type: ignore[arg-type]
    assert client.connected
    assert not client.closed


async def test_connect_until_stopped_closes_when_stop_fires_first() -> None:
    client = _ConnectUntilStoppedClient()
    stop = asyncio.Event()

    task = asyncio.create_task(connect_until_stopped(client, stop))  # type: ignore[arg-type]
    await client.started.wait()
    stop.set()

    assert await task is False
    assert not client.connected
    assert client.closed


async def test_connect_until_stopped_cancellation_closes_and_reaps_connector() -> None:
    client = _ConnectUntilStoppedClient()
    task = asyncio.create_task(  # type: ignore[arg-type]
        connect_until_stopped(client, asyncio.Event())
    )
    await client.started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.connect_cancelled
    assert client.closed
    assert not client.connected


async def test_connect_until_stopped_preserves_connect_failure_without_exception_group() -> None:
    class FailingClient(_ConnectUntilStoppedClient):
        async def connect(self) -> None:
            raise RuntimeError("connect failed")

    client = FailingClient()

    with pytest.raises(RuntimeError, match="connect failed"):
        await connect_until_stopped(client, asyncio.Event())  # type: ignore[arg-type]


async def test_connect_until_stopped_preserves_stop_close_failure() -> None:
    class FailingCloseClient(_ConnectUntilStoppedClient):
        async def close(self) -> None:
            raise RuntimeError("stop close failed")

    client = FailingCloseClient()
    stop = asyncio.Event()
    joining = asyncio.create_task(
        connect_until_stopped(client, stop)  # type: ignore[arg-type]
    )
    await client.started.wait()
    stop.set()

    with pytest.raises(RuntimeError, match="stop close failed"):
        await joining


class TestReconnectingWebSocket:
    def _make_ws(self, url: str = "wss://test.com", **kwargs) -> ReconnectingWebSocket:
        config = ReconnectConfig(**kwargs)
        return ReconnectingWebSocket(url=url, config=config)

    async def test_connect_success(self):
        ws = self._make_ws()
        fake_conn = FakeWSConnection()

        with patch("easycat.reconnecting_ws.websockets.connect", new_callable=AsyncMock) as mock:
            mock.return_value = fake_conn
            await ws.connect()

        assert ws.is_connected
        assert ws._ws is fake_conn

    async def test_connect_when_already_connected_is_noop(self):
        ws = self._make_ws()
        fake_conn = FakeWSConnection()
        ws._ws = fake_conn

        with patch("easycat.reconnecting_ws.websockets.connect", new_callable=AsyncMock) as mock:
            await ws.connect()

        mock.assert_not_called()
        assert ws._ws is fake_conn
        fake_conn.close.assert_not_called()

    async def test_connect_retry_on_failure(self):
        ws = self._make_ws(base_delay=0.01, max_retries=2, jitter_factor=0.0)
        fake_conn = FakeWSConnection()

        call_count = 0

        async def mock_connect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("fail")
            return fake_conn

        with patch("easycat.reconnecting_ws.websockets.connect", side_effect=mock_connect):
            await ws.connect()

        assert call_count == 3
        assert ws.is_connected

    async def test_connect_all_retries_fail(self):
        ws = self._make_ws(base_delay=0.01, max_retries=2, jitter_factor=0.0)

        with (
            patch(
                "easycat.reconnecting_ws.websockets.connect",
                new_callable=AsyncMock,
                side_effect=ConnectionError("fail"),
            ),
            pytest.raises(ConnectionError, match="Failed to connect"),
        ):
            await ws.connect()

    @staticmethod
    def _attach_live(ws: ReconnectingWebSocket, conn) -> None:
        """Simulate a successfully connected socket without patching connect()."""
        ws._ws = conn
        ws._ever_connected = True
        ws._connected.set()

    async def test_send(self):
        ws = self._make_ws()
        fake_conn = FakeWSConnection()
        self._attach_live(ws, fake_conn)

        await ws.send("hello")
        assert fake_conn._sent == ["hello"]

    async def test_send_bytes(self):
        ws = self._make_ws()
        fake_conn = FakeWSConnection()
        self._attach_live(ws, fake_conn)

        await ws.send(b"\x00\x01")
        assert fake_conn._sent == [b"\x00\x01"]

    async def test_send_not_connected_raises(self):
        ws = self._make_ws()
        with pytest.raises(RuntimeError, match="not connected"):
            await ws.send("hello")

    async def test_recv_not_connected_raises(self):
        ws = self._make_ws()
        with pytest.raises(RuntimeError, match="not connected"):
            await ws.recv()

    async def test_close(self):
        ws = self._make_ws()
        fake_conn = FakeWSConnection()
        ws._ws = fake_conn

        await ws.close()
        assert ws._ws is None
        assert ws._closed
        fake_conn.close.assert_called_once()

    async def test_close_when_not_connected(self):
        ws = self._make_ws()
        await ws.close()
        assert ws._closed

    async def test_connect_after_close_raises(self):
        ws = self._make_ws()
        await ws.close()

        with pytest.raises(RuntimeError, match="has been closed"):
            await ws.connect()

    async def test_is_connected_false_when_closed(self):
        ws = self._make_ws()
        assert not ws.is_connected

        fake_conn = FakeWSConnection()
        ws._ws = fake_conn
        assert ws.is_connected

        fake_conn.close_code = 1000
        assert not ws.is_connected

    async def test_recv_iter(self):
        ws = self._make_ws()
        fake_conn = FakeWSConnection()
        fake_conn._messages = ["msg1", "msg2", b"msg3"]
        ws._ws = fake_conn
        ws._closed = True

        messages = []
        async for msg in ws.recv_iter():
            messages.append(msg)

        assert messages == ["msg1", "msg2", b"msg3"]

    async def test_recv_iter_not_connected_raises(self):
        ws = self._make_ws()
        with pytest.raises(RuntimeError, match="not connected"):
            async for _ in ws.recv_iter():
                pass

    async def test_recv_iter_reconnects_on_connection_closed(self):
        """recv_iter should reconnect and keep yielding after a transient drop."""
        callback = AsyncMock()
        config = ReconnectConfig(base_delay=0.01, max_retries=2, jitter_factor=0.0)
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=config,
            on_reconnect=callback,  # Required for recv_iter to attempt reconnection
        )

        close_frame = websockets.frames.Close(1006, "abnormal")

        class DroppingConnection:
            def __init__(self, msgs):
                self._msgs = msgs
                self.close_code = None
                self.close = AsyncMock()

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                for m in self._msgs:
                    yield m
                raise websockets.exceptions.ConnectionClosed(close_frame, None)

        drop_conn = DroppingConnection(["msg1", "msg2"])
        resume_conn = FakeWSConnection()
        resume_conn._messages = ["msg3", "msg4"]
        ws._ws = drop_conn

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            return_value=resume_conn,
        ):
            messages = []
            async for msg in ws.recv_iter():
                messages.append(msg)
                if len(messages) == 4:
                    break
            await ws.close()

        assert messages == ["msg1", "msg2", "msg3", "msg4"]
        callback.assert_awaited_once()

    async def test_recv_iter_limits_successful_reconnect_cycles(self):
        """Successful reconnects are capped to prevent accept/drop churn."""
        callback = AsyncMock()
        config = ReconnectConfig(base_delay=0.01, max_retries=1, jitter_factor=0.0)
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=config,
            on_reconnect=callback,
        )

        close_frame = websockets.frames.Close(1006, "abnormal")

        class DroppingConnection:
            close_code = None
            close = AsyncMock()

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                yield "msg"
                raise websockets.exceptions.ConnectionClosed(close_frame, None)

        ws._ws = DroppingConnection()

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            return_value=DroppingConnection(),
        ) as connect_mock:
            messages = []
            async for msg in ws.recv_iter():
                messages.append(msg)

        assert messages == ["msg", "msg"]
        connect_mock.assert_awaited_once()
        callback.assert_awaited_once()
        assert ws._ws is None
        assert ws.reconnect_attempts_exhausted == 1
        assert ws.reconnect_exhaustion_reason == "successful reconnect cycle budget"

    async def test_normal_peer_close_uses_reconnect_policy(self):
        disconnect = AsyncMock()
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0),
            on_reconnect=AsyncMock(),
            on_disconnect=disconnect,
        )
        connection = FakeWSConnection()
        connection.close_code = 1000
        ws._ws = connection

        assert [message async for message in ws.recv_iter()] == []

        disconnect.assert_awaited_once()
        assert ws.died_abnormally is True
        assert ws.reconnect_attempts_exhausted == 0
        assert ws.reconnect_exhaustion_reason == "successful reconnect cycle budget"

    async def test_disconnect_callback_can_close_without_reconnecting(self):
        connect_fn = AsyncMock()
        close_frame = websockets.frames.Close(1006, "abnormal")

        class DroppingConnection(FakeWSConnection):
            async def _aiter(self):
                raise websockets.exceptions.ConnectionClosed(close_frame, None)
                yield  # pragma: no cover

        ws: ReconnectingWebSocket

        async def disconnect(exc):
            await ws.close()

        ws = ReconnectingWebSocket(
            url="wss://test.com",
            connect_fn=connect_fn,
            on_reconnect=AsyncMock(),
            on_disconnect=disconnect,
        )
        ws._ws = DroppingConnection()

        assert [message async for message in ws.recv_iter()] == []
        connect_fn.assert_not_awaited()
        assert ws._closed is True

    async def test_close_interrupts_cancellation_resistant_connector(self):
        started = asyncio.Event()
        release = asyncio.Event()
        late_connection = FakeWSConnection()

        async def connect_fn(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()
                return late_connection

        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=-1),
            connect_fn=connect_fn,
        )
        connect_task = asyncio.create_task(ws.connect())
        await started.wait()

        await asyncio.wait_for(ws.close(), timeout=0.1)
        with pytest.raises(ConnectionError, match="closed during reconnect"):
            await asyncio.wait_for(connect_task, timeout=0.1)

        assert not ws._background_tasks.empty
        release.set()
        for _ in range(10):
            if late_connection.close.await_count:
                break
            await asyncio.sleep(0)
        late_connection.close.assert_awaited_once()
        for _ in range(10):
            if ws._background_tasks.empty:
                break
            await asyncio.sleep(0)
        assert ws._background_tasks.empty

    async def test_overlapping_connection_races_keep_distinct_ownership(self):
        first_release = asyncio.Event()
        second_release = asyncio.Event()
        first_connection = FakeWSConnection()
        second_connection = FakeWSConnection()

        async def connect(
            release: asyncio.Event,
            connection: FakeWSConnection,
        ) -> FakeWSConnection:
            await release.wait()
            return connection

        ws = ReconnectingWebSocket(url="wss://test.com")
        first = asyncio.create_task(
            ws._connect_attempt_or_close(connect(first_release, first_connection))
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            ws._connect_attempt_or_close(connect(second_release, second_connection))
        )
        await asyncio.sleep(0)

        first_release.set()
        second_release.set()

        assert await first is first_connection
        assert await second is second_connection
        assert ws._background_tasks.empty

    async def test_late_connection_close_failure_is_retained_for_close_retry(self):
        started = asyncio.Event()
        release = asyncio.Event()
        late_connection = FakeWSConnection()
        late_connection.close.side_effect = [RuntimeError("late close failed"), None]

        async def connect_fn(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()
                return late_connection

        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=-1),
            connect_fn=connect_fn,
        )
        connect_task = asyncio.create_task(ws.connect())
        await started.wait()
        await ws.close()
        with pytest.raises(ConnectionError, match="closed during reconnect"):
            await connect_task

        release.set()
        for _ in range(20):
            if late_connection.close.await_count:
                break
            await asyncio.sleep(0)

        assert late_connection.close.await_count == 1
        assert ws._pending_connection_closes == [late_connection]
        await ws.close()
        assert late_connection.close.await_count == 2
        assert ws._pending_connection_closes == []

    async def test_successful_manual_connect_clears_terminal_reconnect_state(self):
        ws = self._make_ws(max_retries=0)
        ws._mark_reconnect_exhausted(0, "successful reconnect cycle budget")

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            return_value=FakeWSConnection(),
        ):
            await ws.connect()

        assert ws.died_abnormally is False
        assert ws.reconnect_attempts_exhausted is None
        assert ws.reconnect_exhaustion_reason is None

    async def test_send_waits_for_in_progress_reconnect(self):
        """A send during a recv_iter-driven reconnect blocks for the new socket.

        Models Findings 2/3: the write path must not race a half-replaced
        socket. With the reconnect window open (``_connected`` cleared after a
        drop), ``send()`` waits until the new socket is attached, then writes
        to it rather than the closed one.
        """
        ws = self._make_ws(base_delay=0.01, max_retries=2, jitter_factor=0.0)
        old_conn = FakeWSConnection()
        new_conn = FakeWSConnection()
        # Simulate the state right after a drop: ever-connected, but the
        # connected event is cleared while a reconnect is in flight.
        ws._ws = old_conn
        ws._ever_connected = True
        ws._connected.clear()

        send_task = asyncio.create_task(ws.send("frame"))
        await asyncio.sleep(0)  # let send() start and block on the event
        assert not send_task.done()
        assert old_conn._sent == []

        # Reconnect completes: new socket attached, event set.
        ws._ws = new_conn
        ws._connected.set()
        await send_task

        # The frame landed on the *new* socket, not the stale one.
        assert new_conn._sent == ["frame"]
        assert old_conn._sent == []

    async def test_send_waits_until_reconnect_callback_primes_socket(self):
        """Ordinary writers cannot overtake a reconnect callback's primer."""
        candidate = FakeWSConnection()
        primer_sent = asyncio.Event()
        release_primer = asyncio.Event()

        async def prime_connection() -> None:
            # The callback itself must still be able to send while the public
            # connection-ready gate remains closed.
            await ws.send("session.update")
            primer_sent.set()
            await release_primer.wait()

        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0),
            on_reconnect=prime_connection,
        )
        # This is a transparent reconnect, not the first connection: writers
        # are therefore expected to wait for installation rather than
        # fast-failing as they would before a socket has ever connected.
        ws._ever_connected = True
        install = asyncio.create_task(
            ws._install_connection(candidate, attempt=0, notify_reconnect=True)
        )
        await primer_sent.wait()

        ordinary_send = asyncio.create_task(ws.send("audio.append"))
        try:
            await asyncio.sleep(0)
            assert candidate._sent == ["session.update"]
            assert not ordinary_send.done()
        finally:
            release_primer.set()
            await install
        await ordinary_send
        assert candidate._sent == ["session.update", "audio.append"]

    async def test_send_prepared_runs_factory_after_reconnect_callback(self):
        """Stateful frame preparation waits for reconnect state reset."""
        candidate = FakeWSConnection()
        callback_started = asyncio.Event()
        release_callback = asyncio.Event()
        state = "dropped"

        async def reset_state() -> None:
            nonlocal state
            state = "replacement"
            callback_started.set()
            await release_callback.wait()

        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0),
            on_reconnect=reset_state,
        )
        ws._ever_connected = True
        install = asyncio.create_task(
            ws._install_connection(candidate, attempt=0, notify_reconnect=True)
        )
        await callback_started.wait()

        prepared_states: list[str] = []

        def prepare() -> bytes:
            prepared_states.append(state)
            return state.encode()

        send = asyncio.create_task(ws.send_prepared(prepare))
        await asyncio.sleep(0)
        assert prepared_states == []

        release_callback.set()
        await install
        assert await send is True
        assert prepared_states == ["replacement"]
        assert candidate._sent == [b"replacement"]

    async def test_spawned_callback_task_loses_bypass_after_installation(self):
        candidate = FakeWSConnection()
        release_child = asyncio.Event()
        child_result: asyncio.Task[object] | None = None

        async def inspect_after_callback() -> object:
            await release_child.wait()
            return ws._reconnect_callback_connection()

        async def prime_connection() -> None:
            nonlocal child_result
            await ws.send("session.update")
            child_result = asyncio.create_task(inspect_after_callback())

        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0),
            on_reconnect=prime_connection,
        )
        ws._ever_connected = True

        await ws._install_connection(candidate, attempt=0, notify_reconnect=True)
        assert child_result is not None
        release_child.set()

        assert await child_result is None

    async def test_send_times_out_if_reconnect_never_completes(self):
        ws = self._make_ws(base_delay=0.01, max_retries=2, jitter_factor=0.0)
        ws._ws = FakeWSConnection()
        ws._ever_connected = True
        ws._connected.clear()
        ws._send_wait_timeout = 0.01

        with pytest.raises(RuntimeError, match="not connected"):
            await ws.send("frame")

    async def test_recv_iter_raises_without_on_reconnect(self):
        """recv_iter should propagate ConnectionClosed when no on_reconnect is set.

        Stateful providers (e.g. ElevenLabs STT/TTS, Deepgram TTS) send init
        messages once; reconnecting without replaying those stalls the stream.
        Without an on_reconnect callback the provider cannot reinitialize, so
        the error should surface for a clean restart.
        """
        ws = self._make_ws(base_delay=0.01, max_retries=1, jitter_factor=0.0)

        close_frame = websockets.frames.Close(1006, "abnormal")

        class DroppingConnection:
            def __init__(self):
                self.close_code = None
                self.close = AsyncMock()

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                yield "msg1"
                raise websockets.exceptions.ConnectionClosed(close_frame, None)

        ws._ws = DroppingConnection()

        messages = []
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            async for msg in ws.recv_iter():
                messages.append(msg)

        # Should have received the message before the disconnect.
        assert messages == ["msg1"]

    async def test_recv_iter_gives_up_when_reconnect_fails(self):
        config = ReconnectConfig(base_delay=0.01, max_retries=1, jitter_factor=0.0)
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=config,
            on_reconnect=AsyncMock(),
        )

        close_frame = websockets.frames.Close(1006, "abnormal")

        class DroppingConnection:
            def __init__(self):
                self.close_code = None
                self.close = AsyncMock()

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                yield "msg1"
                raise websockets.exceptions.ConnectionClosed(close_frame, None)

        ws._ws = DroppingConnection()

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            side_effect=ConnectionError("down"),
        ):
            messages = []
            async for msg in ws.recv_iter():
                messages.append(msg)

        # Iterator ends cleanly instead of raising — downstream TTS
        # consumers see a normal end-of-stream, not an unhandled exception.
        assert messages == ["msg1"]
        assert ws.died_abnormally is True
        assert ws.reconnect_attempts_exhausted == 2

    async def test_send_fast_fails_after_recv_iter_gives_up(self):
        """Finding 1: a send after recv_iter gives up fast-fails.

        When a recv_iter-driven reconnect is exhausted the socket is
        terminally dead (``_ws`` nulled, ``_connected`` cleared, no reconnect
        in flight). A subsequent send() must raise immediately rather than
        burning the full ``_send_wait_timeout`` waiting on a reconnect that
        will never happen.
        """
        config = ReconnectConfig(base_delay=0.01, max_retries=1, jitter_factor=0.0)
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=config,
            on_reconnect=AsyncMock(),
        )
        # Give it a generous wait timeout so a slow path would be observable.
        ws._send_wait_timeout = 5.0

        close_frame = websockets.frames.Close(1006, "abnormal")

        class DroppingConnection:
            def __init__(self):
                self.close_code = None
                self.close = AsyncMock()

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                yield "msg1"
                raise websockets.exceptions.ConnectionClosed(close_frame, None)

        ws._ws = DroppingConnection()

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            side_effect=ConnectionError("down"),
        ):
            async for _ in ws.recv_iter():
                pass

        # recv_iter nulled the socket on give-up.
        assert ws._ws is None

        # send() must fast-fail well under the (5s) wait timeout.
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(RuntimeError, match="not connected"):
            await ws.send("frame")
        assert loop.time() - start < 1.0

    async def test_send_raises_when_close_wakes_blocked_sender(self):
        """Finding 2: a sender blocked during close() never writes to the socket.

        A ``send()`` parked in ``_await_connected`` while the connection is
        closed must fail rather than snapshot and write to the now-closing
        socket. Exactly *how* it fails (``RuntimeError`` once it observes the
        closed/cleared socket, or a wait timeout/cancellation if it had not yet
        re-checked) is scheduler- and Python-version-dependent; the invariant
        we guard is that ``send()`` does not succeed and the frame never lands
        on the stale socket.
        """
        ws = self._make_ws(base_delay=0.01, max_retries=2, jitter_factor=0.0)
        old_conn = FakeWSConnection()
        ws._ws = old_conn
        ws._ever_connected = True
        ws._connected.clear()
        ws._send_wait_timeout = 0.05  # small so a parked sender fails fast

        send_task = asyncio.create_task(ws.send("frame"))
        await asyncio.sleep(0)  # let send() start
        assert not send_task.done()

        await ws.close()

        with pytest.raises((RuntimeError, TimeoutError, asyncio.CancelledError)):
            await send_task
        # The frame never landed on the now-closing socket.
        assert old_conn._sent == []

    async def test_recv_iter_no_reconnect_after_explicit_close(self):
        ws = self._make_ws(base_delay=0.01)

        close_frame = websockets.frames.Close(1006, "abnormal")

        class DroppingConnection:
            def __init__(self):
                self.close_code = None
                self.close = AsyncMock()

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                yield "msg1"
                raise websockets.exceptions.ConnectionClosed(close_frame, None)

        ws._ws = DroppingConnection()
        ws._closed = True

        messages = []
        async for msg in ws.recv_iter():
            messages.append(msg)

        assert messages == ["msg1"]

    async def test_close_during_reconnect_backoff_is_not_abnormal_exhaustion(
        self,
        monkeypatch,
    ):
        """Deliberate shutdown interrupts recovery without producing E305 state."""
        reconnect = AsyncMock()
        disconnect = AsyncMock()
        config = ReconnectConfig(
            base_delay=0.01,
            max_delay=0.01,
            max_retries=2,
            jitter_factor=0.0,
        )
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=config,
            on_reconnect=reconnect,
            on_disconnect=disconnect,
        )

        close_frame = websockets.frames.Close(1006, "abnormal")

        class DroppingConnection:
            close_code = None

            def __init__(self):
                self.close = AsyncMock()

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                yield "msg1"
                raise websockets.exceptions.ConnectionClosed(close_frame, None)

        old_conn = DroppingConnection()
        self._attach_live(ws, old_conn)

        real_sleep = asyncio.sleep
        backoff_started = asyncio.Event()
        release_backoff = asyncio.Event()

        async def controlled_sleep(delay: float) -> None:
            assert delay == config.base_delay
            backoff_started.set()
            await release_backoff.wait()

        monkeypatch.setattr("easycat.reconnecting_ws.asyncio.sleep", controlled_sleep)

        async def consume() -> list[str | bytes]:
            return [message async for message in ws.recv_iter()]

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            side_effect=ConnectionError("provider unavailable"),
        ) as connect_mock:
            receive_task = asyncio.create_task(consume())
            await backoff_started.wait()

            close_task = asyncio.create_task(ws.close())
            await real_sleep(0)
            assert ws._closed is True

            assert await asyncio.wait_for(receive_task, timeout=0.1) == ["msg1"]
            await asyncio.wait_for(close_task, timeout=0.1)
            release_backoff.set()

        connect_mock.assert_awaited_once()
        disconnect.assert_awaited_once()
        reconnect.assert_not_awaited()
        old_conn.close.assert_awaited_once()
        assert ws.died_abnormally is False
        assert ws.reconnect_attempts_exhausted is None

    # ── Additional tests: jitter, event bus, callbacks ───────

    async def test_jitter_applies_to_delay(self):
        ws = self._make_ws(jitter_factor=0.5)
        delay = ws._compute_delay(1.0)
        assert 0.5 <= delay <= 1.5

    async def test_no_jitter_when_factor_zero(self):
        ws = self._make_ws(jitter_factor=0.0)
        delay = ws._compute_delay(2.0)
        assert delay == 2.0

    async def test_event_bus_receives_reconnect_events(self):
        event_bus = EventBus()
        events_received = []

        async def handler(event):
            events_received.append(event)

        event_bus.subscribe(ReconnectAttempt, handler)
        event_bus.subscribe(ReconnectSuccess, handler)

        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0, jitter_factor=0.0),
            event_bus=event_bus,
            provider_name="test_provider",
        )
        fake_conn = FakeWSConnection()

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            return_value=fake_conn,
        ):
            await ws.connect()

        assert len(events_received) == 2
        assert isinstance(events_received[0], ReconnectAttempt)
        assert events_received[0].provider == "test_provider"
        assert events_received[0].attempt == 1
        assert isinstance(events_received[1], ReconnectSuccess)

    async def test_strict_success_observer_rolls_back_candidate_without_retry(self):
        event_bus = EventBus(handler_error_policy="raise")

        async def reject_success(event):
            raise RuntimeError("success observer failed")

        event_bus.subscribe(ReconnectSuccess, reject_success)
        candidate = FakeWSConnection()
        connect_fn = AsyncMock(return_value=candidate)
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=3, base_delay=0.001, jitter_factor=0.0),
            event_bus=event_bus,
            connect_fn=connect_fn,
        )

        with pytest.raises(RuntimeError, match="success observer failed"):
            await ws.connect()

        connect_fn.assert_awaited_once()
        candidate.close.assert_awaited_once()
        assert ws._ws is None
        assert ws._connected.is_set() is False
        assert ws._ever_connected is False

    async def test_reconnect_callback_failure_rolls_back_unprimed_candidate(self):
        events_received = []
        event_bus = EventBus()
        event_bus.subscribe(ReconnectSuccess, events_received.append)
        callback = AsyncMock(side_effect=RuntimeError("request replay failed"))
        candidate = FakeWSConnection()
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0),
            event_bus=event_bus,
            on_reconnect=callback,
        )

        with pytest.raises(RuntimeError, match="request replay failed"):
            await ws._install_connection(candidate, attempt=0, notify_reconnect=True)

        callback.assert_awaited_once()
        candidate.close.assert_awaited_once()
        assert events_received == []
        assert ws._ws is None
        assert ws._connected.is_set() is False
        assert ws._ever_connected is False

    async def test_committed_connection_close_failure_is_retained_for_retry(self):
        candidate = FakeWSConnection()
        candidate.close.side_effect = [RuntimeError("active close failed"), None]
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0),
            connect_fn=AsyncMock(return_value=candidate),
        )
        await ws.connect()

        with pytest.raises(RuntimeError, match="connection cleanup is incomplete"):
            await ws.close()

        assert ws._pending_connection_closes == [candidate]
        await ws.close()
        assert candidate.close.await_count == 2
        assert ws._pending_connection_closes == []

    async def test_internal_connection_close_cancel_is_retryable_without_cancelling_caller(self):
        candidate = FakeWSConnection()
        candidate.close.side_effect = [asyncio.CancelledError(), None]
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0),
            connect_fn=AsyncMock(return_value=candidate),
        )
        await ws.connect()

        with pytest.raises(RuntimeError, match="connection cleanup is incomplete"):
            await ws.close()

        caller = asyncio.current_task()
        assert caller is not None
        assert caller.cancelling() == 0
        assert ws._pending_connection_closes == [candidate]

        await ws.close()

        assert candidate.close.await_count == 2
        assert ws._pending_connection_closes == []

    async def test_connection_close_caller_cancel_wins_and_retains_retry_ownership(self):
        candidate = FakeWSConnection()
        close_started = asyncio.Event()
        close_calls = 0

        async def close_connection() -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                close_started.set()
                await asyncio.Future()

        candidate.close.side_effect = close_connection
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0),
            connect_fn=AsyncMock(return_value=candidate),
        )
        await ws.connect()

        closing = asyncio.create_task(ws.close())
        await close_started.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert ws._pending_connection_closes == [candidate]

        await ws.close()

        assert candidate.close.await_count == 2
        assert ws._pending_connection_closes == []

    async def test_failed_candidate_rollback_close_is_retried_by_close(self):
        event_bus = EventBus(handler_error_policy="raise")

        async def reject_success(event):
            raise RuntimeError("success observer failed")

        event_bus.subscribe(ReconnectSuccess, reject_success)
        candidate = FakeWSConnection()
        candidate.close.side_effect = [RuntimeError("candidate close failed"), None]
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=3),
            event_bus=event_bus,
            connect_fn=AsyncMock(return_value=candidate),
        )

        with pytest.raises(RuntimeError, match="success observer failed"):
            await ws.connect()

        assert ws._pending_connection_closes == [candidate]
        await ws.close()
        assert candidate.close.await_count == 2
        assert ws._pending_connection_closes == []

    async def test_failed_candidate_rollback_close_is_retried_before_reconnect(self):
        event_bus = EventBus(handler_error_policy="raise")
        success_calls = 0

        async def reject_first_success(event):
            nonlocal success_calls
            success_calls += 1
            if success_calls == 1:
                raise RuntimeError("success observer failed")

        event_bus.subscribe(ReconnectSuccess, reject_first_success)
        first = FakeWSConnection()
        first.close.side_effect = [RuntimeError("candidate close failed"), None]
        second = FakeWSConnection()
        connect_fn = AsyncMock(side_effect=[first, second])
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=3),
            event_bus=event_bus,
            connect_fn=connect_fn,
        )

        with pytest.raises(RuntimeError, match="success observer failed"):
            await ws.connect()

        await ws.connect()

        assert first.close.await_count == 2
        assert ws._pending_connection_closes == []
        assert ws._ws is second
        assert connect_fn.await_count == 2
        await ws.close()

    async def test_event_bus_receives_failure_event(self):
        event_bus = EventBus()
        events_received = []

        async def handler(event):
            events_received.append(event)

        event_bus.subscribe(ReconnectAttempt, handler)
        event_bus.subscribe(ReconnectFailure, handler)

        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=1, base_delay=0.01, jitter_factor=0.0),
            event_bus=event_bus,
            provider_name="failing_provider",
        )

        with (
            patch(
                "easycat.reconnecting_ws.websockets.connect",
                new_callable=AsyncMock,
                side_effect=ConnectionError("down"),
            ),
            pytest.raises(ConnectionError),
        ):
            await ws.connect()

        failure_events = [e for e in events_received if isinstance(e, ReconnectFailure)]
        assert len(failure_events) == 1
        assert failure_events[0].provider == "failing_provider"
        assert "down" in failure_events[0].error

    async def test_strict_failure_observer_cannot_bypass_terminal_state(self):
        event_bus = EventBus(handler_error_policy="raise")

        async def reject_failure(event):
            raise RuntimeError("observer failed")

        event_bus.subscribe(ReconnectFailure, reject_failure)
        config = ReconnectConfig(
            max_retries=1,
            base_delay=0.001,
            max_delay=0.001,
            jitter_factor=0.0,
        )
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=config,
            event_bus=event_bus,
            on_reconnect=AsyncMock(),
        )
        close_frame = websockets.frames.Close(1006, "abnormal")

        class DroppingConnection(FakeWSConnection):
            async def _aiter(self):
                raise websockets.exceptions.ConnectionClosed(close_frame, None)
                yield  # pragma: no cover

        ws._ws = DroppingConnection()
        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            side_effect=ConnectionError("down"),
        ):
            assert [message async for message in ws.recv_iter()] == []

        assert ws.died_abnormally is True
        assert ws.reconnect_attempts_exhausted == 2
        assert ws.reconnect_exhaustion_reason == "failed reconnect attempts"

    async def test_on_reconnect_callback_called(self):
        callback = AsyncMock()
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=2, base_delay=0.01, jitter_factor=0.0),
            on_reconnect=callback,
        )

        call_count = 0

        async def mock_connect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("fail")
            return FakeWSConnection()

        with patch("easycat.reconnecting_ws.websockets.connect", side_effect=mock_connect):
            await ws.connect()

        callback.assert_called_once()

    async def test_on_give_up_callback_called(self):
        callback = AsyncMock()
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0, jitter_factor=0.0),
            on_give_up=callback,
        )

        with (
            patch(
                "easycat.reconnecting_ws.websockets.connect",
                new_callable=AsyncMock,
                side_effect=ConnectionError("down"),
            ),
            pytest.raises(ConnectionError),
        ):
            await ws.connect()

        callback.assert_called_once()

    async def test_on_reconnect_not_called_on_first_connect(self):
        callback = AsyncMock()
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=0, jitter_factor=0.0),
            on_reconnect=callback,
        )

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            return_value=FakeWSConnection(),
        ):
            await ws.connect()

        callback.assert_not_called()

    async def test_unlimited_retries(self):
        """With max_retries=-1, should keep retrying until success."""
        ws = ReconnectingWebSocket(
            url="wss://test.com",
            config=ReconnectConfig(max_retries=-1, base_delay=0.01, jitter_factor=0.0),
        )

        call_count = 0

        async def mock_connect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 5:
                raise ConnectionError("fail")
            return FakeWSConnection()

        with patch("easycat.reconnecting_ws.websockets.connect", side_effect=mock_connect):
            await ws.connect()

        assert call_count == 5
        assert ws.is_connected
