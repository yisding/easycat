"""Convenience helpers for common EasyCat setup patterns."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING

from easycat.events import AgentFinal, BotStoppedSpeaking, Interruption, STTFinal, TurnStarted
from easycat.session._session import Session

if TYPE_CHECKING:
    from easycat.config import EasyConfig

logger = logging.getLogger(__name__)


def require_env(name: str) -> str:
    """Load a required environment variable or exit with a clear message."""
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


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
    happening while an example (or production bot) is running.  Lines are
    rendered through the shared stderr console so ``NO_COLOR``/``CI`` are
    honored (a bare ``print`` would ignore them).
    """
    from easycat._console import feedback_console

    def _say(text: str) -> None:
        # ``markup=False``/``highlight=False`` keep the literal text (emoji and
        # user/assistant content) intact rather than letting Rich reinterpret it.
        feedback_console.print(text, markup=False, highlight=False)

    session.subscribe_event(TurnStarted, lambda _e: _say("\U0001f3a4 Listening\u2026"))
    session.subscribe_event(STTFinal, lambda e: _say(f"\U0001f4dd You: {e.text}"))
    session.subscribe_event(AgentFinal, lambda e: _say(f"\U0001f916 Assistant: {e.text}"))
    session.subscribe_event(
        BotStoppedSpeaking, lambda _e: _say("\u2705 Your turn \u2014 you can speak now.")
    )
    session.subscribe_event(Interruption, lambda _e: _say("\u26a1 Interruption detected."))


def run(config: EasyConfig) -> None:
    """Run a voice agent to completion from a synchronous entry point.

    Replaces the ``asyncio.run(main())`` + ``await session.start()`` +
    signal-handling + ``await session.shutdown()`` ceremony that every
    example used to carry.  Runtime feedback (``Listening...``, user
    transcripts, assistant replies) is auto-attached when stderr is a
    TTY so `easycat init → run` feels alive out of the box; tests and
    production pipelines that redirect stderr stay quiet.

    ``EASYCAT_LOG_LEVEL=info`` (or ``debug``/``warning``/``error``) in
    the environment bumps the ``easycat`` logger without needing
    ``debug="light"``, matching the ``LIVEKIT_LOG_LEVEL`` convention.

    Advanced users who need custom orchestration should reach for
    :func:`easycat.create_session` directly and manage the lifecycle
    themselves.
    """
    from easycat._logging import enable_console_logging
    from easycat.config import create_session

    env_level = os.getenv("EASYCAT_LOG_LEVEL", "").strip()
    if env_level:
        # Only attach a console handler when the user explicitly asked for a
        # log level; otherwise stay silent so applications that already own
        # logging aren't overridden.  ``run()`` owns the process, but the
        # handler still goes on the ``easycat`` logger, never root.
        enable_console_logging()

    session = create_session(config)
    if sys.stderr.isatty() and not os.getenv("PYTEST_CURRENT_TEST"):
        attach_runtime_feedback(session)

    async def _run() -> None:
        await session.start()
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        try:
            await stop_event.wait()
        finally:
            await session.shutdown()

    asyncio.run(_run())
