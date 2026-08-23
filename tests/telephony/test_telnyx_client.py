"""Offline tests for the Telnyx Call Control REST client (no network)."""

from __future__ import annotations

import asyncio
import unittest.mock

import aiohttp
from typing import Any

import pytest

from easycat.telephony.telnyx_client import (
    TELNYX_API_BASE_URL,
    TelnyxApiError,
    TelnyxCallControlClient,
    _error_detail,
)

BASE_URL = "https://api.example.test/v2"


# ── Fakes ─────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status = status
        self._body = {} if body is None else body
        self.json_content_type: Any = "unset"

    async def json(self, content_type: Any = None) -> Any:
        self.json_content_type = content_type
        return self._body


class _ResponseContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _RecordedCall:
    def __init__(self, url: str, payload: dict[str, Any] | None) -> None:
        self.url = url
        self.payload = payload


class _RecordingSession:
    def __init__(self, responses: list[_FakeResponse] | None = None) -> None:
        self.calls: list[_RecordedCall] = []
        self.closed = False
        self.close_calls = 0
        self._responses = list(responses or [])

    def post(self, url: str, json: dict[str, Any] | None = None) -> _ResponseContext:
        self.calls.append(_RecordedCall(url, json))
        response = self._responses.pop(0) if self._responses else _FakeResponse()
        return _ResponseContext(response)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def make_client(
    responses: list[_FakeResponse] | None = None,
) -> tuple[TelnyxCallControlClient, _RecordingSession]:
    client = TelnyxCallControlClient("key-123", base_url=BASE_URL)
    session = _RecordingSession(responses)

    async def ensure_session() -> _RecordingSession:
        return session

    client._ensure_session = ensure_session  # type: ignore[method-assign]
    return client, session


# ── Construction ──────────────────────────────────────────────────


class TestConstructor:
    @pytest.mark.parametrize("api_key", ["", None])
    def test_empty_api_key_raises_value_error(self, api_key: str | None) -> None:
        with pytest.raises(ValueError, match="api_key"):
            TelnyxCallControlClient(api_key)  # type: ignore[arg-type]

    def test_authorization_header_carries_bearer_token(self) -> None:
        client = TelnyxCallControlClient("key-123")

        assert client._headers["Authorization"] == "Bearer key-123"

    def test_default_base_url_is_telnyx_v2(self) -> None:
        assert TELNYX_API_BASE_URL == "https://api.telnyx.com/v2"

        client = TelnyxCallControlClient("key-123")

        assert client._base_url == TELNYX_API_BASE_URL


# ── Command paths ─────────────────────────────────────────────────


class TestCommandPaths:
    async def test_answer_posts_action_path_with_payload_passthrough(self) -> None:
        client, session = make_client()
        payload = {"stream_url": "wss://example.com/stream", "command_id": "cmd-1"}

        await client.answer("CC9", payload)

        assert len(session.calls) == 1
        assert session.calls[0].url == f"{BASE_URL}/calls/CC9/actions/answer"
        assert session.calls[0].payload == payload

    async def test_dial_posts_calls_endpoint(self) -> None:
        client, session = make_client()

        await client.dial({"to": "+15550001111"})

        assert session.calls[0].url == f"{BASE_URL}/calls"
        assert session.calls[0].payload == {"to": "+15550001111"}

    async def test_hangup_hits_hangup_path_and_includes_command_id(self) -> None:
        client, session = make_client()

        await client.hangup("CC9", command_id="cmd-hangup")

        assert session.calls[0].url == f"{BASE_URL}/calls/CC9/actions/hangup"
        assert session.calls[0].payload == {"command_id": "cmd-hangup"}

    async def test_transfer_includes_to_from_and_command_id(self) -> None:
        client, session = make_client()

        await client.transfer("CC9", "+15550003333", from_="+15550002222", command_id="cmd-t")

        assert session.calls[0].url == f"{BASE_URL}/calls/CC9/actions/transfer"
        assert session.calls[0].payload == {
            "to": "+15550003333",
            "from": "+15550002222",
            "command_id": "cmd-t",
        }

    async def test_transfer_omits_unset_optional_fields(self) -> None:
        client, session = make_client()

        await client.transfer("CC9", "+15550003333")

        assert session.calls[0].payload == {"to": "+15550003333"}

    async def test_send_dtmf_hits_send_dtmf_path(self) -> None:
        client, session = make_client()

        await client.send_dtmf("CC9", "1234", command_id="cmd-d")

        assert session.calls[0].url == f"{BASE_URL}/calls/CC9/actions/send_dtmf"
        assert session.calls[0].payload == {"digits": "1234", "command_id": "cmd-d"}

    async def test_send_sms_posts_messages_endpoint(self) -> None:
        client, session = make_client()

        await client.send_sms(to="+15550001111", from_="+15550002222", text="hi")

        assert session.calls[0].url == f"{BASE_URL}/messages"
        assert session.calls[0].payload == {
            "to": "+15550001111",
            "from": "+15550002222",
            "text": "hi",
        }
        assert "connection_id" not in session.calls[0].payload

    async def test_send_sms_includes_connection_id_when_set(self) -> None:
        client, session = make_client()

        await client.send_sms(
            to="+15550001111", from_="+15550002222", text="hi", connection_id="conn-1"
        )

        assert session.calls[0].payload["connection_id"] == "conn-1"

    async def test_successful_response_returns_parsed_body(self) -> None:
        client, _session = make_client([_FakeResponse(body={"data": {"id": "CC9"}})])

        result = await client.answer("CC9", {})

        assert result == {"data": {"id": "CC9"}}


# ── Error handling ────────────────────────────────────────────────


class TestErrorHandling:
    async def test_http_400_raises_api_error_with_status_and_detail(self) -> None:
        client, _session = make_client(
            [
                _FakeResponse(
                    status=400,
                    body={"errors": [{"title": "Invalid number", "code": "10015"}]},
                )
            ]
        )

        with pytest.raises(TelnyxApiError) as excinfo:
            await client.answer("CC9", {})

        error = excinfo.value
        assert error.status == 400
        assert error.detail == "10015: Invalid number"

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (
                {"errors": [{"title": "Invalid number", "code": "10015"}]},
                "10015: Invalid number",
            ),
            ({"errors": [{"detail": "bad request"}]}, "bad request"),
            ({"errors": []}, "{'errors': []}"),
            ({"detail": "missing auth"}, "missing auth"),
            ({"unexpected": "shape"}, "{'unexpected': 'shape'}"),
            ("plain failure", "plain failure"),
            (["list", "body"], "['list', 'body']"),
        ],
    )
    def test_error_detail_shapes(self, body: Any, expected: str) -> None:
        assert _error_detail(body) == expected

    def test_api_error_message_contains_status_and_detail(self) -> None:
        error = TelnyxApiError(404, "call not found")

        assert str(error) == "Telnyx API error 404: call not found"


# ── Session lifecycle ─────────────────────────────────────────────


class TestCloseLifecycle:
    async def test_close_closes_underlying_session_once(self) -> None:
        client, session = make_client()
        client._session = session  # type: ignore[assignment]

        await client.close()

        assert session.close_calls == 1
        assert client._session is None

    async def test_second_close_is_safe_after_first(self) -> None:
        client, session = make_client()
        client._session = session  # type: ignore[assignment]

        await client.close()
        await client.close()

        assert session.close_calls == 1

    async def test_close_without_session_is_safe(self) -> None:
        client, _session = make_client()

        await client.close()

        assert client._session is None


# ── Retry / backoff ────────────────────────────────────────────────


class TestRetryBackoff:
    async def test_429_retries_then_succeeds(self) -> None:
        client, session = make_client(
            [
                _FakeResponse(status=429, body={"errors": [{"title": "rate limited"}]}),
                _FakeResponse(body={"data": {"id": "CC9"}}),
            ]
        )
        client._retry_backoff_s = 0.0

        result = await client.answer("CC9", {})

        assert result == {"data": {"id": "CC9"}}
        assert len(session.calls) == 2

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_server_errors_are_retried(self, status: int) -> None:
        client, session = make_client(
            [
                _FakeResponse(status=status),
                _FakeResponse(body={}),
            ]
        )
        client._retry_backoff_s = 0.0

        await client.answer("CC9", {})

        assert len(session.calls) == 2

    async def test_non_retryable_error_raises_immediately(self) -> None:
        client, session = make_client(
            [
                _FakeResponse(status=400, body={"errors": [{"title": "bad"}]}),
                _FakeResponse(body={}),
            ]
        )

        with pytest.raises(TelnyxApiError) as excinfo:
            await client.answer("CC9", {})

        assert excinfo.value.status == 400
        assert len(session.calls) == 1

    async def test_exhausted_retries_raise_last_api_error(self) -> None:
        client, session = make_client(
            [_FakeResponse(status=503) for _ in range(3)]
        )
        client._max_retries = 2
        client._retry_backoff_s = 0.0

        with pytest.raises(TelnyxApiError) as excinfo:
            await client.answer("CC9", {})

        assert excinfo.value.status == 503
        assert len(session.calls) == 3

    async def test_connection_error_retries_then_raises_last(self) -> None:
        client = TelnyxCallControlClient("key-123")
        calls: list[int] = []

        class _FailingSession(_RecordingSession):
            def post(self, url: str, json: Any = None) -> _ResponseContext:
                calls.append(1)
                raise aiohttp.ClientConnectionError("connection reset")

        session = _FailingSession()

        async def ensure_session() -> _RecordingSession:
            return session  # type: ignore[return-value]

        client._ensure_session = ensure_session  # type: ignore[method-assign]
        client._max_retries = 2
        client._retry_backoff_s = 0.0

        with pytest.raises(aiohttp.ClientConnectionError):
            await client.answer("CC9", {})

        assert len(calls) == 3

    async def test_backoff_doubles_between_attempts_and_caps(self) -> None:
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        client = TelnyxCallControlClient("key-123", max_retries=4, retry_backoff_s=0.5)
        responses = [_FakeResponse(status=503) for _ in range(5)]
        session = _RecordingSession(responses)

        async def ensure_session() -> _RecordingSession:
            return session

        client._ensure_session = ensure_session  # type: ignore[method-assign]

        with unittest.mock.patch.object(asyncio, "sleep", fake_sleep):
            with pytest.raises(TelnyxApiError):
                await client.answer("CC9", {})

        for i, delay in enumerate(delays):
            expected_base = 0.5 * (2**i)
            assert expected_base * 0.5 <= delay <= expected_base, (
                f"attempt {i}: {delay} not in [{expected_base * 0.5}, {expected_base}]"
            )

    async def test_max_retries_zero_makes_single_attempt(self) -> None:
        client, session = make_client([_FakeResponse(status=503)])
        client._max_retries = 0

        with pytest.raises(TelnyxApiError) as excinfo:
            await client.answer("CC9", {})

        assert excinfo.value.status == 503
        assert len(session.calls) == 1

    async def test_max_retries_one_makes_exactly_two_attempts(self) -> None:
        client, session = make_client([_FakeResponse(status=500), _FakeResponse(status=500)])
        client._max_retries = 1
        client._retry_backoff_s = 0.0

        with pytest.raises(TelnyxApiError):
            await client.answer("CC9", {})

        assert len(session.calls) == 2

    @pytest.mark.parametrize(
        ("max_retries", "backoff"),
        [(-1, 0.5), (2, -0.1)],
    )
    def test_invalid_retry_parameters_raise_value_error(
        self, max_retries: int, backoff: float
    ) -> None:
        with pytest.raises(ValueError):
            TelnyxCallControlClient("key-123", max_retries=max_retries, retry_backoff_s=backoff)

    async def test_constructor_retry_parameters_are_behavioral(self) -> None:
        client, session = make_client(
            [_FakeResponse(status=503), _FakeResponse(body={})]
        )
        client._max_retries = 1
        client._retry_backoff_s = 0.0

        result = await client.answer("CC9", {})

        assert result == {}
        assert len(session.calls) == 2

    def test_constructor_accepts_retry_parameters(self) -> None:
        client = TelnyxCallControlClient(
            "key-123",
            max_retries=5,
            retry_backoff_s=0.25,
        )

        assert client._max_retries == 5
        assert client._retry_backoff_s == 0.25
