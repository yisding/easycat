"""Parity net between the two WebRTC signaling surfaces (QS6 PR1 safety net).

The stateless WebRTC signaling surface — CORS headers, the stats
forbidden/quota responses, the stats rate-limit/size/record quota deque logic,
the stats-write permission, and request authorization — exists twice today: on
the singleton :class:`~easycat.transports.webrtc.WebRTCTransport`
(``transports/webrtc.py``) and on the multi-session
:class:`~easycat.server.webrtc_routes.WebRTCRoutes` (``server/webrtc_routes.py``).
The two copies are kept "byte-identical" by docstring promise only, with no test
enforcing it.

This is the QS6 PR1 safety net: it pins the two copies to AGREE across an
origin/token/quota matrix BEFORE PR2 changes the transport's auth and PR3 lifts
both onto the shared ``server/_webrtc_handlers.py`` unit. Any drift in either
copy fails here.

The non-ASCII-credential auth case is pinned to the CORRECTED 401 (deny)
behavior. Today ``WebRTCTransport._request_authorized`` calls a raw
``hmac.compare_digest`` with no non-ASCII guard, so a non-ASCII credential
raises ``TypeError`` -> HTTP 500 (a latent DoS). PR2 swaps it to
``server.auth.BearerTokenAuth`` (which guards ``credential.isascii()``), turning
that into a clean deny -> 401. The routes already delegate to ``BearerTokenAuth``
and deny cleanly, so only the transport half xfails until PR2 lands.

These are pure-logic parity checks: no socket bind and no aiortc/aiohttp needed
(``_FakeWeb`` stands in for ``aiohttp.web``).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from easycat._net import normalize_auth_token
from easycat.server.auth import BearerTokenAuth
from easycat.server.transports import CapacityGate
from easycat.server.webrtc_routes import WebRTCRoutes
from easycat.session_manager import SessionManager
from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig

from ._webrtc_fakes import _FakeWeb


class _Req:
    """A minimal aiohttp-style request stand-in for the stateless surface."""

    def __init__(
        self,
        *,
        origin: str | None = None,
        authorization: str | None = None,
        query_token: str | None = None,
        scheme: str | None = None,
        host: str | None = None,
    ) -> None:
        self.headers: dict[str, str] = {}
        if origin is not None:
            self.headers["Origin"] = origin
        if authorization is not None:
            self.headers["Authorization"] = authorization
        self.query: dict[str, str] = {}
        if query_token is not None:
            self.query["token"] = query_token
        if scheme is not None:
            self.scheme = scheme
        if host is not None:
            self.host = host


def _make_pair(config: WebRTCTransportConfig) -> tuple[WebRTCTransport, WebRTCRoutes]:
    """Build a transport + routes over the SAME config with matched auth.

    The routes take an explicit :class:`BearerTokenAuth` built from the same
    token so both surfaces authorize identically; a tokenless config maps both
    to open access (``auth=None``).
    """
    transport = WebRTCTransport(config)
    transport._web = _FakeWeb
    token = normalize_auth_token(config.auth_token)
    auth = (
        BearerTokenAuth(token=token, allow_query_token=config.allow_query_token)
        if token is not None
        else None
    )
    routes = WebRTCRoutes(
        config,
        auth=auth,
        config_factory=lambda _t: None,
        gate=CapacityGate(64),
        manager=SessionManager(),
        runtime_feedback=False,
    )
    routes._web = _FakeWeb
    return transport, routes


def _resp_tuple(response: Any) -> tuple[int, str, str | None, dict[str, str]]:
    return (response.status, response.text, response.content_type, response.headers)


# ── CORS header parity ────────────────────────────────────────────────────

_CORS_CONFIGS = [
    WebRTCTransportConfig(cors_allowed_origins=()),
    WebRTCTransportConfig(cors_allowed_origins=("https://app.example.com",)),
    WebRTCTransportConfig(cors_allowed_origins=("*",)),
    WebRTCTransportConfig(cors_allowed_origins="https://str.example.com"),
]
_CORS_REQUESTS = [
    _Req(),
    _Req(origin="https://app.example.com"),
    _Req(origin="https://app.example.com/"),
    _Req(origin="https://evil.example.com"),
    _Req(origin="http://127.0.0.1:8080", scheme="http", host="127.0.0.1:8080"),
    _Req(origin="https://str.example.com"),
]


@pytest.mark.parametrize("config", _CORS_CONFIGS)
@pytest.mark.parametrize("request_obj", _CORS_REQUESTS)
def test_cors_headers_parity(config: WebRTCTransportConfig, request_obj: _Req) -> None:
    transport, routes = _make_pair(config)
    assert transport._cors_headers(request_obj) == routes._cors_headers(request_obj)


# ── Auth parity (ASCII credentials agree today) ───────────────────────────

_AUTH_CASES = [
    (WebRTCTransportConfig(), _Req(), True),
    (WebRTCTransportConfig(auth_token="sekrit"), _Req(authorization="Bearer sekrit"), True),
    (WebRTCTransportConfig(auth_token="sekrit"), _Req(authorization="Bearer wrong"), False),
    (WebRTCTransportConfig(auth_token="sekrit"), _Req(), False),
    (WebRTCTransportConfig(auth_token="sekrit"), _Req(query_token="sekrit"), False),
    (
        WebRTCTransportConfig(auth_token="sekrit", allow_query_token=True),
        _Req(query_token="sekrit"),
        True,
    ),
    (
        WebRTCTransportConfig(auth_token="sekrit", allow_query_token=True),
        _Req(query_token="wrong"),
        False,
    ),
]


@pytest.mark.parametrize("config, request_obj, expected", _AUTH_CASES)
def test_request_authorization_parity(
    config: WebRTCTransportConfig, request_obj: _Req, expected: bool
) -> None:
    transport, routes = _make_pair(config)
    assert transport._request_authorized(request_obj) is expected
    assert routes._authorized(request_obj) is expected


def test_non_ascii_credential_denied_by_routes() -> None:
    # The routes already delegate to ``BearerTokenAuth``, which guards
    # ``credential.isascii()`` and returns a clean deny (-> 401) — never a raise.
    _, routes = _make_pair(WebRTCTransportConfig(auth_token="sekrit"))
    request_obj = _Req(authorization="Bearer nön-ascii-töken")
    assert routes._authorized(request_obj) is False


def test_non_ascii_credential_denied_by_transport() -> None:
    # Since QS6 PR2 the transport delegates to ``BearerTokenAuth``, which guards
    # ``credential.isascii()`` and returns a clean deny (-> 401) for a non-ASCII
    # credential instead of the old raw ``compare_digest`` TypeError (-> 500).
    transport, _ = _make_pair(WebRTCTransportConfig(auth_token="sekrit"))
    request_obj = _Req(authorization="Bearer nön-ascii-töken")
    assert transport._request_authorized(request_obj) is False


# ── Stats write-permission parity ─────────────────────────────────────────

_SAME_ORIGIN_LOOPBACK = _Req(origin="http://127.0.0.1:8080", scheme="http", host="127.0.0.1:8080")
_STATS_PERMIT_CASES = [
    (WebRTCTransportConfig(host="127.0.0.1"), _SAME_ORIGIN_LOOPBACK, True),
    (WebRTCTransportConfig(host="127.0.0.1"), _Req(), False),
    (
        WebRTCTransportConfig(host="127.0.0.1"),
        _Req(origin="http://evil.example.com", scheme="http", host="127.0.0.1:8080"),
        False,
    ),
    (WebRTCTransportConfig(host="0.0.0.0"), _SAME_ORIGIN_LOOPBACK, False),
    (
        WebRTCTransportConfig(host="0.0.0.0", auth_token="sekrit"),
        _Req(authorization="Bearer sekrit"),
        True,
    ),
    (WebRTCTransportConfig(host="0.0.0.0", auth_token="sekrit"), _Req(), False),
]


@pytest.mark.parametrize("config, request_obj, expected", _STATS_PERMIT_CASES)
def test_stats_write_permitted_parity(
    config: WebRTCTransportConfig, request_obj: _Req, expected: bool
) -> None:
    transport, routes = _make_pair(config)
    assert transport._stats_write_permitted(request_obj) is expected
    assert routes._stats_write_permitted(request_obj) is expected


# ── Stats forbidden/quota response parity ─────────────────────────────────


def test_stats_forbidden_response_parity() -> None:
    transport, routes = _make_pair(WebRTCTransportConfig(cors_allowed_origins=("*",)))
    request_obj = _Req(origin="https://client.example.com")
    assert _resp_tuple(transport._stats_forbidden_response(request_obj)) == _resp_tuple(
        routes._stats_forbidden_response(request_obj)
    )


def test_stats_quota_response_parity() -> None:
    transport, routes = _make_pair(WebRTCTransportConfig(cors_allowed_origins=("*",)))
    request_obj = _Req(origin="https://client.example.com")
    message = "WebRTC stats rate limit exceeded"
    assert _resp_tuple(transport._stats_quota_response(request_obj, message)) == _resp_tuple(
        routes._stats_quota_response(request_obj, message)
    )


# ── Stats quota deque/size/record parity ──────────────────────────────────

_SNAPSHOT = {"kind": "webrtc_client_stats", "schema_version": 1}


def test_stats_quota_error_rate_limit_parity(tmp_path: Path) -> None:
    stats_path = tmp_path / "absent.jsonl"  # never created -> only the deque fires
    config = WebRTCTransportConfig(stats_max_requests_per_minute=3, stats_path=str(stats_path))
    transport, routes = _make_pair(config)

    for _ in range(3):
        assert transport._stats_quota_error(stats_path, _SNAPSHOT) is None
        assert routes._stats_quota_error(stats_path, _SNAPSHOT) is None
        stamp = time.monotonic()
        transport._stats_request_times.append(stamp)
        routes._stats_request_times.append(stamp)

    transport_error = transport._stats_quota_error(stats_path, _SNAPSHOT)
    routes_error = routes._stats_quota_error(stats_path, _SNAPSHOT)
    assert transport_error == routes_error == "WebRTC stats rate limit exceeded"


def test_stats_quota_error_record_limit_parity(tmp_path: Path) -> None:
    stats_path = tmp_path / "stats.jsonl"
    stats_path.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    config = WebRTCTransportConfig(stats_max_records=2, stats_path=str(stats_path))
    transport, routes = _make_pair(config)

    transport_error = transport._stats_quota_error(stats_path, _SNAPSHOT)
    routes_error = routes._stats_quota_error(stats_path, _SNAPSHOT)
    assert transport_error == routes_error == "WebRTC stats artifact record limit exceeded"


def test_stats_quota_error_size_limit_parity(tmp_path: Path) -> None:
    stats_path = tmp_path / "stats.jsonl"
    stats_path.write_text("x" * 100, encoding="utf-8")
    config = WebRTCTransportConfig(stats_max_file_bytes=110, stats_path=str(stats_path))
    transport, routes = _make_pair(config)

    transport_error = transport._stats_quota_error(stats_path, _SNAPSHOT)
    routes_error = routes._stats_quota_error(stats_path, _SNAPSHOT)
    assert transport_error == routes_error == "WebRTC stats artifact size limit exceeded"
