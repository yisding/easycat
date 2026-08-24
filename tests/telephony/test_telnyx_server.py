"""Telnyx voice-app server: webhook auth, token minting, media lifecycle.

All tests are offline: the aiohttp/websockets seams are monkeypatched (or
driven with fakes from ``tests/transports/test_telnyx_transport.py``) so no
real socket is opened, and ``create_session`` / ``SessionManager`` are stubbed
for the media-lifecycle test. The Ed25519 signatures use freshly generated
keys; the answer command is captured through a fake Call Control client.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import easycat.telephony.telnyx_client as telnyx_client_module
import easycat.telephony.telnyx_server as telnyx_server_module
from easycat.config import EasyConfig
from easycat.telephony.telnyx import (
    TELNYX_WEBHOOK_SIGNATURE_HEADER,
    TELNYX_WEBHOOK_TIMESTAMP_HEADER,
    decode_client_state,
)
from easycat.telephony.telnyx_server import (
    TelnyxVoiceServerConfig,
    run_telnyx_voice_app,
    serve_telnyx_voice_app,
)
from easycat.transports._limits import MAX_WEBSOCKET_MESSAGE_BYTES
from easycat.voice_app import VoiceApp
from tests.transports.test_telnyx_transport import _ScriptedTelnyxWebSocket, _start_msg


@pytest.fixture(autouse=True)
def _openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a placeholder key so bare ``EasyConfig`` presets validate."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def _clear_telnyx_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep env convenience fallbacks from leaking between tests."""
    for name in (
        "TELNYX_STREAM_URL",
        "TELNYX_API_KEY",
        "TELNYX_PUBLIC_KEY",
        "TELNYX_CONNECTION_ID",
        "TELNYX_STREAM_TOKEN_SECRET",
        "TELNYX_WS_PORT",
        "TELNYX_MAX_SESSIONS",
        "TELNYX_START_TIMEOUT_S",
        "TELNYX_DRAIN_TIMEOUT_S",
        "TELNYX_FORCE_SHUTDOWN_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)


# ── Webhook fixtures/helpers ──────────────────────────────────────


def _ed25519_pair() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    return key, public_b64


def _sign(key: Ed25519PrivateKey, timestamp: str, body: bytes) -> str:
    return base64.b64encode(key.sign(f"{timestamp}|".encode() + body)).decode("ascii")


def call_initiated_body(call_control_id: str = "CC1") -> bytes:
    return json.dumps(
        {
            "id": "evt-1",
            "event_type": "call.initiated",
            "payload": {"call_control_id": call_control_id},
        }
    ).encode("utf-8")


def signed_request(
    key: Ed25519PrivateKey,
    body: bytes,
    *,
    timestamp: str | None = None,
    signature: str | None = None,
    omit_signature: bool = False,
) -> Any:
    """Build a fake aiohttp request carrying Telnyx signature headers.

    Signs ``{timestamp}|{body}`` with the current clock unless *signature* is
    given verbatim or *omit_signature* drops the header entirely.
    """
    resolved_timestamp = timestamp if timestamp is not None else str(int(time.time()))
    headers = {TELNYX_WEBHOOK_TIMESTAMP_HEADER: resolved_timestamp}
    if not omit_signature:
        headers[TELNYX_WEBHOOK_SIGNATURE_HEADER] = (
            signature if signature is not None else _sign(key, resolved_timestamp, body)
        )

    class _Request:
        async def read(self) -> bytes:
            return body

    request = _Request()
    request.headers = headers  # type: ignore[attr-defined]
    return request


class _RecordingCallControlClient:
    """Stand-in for ``TelnyxCallControlClient`` capturing answer commands."""

    def __init__(self, answers: list[tuple[str, dict[str, Any]]]) -> None:
        self._answers = answers

    async def answer(self, call_control_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._answers.append((call_control_id, payload))
        return {"data": {"call_control_id": call_control_id}}

    async def close(self) -> None:
        return None


@pytest.fixture
def answered(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any]]]:
    """Patch the lazily imported client so every answer command is captured."""
    answers: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        telnyx_client_module,
        "TelnyxCallControlClient",
        lambda _api_key, **_kwargs: _RecordingCallControlClient(answers),
    )
    return answers


# ── Shared fake aiohttp.web + server driver ───────────────────────


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

    def close(self, close_connections: bool = True) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _ServerHarness:
    """Patches the helper's I/O seams and lets a test drive one lifecycle."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.web = _FakeAiohttpWeb()
        self.media_server = _FakeMediaServer()
        self.media_handler: Any = None
        self.serve_kwargs: dict[str, Any] = {}
        self._started = asyncio.Event()
        self._shutdown = asyncio.Event()

        async def _fake_serve_ws(
            handler: Any, host: str, port: int, **kwargs: Any
        ) -> _FakeMediaServer:
            self.media_handler = handler
            self.serve_kwargs = kwargs
            return self.media_server

        def _fake_shutdown_event() -> asyncio.Event:
            self._started.set()
            return self._shutdown

        monkeypatch.setattr(telnyx_server_module, "require_module", lambda *a, **k: self.web)
        monkeypatch.setattr(telnyx_server_module.websockets, "serve", _fake_serve_ws)
        monkeypatch.setattr(telnyx_server_module, "create_shutdown_event", _fake_shutdown_event)

    async def run(self, factory: Any, config: TelnyxVoiceServerConfig, body: Any) -> None:
        """Start the server, await readiness, run ``body``, then shut down."""
        task = asyncio.create_task(serve_telnyx_voice_app(factory, config))
        await self._started.wait()
        try:
            await body(self)
        finally:
            self._shutdown.set()
            await task


def default_config(**overrides: Any) -> TelnyxVoiceServerConfig:
    defaults: dict[str, Any] = {
        "stream_url": "wss://example/media",
        "telnyx_api_key": "telnyx-key",
        "unsafe_allow_unsigned_webhooks": True,
    }
    defaults.update(overrides)
    return TelnyxVoiceServerConfig(**defaults)


# ── Delegation: run('telnyx') ────────────────────────────────────────


@pytest.fixture
def captured_telnyx(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _fake_serve(factory: Any, config: Any) -> None:
        captured["factory"] = factory
        captured["config"] = config

    def _fake_asyncio_run(coro: Any) -> None:
        captured["asyncio_run_called"] = True
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(telnyx_server_module, "serve_telnyx_voice_app", _fake_serve)
    monkeypatch.setattr("asyncio.run", _fake_asyncio_run)
    return captured


def test_telnyx_run_delegates_to_helper(captured_telnyx: dict[str, Any]) -> None:
    VoiceApp(agent="a").run(
        "telnyx",
        stream_url="wss://example/media",
        telnyx_api_key="k",
        telnyx_public_key="pk",
        media_port=9100,
        http_port=9200,
    )
    assert captured_telnyx["asyncio_run_called"] is True
    assert callable(captured_telnyx["factory"])
    config = captured_telnyx["config"]
    assert isinstance(config, TelnyxVoiceServerConfig)
    assert config.stream_url == "wss://example/media"
    assert config.media_port == 9100
    assert config.http_port == 9200
    assert config.telnyx_api_key == "k"
    assert config.telnyx_public_key == "pk"


def test_telnyx_env_fallbacks_feed_server_config(
    monkeypatch: pytest.MonkeyPatch, captured_telnyx: dict[str, Any]
) -> None:
    monkeypatch.setenv("TELNYX_STREAM_URL", "wss://env/media")
    monkeypatch.setenv("TELNYX_API_KEY", "env-api-key")
    monkeypatch.setenv("TELNYX_PUBLIC_KEY", "env-public-key")
    monkeypatch.setenv("TELNYX_STREAM_TOKEN_SECRET", "env-secret")
    VoiceApp(agent="a").run("telnyx")
    config = captured_telnyx["config"]
    assert config.stream_url == "wss://env/media"
    assert config.telnyx_api_key == "env-api-key"
    assert config.telnyx_public_key == "env-public-key"
    assert config.stream_token_secret == "env-secret"


def test_run_telnyx_voice_app_drives_async_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_serve(factory: Any, config: Any) -> None:
        captured["factory"] = factory
        captured["config"] = config

    def _fake_asyncio_run(coro: Any) -> None:
        captured["asyncio_run_called"] = True
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(telnyx_server_module, "serve_telnyx_voice_app", _fake_serve)
    monkeypatch.setattr("asyncio.run", _fake_asyncio_run)

    def factory(transport: Any) -> EasyConfig:
        return EasyConfig.phone(provider="telnyx", transport=transport, agent="a")

    config = default_config()
    run_telnyx_voice_app(factory, config)

    assert captured["asyncio_run_called"] is True
    assert captured["factory"] is factory
    assert captured["config"] is config


def test_run_telnyx_voice_app_is_module_export() -> None:
    assert "run_telnyx_voice_app" in telnyx_server_module.__all__


async def test_telnyx_serve_does_not_call_asyncio_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _fake_serve(factory: Any, config: Any) -> None:
        seen["config"] = config

    def _explode(_coro: Any) -> None:
        raise AssertionError("serve('telnyx') must not call asyncio.run")

    monkeypatch.setattr(telnyx_server_module, "serve_telnyx_voice_app", _fake_serve)
    monkeypatch.setattr("asyncio.run", _explode)

    await VoiceApp(agent="a").serve("telnyx", stream_url="wss://example/media")
    assert seen["config"].stream_url == "wss://example/media"


def test_telnyx_rejects_static_config() -> None:
    app = VoiceApp(config=EasyConfig.phone(provider="telnyx", agent="a"))
    with pytest.raises(ValueError) as exc:
        app.run("telnyx", stream_url="wss://example/media")
    message = str(exc.value)
    assert "config_factory" in message
    assert "per-connection" in message


def test_telnyx_factory_builds_fresh_config_per_transport(
    captured_telnyx: dict[str, Any],
) -> None:
    VoiceApp(agent="telnyx-agent").run("telnyx", stream_url="wss://example/media")
    factory = captured_telnyx["factory"]

    t1 = object()
    t2 = object()
    config1 = factory(t1)
    config2 = factory(t2)

    assert isinstance(config1, EasyConfig)
    assert isinstance(config2, EasyConfig)
    assert config1.transport is t1
    assert config2.transport is t2
    assert config1.agent == "telnyx-agent"
    # The telnyx mode resolves through the phone preset with provider="telnyx".
    from easycat.transports.telnyx_media import TelnyxTransportConfig

    assert isinstance(EasyConfig.phone(provider="telnyx").transport, TelnyxTransportConfig)


def test_telnyx_run_rejects_unknown_kwarg(captured_telnyx: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Unknown keyword argument"):
        VoiceApp(agent="a").run(
            "telnyx",
            stream_url="wss://example/media",
            telnyx_api_ke="oops",  # typo of telnyx_api_key
        )
    assert "asyncio_run_called" not in captured_telnyx


# ── Startup guards ────────────────────────────────────────────────


def test_stream_url_required_raises_value_error() -> None:
    config = TelnyxVoiceServerConfig(telnyx_api_key="k")
    with pytest.raises(ValueError) as exc:
        asyncio.run(serve_telnyx_voice_app(lambda t: None, config))
    assert "stream_url" in str(exc.value)


def test_missing_api_key_raises_value_error() -> None:
    config = TelnyxVoiceServerConfig(
        stream_url="wss://example/media",
        unsafe_allow_unsigned_webhooks=True,
    )
    with pytest.raises(ValueError) as exc:
        asyncio.run(serve_telnyx_voice_app(lambda t: None, config))
    assert "telnyx_api_key" in str(exc.value)


def test_serve_requires_public_key_for_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a public key (and without the escape hatch), refuse to serve so
    an unauthenticated POST /telnyx cannot mint tokens or answer calls."""
    monkeypatch.setattr(telnyx_server_module, "require_module", lambda *a, **k: object())
    config = TelnyxVoiceServerConfig(stream_url="wss://example/media", telnyx_api_key="k")
    with pytest.raises(ValueError) as exc:
        asyncio.run(serve_telnyx_voice_app(lambda t: None, config))
    message = str(exc.value)
    assert "telnyx_public_key" in message
    assert "unsafe_allow_unsigned_webhooks" in message


def test_missing_telnyx_extra_raises_import_error(
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

    monkeypatch.setattr(telnyx_server_module, "require_module", _fake_require_module)

    config = default_config(telnyx_public_key="pk")
    with pytest.raises(ImportError) as exc:
        asyncio.run(serve_telnyx_voice_app(lambda t: None, config))
    assert "easycat[telnyx]" in str(exc.value)


@pytest.mark.parametrize("start_timeout_s", [float("nan"), float("inf")])
def test_nonfinite_start_timeout_raises_value_error(start_timeout_s: float) -> None:
    config = default_config(start_timeout_s=start_timeout_s)

    with pytest.raises(ValueError, match="start_timeout_s"):
        asyncio.run(serve_telnyx_voice_app(lambda t: None, config))


def test_media_listener_bounds_messages_and_disables_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ServerHarness(monkeypatch)
    config = default_config()

    async def body(_h: _ServerHarness) -> None:
        pass

    asyncio.run(harness.run(lambda t: None, config, body))

    assert harness.serve_kwargs.get("compression", "MISSING") is None
    assert harness.serve_kwargs["max_size"] == MAX_WEBSOCKET_MESSAGE_BYTES
    assert "process_request" not in harness.serve_kwargs or (
        harness.serve_kwargs.get("process_request") is None
    )


def test_webhook_route_registered_at_configured_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ServerHarness(monkeypatch)
    config = default_config(webhook_path="/custom/telnyx")

    async def body(h: _ServerHarness) -> None:
        assert "/custom/telnyx" in h.web.routes

    asyncio.run(harness.run(lambda t: None, config, body))


def test_media_listener_closed_when_http_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _FakeAiohttpWeb()
    media_server = _FakeMediaServer()

    async def _boom(self: Any) -> None:
        raise OSError("address already in use")

    monkeypatch.setattr(web.TCPSite, "start", _boom)

    async def _fake_serve_ws(handler: Any, host: str, port: int, **_: Any) -> _FakeMediaServer:
        return media_server

    monkeypatch.setattr(telnyx_server_module, "require_module", lambda *a, **k: web)
    monkeypatch.setattr(telnyx_server_module.websockets, "serve", _fake_serve_ws)

    with pytest.raises(OSError, match="address already in use"):
        asyncio.run(serve_telnyx_voice_app(lambda t: None, default_config()))

    assert media_server.closed is True
    assert web.runner_cleaned is True


# ── Graceful shutdown drains sessions ─────────────────────────────


def test_telnyx_drains_sessions_on_shutdown_even_when_listener_stop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ServerHarness(monkeypatch)
    drained = False

    async def fail_site_stop(_self: Any) -> None:
        raise RuntimeError("HTTP listener stop failed")

    async def record_drain(
        _self: Any,
        _media_server: Any,
        *,
        drain_timeout_s: float,
        force_timeout_s: float,
    ) -> None:
        nonlocal drained
        assert drain_timeout_s >= 0
        assert force_timeout_s >= 0
        drained = True

    monkeypatch.setattr(harness.web.TCPSite, "stop", fail_site_stop)
    monkeypatch.setattr(telnyx_server_module.WebSocketSessionRuntime, "drain", record_drain)
    config = default_config(drain_timeout_s=1.5, force_shutdown_timeout_s=2.5)

    async def run() -> None:
        task = asyncio.create_task(serve_telnyx_voice_app(lambda t: None, config))
        await harness._started.wait()
        harness._shutdown.set()
        with pytest.raises(RuntimeError, match="HTTP listener stop failed"):
            await task

    asyncio.run(run())

    assert drained is True
    assert harness.web.runner_cleaned is True
    assert harness.media_server.closed is True


# ── Webhook authentication + token minting ────────────────────────


def test_signed_call_initiated_mints_token_and_answers(
    monkeypatch: pytest.MonkeyPatch, answered: list[tuple[str, dict[str, Any]]]
) -> None:
    harness = _ServerHarness(monkeypatch)
    key, public_b64 = _ed25519_pair()
    body = call_initiated_body("CC1")
    result: dict[str, Any] = {}

    async def _body(h: _ServerHarness) -> None:
        handler = h.web.routes["/telnyx"]
        result["response"] = await handler(signed_request(key, body))
        # A retry of the same delivery idempotently re-mints the same token.
        result["retry_response"] = await handler(signed_request(key, body))

    config = default_config(
        telnyx_public_key=public_b64,
        unsafe_allow_unsigned_webhooks=False,
    )
    asyncio.run(harness.run(lambda t: None, config, _body))

    assert result["response"].status == 200
    assert len(answered) == 2
    call_control_id, payload = answered[0]
    assert call_control_id == "CC1"
    # The answer embeds the L16@16k bidirectional stream parameters.
    assert payload["stream_bidirectional_codec"] == "L16"
    assert payload["stream_bidirectional_sampling_rate"] == 16000
    assert payload["stream_track"] == "inbound_track"
    assert payload["send_silence_when_idle"] is True
    # client_state binds the call back to its control id.
    assert decode_client_state(payload["client_state"]) == {"call_control_id": "CC1"}
    # The stream URL carries the one-time token on our public wss:// origin.
    parsed = urlsplit(payload["stream_url"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/") == "wss://example/media"
    token = parse_qs(parsed.query)["EasyCatStreamToken"][0]
    assert token
    _, retry_payload = answered[1]
    retry_token = parse_qs(urlsplit(retry_payload["stream_url"]).query)["EasyCatStreamToken"][0]
    assert retry_token == token


def test_webhook_rejects_bad_signature_without_answering(
    monkeypatch: pytest.MonkeyPatch, answered: list[tuple[str, dict[str, Any]]]
) -> None:
    harness = _ServerHarness(monkeypatch)
    key, public_b64 = _ed25519_pair()
    body = call_initiated_body("CC1")
    result: dict[str, Any] = {}

    async def _body(h: _ServerHarness) -> None:
        handler = h.web.routes["/telnyx"]
        forged = await handler(
            signed_request(key, body, signature=_sign(key, str(int(time.time())), b"tampered"))
        )
        missing_sig = await handler(signed_request(key, body, omit_signature=True))
        result["forged"] = forged.status
        result["missing"] = missing_sig.status

    config = default_config(
        telnyx_public_key=public_b64,
        unsafe_allow_unsigned_webhooks=False,
    )
    asyncio.run(harness.run(lambda t: None, config, _body))

    assert result["forged"] == 403
    assert result["missing"] == 403
    assert answered == []
    assert harness.web.site_stopped is True


def test_webhook_signature_failure_does_not_mint_consumable_token(
    monkeypatch: pytest.MonkeyPatch, answered: list[tuple[str, dict[str, Any]]]
) -> None:
    """A rejected delivery must leave zero live grants: no token can be scraped
    from any response and used against the media listener."""
    harness = _ServerHarness(monkeypatch)
    key, public_b64 = _ed25519_pair()
    body = call_initiated_body("CC1")

    async def _body(h: _ServerHarness) -> None:
        handler = h.web.routes["/telnyx"]
        response = await handler(signed_request(key, body, signature="bG9sLXRhbXA="))
        assert response.status == 403
        # Even if an attacker guesses the store's deterministic format, no
        # token was ever issued to them.
        ws = _ScriptedServerSocket(_start_msg(), path="/?EasyCatStreamToken=forged.1.2")
        await h.media_handler(ws)
        assert ws.closed_with == (4003, "Missing or invalid stream token")

    config = default_config(
        telnyx_public_key=public_b64,
        unsafe_allow_unsigned_webhooks=False,
    )
    asyncio.run(harness.run(lambda t: None, config, _body))

    assert answered == []


def test_non_initiated_events_ack_without_answering(
    monkeypatch: pytest.MonkeyPatch, answered: list[tuple[str, dict[str, Any]]]
) -> None:
    harness = _ServerHarness(monkeypatch)
    key, public_b64 = _ed25519_pair()
    body = json.dumps(
        {
            "id": "evt-2",
            "event_type": "call.answered",
            "payload": {"call_control_id": "CC1"},
        }
    ).encode("utf-8")
    statuses: list[int] = []

    async def _body(h: _ServerHarness) -> None:
        handler = h.web.routes["/telnyx"]
        response = await handler(signed_request(key, body))
        statuses.append(response.status)

    config = default_config(
        telnyx_public_key=public_b64,
        unsafe_allow_unsigned_webhooks=False,
    )
    asyncio.run(harness.run(lambda t: None, config, _body))

    assert statuses == [200]
    assert answered == []


def test_invalid_envelope_returns_400(
    monkeypatch: pytest.MonkeyPatch, answered: list[tuple[str, dict[str, Any]]]
) -> None:
    harness = _ServerHarness(monkeypatch)
    key, public_b64 = _ed25519_pair()
    status: dict[str, int] = {}

    async def _body(h: _ServerHarness) -> None:
        handler = h.web.routes["/telnyx"]
        bad_json = await handler(signed_request(key, b"[1,2]"))
        no_ccid = await handler(
            signed_request(
                key,
                json.dumps(
                    {"id": "evt-3", "event_type": "call.initiated", "payload": {}}
                ).encode(),
            )
        )
        status["bad_json"] = bad_json.status
        status["no_ccid"] = no_ccid.status

    config = default_config(
        telnyx_public_key=public_b64,
        unsafe_allow_unsigned_webhooks=False,
    )
    asyncio.run(harness.run(lambda t: None, config, _body))

    assert status["bad_json"] == 400
    assert status["no_ccid"] == 400
    assert answered == []


def test_answer_command_failure_returns_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Call Control API error must surface as 502 so Telnyx retries, without
    crashing the webhook handler."""
    from easycat.telephony.telnyx_client import TelnyxApiError

    class _FailingClient:
        async def answer(self, call_control_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise TelnyxApiError(403, "forbidden")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        telnyx_client_module, "TelnyxCallControlClient", lambda _key: _FailingClient()
    )

    harness = _ServerHarness(monkeypatch)
    key, public_b64 = _ed25519_pair()
    result: dict[str, Any] = {}

    async def _body(h: _ServerHarness) -> None:
        handler = h.web.routes["/telnyx"]
        result["response"] = await handler(signed_request(key, call_initiated_body()))

    config = default_config(telnyx_public_key=public_b64, unsafe_allow_unsigned_webhooks=False)
    asyncio.run(harness.run(lambda t: None, config, _body))

    assert result["response"].status == 502


# ── Media lifecycle (scripted socket end-to-end) ──────────────────


class _ScriptedServerSocket(_ScriptedTelnyxWebSocket):
    """Adds the ``wait_closed`` seam the session runtime awaits."""

    async def wait_closed(self) -> None:
        self.entered.set()
        await self.release.wait()


class _FakeSession:
    def __init__(self, config: Any, events: list[str]) -> None:
        self.config = config
        self._events = events

    async def start(self) -> None:
        self._events.append("start")

    def subscribe_event(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def stop(self, *, force: bool = False) -> None:
        self._events.append("stop")


class _FakeManager:
    def __init__(self) -> None:
        self._sessions: dict[int, Any] = {}
        self.events: list[str] = []

    async def add(self, key: int, session: Any) -> None:
        self.events.append("register")
        self._sessions[key] = session
        await session.start()

    async def remove(self, key: int) -> None:
        self.events.append("unregister")
        session = self._sessions.pop(key, None)
        if session is not None:
            await session.stop()

    async def stop_all(self) -> None:
        self.events.append("stop_all")


def _stub_session_plumbing(
    monkeypatch: pytest.MonkeyPatch,
    manager: _FakeManager,
    created: list[Any],
    events: list[str],
) -> None:
    import easycat.config as config_mod
    import easycat.session_manager as sm_mod

    def _fake_create_session(config: Any) -> _FakeSession:
        events.append("create_session")
        created.append(config)
        return _FakeSession(config, events)

    monkeypatch.setattr(config_mod, "create_session", _fake_create_session)
    monkeypatch.setattr(sm_mod, "SessionManager", lambda: manager)


def test_media_listener_accepts_token_bound_socket_end_to_end(
    monkeypatch: pytest.MonkeyPatch, answered: list[tuple[str, dict[str, Any]]]
) -> None:
    """Full inbound flow: signed webhook → token mint → answer → scripted
    Telnyx socket presents the same token → one session built and drained."""
    events: list[str] = []
    created: list[Any] = []
    manager = _FakeManager()
    _stub_session_plumbing(monkeypatch, manager, created, events)

    harness = _ServerHarness(monkeypatch)
    key, public_b64 = _ed25519_pair()
    body = call_initiated_body("CC1")

    async def _body(h: _ServerHarness) -> None:
        handler = h.web.routes["/telnyx"]
        response = await handler(signed_request(key, body))
        assert response.status == 200

        _, payload = answered[0]
        token = parse_qs(urlsplit(payload["stream_url"]).query)["EasyCatStreamToken"][0]

        ws = _ScriptedServerSocket(
            _start_msg(call_control_id="CC1"),
            path=f"/?EasyCatStreamToken={token}",
        )
        connection = asyncio.create_task(h.media_handler(ws))
        await ws.entered.wait()
        ws.release.set()
        await connection

    config = default_config(telnyx_public_key=public_b64, unsafe_allow_unsigned_webhooks=False)
    asyncio.run(harness.run(lambda t: None, config, _body))

    combined = events + manager.events
    assert len(created) == 1
    assert events[0] == "create_session"
    assert "register" in manager.events
    assert "start" in combined
    assert "unregister" in manager.events
    assert "stop" in combined
    assert harness.media_server.closed is True


def test_stream_token_is_single_use_and_call_bound(
    monkeypatch: pytest.MonkeyPatch, answered: list[tuple[str, dict[str, Any]]]
) -> None:
    """The minted token authorizes exactly one start frame for exactly the
    answered call: replay and cross-call reuse are rejected pre-session."""
    events: list[str] = []
    created: list[Any] = []
    manager = _FakeManager()
    _stub_session_plumbing(monkeypatch, manager, created, events)

    harness = _ServerHarness(monkeypatch)
    key, public_b64 = _ed25519_pair()
    body = call_initiated_body("CC1")

    async def _body(h: _ServerHarness) -> None:
        handler = h.web.routes["/telnyx"]
        response = await handler(signed_request(key, body))
        assert response.status == 200

        _, payload = answered[0]
        token = parse_qs(urlsplit(payload["stream_url"]).query)["EasyCatStreamToken"][0]

        # Replay: same token again after consumption must never yield a session.
        first = _ScriptedServerSocket(
            _start_msg(call_control_id="CC1"),
            path=f"/?EasyCatStreamToken={token}",
        )
        connection = asyncio.create_task(h.media_handler(first))
        await first.entered.wait()
        first.release.set()
        await connection

        replay = _ScriptedServerSocket(
            _start_msg(call_control_id="CC1"),
            path=f"/?EasyCatStreamToken={token}",
        )
        await h.media_handler(replay)
        assert replay.closed_with == (4003, "Missing or invalid stream token")

        # Cross-call binding: a different call_control_id cannot ride CC1's token.
        other = _ScriptedServerSocket(
            _start_msg(call_control_id="CC2"),
            path=f"/?EasyCatStreamToken={token}",
        )
        await h.media_handler(other)
        assert other.closed_with == (4003, "Missing or invalid stream token")

    config = default_config(telnyx_public_key=public_b64, unsafe_allow_unsigned_webhooks=False)
    asyncio.run(harness.run(lambda t: None, config, _body))

    assert len(created) == 1
