"""Standalone multi-session WebTransport server orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from easycat._signals import create_shutdown_event
from easycat.session_manager import SessionManager, log_session_stop_failures
from easycat.transports.webtransport import (
    WebTransportConnectionTransport,
    WebTransportServer,
    WebTransportTransportConfig,
)

logger = logging.getLogger(__name__)


async def _shutdown_webtransport_sessions(
    server: WebTransportServer,
    manager: SessionManager[int],
) -> None:
    """Stop both owners, preserving the first failure while surfacing both."""
    primary_error: BaseException | None = None
    try:
        await server.stop()
    except BaseException as exc:
        primary_error = exc

    try:
        report = await manager.stop_all()
    except BaseException as exc:
        if primary_error is None:
            primary_error = exc
        else:
            logger.error("WebTransport session shutdown also raised", exc_info=exc)
    else:
        if (
            log_session_stop_failures(
                report,
                context="WebTransport session shutdown",
                log=logger,
            )
            and primary_error is None
        ):
            primary_error = RuntimeError(
                f"WebTransport shutdown retained {len(report.failures)} session(s)"
            )

    if primary_error is not None:
        raise primary_error


async def serve_webtransport_config_sessions(
    config_factory: Callable[[WebTransportConnectionTransport], Any],
    config: WebTransportTransportConfig,
    *,
    stop_event: asyncio.Event | None = None,
    runtime_feedback: bool = True,
    announce: bool = True,
) -> None:
    """Serve one EasyCat session per WebTransport client."""
    from easycat.config import create_session

    manager: SessionManager[int] = SessionManager()

    async def handle_connection(transport: WebTransportConnectionTransport) -> None:
        session = create_session(config_factory(transport))
        async with manager.connection(id(transport), session, runtime_feedback=runtime_feedback):
            await transport.wait_closed()

    server = WebTransportServer(config, handle_connection)
    await server.start()
    if announce:
        print(
            "\nServer ready. Connect WebTransport clients to "
            f"https://{config.host}:{config.port}{config.path}"
        )
        print("Press Ctrl+C to stop.\n")

    event = stop_event or create_shutdown_event()
    try:
        await event.wait()
    finally:
        await _shutdown_webtransport_sessions(server, manager)


def run_webtransport_config_server(
    config_factory: Callable[[WebTransportConnectionTransport], Any],
    config: WebTransportTransportConfig,
    *,
    runtime_feedback: bool = True,
    announce: bool = True,
) -> None:
    """Run a WebTransport session server from a synchronous entry point."""
    asyncio.run(
        serve_webtransport_config_sessions(
            config_factory,
            config,
            runtime_feedback=runtime_feedback,
            announce=announce,
        )
    )
