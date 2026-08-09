"""``easycat serve`` — launch a playground or a manifest-backed VoiceServer.

One command from zero to a talking page: ``serve`` constructs a
:class:`~easycat.VoiceApp` with the bundled playground agent and drives it for
the chosen ``--mode`` (``browser`` by default — WebRTC + bundled client with a
live transcript, interruption indicator, and per-turn latency readout).

Security defaults mirror the WebSocket/docker golden path: the signaling
server binds loopback (``127.0.0.1``) unless ``--host`` is overridden, and a
non-loopback bind requires a shared ``--token`` (or ``EASYCAT_SERVE_TOKEN``)
that the bundled client forwards from the page URL's ``#token=`` fragment.
``VoiceApp`` also enforces this guard internally (defense in depth); the CLI
keeps its own pre-flight check so it can emit the exit-code-2 message contract.

The wire protocol behind the playground page is documented in
``docs/browser-playground.md``.

Passing ``--manifest`` selects the production-oriented declarative path and
runs :meth:`easycat.VoiceServer.from_manifest`; host, port, auth, providers,
and agent wiring then come from ``easycat.toml`` and the selected ``--profile``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import typer

from easycat._env import is_truthy
from easycat._net import is_loopback_host
from easycat.cli._errors import cli_command
from easycat.cli._output import emit_command_error, stdout_console

_DEFAULT_AGENT_MODEL = "gpt-5.6-luna"
_DEFAULT_INSTRUCTIONS = (
    "You are a helpful voice assistant. Keep responses concise and conversational."
)

# Modes the serve CLI surfaces (plus the VoiceApp aliases it accepts). Twilio is
# intentionally excluded here — it has its own server shape (Phase 1 / M3).
_SERVE_MODES = frozenset({"browser", "websocket", "local", "ws", "mic"})


def _serve_token_from_env() -> str | None:
    return os.environ.get("EASYCAT_SERVE_TOKEN") or None


def _manifest_from_env() -> Path | None:
    """Resolve the manifest env var independently of Typer's envvar support."""
    value = os.environ.get("EASYCAT_MANIFEST")
    return Path(value) if value else None


def _is_loopback(host: str) -> bool:
    """Reuse the canonical loopback check (covers all 127.0.0.0/8 / ``::1`` /
    ``::ffff:127.*`` forms, not just the three literals) so the CLI pre-flight
    guard is never STRICTER than VoiceApp's authoritative one."""
    return is_loopback_host(host)


def _url_host(host: str) -> str:
    display_host = "localhost" if _is_loopback(host) else host
    if ":" in display_host and not display_host.startswith("["):
        return f"[{display_host}]"
    return display_host


def _playground_url(host: str, port: int, token: str | None) -> str:
    url = f"http://{_url_host(host)}:{port}"
    if token:
        fragment = urlencode({"token": token})
        return f"{url}/webrtc_client.html#{fragment}"
    return url


def _websocket_endpoint(host: str, port: int) -> str:
    return f"ws://{_url_host(host)}:{port}"


def _announce_serve_endpoint(*, mode: str, host: str, port: int, token: str | None) -> None:
    """Print a mode-appropriate endpoint hint before the (blocking) server starts.

    Only ``browser`` mode serves the HTTP playground page; ``websocket`` mode
    starts a raw WebSocket listener (no page) and ``local`` mode opens the
    microphone with no listener at all. Printing the browser playground URL for
    the latter two left users staring at an address that cannot work.
    """
    if mode in {"local", "mic"}:
        stdout_console.print("Listening on your microphone. Press Ctrl+C to stop.")
        return
    if mode in {"websocket", "ws"}:
        endpoint = _websocket_endpoint(host, port)
        stdout_console.print(f"Connect a WebSocket client to {endpoint}")
        if token:
            stdout_console.print("Send the token as an `Authorization: Bearer <token>` header.")
        return
    stdout_console.print(f"Open {_playground_url(host, port, token)}")
    stdout_console.print(
        "The page shows the live transcript, interruption indicator, and per-turn latency."
    )


def _playground_config_factory(
    *,
    agent_model: str,
    instructions: str,
):
    """Build the per-transport config factory for the playground.

    Per-connection modes (``browser``/``websocket``) reject a static ``config``
    and require a ``config_factory``; this builds a fresh ``EasyConfig`` bound
    to the concrete per-connection transport, with a playground agent that
    injects ``instructions`` on every Responses-API request. The transport's
    declared echo-cancellation default is preserved: WebRTC/local transports
    opt in, while raw WebSocket transports stay off.
    """
    from easycat.config import EasyConfig
    from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge

    class _PlaygroundBridge(RemoteResponsesAPIBridge):
        """Responses-API bridge with playground instructions on every request."""

        def _build_request_body(self, turn_input: Any) -> dict[str, Any]:
            body = super()._build_request_body(turn_input)
            body["instructions"] = instructions
            return body

    def factory(transport: Any) -> EasyConfig:
        agent = _PlaygroundBridge(
            base_url="https://api.openai.com",
            model=agent_model,
            api_key=os.environ.get("OPENAI_API_KEY"),
            reasoning_effort="none" if agent_model == _DEFAULT_AGENT_MODEL else None,
        )
        return EasyConfig(transport=transport, agent=agent)

    return factory


def _validate_playground_config() -> None:
    """Surface the playground's credential requirement before serving.

    Each client connection builds a fresh ``EasyConfig`` through the
    per-connection factory, so a missing ``OPENAI_API_KEY`` would otherwise only
    fail server-side when the first client connects — after the CLI has already
    printed the Open URL and started listening. Construct the same browser preset
    once up front via ``EasyConfig.browser()`` (the per-connection agent/transport
    do not affect the credential check) so the catalogued missing-key error
    (``EASYCAT_E203``) fails at startup instead. The throwaway config builds no
    network clients, so it is safe to discard.
    """
    from easycat.config import EasyConfig

    EasyConfig.browser()


def _dev_mode_enabled() -> bool:
    """Whether ``EASYCAT_DEV`` arms dev debugger mode for ``easycat serve``.

    Mirrors the truthy parsing in :mod:`easycat.debugger.dev`: any value other
    than ``""``/``0``/``false``/``no``/``off`` (case-insensitive) enables it.
    """
    return is_truthy(os.environ.get("EASYCAT_DEV"))


def _build_voice_app(
    *,
    agent_model: str,
    instructions: str,
) -> Any:
    """Build the playground :class:`VoiceApp` (extracted for tests)."""
    from easycat.voice_app import VoiceApp

    _validate_playground_config()
    factory = _playground_config_factory(
        agent_model=agent_model,
        instructions=instructions,
    )
    # ``EASYCAT_DEV=1 easycat serve`` arms the always-available dev debugger:
    # durable journaling + a loopback session-selector UI launched once. Purely
    # additive over the autolaunch guard (R7) — it never relaxes the
    # debug='full'-alone-never-autolaunches invariant.
    return VoiceApp(config_factory=factory, dev=_dev_mode_enabled())


def _run_voice_app(
    app: Any,
    *,
    mode: str,
    host: str,
    port: int,
    token: str | None,
) -> None:
    """Run the VoiceApp for *mode* until shutdown (extracted for tests)."""
    if mode in {"local", "mic"}:
        # Local mode has no listener; host/port/token are not applicable.
        app.run(mode)
        return
    if mode == "browser":
        # The CLI already printed the Open URL; suppress VoiceApp's own browser
        # announcement so it is not printed twice.
        app.run(mode, host=host, port=port, serve_token=token, announce=False)
        return
    app.run(mode, host=host, port=port, serve_token=token)


def _build_manifest_server(manifest: Path, *, profile: str) -> Any:
    """Build the manifest-backed server (extracted for offline CLI tests)."""
    from easycat.server import VoiceServer

    return VoiceServer.from_manifest(manifest, profile=profile)


def _announce_manifest_server(server: Any, *, manifest: Path, profile: str) -> None:
    """Print the manifest-owned bind policy without exposing resolved secrets."""
    config = server.config
    host = _url_host(config.host)
    stdout_console.print(
        f"Serving manifest {manifest} (profile: {profile}) on http://{host}:{config.port}",
        markup=False,
        highlight=False,
    )
    stdout_console.print(
        "Health, plan, manifest, metrics, and WebRTC routes use the HTTP port; "
        "raw WebSocket sessions use the next port.",
        markup=False,
        highlight=False,
    )


def _run_manifest_server(server: Any) -> None:
    """Run a VoiceServer until shutdown (extracted for tests)."""
    server.run()


@cli_command
def serve(
    manifest: Path | None = typer.Option(
        None,
        "--manifest",
        help=(
            "Run a VoiceServer from easycat.toml. Manifest server/profile settings "
            "replace the playground options."
        ),
        envvar="EASYCAT_MANIFEST",
        show_envvar=True,
    ),
    profile: str = typer.Option(
        "default",
        "--profile",
        help="Voice profile table to serve (for example, voice.default).",
    ),
    mode: str = typer.Option(
        "browser",
        "--mode",
        help=(
            "Deployment mode to serve: browser (WebRTC + bundled client), "
            "websocket (per-client WebSocket sessions), or local (mic)."
        ),
    ),
    port: int = typer.Option(8080, "--port", "-p", help="Port for the playground server."),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help=(
            "Bind address. Defaults to loopback; a non-loopback bind requires "
            "--token (or EASYCAT_SERVE_TOKEN)."
        ),
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        help=(
            "Shared secret required by the signaling endpoints. Defaults to "
            "EASYCAT_SERVE_TOKEN when set. The printed Open URL includes it as #token=."
        ),
        envvar="EASYCAT_SERVE_TOKEN",
        show_envvar=True,
    ),
    agent_model: str = typer.Option(
        _DEFAULT_AGENT_MODEL,
        "--agent-model",
        help="OpenAI Responses API model used for the playground agent.",
    ),
    instructions: str = typer.Option(
        _DEFAULT_INSTRUCTIONS,
        "--instructions",
        help="System-style guidance for the playground agent.",
    ),
) -> None:
    """Serve the playground, or use --manifest for a declarative VoiceServer."""
    # Typer's supported dependency floor does not populate
    # Path options from ``envvar=`` in every supported Click combination. Keep
    # the option metadata for help and newer Typer versions, while resolving
    # the documented fallback ourselves when the CLI option was not supplied.
    manifest = manifest or _manifest_from_env()
    if manifest is not None:
        server = _build_manifest_server(manifest, profile=profile)
        _announce_manifest_server(server, manifest=manifest, profile=profile)
        _run_manifest_server(server)
        return
    if profile != "default":
        emit_command_error(
            "serve",
            "--profile requires --manifest (or EASYCAT_MANIFEST).",
            json_output=False,
        )
        raise typer.Exit(2)

    if mode not in _SERVE_MODES:
        emit_command_error(
            "serve",
            f"Unknown --mode {mode!r}. Choose one of: {', '.join(sorted(_SERVE_MODES))}.",
            json_output=False,
        )
        raise typer.Exit(2)

    token = token or _serve_token_from_env()
    # Local/mic mode opens no listener — host/port/token are ignored by
    # ``_run_voice_app`` there — so the non-loopback bind-token guard only
    # applies to the listener modes (browser/websocket). Without this gate,
    # ``serve --mode local --host 0.0.0.0`` would be rejected for a bind that
    # never happens.
    is_listener_mode = mode not in {"local", "mic"}
    if is_listener_mode and not _is_loopback(host) and not token:
        emit_command_error(
            "serve",
            f"Refusing to bind {host!r} without a token. Pass --token (or set "
            "EASYCAT_SERVE_TOKEN) when serving beyond loopback, and put the server "
            "behind HTTPS for browser microphone access.",
            json_output=False,
        )
        raise typer.Exit(2)

    # Fail fast on a missing OPENAI_API_KEY before announcing an endpoint or
    # binding a listener — the playground builds its config per-connection, so
    # without this the first client would hit a server-side failure instead.
    _validate_playground_config()

    app = _build_voice_app(agent_model=agent_model, instructions=instructions)
    # Mode-appropriate hint: browser prints the playground URL, websocket prints
    # the ws:// endpoint, local prints the mic message — never the page URL for a
    # mode that does not serve it.
    _announce_serve_endpoint(mode=mode, host=host, port=port, token=token)
    _run_voice_app(app, mode=mode, host=host, port=port, token=token)
