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


def _install_shutdown_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event
) -> bool:
    """Wire SIGINT/SIGTERM to set ``stop_event``.

    Returns ``True`` when at least one handler was installed.  On Windows
    the default ``ProactorEventLoop`` raises ``NotImplementedError`` from
    ``add_signal_handler``; we swallow it so callers can fall back to
    ``KeyboardInterrupt``-driven shutdown instead of surfacing an
    asyncio-internals traceback.
    """
    installed = False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            # NotImplementedError: ProactorEventLoop on Windows.
            # RuntimeError/ValueError: handler set off the main thread.
            pass
    return installed


async def wait_for_shutdown_signal(session: Session) -> None:
    """Run until SIGINT/SIGTERM, then stop the session cleanly.

    On platforms where the event loop cannot register signal handlers
    (e.g. Windows' ``ProactorEventLoop``), falls back to letting
    ``KeyboardInterrupt`` propagate so the caller's teardown still runs.
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    if not _install_shutdown_signal_handlers(loop, stop_event):
        # No signal handler support: block until cancelled / KeyboardInterrupt.
        try:
            await asyncio.Event().wait()
        finally:
            await session.stop()
        return

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
        # Live transcript feedback (Listening.../You.../Assistant...) always
        # shows on an interactive TTY. The one-line "what got wired" banner is
        # an extra on top, suppressed independently via EASYCAT_QUIET or the
        # repo's standard NO_COLOR / CI conventions (see cli/_output.py) — so
        # silencing the banner never costs you the transcripts.
        banner_suppressed = bool(
            os.getenv("EASYCAT_QUIET") or os.getenv("NO_COLOR") or os.getenv("CI") == "true"
        )
        if not banner_suppressed:
            print(_wired_summary(config), file=sys.stderr)
        attach_runtime_feedback(session)

    async def _run() -> None:
        # ``async with`` is the one public teardown idiom: __aenter__
        # starts the session and __aexit__ tears it down with
        # ``stop(force=True)``.
        async with session:
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            if _install_shutdown_signal_handlers(loop, stop_event):
                await stop_event.wait()
            else:
                # No signal-handler support (e.g. Windows ProactorEventLoop).
                # Block until KeyboardInterrupt; asyncio.run propagates it and
                # ``async with session`` still tears the session down cleanly.
                await asyncio.Event().wait()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        # Ctrl+C on the fallback (signal-handler-less) path: exit cleanly
        # instead of dumping a traceback. Teardown already ran via __aexit__.
        pass


# Transport-config type -> human label for the "what got wired" summary.
# There is no transport-name registry (``config.py``'s transport map is
# type -> factory), so the summary owns this small lookup itself.
_TRANSPORT_LABELS: dict[str, str] = {
    "LocalTransportConfig": "local-mic",
    "WebRTCTransportConfig": "browser",
    "TwilioTransportConfig": "phone",
    "WebSocketTransportConfig": "websocket",
    "WebTransportTransportConfig": "webtransport",
}


def _wired_summary(config: EasyConfig) -> str:
    """One-line "what got wired" summary for the TTY happy path.

    Names the resolved STT, TTS, and transport, plus echo cancellation
    as a resolved on/off (annotated ``(auto)`` only when the caller left
    ``enable_echo_cancellation`` unset, i.e. the value was derived from
    the transport default).  By the time ``run()`` sees ``config`` its
    ``__post_init__`` has already resolved the string shortcuts and the
    echo-cancellation tri-state, so these reads never hit ``None``.
    """
    from easycat.config import _provider_display_name

    stt_label = _provider_display_name(config.stt, "STT") if config.stt is not None else "none"
    tts_label = _provider_display_name(config.tts, "TTS") if config.tts is not None else "none"
    transport_label = _TRANSPORT_LABELS.get(
        type(config.transport).__name__,
        type(config.transport).__name__.replace("Config", ""),
    )

    echo = config.echo_cancellation
    echo_on = bool(echo.enabled) if echo is not None else False
    echo_label = "on" if echo_on else "off"
    if config.enable_echo_cancellation is None:
        echo_label += " (auto)"

    # Noise reduction is opt-in: a reducer is only wired when
    # ``enable_noise_reduction`` is set or an explicit config is provided
    # (mirrors the create_session gating in config.py).
    nr_on = config.enable_noise_reduction or config.noise_reduction is not None
    nr_label = "on" if nr_on else "off"

    return (
        f"easycat: wired stt={stt_label}, tts={tts_label}, "
        f"transport={transport_label}, noise-reduction={nr_label}, "
        f"echo-cancel={echo_label}"
    )
