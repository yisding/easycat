"""Unit tests for the unified ``easycat.server.auth`` layer.

These exercise behavior (not call-spy assertions) for the bearer/query token
policies, the ``NoAuth`` open policy, the structured non-loopback bind guard
(the property that closes the ``0.0.0.0`` WebSocket gap), the env helper, and
the ``RequestLike`` adapters for all three server request shapes.
"""

from __future__ import annotations

import pytest

from easycat.server.auth import (
    EASYCAT_SERVE_TOKEN_ENV,
    BearerTokenAuth,
    NoAuth,
    authorized_bind,
    bearer_auth_from_env,
    enforce_bind_guard,
    from_aiohttp_request,
    from_h3_headers,
    from_websocket,
)


class _AiohttpReq:
    """Minimal aiohttp-style request: ``.headers`` + ``.query`` mappings."""

    def __init__(self, headers: dict[str, str], query: dict[str, str]) -> None:
        self.headers = headers
        self.query = query


class _WsHeaders(dict):
    """Stand-in for a ``websockets`` ``Headers`` mapping (``.get`` is enough)."""


# ── BearerTokenAuth: header ──────────────────────────────────────────


def test_bearer_accepts_correct_authorization_header() -> None:
    auth = BearerTokenAuth(token="sekrit")
    result = auth.authorize(
        from_aiohttp_request(_AiohttpReq({"Authorization": "Bearer sekrit"}, {}))
    )
    assert result.allowed is True
    assert result.reason == "allowed"


def test_bearer_rejects_wrong_header_token_as_invalid() -> None:
    auth = BearerTokenAuth(token="sekrit")
    result = auth.authorize(
        from_aiohttp_request(_AiohttpReq({"Authorization": "Bearer wrong"}, {}))
    )
    assert result.allowed is False
    assert result.reason == "invalid"


def test_bearer_reports_missing_when_no_credential() -> None:
    auth = BearerTokenAuth(token="sekrit")
    result = auth.authorize(from_aiohttp_request(_AiohttpReq({}, {})))
    assert result.allowed is False
    assert result.reason == "missing"


def test_bearer_ignores_non_bearer_scheme() -> None:
    auth = BearerTokenAuth(token="sekrit")
    result = auth.authorize(
        from_aiohttp_request(_AiohttpReq({"Authorization": "Basic sekrit"}, {}))
    )
    assert result.allowed is False
    assert result.reason == "missing"


# ── BearerTokenAuth: query token (default OFF) ───────────────────────


def test_query_token_rejected_when_not_opted_in() -> None:
    # Default-OFF: a correct ``?token=`` value does not authenticate.
    auth = BearerTokenAuth(token="sekrit")
    result = auth.authorize(from_aiohttp_request(_AiohttpReq({}, {"token": "sekrit"})))
    assert result.allowed is False
    assert result.reason == "missing"


def test_query_token_accepted_when_opted_in() -> None:
    auth = BearerTokenAuth(token="sekrit", allow_query_token=True)
    result = auth.authorize(from_aiohttp_request(_AiohttpReq({}, {"token": "sekrit"})))
    assert result.allowed is True
    assert result.reason == "allowed"


def test_query_token_wrong_value_is_invalid_when_opted_in() -> None:
    auth = BearerTokenAuth(token="sekrit", allow_query_token=True)
    result = auth.authorize(from_aiohttp_request(_AiohttpReq({}, {"token": "wrong"})))
    assert result.allowed is False
    assert result.reason == "invalid"


def test_bearer_header_takes_precedence_over_query() -> None:
    auth = BearerTokenAuth(token="sekrit", allow_query_token=True)
    # Header wins: a correct header authorizes regardless of the query value.
    result = auth.authorize(
        from_aiohttp_request(_AiohttpReq({"Authorization": "Bearer sekrit"}, {"token": "wrong"}))
    )
    assert result.allowed is True


# ── BearerTokenAuth: non-ASCII credential → clean 401, never a TypeError ──


def test_bearer_non_ascii_header_credential_is_invalid_not_typeerror() -> None:
    # ``hmac.compare_digest`` raises TypeError on a non-ASCII str; an attacker
    # could otherwise force a 500 on the (try-less) WebRTC auth check. A
    # non-ASCII credential must resolve to a clean 401, never propagate.
    auth = BearerTokenAuth(token="sekrit")
    result = auth.authorize(
        from_aiohttp_request(_AiohttpReq({"Authorization": "Bearer café"}, {}))
    )
    assert result.allowed is False
    assert result.reason == "invalid"


def test_bearer_non_ascii_query_credential_is_invalid_not_typeerror() -> None:
    auth = BearerTokenAuth(token="sekrit", allow_query_token=True)
    result = auth.authorize(from_aiohttp_request(_AiohttpReq({}, {"token": "café"})))
    assert result.allowed is False
    assert result.reason == "invalid"


def test_bearer_non_ascii_configured_token_is_invalid_not_typeerror() -> None:
    # The guard must be two-sided: a non-ASCII *configured* token (operator
    # misconfiguration) would otherwise make compare_digest raise TypeError on
    # every request carrying an ASCII credential — a 500 on each authorized
    # endpoint instead of a clean deny.
    auth = BearerTokenAuth(token="café-secret")
    result = auth.authorize(
        from_aiohttp_request(_AiohttpReq({"Authorization": "Bearer sekrit"}, {}))
    )
    assert result.allowed is False
    assert result.reason == "invalid"
    # Even a byte-identical credential is denied rather than risking the
    # TypeError; a non-ASCII token is not a usable secret for this policy.
    result = auth.authorize(
        from_aiohttp_request(_AiohttpReq({"Authorization": "Bearer café-secret"}, {}))
    )
    assert result.allowed is False


# ── BearerTokenAuth: a blank token never authorizes ──────────────────


def test_blank_token_does_not_authorize_empty_credential() -> None:
    # ``compare_digest("", "")`` is True, so a blank-token policy would otherwise
    # accept an empty ``Authorization: Bearer `` credential. A blank token is not
    # a usable secret and must never authorize.
    auth = BearerTokenAuth(token="")
    empty = auth.authorize(from_aiohttp_request(_AiohttpReq({"Authorization": "Bearer "}, {})))
    assert empty.allowed is False
    bare = auth.authorize(from_aiohttp_request(_AiohttpReq({"Authorization": "Bearer"}, {})))
    assert bare.allowed is False


def test_whitespace_token_does_not_authorize() -> None:
    auth = BearerTokenAuth(token="   ", allow_query_token=True)
    result = auth.authorize(from_aiohttp_request(_AiohttpReq({}, {"token": "   "})))
    assert result.allowed is False


# ── NoAuth ───────────────────────────────────────────────────────────


def test_noauth_always_allows() -> None:
    auth = NoAuth()
    assert auth.authorize(from_aiohttp_request(_AiohttpReq({}, {}))).allowed is True


# ── enforce_bind_guard ───────────────────────────────────────────────


def test_bind_guard_raises_for_non_loopback_without_token() -> None:
    with pytest.raises(ValueError) as exc:
        enforce_bind_guard("0.0.0.0", auth=None)
    message = str(exc.value)
    assert "0.0.0.0" in message
    assert "unsafe_allow_no_auth" in message


def test_bind_guard_allows_loopback_without_token() -> None:
    # No raise.
    enforce_bind_guard("127.0.0.1", auth=None)
    enforce_bind_guard("localhost", auth=None)
    enforce_bind_guard("::1", auth=None)


def test_bind_guard_allows_non_loopback_with_token() -> None:
    enforce_bind_guard("0.0.0.0", auth=BearerTokenAuth(token="sekrit"))


def test_bind_guard_rejects_non_loopback_with_whitespace_token() -> None:
    with pytest.raises(ValueError, match="without a token"):
        enforce_bind_guard("0.0.0.0", auth=BearerTokenAuth(token="   "))


def test_bind_guard_allows_non_loopback_with_unsafe_escape_hatch() -> None:
    enforce_bind_guard("0.0.0.0", auth=None, unsafe_allow_no_auth=True)


def test_bind_guard_raises_for_noauth_without_escape_hatch() -> None:
    # ``NoAuth`` carries no token, so a non-loopback bind still raises unless the
    # escape hatch is explicit.
    with pytest.raises(ValueError):
        enforce_bind_guard("0.0.0.0", auth=NoAuth())


# ── F1: the escape hatch is honored from the POLICY field, not just the
# ``unsafe_allow_no_auth`` parameter (previously the policy field was dead code).


def test_bind_guard_default_raises_for_non_loopback_no_token() -> None:
    # Fail-safe default: neither the parameter nor a policy escape hatch set.
    with pytest.raises(ValueError):
        enforce_bind_guard("0.0.0.0", auth=NoAuth(unsafe_allow_no_auth=False))


def test_bind_guard_honors_noauth_policy_escape_hatch() -> None:
    # ``NoAuth(unsafe_allow_no_auth=True)`` must be honored even though the
    # ``unsafe_allow_no_auth`` PARAMETER is left at its default (False).
    enforce_bind_guard("0.0.0.0", auth=NoAuth(unsafe_allow_no_auth=True))


def test_bind_guard_honors_bearer_token_policy_escape_hatch() -> None:
    # A tokenless ``BearerTokenAuth`` is unusual, but its policy-carried
    # ``unsafe_allow_no_auth=True`` must still open the bind (the field was
    # silently ignored before the fix).
    enforce_bind_guard(
        "0.0.0.0",
        auth=BearerTokenAuth(token="", unsafe_allow_no_auth=True),
    )


@pytest.mark.asyncio
async def test_authorized_bind_rejects_before_constructing_binder() -> None:
    called = False

    async def binder() -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(ValueError, match="without a token"):
        await authorized_bind("0.0.0.0", auth=None, binder=binder)

    assert called is False


@pytest.mark.asyncio
async def test_authorized_bind_preserves_backend_result_and_error() -> None:
    result = object()

    async def successful_binder() -> object:
        return result

    assert await authorized_bind("127.0.0.1", auth=None, binder=successful_binder) is result

    error = RuntimeError("bind failed")

    async def failing_binder() -> object:
        raise error

    with pytest.raises(RuntimeError) as exc_info:
        await authorized_bind("127.0.0.1", auth=None, binder=failing_binder)
    assert exc_info.value is error


# ── bearer_auth_from_env ─────────────────────────────────────────────


def test_bearer_auth_from_env_reads_serve_token(monkeypatch: pytest.MonkeyPatch) -> None:
    assert EASYCAT_SERVE_TOKEN_ENV == "EASYCAT_SERVE_TOKEN"
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "from-env")
    auth = bearer_auth_from_env()
    assert auth is not None
    assert auth.token == "from-env"


def test_bearer_auth_from_env_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    assert bearer_auth_from_env() is None


def test_bearer_auth_from_env_treats_whitespace_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", " \t ")
    assert bearer_auth_from_env() is None
    with pytest.raises(ValueError, match="without a token"):
        enforce_bind_guard("0.0.0.0", auth=bearer_auth_from_env())


def test_bearer_auth_from_env_ignores_the_server_token_typo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The env var is EASYCAT_SERVE_TOKEN, NOT EASYCAT_SERVER_TOKEN.
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    monkeypatch.setenv("EASYCAT_SERVER_TOKEN", "wrong-var")
    assert bearer_auth_from_env() is None


# ── RequestLike adapters ─────────────────────────────────────────────


def test_from_websocket_adapter_reads_header_and_query() -> None:
    headers = _WsHeaders({"Authorization": "Bearer sekrit"})
    req = from_websocket(headers, "/voice?token=qtok")
    assert req.authorization_header == "Bearer sekrit"
    assert req.query_token == "qtok"


def test_from_websocket_adapter_handles_missing_credentials() -> None:
    req = from_websocket(_WsHeaders({}), "/voice")
    assert req.authorization_header is None
    assert req.query_token is None


def test_from_h3_headers_adapter_reads_bearer_and_query_token() -> None:
    req = from_h3_headers(
        [
            (b":method", b"CONNECT"),
            (b"authorization", b"Bearer sekrit"),
        ],
        "/easycat?token=qtok",
    )
    assert req.authorization_header == "Bearer sekrit"
    assert req.query_token == "qtok"


def test_from_h3_headers_adapter_rejects_non_ascii_without_decode_error() -> None:
    req = from_h3_headers([(b"authorization", b"Bearer \xff")], "/easycat")
    result = BearerTokenAuth(token="sekrit").authorize(req)
    assert result.allowed is False
    assert result.reason == "invalid"


def test_from_h3_headers_adapter_handles_malformed_path() -> None:
    req = from_h3_headers([], "//[malformed")
    assert req.authorization_header is None
    assert req.query_token is None


def test_from_aiohttp_adapter_handles_missing_attributes() -> None:
    # A partial/fake request must degrade to ``None`` rather than raise.
    req = from_aiohttp_request(object())
    assert req.authorization_header is None
    assert req.query_token is None


def test_websocket_adapter_drives_bearer_auth() -> None:
    auth = BearerTokenAuth(token="sekrit")
    req = from_websocket(_WsHeaders({"Authorization": "Bearer sekrit"}), "/voice")
    assert auth.authorize(req).allowed is True
