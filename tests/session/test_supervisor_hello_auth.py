from __future__ import annotations

import json

import pytest

from easycat.supervisor import serve_supervisor_websocket
from tests.session.test_supervisor import _FakeSupervisorWebSocket


@pytest.mark.xfail(
    strict=True,
    reason=(
        "serve_supervisor_websocket hello reports auth_required=False whenever "
        "allow_unauthenticated=True, even when expected_token is set and enforced"
    ),
)
async def test_hello_auth_required_matches_enforced_token() -> None:
    # A token is configured, so supervisor_message_authorized() enforces it
    # regardless of allow_unauthenticated. The hello must say so, because the
    # browser client uses auth_required to decide whether to prompt for a token.
    ws = _FakeSupervisorWebSocket([json.dumps({"type": "subscribe", "session_id": "s"})])

    await serve_supervisor_websocket(
        ws,
        {},
        expected_token="secret",
        allow_unauthenticated=True,
    )

    hello = json.loads(ws.sent[0])
    assert hello["type"] == "hello"
    # The tokenless subscribe is rejected, proving the token is enforced.
    assert ws.close_code == 4401
    assert hello["auth_required"] is True
