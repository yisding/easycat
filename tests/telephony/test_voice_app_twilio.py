"""Twilio-mode delegation, TwiML/token lifecycle, and media lifecycle.

All tests are offline: the server helper is monkeypatched (or driven with fakes)
so no real socket is opened, and ``create_session`` / ``SessionManager`` are
stubbed for the media-lifecycle test. The TwiML/token path exercises the real
:class:`TwilioStreamTokenStore` and :func:`twiml_connect_stream`.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

import easycat.telephony.server as server_module
from easycat.config import EasyConfig, TelephonyConfig
from easycat.telephony import twilio_stream_parameters_from_form
from easycat.telephony.server import (
    TwilioVoiceServerConfig,
    run_twilio_voice_app,
    serve_twilio_voice_app,
)
from easycat.transports import TwilioStreamTokenStore
from easycat.transports.twilio_media import TWILIO_STREAM_TOKEN_PARAMETER, twiml_connect_stream
from easycat.voice_app import VoiceApp


@pytest.fixture(autouse=True)
def _openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a placeholder key so bare ``EasyConfig`` presets validate."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def _clear_twilio_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep env convenience fallbacks from leaking between tests."""
    monkeypatch.delenv("TWILIO_STREAM_URL", raising=False)
    monkeypatch.delenv("TWILIO_STREAM_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)


class _FakeTwilioTransport:
    """Concrete-enough stand-in for ``TwilioConnectionTransport``."""

    def __init__(self, ws: Any = None, *, config: Any = None) -> None:
        self.ws = ws
        self.config = config


# ── Shared fake aiohttp.web + server driver ───────────────────────────


class _FakeAiohttpWeb:
    """Minimal aiohttp.web stand-in capturing routes and teardown calls."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.runner_cleaned = False
        self.site_stopped = False
        web = self

        class Response:
            def __init__(
                self,
                *,
                text: str = "",
                content_type: str = "text/plain",
                status: int = 200,
            ) -> None:
                self.text = text
                self.content_type = content_type
                self.status = status

        class _Router:
            def add_post(self, path: str, handler: Any) -> None:
                web.routes[path] = handler

        class Application:
            def __init__(self) -> None:
                self.router = _Router()

        class AppRunner:
            def __init__(self, app: Any) -> None:
                self.app = app

            async def setup(self) -> None:
                return None

            async def cleanup(self) -> None:
                web.runner_cleaned = True

        class TCPSite:
            def __init__(self, runner: Any, host: str, port: int) -> None:
                self.runner = runner

            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                web.site_stopped = True

        self.Response = Response
        self.Application = Application
        self.AppRunner = AppRunner
        self.TCPSite = TCPSite


class _FakeMediaServer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _ServerHarness:
    """Patches the helper's I/O seams and lets a test drive one lifecycle."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.web = _FakeAiohttpWeb()
        self.media_server = _FakeMediaServer()
        self.media_handler: Any = None
        self._started = asyncio.Event()
        self._shutdown = asyncio.Event()

        async def _fake_serve_ws(handler: Any, host: str, port: int) -> _FakeMediaServer:
            self.media_handler = handler
            return self.media_server

        def _fake_shutdown_event() -> asyncio.Event:
            self._started.set()
            return self._shutdown

        monkeypatch.setattr(server_module, "require_module", lambda *a, **k: self.web)
        monkeypatch.setattr(server_module.websockets, "serve", _fake_serve_ws)
        monkeypatch.setattr(server_module, "create_shutdown_event", _fake_shutdown_event)

    async def run(self, factory: Any, config: TwilioVoiceServerConfig, body: Any) -> None:
        """Start the server, await readiness, run ``body``, then shut down."""
        task = asyncio.create_task(serve_twilio_voice_app(factory, config))
        await self._started.wait()
        try:
            await body(self)
        finally:
            self._shutdown.set()
            await task


# ── Delegation: run('twilio') ────────────────────────────────────────


@pytest.fixture
def captured_twilio(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the (factory, config) passed to the server helper and short-circuit
    ``asyncio.run`` so ``run('twilio')`` never opens a socket."""
    captured: dict[str, Any] = {}

    async def _fake_serve(factory: Any, config: Any, *, on_session: Any = None) -> None:
        captured["factory"] = factory
        captured["config"] = config

    def _fake_asyncio_run(coro: Any) -> None:
        # Drive the (fake) coroutine to completion without owning a loop.
        captured["asyncio_run_called"] = True
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(server_module, "serve_twilio_voice_app", _fake_serve)
    monkeypatch.setattr("asyncio.run", _fake_asyncio_run)
    return captured


def test_twilio_run_delegates_to_helper(captured_twilio: dict[str, Any]) -> None:
    VoiceApp(agent="a").run(
        "twilio",
        stream_url="wss://example/media",
        media_port=9100,
        http_port=9200,
    )
    assert captured_twilio["asyncio_run_called"] is True
    assert callable(captured_twilio["factory"])
    config = captured_twilio["config"]
    assert isinstance(config, TwilioVoiceServerConfig)
    assert config.stream_url == "wss://example/media"
    assert config.media_port == 9100
    assert config.http_port == 9200


def test_phone_alias_delegates_to_twilio(captured_twilio: dict[str, Any]) -> None:
    VoiceApp(agent="a").run("phone", stream_url="wss://example/media")
    assert captured_twilio["asyncio_run_called"] is True
    assert isinstance(captured_twilio["config"], TwilioVoiceServerConfig)


def test_twilio_default_max_sessions_uses_server_config_default(
    captured_twilio: dict[str, Any],
) -> None:
    """Without an explicit limit anywhere, the TwilioVoiceServerConfig default applies."""
    VoiceApp(agent="a").run("twilio", stream_url="wss://example/media")
    assert captured_twilio["config"].max_sessions == 64


def test_twilio_forwards_max_sessions_from_construction(
    captured_twilio: dict[str, Any],
) -> None:
    """``max_sessions`` is mode-neutral, so a construction-time value reaches twilio
    (mirroring the browser/websocket builders)."""
    VoiceApp(agent="a", max_sessions=3).run("twilio", stream_url="wss://example/media")
    assert captured_twilio["config"].max_sessions == 3


def test_twilio_run_max_sessions_overrides_construction(
    captured_twilio: dict[str, Any],
) -> None:
    """A ``run('twilio', max_sessions=...)`` value wins over the construction-time one."""
    VoiceApp(agent="a", max_sessions=3).run(
        "twilio", stream_url="wss://example/media", max_sessions=7
    )
    assert captured_twilio["config"].max_sessions == 7


def test_run_twilio_voice_app_drives_async_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sync wrapper owns ``asyncio.run`` and drives ``serve_twilio_voice_app``.

    Mirrors ``run_webrtc_config_server`` / ``run_websocket_config_server``: the
    loop ownership lives next to the async server, so ``VoiceApp._run_twilio``
    no longer calls ``asyncio.run`` itself.
    """
    captured: dict[str, Any] = {}

    async def _fake_serve(factory: Any, config: Any, *, on_session: Any = None) -> None:
        captured["factory"] = factory
        captured["config"] = config

    def _fake_asyncio_run(coro: Any) -> None:
        captured["asyncio_run_called"] = True
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(server_module, "serve_twilio_voice_app", _fake_serve)
    monkeypatch.setattr("asyncio.run", _fake_asyncio_run)

    def factory(transport: Any) -> EasyConfig:
        return EasyConfig.phone(transport=transport, agent="a")

    config = TwilioVoiceServerConfig(stream_url="wss://example/media")
    run_twilio_voice_app(factory, config)

    assert captured["asyncio_run_called"] is True
    assert captured["factory"] is factory
    assert captured["config"] is config


def test_run_twilio_voice_app_is_module_export() -> None:
    """The sync wrapper is part of the telephony.server public surface."""
    assert "run_twilio_voice_app" in server_module.__all__


def test_twilio_serve_does_not_call_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """``serve('twilio')`` is the async entry — only ``run()`` owns the loop."""
    seen: dict[str, Any] = {}

    async def _fake_serve(factory: Any, config: Any, *, on_session: Any = None) -> None:
        seen["config"] = config

    def _explode(_coro: Any) -> None:
        raise AssertionError("serve('twilio') must not call asyncio.run")

    monkeypatch.setattr(server_module, "serve_twilio_voice_app", _fake_serve)
    monkeypatch.setattr("asyncio.run", _explode)

    # Drive serve() on a manually-owned loop so the only asyncio.run that could
    # fire is one inside serve() itself (which must NOT happen).
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            VoiceApp(agent="a").serve("twilio", stream_url="wss://example/media")
        )
    finally:
        loop.close()
    assert seen["config"].stream_url == "wss://example/media"


def test_twilio_stream_url_from_env(
    monkeypatch: pytest.MonkeyPatch, captured_twilio: dict[str, Any]
) -> None:
    monkeypatch.setenv("TWILIO_STREAM_URL", "wss://env/media")
    monkeypatch.setenv("TWILIO_STREAM_TOKEN_SECRET", "env-secret")
    VoiceApp(agent="a").run("twilio")
    config = captured_twilio["config"]
    assert config.stream_url == "wss://env/media"
    assert config.stream_token_secret == "env-secret"


# ── Per-connection: static config rejected, factory required ──────────


def test_twilio_rejects_static_config() -> None:
    app = VoiceApp(config=EasyConfig.phone(agent="a"))
    with pytest.raises(ValueError) as exc:
        app.run("twilio", stream_url="wss://example/media")
    message = str(exc.value)
    assert "config_factory" in message
    assert "per-connection" in message


def test_twilio_uses_supplied_config_factory(captured_twilio: dict[str, Any]) -> None:
    def factory(transport: Any) -> EasyConfig:
        return EasyConfig.phone(transport=transport, agent="fac")

    VoiceApp(config_factory=factory).run("twilio", stream_url="wss://example/media")
    assert captured_twilio["factory"] is factory


# ── Factory-called-once / fresh config per transport ──────────────────


def test_twilio_factory_builds_fresh_config_per_transport(
    captured_twilio: dict[str, Any],
) -> None:
    VoiceApp(agent="phone-agent").run("twilio", stream_url="wss://example/media")
    factory = captured_twilio["factory"]

    t1 = _FakeTwilioTransport()
    t2 = _FakeTwilioTransport()
    config1 = factory(t1)
    config2 = factory(t2)

    assert isinstance(config1, EasyConfig)
    assert isinstance(config2, EasyConfig)
    assert config1 is not config2
    assert config1.transport is t1
    assert config2.transport is t2
    assert config1.agent == "phone-agent"


def test_dtmf_voicemail_opt_in_flows_through_telephony_config(
    captured_twilio: dict[str, Any],
) -> None:
    """DTMF/voicemail are opted in via ``TelephonyConfig`` inside the factory —
    never via ``TwilioVoiceServerConfig``."""

    def factory(transport: Any) -> EasyConfig:
        return EasyConfig.phone(
            transport=transport,
            agent="a",
            telephony=TelephonyConfig(
                enable_dtmf_aggregator=True,
                enable_voicemail_detector=True,
            ),
        )

    VoiceApp(config_factory=factory).run("twilio", stream_url="wss://example/media")
    config = captured_twilio["factory"](_FakeTwilioTransport())
    assert config.telephony is not None
    assert config.telephony.enable_dtmf_aggregator is True
    assert config.telephony.enable_voicemail_detector is True


def test_server_config_has_no_dtmf_or_voicemail_fields() -> None:
    """``TwilioVoiceServerConfig`` must NOT carry the telephony opt-in flags."""
    config = TwilioVoiceServerConfig()
    assert not hasattr(config, "enable_dtmf_aggregator")
    assert not hasattr(config, "enable_voicemail_detector")


def test_server_config_defaults_match_spec() -> None:
    config = TwilioVoiceServerConfig()
    assert config.host == "0.0.0.0"
    assert config.media_port == 8766
    assert config.http_host == "0.0.0.0"
    assert config.http_port == 8000
    assert config.stream_url is None
    assert config.stream_token_secret is None
    assert config.twilio_auth_token is None
    assert config.trust_proxy_headers is False
    assert config.unsafe_allow_unsigned_webhooks is False
    assert config.max_sessions == 64


# ── Missing telephony extra ───────────────────────────────────────────


def test_missing_telephony_extra_raises_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hiding aiohttp.web makes the helper raise a clear missing-extra error."""

    def _fake_require_module(module_name: str, **kwargs: Any) -> Any:
        if module_name == "aiohttp.web":
            from easycat._extras import _extra_install_hint

            hint = _extra_install_hint(kwargs.get("extra"))
            purpose = kwargs.get("purpose") or module_name
            raise ImportError(f"{purpose} requires the {module_name} package.{hint}")
        raise AssertionError(f"unexpected require_module({module_name!r})")

    monkeypatch.setattr(server_module, "require_module", _fake_require_module)

    config = TwilioVoiceServerConfig(stream_url="wss://example/media")
    with pytest.raises(ImportError) as exc:
        asyncio.run(serve_twilio_voice_app(lambda t: EasyConfig.phone(transport=t), config))
    assert "easycat[telephony]" in str(exc.value)


def test_run_twilio_surfaces_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run('twilio')`` surfaces the gated ImportError from the helper."""

    def _fake_require_module(module_name: str, **kwargs: Any) -> Any:
        if module_name == "aiohttp.web":
            raise ImportError(
                "VoiceApp twilio mode requires the aiohttp.web package. easycat[telephony]"
            )
        raise AssertionError(f"unexpected require_module({module_name!r})")

    monkeypatch.setattr(server_module, "require_module", _fake_require_module)
    with pytest.raises(ImportError) as exc:
        VoiceApp(agent="a").run("twilio", stream_url="wss://example/media")
    assert "easycat[telephony]" in str(exc.value)


# ── stream_url required ───────────────────────────────────────────────


def test_stream_url_required_raises_value_error() -> None:
    config = TwilioVoiceServerConfig(stream_url=None)
    with pytest.raises(ValueError) as exc:
        asyncio.run(serve_twilio_voice_app(lambda t: EasyConfig.phone(transport=t), config))
    assert "stream_url" in str(exc.value)


def test_run_twilio_without_stream_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without stream_url (kwarg or env) the helper's ValueError surfaces."""
    monkeypatch.delenv("TWILIO_STREAM_URL", raising=False)
    with pytest.raises(ValueError) as exc:
        VoiceApp(agent="a").run("twilio")
    assert "stream_url" in str(exc.value)


# ── media listener cleanup on HTTP startup failure ────────────────────


def test_media_listener_closed_when_http_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If TwiML HTTP setup fails (e.g. http_port already in use), the already-
    bound media WebSocket listener must be closed (and the runner cleaned up)
    instead of leaking the media port."""
    web = _FakeAiohttpWeb()
    media_server = _FakeMediaServer()

    # Simulate ``http_port`` already in use: TCPSite.start raises after the
    # media listener is already bound.
    async def _boom(self: Any) -> None:
        raise OSError("address already in use")

    monkeypatch.setattr(web.TCPSite, "start", _boom)

    async def _fake_serve_ws(handler: Any, host: str, port: int) -> _FakeMediaServer:
        return media_server

    monkeypatch.setattr(server_module, "require_module", lambda *a, **k: web)
    monkeypatch.setattr(server_module.websockets, "serve", _fake_serve_ws)

    config = TwilioVoiceServerConfig(stream_url="wss://example/media", twilio_auth_token="tok")

    with pytest.raises(OSError, match="address already in use"):
        asyncio.run(serve_twilio_voice_app(lambda t: EasyConfig.phone(transport=t), config))

    assert media_server.closed is True
    assert web.runner_cleaned is True


# ── session('twilio') still raises ────────────────────────────────────


def test_session_twilio_raises() -> None:
    app = VoiceApp(agent="a")
    with pytest.raises(ValueError):
        app.session("twilio")


# ── TwiML / token lifecycle (real store + real TwiML) ─────────────────


def test_twiml_handler_embeds_consumable_stream_token() -> None:
    """The /twiml handler returns XML embedding a token the same store accepts
    exactly once; replay and forged tokens are rejected."""
    store = TwilioStreamTokenStore("secret")
    token = store.issue()
    form_items = [("From", "+15551234567"), ("To", "+15557654321"), ("Direction", "inbound")]

    xml = twiml_connect_stream(
        "wss://example/media",
        parameters=twilio_stream_parameters_from_form(form_items),
        stream_token=token,
    )

    assert xml.startswith('<?xml version="1.0"')
    assert "<Connect>" in xml
    assert "wss://example/media" in xml
    assert TWILIO_STREAM_TOKEN_PARAMETER in xml
    assert token in xml
    assert 'name="From" value="+15551234567"' in xml

    # The embedded token is consumed exactly once; replay and forgery fail.
    assert store.consume(token) is True
    assert store.consume(token) is False
    assert store.consume("forged.999999.sig") is False


class _FakeTwimlRequest:
    def __init__(
        self,
        form: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
        path_qs: str = "/twiml",
        scheme: str = "https",
    ) -> None:
        self._form = form
        self.headers = headers or {}
        self.path_qs = path_qs
        self.scheme = scheme

    async def post(self) -> dict[str, str]:
        return self._form


def test_twiml_handler_returns_application_xml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the helper, capture the /twiml aiohttp handler, and run it against a
    fake request, asserting the XML response and listener teardown."""
    harness = _ServerHarness(monkeypatch)
    result: dict[str, Any] = {}

    async def _body(h: _ServerHarness) -> None:
        handler = h.web.routes["/twiml"]
        request = _FakeTwimlRequest({"From": "+15551234567", "Direction": "inbound"})
        result["response"] = await handler(request)

    # No auth_token: the unsigned-webhook escape hatch keeps the listener open so
    # this test stays focused on the XML/token response shape.
    config = TwilioVoiceServerConfig(
        stream_url="wss://example/media", unsafe_allow_unsigned_webhooks=True
    )
    asyncio.run(harness.run(lambda t: EasyConfig.phone(transport=t), config, _body))

    response = result["response"]
    assert response.content_type == "application/xml"
    assert "<Connect>" in response.text
    assert "wss://example/media" in response.text
    assert TWILIO_STREAM_TOKEN_PARAMETER in response.text
    # Shutdown tore down both listeners.
    assert harness.media_server.closed is True
    assert harness.web.site_stopped is True
    assert harness.web.runner_cleaned is True


# ── Webhook authentication (signature validation before token minting) ─


def test_serve_requires_auth_token_for_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without auth_token (and without the escape hatch), the helper refuses to
    serve so an unauthenticated POST /twiml cannot mint a stream token."""
    # Get past the telephony-extra gate; the auth check follows immediately.
    monkeypatch.setattr(server_module, "require_module", lambda *a, **k: object())
    config = TwilioVoiceServerConfig(stream_url="wss://example/media")
    with pytest.raises(ValueError) as exc:
        asyncio.run(serve_twilio_voice_app(lambda t: EasyConfig.phone(transport=t), config))
    message = str(exc.value)
    assert "auth_token" in message
    assert "unsafe_allow_unsigned_webhooks" in message


def test_run_twilio_requires_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run('twilio')`` surfaces the missing-auth_token guard from the helper."""
    monkeypatch.setattr(server_module, "require_module", lambda *a, **k: object())
    with pytest.raises(ValueError) as exc:
        VoiceApp(agent="a").run("twilio", stream_url="wss://example/media")
    assert "auth_token" in str(exc.value)


def test_twilio_auth_token_from_env(
    monkeypatch: pytest.MonkeyPatch, captured_twilio: dict[str, Any]
) -> None:
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-secret")
    VoiceApp(agent="a").run("twilio", stream_url="wss://example/media")
    assert captured_twilio["config"].twilio_auth_token == "twilio-secret"


def test_twiml_handler_validates_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """With auth_token set, POST /twiml mints a token only for a valid Twilio
    signature; a bad or missing signature is rejected with 403 before any token
    is issued."""
    from easycat.telephony.twiml import compute_twilio_webhook_signature

    harness = _ServerHarness(monkeypatch)
    result: dict[str, Any] = {}
    form = {"From": "+15551234567", "Direction": "inbound"}
    public_url = "https://relay.example/twiml"
    signature = compute_twilio_webhook_signature(
        auth_token="tw-secret", url=public_url, params=list(form.items())
    )

    async def _body(h: _ServerHarness) -> None:
        handler = h.web.routes["/twiml"]
        good = _FakeTwimlRequest(
            form, headers={"Host": "relay.example", "X-Twilio-Signature": signature}
        )
        result["good"] = await handler(good)
        bad = _FakeTwimlRequest(
            form, headers={"Host": "relay.example", "X-Twilio-Signature": "wrong"}
        )
        result["bad"] = await handler(bad)
        missing = _FakeTwimlRequest(form, headers={"Host": "relay.example"})
        result["missing"] = await handler(missing)

    config = TwilioVoiceServerConfig(
        stream_url="wss://example/media", twilio_auth_token="tw-secret"
    )
    asyncio.run(harness.run(lambda t: EasyConfig.phone(transport=t), config, _body))

    # Valid signature -> TwiML with an embedded stream token.
    assert result["good"].status == 200
    assert result["good"].content_type == "application/xml"
    assert TWILIO_STREAM_TOKEN_PARAMETER in result["good"].text
    # Forged / missing signature -> 403, no token minted.
    assert result["bad"].status == 403
    assert TWILIO_STREAM_TOKEN_PARAMETER not in result["bad"].text
    assert result["missing"].status == 403


def test_twilio_server_config_reads_auth_token_and_trust_proxy_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VoiceApp sources the webhook auth token AND trust_proxy_headers from env."""
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "env-twilio-secret")
    monkeypatch.setenv("TWILIO_STREAM_URL", "wss://example/media")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")

    config = VoiceApp(agent="a")._twilio_server_config()

    assert config.twilio_auth_token == "env-twilio-secret"
    assert config.trust_proxy_headers is True


# ── Media lifecycle (fake ServerConnection + stubbed session) ─────────


class _FakeWs:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def wait_closed(self) -> None:
        self._events.append("wait_closed")


class _FakeSession:
    def __init__(self, config: Any, events: list[str]) -> None:
        self.config = config
        self._events = events

    async def start(self) -> None:
        self._events.append("start")

    async def stop(self) -> None:
        self._events.append("stop")


class _FakeManager:
    def __init__(self) -> None:
        self._sessions: dict[int, Any] = {}
        self.events: list[str] = []

    @asynccontextmanager
    async def connection(self, key: int, session: Any, **kwargs: Any) -> Any:
        self.events.append("register")
        self._sessions[key] = session
        await session.start()
        try:
            yield session
        finally:
            self.events.append("unregister")
            self._sessions.pop(key, None)
            await session.stop()

    async def stop_all(self) -> None:
        self.events.append("stop_all")
        for session in list(self._sessions.values()):
            await session.stop()
        self._sessions.clear()


def test_media_handler_creates_and_tears_down_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``handle_twilio_connection`` builds a transport, calls the factory ->
    create_session, registers via SessionManager, awaits wait_closed, and the
    server tears every session down on shutdown."""
    events: list[str] = []
    factory_transports: list[Any] = []
    manager = _FakeManager()

    def _fake_create_session(config: Any) -> _FakeSession:
        events.append("create_session")
        return _FakeSession(config, events)

    import easycat.config as config_mod
    import easycat.session_manager as sm_mod
    import easycat.transports.twilio_media as twilio_mod

    monkeypatch.setattr(config_mod, "create_session", _fake_create_session)
    monkeypatch.setattr(sm_mod, "SessionManager", lambda: manager)
    monkeypatch.setattr(twilio_mod, "TwilioConnectionTransport", _FakeTwilioTransport)

    harness = _ServerHarness(monkeypatch)

    def _factory(transport: Any) -> EasyConfig:
        factory_transports.append(transport)
        return EasyConfig.phone(transport=transport, agent="a")

    async def _body(h: _ServerHarness) -> None:
        await h.media_handler(_FakeWs(events))  # one full call lifecycle

    config = TwilioVoiceServerConfig(
        stream_url="wss://example/media", unsafe_allow_unsigned_webhooks=True
    )
    asyncio.run(harness.run(_factory, config, _body))

    # The factory saw the transport built from the fake ws.
    assert len(factory_transports) == 1
    assert isinstance(factory_transports[0], _FakeTwilioTransport)
    # Full lifecycle: create -> register -> start -> wait_closed -> unregister -> stop.
    combined = events + manager.events
    assert events[0] == "create_session"
    assert "register" in manager.events
    assert "start" in combined
    assert "wait_closed" in combined
    assert "unregister" in manager.events
    assert "stop" in combined
    # Server teardown closes the media listener and stops all sessions.
    assert harness.media_server.closed is True
    assert "stop_all" in manager.events


class _BlockingWs:
    """Fake ws whose ``wait_closed`` blocks until released, recording closes."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.close_calls: list[tuple[Any, Any]] = []

    async def wait_closed(self) -> None:
        self.entered.set()
        await self.release.wait()

    async def close(self, *, code: Any = None, reason: Any = None) -> None:
        self.close_calls.append((code, reason))


def test_media_handler_rejects_connections_over_session_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once ``max_sessions`` sessions are live, extra sockets are closed (1013)
    without building or starting a session."""
    created: list[Any] = []
    manager = _FakeManager()

    def _fake_create_session(config: Any) -> _FakeSession:
        created.append(config)
        return _FakeSession(config, manager.events)

    import easycat.config as config_mod
    import easycat.session_manager as sm_mod
    import easycat.transports.twilio_media as twilio_mod

    monkeypatch.setattr(config_mod, "create_session", _fake_create_session)
    monkeypatch.setattr(sm_mod, "SessionManager", lambda: manager)
    monkeypatch.setattr(twilio_mod, "TwilioConnectionTransport", _FakeTwilioTransport)

    harness = _ServerHarness(monkeypatch)

    def _factory(transport: Any) -> EasyConfig:
        return EasyConfig.phone(transport=transport, agent="a")

    config = TwilioVoiceServerConfig(
        stream_url="wss://example/media",
        unsafe_allow_unsigned_webhooks=True,
        max_sessions=1,
    )

    async def _body(h: _ServerHarness) -> None:
        ws_active = _BlockingWs()
        ws_rejected = _BlockingWs()
        # First connection fills the single slot and parks in wait_closed.
        active = asyncio.create_task(h.media_handler(ws_active))
        await ws_active.entered.wait()
        # Second connection is over the limit: rejected with 1013, no session.
        await h.media_handler(ws_rejected)
        assert ws_rejected.close_calls == [(1013, "Server is at the configured session limit")]
        assert len(created) == 1
        # Releasing the first connection frees the slot and lets it tear down.
        ws_active.release.set()
        await active

    asyncio.run(harness.run(_factory, config, _body))

    # Exactly one session was ever created/started despite two connections.
    assert len(created) == 1
