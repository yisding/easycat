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
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, overload

from easycat._net import is_loopback_host, normalize_auth_token

if TYPE_CHECKING:
    from easycat.config import EasyConfig
    from easycat.session import Session
    from easycat.transports.webrtc import WebRTCTransport
    from easycat.transports.websocket import WebSocketConnectionTransport

VoiceMode = Literal["local", "browser", "websocket", "twilio"]
VoiceModeInput = Literal["local", "browser", "websocket", "twilio", "mic", "ws", "phone"]

_LocalMode = Literal["local", "mic"]
_BrowserMode = Literal["browser"]
_WebSocketMode = Literal["websocket", "ws"]
_TwilioMode = Literal["twilio", "phone"]


class _VoiceConfigKwargs(TypedDict, total=False):
    """Beginner-facing config fields shared by construction and local mode.

    Provider and agent inputs intentionally remain open-ended: EasyCat accepts
    registered third-party config objects and several optional agent-framework
    specifications that cannot be expressed as one closed static union. The
    finite field names and scalar policy values still get precise checking.
    """

    stt: Any
    tts: Any
    vad: Any
    debug: Literal["off", "light", "full"]


class _VoiceAppInitKwargs(_VoiceConfigKwargs, total=False):
    host: str
    port: int
    serve_token: str | None
    max_sessions: int


class _LocalModeKwargs(_VoiceConfigKwargs, total=False):
    agent: Any


class _ServerModeKwargs(TypedDict, total=False):
    host: str
    port: int
    serve_token: str | None
    max_sessions: int
    unsafe_allow_no_auth: bool


class _BrowserModeKwargs(_ServerModeKwargs, total=False):
    announce: bool


class _WebSocketModeKwargs(_ServerModeKwargs):
    pass


class _TwilioModeKwargs(TypedDict, total=False):
    host: str
    media_port: int
    http_host: str
    http_port: int
    stream_url: str | None
    stream_token_secret: str | None
    twilio_auth_token: str | None
    trust_proxy_headers: bool | None
    unsafe_allow_unsigned_webhooks: bool
    max_sessions: int
    start_timeout_s: float
    public_twiml_url: str | None
    drain_timeout_s: float
    force_shutdown_timeout_s: float


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
        # Declarative framework agent *specs* (OpenAI/PydanticAI/LangChain/
        # LangGraph/Llama) are rebuilt into a fresh bridge per connection, and
        # that bridge — not the wrapped spec — owns the mutable per-session
        # state, so the same spec is safe to forward into every connection (this
        # keeps the documented ``VoiceApp(agent=Agent(...)).run("browser")``
        # quickstart working). Built bridges/runners, conversation-pinned
        # runnables, and unrecognized objects carry per-session state by
        # reference and are rejected, so they need a ``config_factory``. Import
        # from ``_factory`` directly (like the other internal callers) so this
        # guard does not eagerly pull in every bridge module via the package.
        from easycat.integrations.agents._factory import is_reusable_agent_spec

        return is_reusable_agent_spec(value)
    # Provider *config* dataclasses are specs; built provider instances are not.
    # Use the factory predicates so registered third-party extension configs
    # (not just the built-in ``STTConfig`` / ``TTSConfig`` unions) are accepted.
    if field == "stt":
        from easycat.stt.factory import STTProviderConfig, is_stt_config

        return isinstance(value, STTProviderConfig) or is_stt_config(value)
    if field == "tts":
        from easycat.tts.factory import TTSProviderConfig, is_tts_config

        return isinstance(value, TTSProviderConfig) or is_tts_config(value)
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

    @overload
    def __init__(
        self,
        agent: Any | None = None,
        *,
        config: None = None,
        config_factory: None = None,
        dev: bool = False,
        **config_kwargs: Unpack[_VoiceAppInitKwargs],
    ) -> None: ...

    @overload
    def __init__(
        self,
        agent: None = None,
        *,
        config: EasyConfig,
        config_factory: None = None,
        dev: bool = False,
    ) -> None: ...

    @overload
    def __init__(
        self,
        agent: None = None,
        *,
        config: None = None,
        config_factory: Callable[[Any], EasyConfig],
        dev: bool = False,
    ) -> None: ...

    def __init__(
        self,
        agent: Any | None = None,
        *,
        config: EasyConfig | None = None,
        config_factory: Callable[[Any], EasyConfig] | None = None,
        dev: bool = False,
        **config_kwargs: object,
    ) -> None:
        resolved_config_kwargs: dict[str, Any] = dict(config_kwargs)
        # ``agent`` is a high-level field; fold it into the kwargs bag so the
        # allow-list and mutual-exclusion rules treat it uniformly. ``agent`` is
        # a named parameter, so the language already rejects passing it both
        # positionally and by keyword (``TypeError``).
        if agent is not None:
            resolved_config_kwargs["agent"] = agent

        unknown = set(resolved_config_kwargs) - _ALLOWED_CONFIG_FIELDS
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
        if resolved_config_kwargs:
            # Name the offending high-level fields so the conflict is concrete.
            fields = ", ".join(sorted(resolved_config_kwargs))
            styles.append(f"high-level field(s) ({fields})")
        if len(styles) > 1:
            raise ValueError(
                "VoiceApp construction inputs are mutually exclusive; pass exactly "
                f"one of `config`, `config_factory`, or high-level fields. Got: "
                f"{', '.join(styles)}."
            )

        self._config = config
        self._config_factory = config_factory
        self._config_kwargs = resolved_config_kwargs
        self.dev = dev

    def _forwardable_config_kwargs(self) -> dict[str, Any]:
        """Return only the keys safe to forward into an ``EasyConfig`` preset.

        Server-policy fields (``host`` / ``port`` / ``serve_token`` /
        ``max_sessions``) live in ``_config_kwargs`` for the transport-config
        builders, but ``EasyConfig`` and its presets have no such fields —
        forwarding them would crash the preset constructor per connection.

        Dev mode (``VoiceApp(dev=True)``) defaults the session to durable
        debugging (``debug="full"``) when no explicit ``debug`` was supplied, so
        the dev timeline has a journal to read. Durable journaling and UI
        autolaunch stay separate concepts: this only enables the journal; the
        UI launch is the additive dev opt-in in :meth:`_arm_dev_debugger`.
        """
        forwarded = {
            key: value
            for key, value in self._config_kwargs.items()
            if key in _FORWARDED_CONFIG_FIELDS
        }
        if self.dev and "debug" not in forwarded:
            forwarded["debug"] = "full"
        return forwarded

    # ── Public entry points ──────────────────────────────────────────

    def session(
        self,
        mode: _LocalMode | None = None,
        **kwargs: Unpack[_LocalModeKwargs],
    ) -> Session:
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

    def resolve_config(
        self,
        mode: VoiceModeInput,
        *,
        transport: Any | None = None,
    ) -> EasyConfig:
        """Resolve one descriptor-only config without starting a session/server.

        This is the preflight/inspection peer of :meth:`run`: provider
        shortcuts, credentials, VAD, echo-cancellation, and transport defaults
        are resolved and validated, but no provider client, audio device,
        listener, or session is created. For a custom ``config_factory``, pass
        the concrete transport the factory should inspect; EasyCat never calls
        an application factory with a fabricated transport.
        """
        from easycat.config import EasyConfig

        resolved = _normalize_mode(mode)
        if self._config_factory is not None:
            if transport is None:
                raise ValueError(
                    "resolve_config() cannot inspect a config_factory without a "
                    "concrete transport; pass transport=... explicitly."
                )
            return self._config_factory(transport)
        if resolved == "local":
            if transport is not None:
                return EasyConfig.mic(
                    transport=transport,
                    **self._forwardable_config_kwargs(),
                )
            return self._local_config()

        # Enforce the same static-config/live-collaborator safety rules as the
        # real per-connection server before presenting a preview as runnable.
        factory = self._per_connection_factory(resolved)
        if transport is not None:
            return factory(transport)

        forwarded = self._forwardable_config_kwargs()
        if resolved == "browser":
            transport_config, _unsafe = self._browser_transport_config()
            return EasyConfig.browser(transport=transport_config, **forwarded)
        if resolved == "websocket":
            from easycat.transports.websocket import WebSocketTransportConfig

            return EasyConfig(transport=WebSocketTransportConfig(), **forwarded)
        return EasyConfig.phone(**forwarded)

    @overload
    async def serve(
        self,
        mode: _LocalMode,
        **kwargs: Unpack[_LocalModeKwargs],
    ) -> None: ...

    @overload
    async def serve(
        self,
        mode: _BrowserMode,
        **kwargs: Unpack[_BrowserModeKwargs],
    ) -> None: ...

    @overload
    async def serve(
        self,
        mode: _WebSocketMode,
        **kwargs: Unpack[_WebSocketModeKwargs],
    ) -> None: ...

    @overload
    async def serve(
        self,
        mode: _TwilioMode,
        **kwargs: Unpack[_TwilioModeKwargs],
    ) -> None: ...

    async def serve(self, mode: VoiceModeInput, **kwargs: object) -> None:
        """Async entry point — run the app for *mode* until shutdown.

        This is the composable async verb (it never calls ``asyncio.run``;
        :meth:`run` is the sole loop owner). Use it from inside an existing
        event loop or to compose a ``VoiceApp`` from a higher-level server.
        """
        mode_kwargs: dict[str, Any] = dict(kwargs)
        resolved = _normalize_mode(mode)
        if resolved == "local":
            await self._serve_local(**mode_kwargs)
            return
        # Per-connection server modes build sessions downstream; launch the dev
        # registry UI once up front so the selector is ready as they register.
        self._arm_dev_registry()
        if resolved == "browser":
            await self._serve_browser(**mode_kwargs)
        elif resolved == "websocket":
            await self._serve_websocket(**mode_kwargs)
        else:  # twilio
            await self._serve_twilio(**mode_kwargs)

    @overload
    def run(
        self,
        mode: _LocalMode,
        **kwargs: Unpack[_LocalModeKwargs],
    ) -> None: ...

    @overload
    def run(
        self,
        mode: _BrowserMode,
        **kwargs: Unpack[_BrowserModeKwargs],
    ) -> None: ...

    @overload
    def run(
        self,
        mode: _WebSocketMode,
        **kwargs: Unpack[_WebSocketModeKwargs],
    ) -> None: ...

    @overload
    def run(
        self,
        mode: _TwilioMode,
        **kwargs: Unpack[_TwilioModeKwargs],
    ) -> None: ...

    def run(self, mode: VoiceModeInput, **kwargs: object) -> None:
        """Synchronous entry point — the only method that owns the event loop.

        ``run()`` is the sole ``asyncio.run`` caller across ``VoiceApp`` (the
        per-mode ``run_*`` helpers it delegates to own their own loop). The mode
        is required so starting a local device, network listener, or phone
        integration is always an explicit choice.
        """
        mode_kwargs: dict[str, Any] = dict(kwargs)
        resolved = _normalize_mode(mode)
        if resolved == "local":
            self._run_local(**mode_kwargs)
            return
        # Per-connection server modes build sessions downstream; launch the dev
        # registry UI once up front so the selector is ready as they register.
        self._arm_dev_registry()
        if resolved == "browser":
            self._run_browser(**mode_kwargs)
        elif resolved == "websocket":
            self._run_websocket(**mode_kwargs)
        else:  # twilio
            self._run_twilio(**mode_kwargs)

    # ── Local mode ───────────────────────────────────────────────────

    def _build_local_session(self, **kwargs: Any) -> Session:
        """Build an un-started local :class:`Session` from the app config."""
        from easycat.config import create_session

        config = self._local_config(**kwargs)
        session = create_session(config)
        self._arm_dev_debugger(session)
        return session

    def _arm_dev_debugger(self, session: Session) -> None:
        """Register *session* and launch the dev debugger UI once (dev mode only).

        Purely additive over the ``_autolaunch.py`` guard (R7): this is a
        SEPARATE trigger keyed on ``VoiceApp(dev=True)`` / ``EASYCAT_DEV`` and
        never relaxes the ``debug='full'``-alone-never-autolaunches guarantee.
        No-ops when dev mode is not opted in.
        """
        from easycat.debugger.dev import maybe_launch_dev_debugger

        maybe_launch_dev_debugger(session, dev=self.dev)

    def _arm_dev_registry(self) -> None:
        """Launch the dev registry UI once for a per-connection server mode.

        Additive over the ``_autolaunch.py`` guard (R7): a no-op unless dev mode
        is opted in via ``VoiceApp(dev=True)`` / ``EASYCAT_DEV``.
        """
        from easycat.debugger.dev import maybe_launch_dev_registry_ui

        maybe_launch_dev_registry_ui(dev=self.dev)

    def _local_config(self, **kwargs: Any) -> EasyConfig:
        """Resolve the local-mode :class:`EasyConfig` per construction style."""
        from easycat.config import EasyConfig

        if self._config is not None:
            # A static, transport-bearing config is only safe here (local is
            # single-session, so there is nothing to clone per connection). The
            # config is used verbatim, so any per-call kwargs would be silently
            # dropped — fail loud on a typo instead, matching the server modes'
            # ``_reject_unknown_mode_kwargs`` guard and the high-level branch
            # below (which raises through ``EasyConfig.mic``).
            self._reject_unknown_mode_kwargs("local", kwargs)
            return self._config
        if self._config_factory is not None:
            from easycat.transports import LocalTransport

            # Same as the static-config branch: the factory owns the config, so
            # stray run/serve/session kwargs are typos, not overrides.
            self._reject_unknown_mode_kwargs("local", kwargs)
            return self._config_factory(LocalTransport())
        return EasyConfig.mic(**{**self._forwardable_config_kwargs(), **kwargs})

    def _run_local(self, **kwargs: Any) -> None:
        from easycat.helpers import run_session

        session = self._build_local_session(**kwargs)
        run_session(session)

    async def _serve_local(self, **kwargs: Any) -> None:
        """Run a local-mic session until shutdown on the composable async path.

        Unlike :meth:`_run_local` (which wires console runtime feedback via
        :func:`easycat.helpers.run_session`), the async ``serve('local')`` path
        starts the session directly and prints nothing: ``serve`` is the verb
        for composing a ``VoiceApp`` inside a larger event loop, so any
        user-facing status output is the caller's responsibility.
        """
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

    @staticmethod
    def _reject_unknown_mode_kwargs(mode: str, kwargs: dict[str, Any]) -> None:
        """Fail loud on a misspelled ``run()``/``serve()`` server field.

        Each mode builder ``pop``s every field it accepts; anything left in
        ``kwargs`` is a typo (e.g. ``serve_token`` → ``serve_tokn``) that would
        otherwise be silently dropped — re-binding the default port or, worse,
        running an unauthenticated server. This mirrors the construction-time
        allow-list guard so the run/serve entry points are fail-loud too.
        """
        if kwargs:
            raise ValueError(
                f"Unknown keyword argument(s) for {mode!r} mode: {sorted(kwargs)}. "
                "Check for a typo; run()/serve() forward only that mode's documented "
                "server fields."
            )

    def _browser_transport_config(self, **kwargs: Any) -> tuple[Any, bool]:
        """Build the :class:`WebRTCTransportConfig` plus the resolved
        ``unsafe_allow_no_auth`` flag.

        The flag is returned (not just consumed by the token pre-check) so the
        run/serve methods can forward it to
        :func:`~easycat.server.webrtc_routes.serve_webrtc_config_sessions`, whose
        own non-loopback guard would otherwise re-reject an intentionally
        unauthenticated bind.
        """
        from easycat.transports._webrtc_config import WebRTCTransportConfig

        host = kwargs.pop("host", self._config_kwargs.get("host", "127.0.0.1"))
        port = kwargs.pop("port", self._config_kwargs.get("port", 8080))
        max_sessions = kwargs.pop("max_sessions", self._config_kwargs.get("max_sessions"))
        unsafe_allow_no_auth = kwargs.pop("unsafe_allow_no_auth", False)
        token = self._resolve_serve_token(
            kwargs.pop("serve_token", self._config_kwargs.get("serve_token")),
            host=host,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
        )
        self._reject_unknown_mode_kwargs("browser", kwargs)
        # Only override the WebRTCTransportConfig default when a limit is given,
        # keeping that dataclass the single source of the default capacity.
        capacity = {} if max_sessions is None else {"max_sessions": max_sessions}
        config = WebRTCTransportConfig(host=host, port=port, auth_token=token, **capacity)
        return config, unsafe_allow_no_auth

    def _browser_factory(self) -> Callable[[WebRTCTransport], EasyConfig]:
        return self._per_connection_factory("browser")

    def _run_browser(self, *, announce: bool = True, **kwargs: Any) -> None:
        from easycat.server.webrtc_routes import run_webrtc_config_server

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

    async def _serve_browser(self, *, announce: bool = True, **kwargs: Any) -> None:
        from easycat.server.webrtc_routes import serve_webrtc_config_sessions

        transport_config, unsafe_allow_no_auth = self._browser_transport_config(**kwargs)
        # Mirror ``_run_browser``: announce the URL ourselves and suppress the
        # helper's plainer "Server ready..." line so the same ``announce`` knob
        # behaves identically across run() and serve(). Delegating to the helper
        # would print only the bare origin, so following that hint on a
        # token-protected serve opens the bundled page unauthenticated (→ 401).
        # ``_announce_browser_url`` keeps the token value out of logs.
        if announce:
            self._announce_browser_url(transport_config)
        await serve_webrtc_config_sessions(
            self._browser_factory(),
            transport_config,
            announce=False,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
        )

    def _announce_browser_url(self, transport_config: Any) -> None:
        from easycat.cli._output import stdout_console

        host = transport_config.host
        port = transport_config.port
        display_host = "localhost" if host in {"127.0.0.1", "localhost", "::1"} else host
        base_url = f"http://{display_host}:{port}"
        if not transport_config.auth_token:
            stdout_console.print(f"Open {base_url}")
            return
        # A serve token is configured (and required for non-loopback binds), but
        # it must never be written to logs. Print a ready-to-edit URL with a
        # placeholder so the operator pastes their own token in its place; the
        # bundled client reads the bearer token from the ``#token=`` fragment,
        # which is not sent to the server, and forwards it in request headers.
        stdout_console.print(f"Open {base_url}/webrtc_client.html#token=<your serve token>")
        stdout_console.print(
            "Replace <your serve token> with the serve token you configured "
            "(URL-encode it first; the page reads it from the #token= fragment)."
        )

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
        self._reject_unknown_mode_kwargs("websocket", kwargs)
        server_config = WebSocketSessionServerConfig(
            host=host, port=port, auth_token=token, max_sessions=max_sessions
        )
        return server_config, unsafe_allow_no_auth

    def _websocket_factory(
        self,
    ) -> Callable[[WebSocketConnectionTransport], EasyConfig]:
        return self._per_connection_factory("websocket")

    def _run_websocket(self, **kwargs: Any) -> None:
        from easycat.server.websocket import run_websocket_config_server

        server_config, unsafe_allow_no_auth = self._websocket_server_config(**kwargs)
        run_websocket_config_server(
            self._websocket_factory(),
            server_config,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
        )

    async def _serve_websocket(self, **kwargs: Any) -> None:
        from easycat.server.websocket import serve_websocket_config_sessions

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
        # The Twilio auth token authenticates the ``POST /twiml`` webhook
        # (X-Twilio-Signature) before a media-stream token is minted. Source it
        # from TWILIO_AUTH_TOKEN — the same env var the twilio-phone scaffold
        # requires — so the secure path is automatic when the operator sets it.
        twilio_auth_token = kwargs.pop("twilio_auth_token", None) or os.environ.get(
            "TWILIO_AUTH_TOKEN"
        )
        # ``trust_proxy_headers`` honors X-Forwarded-Proto/Host when reconstructing
        # the signed public URL behind a TLS-terminating proxy. An explicit kwarg
        # wins; otherwise fall back to the TRUST_PROXY_HEADERS env var.
        trust_proxy_headers = kwargs.pop("trust_proxy_headers", None)
        if trust_proxy_headers is None:
            trust_proxy_headers = os.environ.get("TRUST_PROXY_HEADERS", "").lower() in {
                "1",
                "true",
                "yes",
            }
        unsafe_allow_unsigned_webhooks = kwargs.pop("unsafe_allow_unsigned_webhooks", False)
        max_sessions = kwargs.pop(
            "max_sessions",
            self._config_kwargs.get("max_sessions", TwilioVoiceServerConfig.max_sessions),
        )
        start_timeout_s = kwargs.pop(
            "start_timeout_s",
            float(
                os.environ.get(
                    "TWILIO_START_TIMEOUT_S",
                    TwilioVoiceServerConfig.start_timeout_s,
                )
            ),
        )
        public_twiml_url = kwargs.pop("public_twiml_url", None) or os.environ.get(
            "TWILIO_PUBLIC_TWIML_URL"
        )
        drain_timeout_s = kwargs.pop(
            "drain_timeout_s",
            float(
                os.environ.get(
                    "TWILIO_DRAIN_TIMEOUT_S",
                    TwilioVoiceServerConfig.drain_timeout_s,
                )
            ),
        )
        force_shutdown_timeout_s = kwargs.pop(
            "force_shutdown_timeout_s",
            float(
                os.environ.get(
                    "TWILIO_FORCE_SHUTDOWN_TIMEOUT_S",
                    TwilioVoiceServerConfig.force_shutdown_timeout_s,
                )
            ),
        )
        self._reject_unknown_mode_kwargs("twilio", kwargs)
        return TwilioVoiceServerConfig(
            host=host,
            media_port=media_port,
            http_host=http_host,
            http_port=http_port,
            stream_url=stream_url,
            stream_token_secret=stream_token_secret,
            twilio_auth_token=twilio_auth_token,
            trust_proxy_headers=bool(trust_proxy_headers),
            unsafe_allow_unsigned_webhooks=unsafe_allow_unsigned_webhooks,
            max_sessions=max_sessions,
            start_timeout_s=start_timeout_s,
            public_twiml_url=public_twiml_url,
            drain_timeout_s=drain_timeout_s,
            force_shutdown_timeout_s=force_shutdown_timeout_s,
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

        Per-connection modes reject a static ``config`` because it may contain
        stateful providers or agent bridges, and require ``config_factory``.
        High-level fields produce a fresh preset per transport.
        """
        if self._config is not None:
            raise ValueError(
                f"VoiceApp {mode!r} mode is per-connection and cannot reuse a static "
                "`config` across concurrent sessions. Pass a `config_factory` "
                "instead, or construct with "
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
        shared :func:`~easycat._net.normalize_auth_token`) so a
        ``"   "`` value cannot satisfy this guard as truthy while the downstream
        WebSocket authorizer treats it as no token at all — keeping the bind
        guard and request authorization in sync across both transports.
        """
        resolved = normalize_auth_token(token or os.environ.get(_SERVE_TOKEN_ENV))
        if resolved is None and not is_loopback_host(host) and not unsafe_allow_no_auth:
            raise ValueError(
                f"Refusing to bind {host!r} without a token. Pass serve_token= "
                f"(or set {_SERVE_TOKEN_ENV}) when serving beyond loopback, or pass "
                "unsafe_allow_no_auth=True to bind an unauthenticated endpoint."
            )
        return resolved
