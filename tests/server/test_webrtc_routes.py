"""Integration + unit tests for the mounted WebRTC routes (M7).

These cover the transport-instance-free :class:`WebRTCRoutes` unit and its
mounting on :class:`VoiceServer` under ``/webrtc/*``:

* the decoupling assertion — config/stats/root/cors handlers run with NO
  :class:`WebRTCTransport` instance;
* ``POST /webrtc/offer`` returns 200 + an answer and creates one session per
  offer (each offer gets an isolated peer connection);
* ``GET /webrtc/config`` omits TURN credentials by default;
* ``POST /webrtc/stats`` sanitizes snapshots;
* the unified ``AuthPolicy`` guards the mounted routes (tokenless 401, Bearer
  200), and ``?token=`` is rejected unless ``allow_query_token=True`` (the M5
  default-off posture now applied to mounted WebRTC);
* the SHARED capacity gate spans WebRTC offers AND ``/ws`` connections;
* draining rejects new offers with 503;
* ``stop()`` force-drains an active WebRTC session.

The pure-aiohttp routes use ``aiohttp.test_utils`` (no socket bind); the
offer/capacity/draining/stop tests bind a real port-0 :class:`VoiceServer` and
fake the aiortc layer via :func:`_install_fake_webrtc_modules` (reusing the
transport-test fakes).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import AsyncIterator

import pytest

from easycat.server import BearerTokenAuth, VoiceServer, VoiceServerConfig
from easycat.server.transports import CapacityGate
from easycat.server.webrtc_routes import WebRTCRoutes
from easycat.session_manager import SessionManager
from easycat.transports.webrtc import ICEServer, WebRTCTransportConfig
from tests.transports._webrtc_fakes import _install_fake_webrtc_modules

_HAS_AIOHTTP = importlib.util.find_spec("aiohttp") is not None

pytestmark = pytest.mark.skipif(not _HAS_AIOHTTP, reason="aiohttp not installed")


class _FakeSession:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self, *args: object, **kwargs: object) -> None:
        self.stopped.set()


def _make_routes(
    config: WebRTCTransportConfig,
    *,
    auth: BearerTokenAuth | None = None,
    max_sessions: int = 64,
    sessions: list[_FakeSession] | None = None,
) -> WebRTCRoutes:
    """Build a standalone :class:`WebRTCRoutes` over a fresh gate + manager."""
    gate: CapacityGate[int] = CapacityGate(max_sessions)
    manager: SessionManager[int] = SessionManager()

    def factory(_transport: object) -> _FakeSession:
        session = _FakeSession()
        if sessions is not None:
            sessions.append(session)
        return session

    return WebRTCRoutes(
        config,
        auth=auth,
        config_factory=factory,
        gate=gate,
        manager=manager,
        runtime_feedback=False,
    )


# ── Decoupling unit tests (no WebRTCTransport instance, no socket) ───────


async def _aiohttp_app_with_routes(routes: WebRTCRoutes, *, prefix: str = "/webrtc") -> object:
    from aiohttp import web

    app = web.Application()
    routes.register(app, prefix=prefix, web=web)
    return app


@pytest.mark.integration_socket
async def test_config_handler_runs_without_a_transport_instance() -> None:
    # The decoupling assertion: ``handle_config`` answers with no
    # ``WebRTCTransport`` instance anywhere — it reads only the config.
    from aiohttp.test_utils import TestClient, TestServer

    routes = _make_routes(
        WebRTCTransportConfig(
            static_dir=None,
            ice_servers=[
                ICEServer(
                    urls=["stun:stun.example.com:3478", "turn:turn.example.com:3478"],
                    username="user",
                    credential="pass",
                ),
            ],
        )
    )
    app = await _aiohttp_app_with_routes(routes)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/webrtc/config")
        assert resp.status == 200
        data = await resp.json()
    assert "iceServers" in data
    # Hidden TURN URLs are omitted rather than producing an invalid browser
    # RTCIceServer; a STUN URL in the same configured entry remains public.
    assert data["iceServers"] == [{"urls": ["stun:stun.example.com:3478"]}]


def test_public_ice_config_filters_incomplete_turn_but_server_config_keeps_it() -> None:
    routes = _make_routes(
        WebRTCTransportConfig(
            static_dir=None,
            ice_servers=[
                ICEServer(urls="turn:missing-user.example.com", credential="pass"),
                ICEServer(urls="turns:missing-credential.example.com", username="user"),
                ICEServer(
                    urls=["stun:stun.example.com", "turn:turn.example.com"],
                    username="user",
                    credential="pass",
                ),
            ],
        )
    )

    signaling = routes._signaling()
    assert signaling.ice_servers_as_dicts(include_credentials=True) == [
        {"urls": ["turn:missing-user.example.com"], "credential": "pass"},
        {"urls": ["turns:missing-credential.example.com"], "username": "user"},
        {
            "urls": ["stun:stun.example.com", "turn:turn.example.com"],
            "username": "user",
            "credential": "pass",
        },
    ]
    assert signaling.ice_servers_as_dicts(
        include_credentials=True,
        browser_safe=True,
    ) == [
        {
            "urls": ["stun:stun.example.com", "turn:turn.example.com"],
            "username": "user",
            "credential": "pass",
        },
    ]


@pytest.mark.integration_socket
async def test_stats_handler_sanitizes_without_a_transport_instance(tmp_path: object) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    stats_path = f"{tmp_path}/webrtc-stats.jsonl"
    routes = _make_routes(WebRTCTransportConfig(static_dir=None, stats_path=stats_path))
    app = await _aiohttp_app_with_routes(routes)
    async with TestClient(TestServer(app)) as client:
        # Same-origin loopback validation request authorizes the unauthenticated
        # stats write.
        host = client.host
        port = client.port
        resp = await client.post(
            "/webrtc/stats",
            json={
                "kind": "webrtc_client_stats",
                "label": "first_received_audio",
                "local_candidate_ip": "192.168.1.20",
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time_ms": 12.5,
                    "local_candidate_id": "candidate-secret",
                },
            },
            headers={"Origin": f"http://{host}:{port}"},
        )
        assert resp.status == 200
    from pathlib import Path

    line = Path(stats_path).read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["label"] == "first_received_audio"
    assert payload["candidate_pair"] == {
        "current_round_trip_time_ms": 12.5,
        "state": "succeeded",
    }
    assert "candidate-secret" not in line
    assert "192.168.1.20" not in line


@pytest.mark.integration_socket
async def test_stats_handler_enforces_record_limit_via_in_memory_counter(
    tmp_path: object,
) -> None:
    from pathlib import Path

    from aiohttp.test_utils import TestClient, TestServer

    stats_path = f"{tmp_path}/webrtc-stats.jsonl"
    routes = _make_routes(
        WebRTCTransportConfig(static_dir=None, stats_path=stats_path, stats_max_records=2)
    )
    app = await _aiohttp_app_with_routes(routes)
    async with TestClient(TestServer(app)) as client:
        origin = f"http://{client.host}:{client.port}"

        async def _post() -> int:
            resp = await client.post(
                "/webrtc/stats",
                json={"kind": "webrtc_client_stats", "schema_version": 1},
                headers={"Origin": origin},
            )
            return resp.status

        assert await _post() == 200
        assert await _post() == 200
        over_limit = await client.post(
            "/webrtc/stats",
            json={"kind": "webrtc_client_stats", "schema_version": 1},
            headers={"Origin": origin},
        )
        assert over_limit.status == 429
        assert "record limit exceeded" in await over_limit.text()

    lines = Path(stats_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


@pytest.mark.integration_socket
async def test_cors_preflight_handler_runs_without_a_transport_instance() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    origin = "https://app.example.com"
    routes = _make_routes(WebRTCTransportConfig(static_dir=None, cors_allowed_origins=(origin,)))
    app = await _aiohttp_app_with_routes(routes)
    async with TestClient(TestServer(app)) as client:
        resp = await client.options(
            "/webrtc/config",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        assert resp.status == 200
        assert resp.headers["Access-Control-Allow-Origin"] == origin
        assert "GET" in resp.headers["Access-Control-Allow-Methods"]


@pytest.mark.integration_socket
async def test_root_redirect_appends_webrtc_base_and_drops_token(
    tmp_path: object,
) -> None:
    # A static dir with a bundled client triggers the root redirect; it must
    # append ``?webrtc=/webrtc`` (the mounted base), preserve non-secret query
    # state, and avoid copying a token into a second request.
    from pathlib import Path

    from aiohttp.test_utils import TestClient, TestServer

    static_dir = Path(str(tmp_path))
    (static_dir / "webrtc_client.html").write_text("<html></html>", encoding="utf-8")
    routes = _make_routes(WebRTCTransportConfig(static_dir=str(static_dir)))
    app = await _aiohttp_app_with_routes(routes)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/?token=sekrit&view=compact", allow_redirects=False)
        assert resp.status == 302
        location = resp.headers["Location"]
    assert location.startswith("/webrtc_client.html?")
    assert "token=" not in location
    assert "view=compact" in location
    assert "webrtc=/webrtc" in location


@pytest.mark.integration_socket
async def test_root_redirect_replaces_untrusted_webrtc_base(
    tmp_path: object,
) -> None:
    from pathlib import Path

    from aiohttp.test_utils import TestClient, TestServer

    static_dir = Path(str(tmp_path))
    (static_dir / "webrtc_client.html").write_text("<html></html>", encoding="utf-8")
    routes = _make_routes(WebRTCTransportConfig(static_dir=str(static_dir)))
    app = await _aiohttp_app_with_routes(routes)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/?webrtc=.attacker.test&token=sekrit&view=compact",
            allow_redirects=False,
        )
        assert resp.status == 302
        location = resp.headers["Location"]

    assert location == "/webrtc_client.html?view=compact&webrtc=/webrtc"


@pytest.mark.integration_socket
async def test_root_redirect_preserves_sanitized_flat_base(
    tmp_path: object,
) -> None:
    # Flat mode (``prefix=""``) has no trusted mount base to substitute, so a
    # clean same-origin ``?webrtc=/proxy`` (e.g. a reverse-proxy path prefix)
    # must be PRESERVED — dropping it would break ``/proxy/offer`` routing.
    from pathlib import Path

    from aiohttp.test_utils import TestClient, TestServer

    static_dir = Path(str(tmp_path))
    (static_dir / "webrtc_client.html").write_text("<html></html>", encoding="utf-8")
    routes = _make_routes(WebRTCTransportConfig(static_dir=str(static_dir)))
    app = await _aiohttp_app_with_routes(routes, prefix="")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/?webrtc=/proxy/&token=sekrit&view=compact",
            allow_redirects=False,
        )
        assert resp.status == 302
        location = resp.headers["Location"]

    # Trailing slash trimmed; same-origin prefix and non-secret state kept;
    # the query token is dropped.
    assert location == "/webrtc_client.html?view=compact&webrtc=/proxy"


@pytest.mark.integration_socket
@pytest.mark.parametrize(
    "raw",
    [
        "/../x",  # path traversal
        "//evil.test",  # protocol-relative (cross-origin)
        "https://evil.test",  # absolute cross-origin URL
        "evil",  # not absolute path
        "/a/../b",  # mid-path traversal
        r"/\evil.test",  # backslash host trick
    ],
)
async def test_root_redirect_rejects_unsafe_flat_base(
    tmp_path: object,
    raw: str,
) -> None:
    # Flat mode must reject any non-same-origin / traversal ``?webrtc=`` value:
    # no trusted base means the redirect carries NO ``webrtc`` param at all.
    from pathlib import Path
    from urllib.parse import urlencode

    from aiohttp.test_utils import TestClient, TestServer

    static_dir = Path(str(tmp_path))
    (static_dir / "webrtc_client.html").write_text("<html></html>", encoding="utf-8")
    routes = _make_routes(WebRTCTransportConfig(static_dir=str(static_dir)))
    app = await _aiohttp_app_with_routes(routes, prefix="")
    query = urlencode({"webrtc": raw, "token": "sekrit", "view": "compact"})
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/?{query}", allow_redirects=False)
        assert resp.status == 302
        location = resp.headers["Location"]

    assert "webrtc=" not in location
    assert location == "/webrtc_client.html?view=compact"


# ── Mounted VoiceServer integration tests ────────────────────────────────


@pytest.fixture
def fake_webrtc(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_webrtc_modules(monkeypatch)


async def _running_server(
    config: VoiceServerConfig,
    *,
    sessions: list[_FakeSession] | None = None,
    transports: list[object] | None = None,
) -> VoiceServer:
    def factory(transport: object) -> _FakeSession:
        if transports is not None:
            transports.append(transport)
        session = _FakeSession()
        if sessions is not None:
            sessions.append(session)
        return session

    server = VoiceServer(config, session_factory=factory)
    await server.start()
    return server


def _base_url(server: VoiceServer) -> str:
    address = server.http_address
    assert address is not None
    host, port = address
    return f"http://{host}:{port}"


@pytest.fixture
async def client() -> AsyncIterator[object]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        yield session


_OFFER_BODY = {"sdp": "v=0\r\n", "type": "offer"}


@pytest.mark.integration_socket
async def test_offer_returns_answer_and_one_session_per_offer(
    fake_webrtc: None, client: object
) -> None:
    sessions: list[_FakeSession] = []
    transports: list[object] = []
    # ``drain_timeout_s=0`` so ``stop`` force-drains the still-open fake peers
    # without waiting the full grace window (the fake peer never self-closes).
    server = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, enable_websocket=False, drain_timeout_s=0.0),
        sessions=sessions,
        transports=transports,
    )
    try:
        url = f"{_base_url(server)}/webrtc/offer"
        for _ in range(2):
            async with client.post(url, json=_OFFER_BODY) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data == {"sdp": "fake-answer", "type": "answer"}
        assert len(sessions) == 2
        # Each offer got an isolated per-peer transport.
        assert len(transports) == 2
        assert transports[0] is not transports[1]
        assert [t.offer_request.path for t in transports] == [
            "/webrtc/offer",
            "/webrtc/offer",
        ]
        for session in sessions:
            await asyncio.wait_for(session.started.wait(), timeout=1)
    finally:
        await server.stop()
    # ``stop`` force-drained the active WebRTC sessions.
    assert all(session.stopped.is_set() for session in sessions)


@pytest.mark.integration_socket
async def test_config_exposes_default_stun_on_mounted_route(
    fake_webrtc: None, client: object
) -> None:
    config = VoiceServerConfig(
        host="127.0.0.1",
        port=0,
        enable_websocket=False,
        cors_allowed_origins=(),
    )
    server = VoiceServer(config, session_factory=lambda _t: _FakeSession())
    # Configure ICE servers via the derived WebRTC config path: re-build routes
    # with explicit ICE servers by mounting after overriding the config.
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/webrtc/config") as resp:
            assert resp.status == 200
            data = await resp.json()
            assert "iceServers" in data
            # Default config ships a single public STUN server, URL only.
            assert data["iceServers"] == [{"urls": ["stun:stun.l.google.com:19302"]}]
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_stats_sanitizes_on_mounted_route(fake_webrtc: None, client: object) -> None:
    server = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, enable_websocket=False)
    )
    try:
        base = _base_url(server)
        async with client.post(
            f"{base}/webrtc/stats",
            json={"kind": "webrtc_client_stats", "sequence": 1},
            headers={"Origin": base},
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data == {"status": "ok"}
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_unified_auth_rejects_tokenless_offer_and_accepts_bearer(
    fake_webrtc: None, client: object
) -> None:
    config = VoiceServerConfig(
        host="127.0.0.1",
        port=0,
        enable_websocket=False,
        auth=BearerTokenAuth(token="sekrit"),
        drain_timeout_s=0.0,
    )
    server = await _running_server(config)
    try:
        url = f"{_base_url(server)}/webrtc/offer"
        # Tokenless offer is rejected by the unified AuthPolicy.
        async with client.post(url, json=_OFFER_BODY) as resp:
            assert resp.status == 401
        # Bearer header authorizes.
        async with client.post(
            url, json=_OFFER_BODY, headers={"Authorization": "Bearer sekrit"}
        ) as resp:
            assert resp.status == 200
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_query_token_rejected_unless_allow_query_token(
    fake_webrtc: None, client: object
) -> None:
    # Default-off: a correct ``?token=`` is rejected (allow_query_token=False).
    rejecting = await _running_server(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            enable_websocket=False,
            auth=BearerTokenAuth(token="sekrit"),
        )
    )
    try:
        async with client.post(
            f"{_base_url(rejecting)}/webrtc/offer?token=sekrit", json=_OFFER_BODY
        ) as resp:
            assert resp.status == 401
    finally:
        await rejecting.stop()

    # Opt-in: ``allow_query_token=True`` accepts the query token.
    accepting = await _running_server(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            enable_websocket=False,
            auth=BearerTokenAuth(token="sekrit", allow_query_token=True),
            drain_timeout_s=0.0,
        )
    )
    try:
        async with client.post(
            f"{_base_url(accepting)}/webrtc/offer?token=sekrit", json=_OFFER_BODY
        ) as resp:
            assert resp.status == 200
    finally:
        await accepting.stop()


@pytest.mark.integration_socket
async def test_shared_gate_caps_offers_and_spans_websocket(
    fake_webrtc: None, client: object
) -> None:
    # max_sessions=1: the first offer reserves the only slot via the SHARED gate,
    # so a second offer gets 503 — and a ``/ws`` connection would be rejected too
    # (capacity spans both). We assert the gate is the single shared owner.
    server = await _running_server(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            max_sessions=1,
            enable_websocket=True,
            drain_timeout_s=0.0,
        )
    )
    try:
        url = f"{_base_url(server)}/webrtc/offer"
        async with client.post(url, json=_OFFER_BODY) as resp:
            assert resp.status == 200
        # The WebRTC routes and the ``/ws`` handler share ``server._gate``.
        assert server._webrtc_routes._gate is server._gate
        assert server._gate.reserved_count == 1
        async with client.post(url, json=_OFFER_BODY) as resp:
            assert resp.status == 503
            data = await resp.json()
            assert "session limit" in data["error"]
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_draining_rejects_new_offers(fake_webrtc: None, client: object) -> None:
    server = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, enable_websocket=False)
    )
    try:
        server._gate.start_draining()
        async with client.post(f"{_base_url(server)}/webrtc/offer", json=_OFFER_BODY) as resp:
            assert resp.status == 503
            data = await resp.json()
            assert "shutting down" in data["error"]
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_webrtc_offer_rejections_count_in_server_metrics(
    fake_webrtc: None, client: object
) -> None:
    # Regression: WebRTC offer rejections (auth / draining / capacity) share the
    # server's gate, so they must feed the SAME rejection metric path as ``/ws``.
    # Otherwise ``/metrics`` and ``easycat.server.sessions.rejected.total``
    # undercount the WebRTC transport.
    server = await _running_server(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            max_sessions=1,
            enable_websocket=False,
            auth=BearerTokenAuth(token="sekrit"),
        )
    )
    try:
        base = f"{_base_url(server)}/webrtc/offer"
        assert server.metrics_payload()["sessions_rejected_total"] == 0
        # (a) tokenless offer -> auth rejection counted.
        async with client.post(base, json=_OFFER_BODY) as resp:
            assert resp.status == 401
        assert server.metrics_payload()["sessions_rejected_total"] == 1
        # (b) draining offer -> rejection counted.
        server._gate.start_draining()
        async with client.post(
            base, json=_OFFER_BODY, headers={"Authorization": "Bearer sekrit"}
        ) as resp:
            assert resp.status == 503
        assert server.metrics_payload()["sessions_rejected_total"] == 2
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_webrtc_offer_emits_connections_active_metric(
    fake_webrtc: None, client: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: an accepted WebRTC offer reserves on the shared gate, so it
    # must refresh the SAME ``easycat.server.connections.active`` gauge as a
    # ``/ws`` session. Otherwise OTel alerts/autoscaling miss WebRTC-only traffic
    # even though the JSON ``/metrics`` snapshot (gate-read) stays correct.
    from easycat.server import metrics as server_metrics

    observed: list[tuple[int, str]] = []
    real = server_metrics.observe_connections_active

    def spy(count: int, *, server_state: str) -> None:
        observed.append((count, server_state))
        real(count, server_state=server_state)  # type: ignore[arg-type]

    monkeypatch.setattr(server_metrics, "observe_connections_active", spy)
    server = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, enable_websocket=False, drain_timeout_s=0.0)
    )
    try:
        async with client.post(f"{_base_url(server)}/webrtc/offer", json=_OFFER_BODY) as resp:
            assert resp.status == 200
        # The accepted offer emitted the active-connection gauge at count 1.
        assert (1, "serving") in observed
    finally:
        await server.stop()


@pytest.mark.integration_socket
async def test_stop_force_drains_active_webrtc_session(fake_webrtc: None, client: object) -> None:
    sessions: list[_FakeSession] = []
    server = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, enable_websocket=False),
        sessions=sessions,
    )
    async with client.post(f"{_base_url(server)}/webrtc/offer", json=_OFFER_BODY) as resp:
        assert resp.status == 200
    assert len(sessions) == 1
    # The session is tracked in the SHARED active set so the drain step reaches it.
    assert len(server._active_session_objs) == 1
    # force=True collapses the grace window so the active session is force-stopped.
    await server.stop(force=True)
    assert sessions[0].stopped.is_set()
    assert server._active_session_objs == {}


@pytest.mark.integration_socket
async def test_no_webrtc_routes_when_disabled(client: object) -> None:
    server = await _running_server(
        VoiceServerConfig(host="127.0.0.1", port=0, enable_webrtc=False, enable_websocket=False)
    )
    try:
        assert server._webrtc_routes is None
        async with client.post(f"{_base_url(server)}/webrtc/offer", json=_OFFER_BODY) as resp:
            assert resp.status == 404
    finally:
        await server.stop()
