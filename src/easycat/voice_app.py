"""VoiceApp — the product-level voice-bot app object.

``VoiceApp`` is the primary first-run surface: one noun for a voice product
that resolves an :class:`~easycat.config.EasyConfig` preset per deployment mode
and drives the matching runtime. It is a thin, typing-first orchestrator over
the existing building blocks (``EasyConfig`` presets, ``create_session``,
``run_session``, and the per-transport config-server helpers) — it adds no new
provider wiring of its own.

Construction accepts inputs three **mutually exclusive** ways:

#. High-level ``EasyConfig`` fields via ``**config_kwargs`` (governed by an
   allow-list); the chosen mode picks the matching preset.
#. A static ``config=EasyConfig.<preset>(...)`` — valid for ``local`` only,
   because a transport-bearing config cannot be safely cloned per connection.
#. A per-transport ``config_factory`` — the only safe per-connection path for
   ``browser`` / ``websocket`` / ``twilio``.

The module imports no heavy provider SDKs at import time; transport and session
construction is deferred to the per-mode methods.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from easycat.config import EasyConfig
    from easycat.session import Session
    from easycat.transports.webrtc import WebRTCTransport
    from easycat.transports.websocket import WebSocketConnectionTransport

VoiceMode = Literal["local", "browser", "websocket", "twilio"]

# Mode aliases resolve to their canonical name before any dispatch.
_MODE_ALIASES: dict[str, VoiceMode] = {
    "mic": "local",
    "ws": "websocket",
    "phone": "twilio",
}
_CANONICAL_MODES: frozenset[str] = frozenset({"local", "browser", "websocket", "twilio"})

# High-level ``EasyConfig`` fields ``VoiceApp`` forwards into the chosen preset.
# These are the ONLY keys forwarded into ``mic()`` / ``browser()`` / ``phone()``
# / ``EasyConfig(...)`` — ``EasyConfig`` and its presets have no host/port/auth
# fields, so forwarding a server-policy key would crash the preset constructor.
_FORWARDED_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "agent",
        "stt",
        "tts",
        "vad",
        "debug",
    }
)

# Server-policy fields ``VoiceApp`` accepts at construction for the server modes
# but NEVER forwards into an ``EasyConfig`` preset. They are read directly by the
# transport-config builders (``_browser_transport_config`` /
# ``_websocket_server_config``). ``EasyConfig`` has no such fields, so forwarding
# them into a preset would raise ``TypeError`` per connection.
_SERVER_POLICY_FIELDS: frozenset[str] = frozenset(
    {
        "host",
        "port",
        "serve_token",
        "max_sessions",
    }
)

# The full set of keys the constructor accepts via ``**config_kwargs``. ``dev``
# is owned by ``VoiceApp`` (controls the dev/debugger opt-in) and is NEVER
# accepted here. Any key outside this allow-list raises a ``ValueError`` so a
# typo or a misplaced field fails loudly.
_ALLOWED_CONFIG_FIELDS: frozenset[str] = _FORWARDED_CONFIG_FIELDS | _SERVER_POLICY_FIELDS

# Environment variable for the shared serve token (shipped name — do NOT rename
# to ``EASYCAT_SERVER_TOKEN`` while migrating ``serve`` through ``VoiceApp``).
_SERVE_TOKEN_ENV = "EASYCAT_SERVE_TOKEN"


def _normalize_mode(mode: str) -> VoiceMode:
    """Resolve an alias to its canonical mode name or raise ``ValueError``."""
    canonical = _MODE_ALIASES.get(mode, mode)
    if canonical not in _CANONICAL_MODES:
        valid = sorted(_CANONICAL_MODES | set(_MODE_ALIASES))
        raise ValueError(f"Unknown VoiceApp mode {mode!r}. Valid modes: {valid}.")
    return canonical  # type: ignore[return-value]


# High-level fields that can hold a *live* collaborator (a built provider or an
# agent bridge) rather than an immutable spec. Derived from the forwarded fields
# minus ``debug``, which is always a flag/level, never a stateful collaborator —
# so a new forwardable field is covered by the live-reuse guard automatically.
_LIVE_CAPABLE_FIELDS: frozenset[str] = _FORWARDED_CONFIG_FIELDS - frozenset({"debug"})


def _is_shareable_spec(field: str, value: Any) -> bool:
    """Return ``True`` when *value* for high-level *field* is safe to reuse
    across concurrent sessions.

    Per-connection modes build a fresh ``EasyConfig`` per connection but forward
    the same high-level field values into each one. That is only safe for
    *specs* — a provider-name string, a debug flag, a provider *config*
    dataclass, or a declarative framework agent spec — from which a fresh
    provider/bridge is built per session. A built provider or agent bridge
    instance (e.g. a ``RemoteResponsesAPIBridge`` or an already-constructed
    STT/TTS/VAD provider) carries per-session stream or conversation state, so
    reusing one object across connections would let concurrent sessions corrupt
    each other.
    """
    # Only ``None`` and a provider-name/URL ``str`` are universal scalar specs
    # for these fields. ``bool``/``int``/``float`` are never valid here, so they
    # must NOT short-circuit to "shareable" — letting them fall through to the
    # per-field checks below makes a stray ``agent=True`` / ``stt=1`` typo fail
    # loudly at construction (rejected as non-shareable) instead of being
    # forwarded into every per-connection ``EasyConfig`` to blow up later. (Note
    # ``bool`` is an ``int`` subclass, so it must not be treated as a spec.)
    if value is None or isinstance(value, str):
        return True
    if field == "agent":
        # Framework agent *specs* (OpenAI/PydanticAI/LangChain/LangGraph/Llama)
        # are rebuilt into a fresh bridge per session; built bridges/runners and
        # unrecognized objects are not, so they need a ``config_factory``. Import
        # from ``_factory`` directly (like the other internal callers) so this
        # guard does not eagerly pull in every bridge module via the package.
        from easycat.integrations.agents._factory import is_reusable_agent_spec

        return is_reusable_agent_spec(value)
    # Provider *config* dataclasses are specs; built provider instances are not.
    # Use the factory predicates so registered third-party extension configs
    # (not just the built-in ``STTConfig`` / ``TTSConfig`` unions) are accepted.
    if field == "stt":
        from easycat.stt.factory import is_stt_config

        return is_stt_config(value)
    if field == "tts":
        from easycat.tts.factory import is_tts_config

        return is_tts_config(value)
    if field == "vad":
        from easycat.vad import VADConfig

        return isinstance(value, VADConfig)
    return False


class VoiceApp:
    """A product-level voice bot that runs across local / browser / websocket / twilio.

    Examples
    --------
    >>> from easycat import VoiceApp
    >>> app = VoiceApp(agent=agent)  # doctest: +SKIP
    >>> app.run("browser")           # doctest: +SKIP

    Construction precedence (mutually exclusive — passing more than one raises
    ``ValueError`` naming the conflict):

    * high-level ``**config_kwargs`` (allow-listed; the mode picks the preset)
    * a static ``config`` (``local`` only)
    * a per-transport ``config_factory`` (the only safe per-connection path)

    ``dev`` is owned by ``VoiceApp`` and is never forwarded into a preset.
    """

    def __init__(
        self,
        agent: Any | None = None,
        *,
        config: EasyConfig | None = None,
        config_factory: Callable[[Any], EasyConfig] | None = None,
        dev: bool = False,
        **config_kwargs: Any,
    ) -> None:
        # ``agent`` is a high-level field; fold it into the kwargs bag so the
        # allow-list and mutual-exclusion rules treat it uniformly. ``agent`` is
        # a named parameter, so the language already rejects passing it both
        # positionally and by keyword (``TypeError``).
        if agent is not None:
            config_kwargs["agent"] = agent

        unknown = set(config_kwargs) - _ALLOWED_CONFIG_FIELDS
        if unknown:
            allowed = sorted(_ALLOWED_CONFIG_FIELDS)
            raise ValueError(
                f"Unknown VoiceApp field(s): {sorted(unknown)}. "
                f"Allowed high-level fields: {allowed} (and `dev`, which VoiceApp "
                "owns). For anything else, pass a full `config=` or `config_factory=`."
            )

        # ``config`` / ``config_factory`` / high-level fields are mutually
        # exclusive: exactly one input style per app.
        styles: list[str] = []
        if config is not None:
            styles.append("config")
        if config_factory is not None:
            styles.append("config_factory")
        if config_kwargs:
            # Name the offending high-level fields so the conflict is concrete.
            fields = ", ".join(sorted(config_kwargs))
            styles.append(f"high-level field(s) ({fields})")
        if len(styles) > 1:
            raise ValueError(
                "VoiceApp construction inputs are mutually exclusive; pass exactly "
                f"one of `config`, `config_factory`, or high-level fields. Got: "
                f"{', '.join(styles)}."
            )

        self._config = config
        self._config_factory = config_factory
        self._config_kwargs = config_kwargs
        self.dev = dev

    def _forwardable_config_kwargs(self) -> dict[str, Any]:
        """Return only the keys safe to forward into an ``EasyConfig`` preset.

        Server-policy fields (``host`` / ``port`` / ``serve_token`` /
        ``max_sessions``) live in ``_config_kwargs`` for the transport-config
        builders, but ``EasyConfig`` and its presets have no such fields —
        forwarding them would crash the preset constructor per connection.
        """
        return {
            key: value
            for key, value in self._config_kwargs.items()
            if key in _FORWARDED_CONFIG_FIELDS
        }

    # ── Public entry points ──────────────────────────────────────────

    def session(self, mode: VoiceMode | None = None, **kwargs: Any) -> Session:
        """Return one un-started, caller-owned :class:`Session`.

        Only valid for the single-session ``local`` mode. ``browser`` /
        ``websocket`` / ``twilio`` are multi-session modes with no single
        ``Session`` to hand back — call :meth:`serve` / :meth:`run` instead.
        The caller is responsible for starting and stopping the returned
        session (matching :func:`easycat.create_session`).
        """
        resolved = _normalize_mode(mode or "local")
        if resolved != "local":
            raise ValueError(
                f"session() is only available for the single-session 'local' mode, "
                f"not {resolved!r}; that mode is multi-session — use serve()/run()."
            )
        return self._build_local_session(**kwargs)

    async def serve(self, mode: VoiceMode | None = None, **kwargs: Any) -> None:
        """Async entry point — run the app for *mode* until shutdown.

        This is the composable async verb (it never calls ``asyncio.run``;
        :meth:`run` is the sole loop owner). Use it from inside an existing
        event loop or to compose a ``VoiceApp`` from a higher-level server.
        """
        resolved = _normalize_mode(mode or "browser")
        if resolved == "local":
            await self._serve_local(**kwargs)
        elif resolved == "browser":
            await self._serve_browser(**kwargs)
        elif resolved == "websocket":
            await self._serve_websocket(**kwargs)
        else:  # twilio
            await self._serve_twilio(**kwargs)

    def run(self, mode: VoiceMode | None = None, **kwargs: Any) -> None:
        """Synchronous entry point — the only method that owns the event loop.

        ``run()`` is the sole ``asyncio.run`` caller across ``VoiceApp`` (the
        per-mode ``run_*`` helpers it delegates to own their own loop). Defaults
        to the ``browser`` mode.
        """
        resolved = _normalize_mode(mode or "browser")
        if resolved == "local":
            self._run_local(**kwargs)
        elif resolved == "browser":
            self._run_browser(**kwargs)
        elif resolved == "websocket":
            self._run_websocket(**kwargs)
        else:  # twilio
            self._run_twilio(**kwargs)

    # ── Local mode ───────────────────────────────────────────────────

    def _build_local_session(self, **kwargs: Any) -> Session:
        """Build an un-started local :class:`Session` from the app config."""
        from easycat.config import create_session

        config = self._local_config(**kwargs)
        return create_session(config)

    def _local_config(self, **kwargs: Any) -> EasyConfig:
        """Resolve the local-mode :class:`EasyConfig` per construction style."""
        from easycat.config import EasyConfig

        if self._config is not None:
            # A static, transport-bearing config is only safe here (local is
            # single-session, so there is nothing to clone per connection).
            return self._config
        if self._config_factory is not None:
            from easycat.transports import LocalTransport

            return self._config_factory(LocalTransport())
        return EasyConfig.mic(**{**self._forwardable_config_kwargs(), **kwargs})

    def _run_local(self, **kwargs: Any) -> None:
        from easycat.helpers import run_session

        session = self._build_local_session(**kwargs)
        run_session(session)

    async def _serve_local(self, **kwargs: Any) -> None:
        from easycat.helpers import wait_for_shutdown_signal

        session = self._build_local_session(**kwargs)
        # ``wait_for_shutdown_signal`` calls ``session.stop()`` once the signal
        # fires (and on its KeyboardInterrupt fallback), so it owns teardown on
        # those paths. It does NOT, however, stop the session when *this*
        # coroutine is cancelled from an outer event loop (its signal-handler
        # path awaits the stop event without a ``finally``), which would leave
        # the microphone/provider tasks running. Guard with ``finally`` so a
        # cancelled ``serve('local')`` still tears the session down. ``stop()``
        # is idempotent — on the normal signal path the session is already
        # closed, so this second call is a no-op.
        await session.start()
        try:
            await wait_for_shutdown_signal(session)
        finally:
            await session.stop(force=True)

    # ── Browser mode (WebRTC) ────────────────────────────────────────

    def _browser_transport_config(self, **kwargs: Any) -> tuple[Any, bool]:
        """Build the :class:`WebRTCTransportConfig` plus the resolved
        ``unsafe_allow_no_auth`` flag.

        The flag is returned (not just consumed by the token pre-check) so the
        run/serve methods can forward it to
        :func:`~easycat.transports.webrtc.serve_webrtc_config_sessions`, whose
        own non-loopback guard would otherwise re-reject an intentionally
        unauthenticated bind.
        """
        from easycat.transports.webrtc import WebRTCTransportConfig

        host = kwargs.pop("host", self._config_kwargs.get("host", "127.0.0.1"))
        port = kwargs.pop("port", self._config_kwargs.get("port", 8080))
        max_sessions = kwargs.pop("max_sessions", self._config_kwargs.get("max_sessions"))
        unsafe_allow_no_auth = kwargs.pop("unsafe_allow_no_auth", False)
        token = self._resolve_serve_token(
            kwargs.pop("serve_token", self._config_kwargs.get("serve_token")),
            host=host,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
        )
        # Only override the WebRTCTransportConfig default when a limit is given,
        # keeping that dataclass the single source of the default capacity.
        capacity = {} if max_sessions is None else {"max_sessions": max_sessions}
        config = WebRTCTransportConfig(host=host, port=port, auth_token=token, **capacity)
        return config, unsafe_allow_no_auth

    def _browser_factory(self) -> Callable[[WebRTCTransport], EasyConfig]:
        return self._per_connection_factory("browser")

    def _run_browser(self, *, announce: bool = True, **kwargs: Any) -> None:
        from easycat.transports.webrtc import run_webrtc_config_server

        transport_config, unsafe_allow_no_auth = self._browser_transport_config(**kwargs)
        # ``run_webrtc_config_server`` blocks until shutdown, so the URL must be
        # announced first. Pass ``announce=False`` to suppress the helper's own
        # "Server ready..." line and avoid a duplicate. Callers that already
        # printed the URL (e.g. ``easycat serve``) pass ``announce=False`` here.
        if announce:
            self._announce_browser_url(transport_config)
        run_webrtc_config_server(
            self._browser_factory(),
            transport_config,
            announce=False,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
        )

    async def _serve_browser(self, **kwargs: Any) -> None:
        from easycat.transports.webrtc import serve_webrtc_config_sessions

        transport_config, unsafe_allow_no_auth = self._browser_transport_config(**kwargs)
        await serve_webrtc_config_sessions(
            self._browser_factory(),
            transport_config,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
        )

    def _announce_browser_url(self, transport_config: Any) -> None:
        from urllib.parse import urlencode

        from easycat.cli._output import stdout_console

        host = transport_config.host
        port = transport_config.port
        display_host = "localhost" if host in {"127.0.0.1", "localhost", "::1"} else host
        url = f"http://{display_host}:{port}"
        if transport_config.auth_token:
            # URL-encode the token so query-special characters (``+``, ``&``,
            # ``#``, spaces) survive into the bundled client's ``?token=`` read,
            # matching the CLI ``serve`` path (``cli/serve.py``).
            query = urlencode({"token": transport_config.auth_token})
            url = f"{url}/webrtc_client.html?{query}"
        stdout_console.print(f"Open {url}")

    # ── WebSocket mode ───────────────────────────────────────────────

    def _websocket_server_config(self, **kwargs: Any) -> tuple[Any, bool]:
        """Build the server config plus the resolved ``unsafe_allow_no_auth`` flag.

        The flag is returned (not just consumed by the pre-check) so the
        run/serve methods can forward it to the shared websocket serve helper,
        whose own non-loopback guard would otherwise re-reject an intentionally
        unauthenticated bind.
        """
        from easycat.transports.websocket import WebSocketSessionServerConfig

        host = kwargs.pop("host", self._config_kwargs.get("host", "127.0.0.1"))
        port = kwargs.pop("port", self._config_kwargs.get("port", 8765))
        max_sessions = kwargs.pop("max_sessions", self._config_kwargs.get("max_sessions", 10))
        unsafe_allow_no_auth = kwargs.pop("unsafe_allow_no_auth", False)
        token = self._resolve_serve_token(
            kwargs.pop("serve_token", self._config_kwargs.get("serve_token")),
            host=host,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
        )
        server_config = WebSocketSessionServerConfig(
            host=host, port=port, auth_token=token, max_sessions=max_sessions
        )
        return server_config, unsafe_allow_no_auth

    def _websocket_factory(self) -> Callable[[WebSocketConnectionTransport], EasyConfig]:
        return self._per_connection_factory("websocket")

    def _run_websocket(self, **kwargs: Any) -> None:
        from easycat.transports.websocket import run_websocket_config_server

        server_config, unsafe_allow_no_auth = self._websocket_server_config(**kwargs)
        run_websocket_config_server(
            self._websocket_factory(),
            server_config,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
        )

    async def _serve_websocket(self, **kwargs: Any) -> None:
        from easycat.transports.websocket import serve_websocket_config_sessions

        server_config, unsafe_allow_no_auth = self._websocket_server_config(**kwargs)
        await serve_websocket_config_sessions(
            self._websocket_factory(),
            server_config,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
        )

    # ── Twilio mode ──────────────────────────────────────────────────

    def _twilio_server_config(self, **kwargs: Any) -> Any:
        """Build the :class:`TwilioVoiceServerConfig` for the twilio listeners.

        Twilio-listener fields (``host`` / ``media_port`` / ``http_host`` /
        ``http_port`` / ``stream_url`` / ``stream_token_secret`` /
        ``twilio_auth_token`` / ``trust_proxy_headers`` /
        ``unsafe_allow_unsigned_webhooks``) come from
        ``run('twilio', ...)`` / ``serve('twilio', ...)`` ``**kwargs``. They are
        twilio-specific (two listeners, each with its own host/port pair), so
        they are NOT taken from the generic constructor ``host`` / ``port``.
        ``max_sessions`` is the one mode-neutral server-policy field, so it
        mirrors the browser/websocket builders: a ``run``/``serve`` value wins,
        otherwise a ``max_sessions=`` given at construction, otherwise the
        ``TwilioVoiceServerConfig`` default. ``stream_url`` /
        ``stream_token_secret`` / ``twilio_auth_token`` fall back to
        ``TWILIO_STREAM_URL`` / ``TWILIO_STREAM_TOKEN_SECRET`` /
        ``TWILIO_AUTH_TOKEN`` as a convenience.
        ``twilio_auth_token`` is the Twilio *account* auth token (validating the
        ``X-Twilio-Signature`` webhook header) — distinct from the
        browser/websocket ``serve_token`` that gates the signaling bind.
        ``stream_url`` and ``twilio_auth_token`` are both required (the helper
        raises a clear ``ValueError`` when ``stream_url`` is missing, or when
        ``twilio_auth_token`` is missing without
        ``unsafe_allow_unsigned_webhooks``).
        """
        from easycat.telephony.server import TwilioVoiceServerConfig

        host = kwargs.pop("host", "0.0.0.0")
        media_port = kwargs.pop("media_port", 8766)
        http_host = kwargs.pop("http_host", "0.0.0.0")
        http_port = kwargs.pop("http_port", 8000)
        stream_url = kwargs.pop("stream_url", None) or os.environ.get("TWILIO_STREAM_URL")
        stream_token_secret = kwargs.pop("stream_token_secret", None) or os.environ.get(
            "TWILIO_STREAM_TOKEN_SECRET"
        )
        twilio_auth_token = kwargs.pop("twilio_auth_token", None) or os.environ.get(
            "TWILIO_AUTH_TOKEN"
        )
        trust_proxy_headers = kwargs.pop("trust_proxy_headers", False)
        unsafe_allow_unsigned_webhooks = kwargs.pop("unsafe_allow_unsigned_webhooks", False)
        max_sessions = kwargs.pop(
            "max_sessions",
            self._config_kwargs.get("max_sessions", TwilioVoiceServerConfig.max_sessions),
        )
        return TwilioVoiceServerConfig(
            host=host,
            media_port=media_port,
            http_host=http_host,
            http_port=http_port,
            stream_url=stream_url,
            stream_token_secret=stream_token_secret,
            twilio_auth_token=twilio_auth_token,
            trust_proxy_headers=trust_proxy_headers,
            unsafe_allow_unsigned_webhooks=unsafe_allow_unsigned_webhooks,
            max_sessions=max_sessions,
        )

    def _twilio_factory(self) -> Callable[[Any], EasyConfig]:
        return self._per_connection_factory("twilio")

    def _run_twilio(self, **kwargs: Any) -> None:
        from easycat.telephony.server import run_twilio_voice_app

        factory = self._twilio_factory()
        server_config = self._twilio_server_config(**kwargs)
        run_twilio_voice_app(factory, server_config)

    async def _serve_twilio(self, **kwargs: Any) -> None:
        from easycat.telephony.server import serve_twilio_voice_app

        factory = self._twilio_factory()
        server_config = self._twilio_server_config(**kwargs)
        await serve_twilio_voice_app(factory, server_config)

    # ── Shared helpers ───────────────────────────────────────────────

    def _per_connection_factory(self, mode: VoiceMode) -> Callable[[Any], EasyConfig]:
        """Build the per-transport ``config_factory`` for a per-connection mode.

        Per-connection modes reject a static ``config`` (it cannot be safely
        cloned per connection — there is no clone helper and
        ``dataclasses.replace`` shares grouped sub-configs by reference) and
        REQUIRE ``config_factory``. When the app was constructed with high-level
        fields, synthesize a factory that builds a *fresh* preset per transport.
        """
        if self._config is not None:
            raise ValueError(
                f"VoiceApp {mode!r} mode is per-connection and cannot reuse a static "
                "`config` (it would share grouped sub-configs across concurrent "
                "sessions). Pass a `config_factory` instead, or construct with "
                "high-level fields."
            )
        if self._config_factory is not None:
            return self._config_factory

        from easycat.config import EasyConfig

        # Only EasyConfig-forwardable fields reach the preset; server-policy
        # fields stay with the transport-config builders.
        forwarded = self._forwardable_config_kwargs()

        # A per-connection mode forwards the same high-level values into a fresh
        # ``EasyConfig`` per connection. Reusing a *live* collaborator (a built
        # provider or agent bridge) that way shares one stateful object across
        # concurrent sessions — the same hazard that makes a static ``config``
        # unsafe above. Reject it with the same remedy: pass a ``config_factory``
        # that builds fresh collaborators per connection.
        live_fields = sorted(
            field
            for field in _LIVE_CAPABLE_FIELDS
            if field in forwarded and not _is_shareable_spec(field, forwarded[field])
        )
        if live_fields:
            raise ValueError(
                f"VoiceApp {mode!r} mode is per-connection and cannot reuse live "
                f"high-level field(s) {live_fields} across connections: a built "
                "provider or agent bridge carries per-session state, so concurrent "
                "sessions would corrupt each other. Pass a provider-name string or a "
                "provider-config instead, or a `config_factory` that builds fresh "
                "collaborators per connection."
            )

        if mode == "browser":

            def _browser_config(transport: Any) -> EasyConfig:
                return EasyConfig.browser(transport=transport, **forwarded)

            return _browser_config

        if mode == "twilio":

            def _phone_config(transport: Any) -> EasyConfig:
                return EasyConfig.phone(transport=transport, **forwarded)

            return _phone_config

        # WebSocket has no dedicated preset; build EasyConfig with the
        # per-connection transport directly.
        def _ws_config(transport: Any) -> EasyConfig:
            return EasyConfig(transport=transport, **forwarded)

        return _ws_config

    def _resolve_serve_token(
        self,
        token: str | None,
        *,
        host: str,
        unsafe_allow_no_auth: bool,
    ) -> str | None:
        """Enforce the non-loopback token guard for server modes.

        A non-loopback bind requires a token for BOTH the WebRTC and WebSocket
        paths. The token falls back to ``EASYCAT_SERVE_TOKEN``. The only
        structured escape hatch is ``unsafe_allow_no_auth=True``.

        Blank or whitespace-only tokens are normalized to ``None`` (via the
        shared :func:`~easycat.transports.websocket._normalize_auth_token`) so a
        ``"   "`` value cannot satisfy this guard as truthy while the downstream
        WebSocket authorizer treats it as no token at all — keeping the bind
        guard and request authorization in sync across both transports.
        """
        from easycat.transports.webrtc import _is_loopback_host
        from easycat.transports.websocket import _normalize_auth_token

        resolved = _normalize_auth_token(token or os.environ.get(_SERVE_TOKEN_ENV))
        if resolved is None and not _is_loopback_host(host) and not unsafe_allow_no_auth:
            raise ValueError(
                f"Refusing to bind {host!r} without a token. Pass serve_token= "
                f"(or set {_SERVE_TOKEN_ENV}) when serving beyond loopback, or pass "
                "unsafe_allow_no_auth=True to bind an unauthenticated endpoint."
            )
        return resolved
