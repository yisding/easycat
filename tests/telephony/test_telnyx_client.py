"""Offline tests for the Telnyx Call Control REST client (no network)."""

from __future__ import annotations

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
