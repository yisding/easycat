from __future__ import annotations

from easycat.server import VoiceServer, VoiceServerConfig
from easycat.server.auth import BearerTokenAuth
from easycat.server.routes import _authorized_readonly_request


class _FakeRequest:
    def __init__(self, authorization: str | None = None, token: str | None = None) -> None:
        self.headers = {}
        if authorization is not None:
            self.headers["Authorization"] = authorization
        self.query = {}
        if token is not None:
            self.query["token"] = token


def test_readonly_request_auth_remains_open_without_policy() -> None:
    server = VoiceServer(VoiceServerConfig(host="127.0.0.1", port=0))

    assert _authorized_readonly_request(server, _FakeRequest()) is True


def test_readonly_request_auth_requires_configured_bearer_token() -> None:
    server = VoiceServer(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            auth=BearerTokenAuth("secret-token"),
        )
    )

    assert _authorized_readonly_request(server, _FakeRequest()) is False
    assert _authorized_readonly_request(server, _FakeRequest("Bearer wrong-token")) is False
    assert _authorized_readonly_request(server, _FakeRequest("Bearer secret-token")) is True
