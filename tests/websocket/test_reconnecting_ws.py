"""Tests for ReconnectingWebSocket wrapper (WS8 Task 8.1)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import websockets.exceptions
import websockets.frames

from easycat.events import EventBus, ReconnectAttempt, ReconnectFailure, ReconnectSuccess
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket


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

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            side_effect=ConnectionError("fail"),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await ws.connect()

    async def test_send(self):
        ws = self._make_ws()
        fake_conn = FakeWSConnection()
        ws._ws = fake_conn

        await ws.send("hello")
        assert fake_conn._sent == ["hello"]

    async def test_send_bytes(self):
        ws = self._make_ws()
        fake_conn = FakeWSConnection()
        ws._ws = fake_conn

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
        ws = self._make_ws(base_delay=0.01, max_retries=2, jitter_factor=0.0)

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

        assert messages == ["msg1", "msg2", "msg3", "msg4"]

    async def test_recv_iter_gives_up_when_reconnect_fails(self):
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

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            side_effect=ConnectionError("down"),
        ):
            messages = []
            async for msg in ws.recv_iter():
                messages.append(msg)

        assert messages == ["msg1"]

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

    # ── WS8-specific tests: jitter, event bus, callbacks ───────

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

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            side_effect=ConnectionError("down"),
        ):
            with pytest.raises(ConnectionError):
                await ws.connect()

        failure_events = [e for e in events_received if isinstance(e, ReconnectFailure)]
        assert len(failure_events) == 1
        assert failure_events[0].provider == "failing_provider"
        assert "down" in failure_events[0].error

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

        with patch(
            "easycat.reconnecting_ws.websockets.connect",
            new_callable=AsyncMock,
            side_effect=ConnectionError("down"),
        ):
            with pytest.raises(ConnectionError):
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
