"""``easycat serve`` — launch a voice playground through ``VoiceApp``.

One command from zero to a talking page: ``serve`` constructs a
:class:`~easycat.VoiceApp` with the bundled playground agent and drives it for
the chosen ``--mode`` (``browser`` by default — WebRTC + bundled client with a
live transcript, interruption indicator, and per-turn latency readout).

Security defaults mirror the WebSocket/docker golden path: the signaling
server binds loopback (``127.0.0.1``) unless ``--host`` is overridden, and a
non-loopback bind requires a shared ``--token`` (or ``EASYCAT_SERVE_TOKEN``)
that the bundled client forwards from the page URL's ``?token=`` query.
``VoiceApp`` also enforces this guard internally (defense in depth); the CLI
keeps its own pre-flight check so it can emit the exit-code-2 message contract.

The wire protocol behind the playground page is documented in
``docs/browser-playground.md``.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

import typer

from easycat.cli._errors import cli_command
from easycat.cli._output import emit_command_error, stdout_console

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_AGENT_MODEL = "gpt-4o-mini"
_DEFAULT_INSTRUCTIONS = (
    "You are a helpful voice assistant. Keep responses concise and conversational."
)

# Modes the serve CLI surfaces (plus the VoiceApp aliases it accepts). Twilio is
# intentionally excluded here — it has its own server shape (Phase 1 / M3).
_SERVE_MODES = frozenset({"browser", "websocket", "local", "ws", "mic"})


def _serve_token_from_env() -> str | None:
    return os.environ.get("EASYCAT_SERVE_TOKEN") or None


def _playground_url(host: str, port: int, token: str | None) -> str:
    display_host = "localhost" if host in _LOOPBACK_HOSTS else host
    url = f"http://{display_host}:{port}"
    if token:
        query = urlencode({"token": token})
        return f"{url}/webrtc_client.html?{query}"
    return url


def _playground_config_factory(
    *,
    agent_model: str,
    instructions: str,
):
    """Build the per-transport config factory for the playground.

    Per-connection modes (``browser``/``websocket``) reject a static ``config``
    and require a ``config_factory``; this builds a fresh ``EasyConfig.browser``
    bound to the concrete per-connection transport, with a playground agent that
    injects ``instructions`` on every Responses-API request.
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
        )
        return EasyConfig.browser(transport=transport, agent=agent)

    return factory


def _build_voice_app(
    *,
    agent_model: str,
    instructions: str,
) -> Any:
    """Build the playground :class:`VoiceApp` (extracted for tests)."""
    from easycat.voice_app import VoiceApp

    factory = _playground_config_factory(
        agent_model=agent_model,
        instructions=instructions,
    )
    return VoiceApp(config_factory=factory)


def _validate_playground_config() -> None:
    """Surface the playground's credential requirement before serving.

    Each client connection builds a fresh ``EasyConfig.browser`` through the
    per-connection factory, so a missing ``OPENAI_API_KEY`` would otherwise only
    fail server-side when the first client connects — after the CLI has already
    printed the Open URL and started listening. Construct the same browser preset
    once up front (the per-connection agent/transport do not affect the
    credential check) so the catalogued missing-key error
    (``EASYCAT_E203``) fails at startup instead. The throwaway config builds no
    network clients, so it is safe to discard.
    """
    from easycat.config import EasyConfig

    EasyConfig.browser()


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
        app.run(mode, host=host, port=port, auth_token=token, announce=False)
        return
    app.run(mode, host=host, port=port, auth_token=token)


@cli_command
def serve(
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
            "EASYCAT_SERVE_TOKEN when set. The printed Open URL includes it as ?token=."
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
    """Serve the voice playground via VoiceApp (browser/WebRTC by default)."""
    if mode not in _SERVE_MODES:
        emit_command_error(
            "serve",
            f"Unknown --mode {mode!r}. Choose one of: browser, websocket, local.",
            json_output=False,
        )
        raise typer.Exit(2)

    token = token or _serve_token_from_env()
    if host not in _LOOPBACK_HOSTS and not token:
        emit_command_error(
            "serve",
            f"Refusing to bind {host!r} without a token. Pass --token (or set "
            "EASYCAT_SERVE_TOKEN) when serving beyond loopback, and put the server "
            "behind HTTPS for browser microphone access.",
            json_output=False,
        )
        raise typer.Exit(2)

    # Fail fast on a missing OPENAI_API_KEY: the playground config is built
    # per-connection, so without this the CLI would announce the Open URL and
    # start listening, leaving the first client to hit a server-side failure.
    _validate_playground_config()

    app = _build_voice_app(agent_model=agent_model, instructions=instructions)
    # Only browser mode serves the bundled playground page; websocket/local modes
    # start a raw WebSocket server or a local mic session (each announces itself),
    # so the playground URL and page instructions would mislead there.
    if mode == "browser":
        stdout_console.print(f"Open {_playground_url(host, port, token)}")
        stdout_console.print(
            "The page shows the live transcript, interruption indicator, and per-turn latency."
        )
    _run_voice_app(app, mode=mode, host=host, port=port, token=token)
