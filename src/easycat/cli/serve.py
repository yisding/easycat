"""``easycat serve`` — launch the browser voice playground.

One command from zero to a talking browser page: builds
``EasyConfig.browser()`` with the bundled WebRTC client (live transcript,
interruption indicator, per-turn latency readout), starts the session, and
prints the URL to open.

Security defaults mirror the WebSocket/docker golden path: the signaling
server binds loopback (``127.0.0.1``) unless ``--host`` is overridden, and a
non-loopback bind requires a shared ``--token`` (or ``EASYCAT_SERVE_TOKEN``)
that the bundled client forwards from the page URL's ``?token=`` query.

The wire protocol behind the playground page is documented in
``docs/browser-playground.md``.
"""

from __future__ import annotations

import os
from typing import Any

import typer

from easycat.cli._errors import cli_command
from easycat.cli._output import emit_command_error, stdout_console

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_AGENT_MODEL = "gpt-4o-mini"
_DEFAULT_INSTRUCTIONS = (
    "You are a helpful voice assistant. Keep responses concise and conversational."
)


def _serve_token_from_env() -> str | None:
    return os.environ.get("EASYCAT_SERVE_TOKEN") or None


def _playground_url(host: str, port: int, token: str | None) -> str:
    display_host = "localhost" if host in _LOOPBACK_HOSTS else host
    url = f"http://{display_host}:{port}"
    if token:
        url = f"{url}/?token={token}"
    return url


def _build_serve_session(
    *,
    host: str,
    port: int,
    token: str | None,
    agent_model: str,
    instructions: str,
) -> Any:
    """Build the playground session: EasyConfig.browser + bundled client."""
    from easycat.config import EasyConfig, create_session
    from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge
    from easycat.transports.webrtc import WebRTCTransportConfig

    class _PlaygroundBridge(RemoteResponsesAPIBridge):
        """Responses-API bridge with playground instructions on every request."""

        def _build_request_body(self, turn_input: Any) -> dict[str, Any]:
            body = super()._build_request_body(turn_input)
            body["instructions"] = instructions
            return body

    agent = _PlaygroundBridge(
        base_url="https://api.openai.com",
        model=agent_model,
        api_key=os.environ.get("OPENAI_API_KEY"),
    )
    config = EasyConfig.browser(
        transport=WebRTCTransportConfig(host=host, port=port, auth_token=token),
        agent=agent,
    )
    return create_session(config)


def _run_serve(session: Any) -> None:
    """Run the playground session until shutdown (extracted for tests)."""
    from easycat.helpers import run_session

    run_session(session)


@cli_command
def serve(
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
    """Serve the browser voice playground (WebRTC + bundled client)."""
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

    session = _build_serve_session(
        host=host,
        port=port,
        token=token,
        agent_model=agent_model,
        instructions=instructions,
    )
    stdout_console.print(f"Open {_playground_url(host, port, token)}")
    stdout_console.print(
        "The page shows the live transcript, interruption indicator, and per-turn latency."
    )
    _run_serve(session)
