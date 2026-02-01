"""Tests for ReconnectingWebSocket wrapper."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import websockets.exceptions
import websockets.frames

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
        assert config.extra_headers == {}

    def test_custom(self):
        config = ReconnectConfig(
            max_retries=5,
            base_delay=0.5,
            extra_headers={"Authorization": "Bearer test"},
        )
        assert config.max_retries == 5
        assert config.extra_headers["Authorization"] == "Bearer test"


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
        ws = self._make_ws(base_delay=0.01, max_retries=2)
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
        ws = self._make_ws(base_delay=0.01, max_retries=2)

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
        ws = self._make_ws(base_delay=0.01, max_retries=2)

        close_frame = websockets.frames.Close(1006, "abnormal")

        class DroppingConnection:
            """Simulates a connection that drops after yielding some messages."""

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

        # After drop, reconnect yields these messages then ends cleanly
        resume_conn = FakeWSConnection()
        resume_conn._messages = ["msg3", "msg4"]

        ws._ws = drop_conn  # start "connected"

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
        """recv_iter should end if reconnection exhausts retries."""
        ws = self._make_ws(base_delay=0.01, max_retries=1)

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

        # Got msg1, then connection dropped, reconnect failed — iteration ends
        assert messages == ["msg1"]

    async def test_recv_iter_no_reconnect_after_explicit_close(self):
        """recv_iter should not reconnect if close() was called."""
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
        # Mark as explicitly closed — should not reconnect
        ws._closed = True

        messages = []
        async for msg in ws.recv_iter():
            messages.append(msg)

        assert messages == ["msg1"]
