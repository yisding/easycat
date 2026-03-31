"""Convenience helpers for common EasyCat setup patterns."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from easycat.event_logging import EventLoggingConfig
from easycat.events import AgentFinal, BotStoppedSpeaking, Interruption, STTFinal, TurnStarted
from easycat.session._session import Session

logger = logging.getLogger(__name__)


def require_env(name: str) -> str:
    """Load a required environment variable or exit with a clear message."""
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def default_event_logging() -> EventLoggingConfig:
    """Useful event trace defaults without overwhelming partials."""
    return EventLoggingConfig(enabled=True, include_partials=False)


async def wait_for_shutdown_signal(session: Session) -> None:
    """Run until SIGINT/SIGTERM, then stop the session cleanly."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await session.stop()


def attach_runtime_feedback(session: Session) -> None:
    """Print useful status updates and transcripts to the console.

    Subscribes to key lifecycle events so developers can see what is
    happening while an example (or production bot) is running.
    """
    session.subscribe_event(TurnStarted, lambda _e: print("\U0001f3a4 Listening\u2026"))
    session.subscribe_event(STTFinal, lambda e: print(f"\U0001f4dd You: {e.text}"))
    session.subscribe_event(AgentFinal, lambda e: print(f"\U0001f916 Assistant: {e.text}"))
    session.subscribe_event(
        BotStoppedSpeaking, lambda _e: print("\u2705 Your turn \u2014 you can speak now.")
    )
    session.subscribe_event(Interruption, lambda _e: print("\u26a1 Interruption detected."))
