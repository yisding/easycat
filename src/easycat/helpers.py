"""Convenience helpers for common EasyCat setup patterns."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING, Literal

from easycat._signals import create_shutdown_event as _create_shutdown_event
from easycat._signals import scoped_shutdown_signal_handlers as _shutdown_signal_handler_scope
from easycat.echo_cancellation import EchoCancellationConfig
from easycat.events import AgentFinal, BotStoppedSpeaking, Interruption, STTFinal, TurnStarted
from easycat.runtime._event_tasks import RuntimeTaskScope
from easycat.session._session import Session

if TYPE_CHECKING:
    from easycat.config import EasyConfig

logger = logging.getLogger(__name__)
FeedbackMode = Literal["auto", "on", "off"]


def require_env(name: str) -> str:
    """Load a required environment variable or exit with a clear message."""
    value = os.getenv(name)
    if not value:
        raise SystemExit(
            f"{name} is required. Set it before running, for example: "
            f"export {name}=...; verify provider keys with `uv run easycat doctor`. "
            "With a project `.env`, run commands as `uv run --env-file .env ...` "
            "and verify with `uv run easycat doctor --env-file .env`."
        )
    return value


def create_shutdown_event() -> asyncio.Event:
    """Return an event set by SIGINT/SIGTERM when the event loop supports it."""
    return _create_shutdown_event()


async def wait_for_shutdown_signal(session: Session) -> None:
    """Run until SIGINT/SIGTERM, then stop the session cleanly.

    On platforms where the event loop cannot register signal handlers
    (e.g. Windows' ``ProactorEventLoop``), falls back to letting
    ``KeyboardInterrupt`` propagate so the caller's teardown still runs.
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    with _shutdown_signal_handler_scope(loop, stop_event) as installed:
        if not installed:
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


def _feedback_enabled(
    feedback: FeedbackMode,
    *,
    stderr_isatty: bool | None = None,
) -> bool:
    """Resolve the ``run(..., feedback=...)`` policy to a boolean."""
    if os.getenv("EASYCAT_QUIET"):
        return False
    if feedback == "on":
        return True
    if feedback == "off":
        return False
    if feedback == "auto":
        if stderr_isatty is None:
            stderr_isatty = sys.stderr.isatty()
        return stderr_isatty and not os.getenv("PYTEST_CURRENT_TEST")
    raise ValueError(f"Unknown feedback mode: {feedback!r}. Use 'auto', 'on', or 'off'.")


def _enable_console_logging_from_env() -> None:
    """Enable EasyCat console logging when explicitly requested by env."""
    env_level = os.getenv("EASYCAT_LOG_LEVEL", "").strip()
    if not env_level:
        return

    from easycat._logging import enable_console_logging

    # Only attach a console handler when the user explicitly asked for a log
    # level; otherwise stay silent so applications that already own logging
    # aren't overridden.
    enable_console_logging()


async def _await_session_until_shutdown(session: Session) -> None:
    """Run a prebuilt session in the current loop until signal or self-stop."""
    # ``async with`` is the one public teardown idiom: __aenter__ starts the
    # session and __aexit__ tears it down with ``stop(force=True)``.
    async with session:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        with _shutdown_signal_handler_scope(loop, stop_event) as installed:
            if installed:
                waiters = RuntimeTaskScope(
                    owner_label="run-session",
                    member_name="shutdown_waiter",
                    cohort="shutdown",
                    logger=logger,
                    failure_message="Run-session shutdown waiter failed",
                    drop_if_closed=False,
                )
                signal_waiter = waiters.create_task(
                    stop_event.wait(),
                    task_name="run-session-signal-waiter",
                )
                session_waiter = waiters.create_task(
                    session.wait_closed(),
                    task_name="run-session-close-waiter",
                )
                assert signal_waiter is not None
                assert session_waiter is not None
                pending = {signal_waiter, session_waiter}
                try:
                    done, _pending = await asyncio.wait(
                        pending,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        task.result()
                finally:
                    await waiters.cancel_and_drain()
            else:
                # No signal-handler support (e.g. Windows ProactorEventLoop).
                # Session-driven shutdown still completes normally; Ctrl+C is
                # propagated by the synchronous wrapper when it owns the loop.
                await session.wait_closed()


def _run_session_until_shutdown(session: Session) -> None:
    """Run a prebuilt session until it closes or receives SIGINT/SIGTERM."""

    try:
        asyncio.run(_await_session_until_shutdown(session))
    except KeyboardInterrupt:
        # Ctrl+C on the fallback (signal-handler-less) path: exit cleanly
        # instead of dumping a traceback. Teardown already ran via __aexit__.
        pass


def run_session(session: Session, *, feedback: FeedbackMode = "auto") -> None:
    """Run a prebuilt session to completion from a synchronous entry point.

    Use this when you need the session object before startup — for example to
    subscribe event handlers, serve the debugger UI, or inject app-specific
    runtime hooks — but still want the same signal handling, console feedback
    policy, and ``async with session:`` teardown path as :func:`run`.

    For the simplest first-run path, prefer ``run(EasyConfig.mic(...))``.
    """
    _enable_console_logging_from_env()

    if _feedback_enabled(feedback):
        attach_runtime_feedback(session)

    _run_session_until_shutdown(session)


def _prepare_configured_session(config: EasyConfig, *, feedback: FeedbackMode) -> Session:
    """Build a session with the feedback/logging policy shared by run/arun."""
    from easycat.config import create_session

    feedback_on = _feedback_enabled(feedback)
    _enable_console_logging_from_env()

    session = create_session(config)
    if feedback_on:
        # Live transcript feedback (Listening.../You.../Assistant...) follows
        # ``feedback``. The one-line "what got wired" banner is an extra on
        # top, suppressed independently via EASYCAT_QUIET or the repo's
        # standard NO_COLOR / CI conventions (see cli/_output.py) — so
        # silencing the banner never costs you the transcripts.
        banner_suppressed = bool(
            os.getenv("EASYCAT_QUIET") or os.getenv("NO_COLOR") or os.getenv("CI") == "true"
        )
        if not banner_suppressed:
            print(_wired_summary(config), file=sys.stderr)
        attach_runtime_feedback(session)
    return session


def _require_sync_entry_point() -> None:
    """Fail before side effects when ``run`` is called from an active loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "easycat.run() cannot be called while an event loop is running; "
        "use `await easycat.arun(config, feedback=...)` instead."
    )


async def arun(config: EasyConfig, *, feedback: FeedbackMode = "auto") -> None:
    """Run a voice agent in the caller's existing asyncio event loop.

    This is the behavioral peer of :func:`run`: it creates the same session,
    applies the same logging and runtime-feedback policy, waits for an OS
    shutdown signal or session-driven close, and tears down through
    ``async with session:`` (which calls ``stop(force=True)`` on exit).

    Use ``await arun(config)`` in notebooks, ASGI lifespan hooks, async test
    harnesses, and any application that already owns its event loop. Use
    :func:`run` only at a synchronous process entry point.
    """
    session = _prepare_configured_session(config, feedback=feedback)
    await _await_session_until_shutdown(session)


def run(config: EasyConfig, *, feedback: FeedbackMode = "auto") -> None:
    """Run a voice agent to completion from a synchronous entry point.

    Replaces the ``asyncio.run(main())`` wrapper, manual
    ``await session.start()``, signal waiting, and teardown ceremony
    that every example used to carry. Internally it uses the public
    ``async with session:`` lifecycle: entering starts the session and
    exiting calls ``stop(force=True)``. ``feedback="auto"`` preserves
    the default first-run behavior: runtime feedback (``Listening...``,
    user transcripts, assistant replies) is auto-attached when stderr
    is a TTY and EasyCat is not running under pytest, so
    `easycat init → run` feels alive out of the box while tests and
    redirected production logs stay quiet. Use ``feedback="on"`` to
    force that console feedback, or ``feedback="off"`` to suppress it.

    ``EASYCAT_LOG_LEVEL=info`` (or ``debug``/``warning``/``warn``/
    ``error``/``critical``) in the environment bumps the ``easycat``
    logger without needing ``debug="light"``, matching the
    ``LIVEKIT_LOG_LEVEL`` convention.

    Advanced users who need custom orchestration should reach for
    :func:`easycat.create_session` directly and manage the lifecycle
    themselves. If an asyncio event loop is already running, use
    ``await easycat.arun(config, feedback=...)`` instead; ``run`` fails before
    creating a session so it never attempts a nested ``asyncio.run``.
    """
    _require_sync_entry_point()
    session = _prepare_configured_session(config, feedback=feedback)
    _run_session_until_shutdown(session)


# Transport-config type -> human label for the "what got wired" summary.
# There is no transport-name registry (``easycat.config`` maps config types
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
    echo_on = echo.enabled if isinstance(echo, EchoCancellationConfig) else echo is not None
    echo_label = "on" if echo_on else "off"
    if config.enable_echo_cancellation is None:
        echo_label += " (auto)"

    # Noise reduction is opt-in: a reducer is only wired when
    # ``enable_noise_reduction`` is set or an explicit config is provided
    # (mirrors the create_session gating in ``easycat.config``).
    nr_on = config.enable_noise_reduction or config.noise_reduction is not None
    nr_label = "on" if nr_on else "off"

    return (
        f"easycat: wired stt={stt_label}, tts={tts_label}, "
        f"transport={transport_label}, noise-reduction={nr_label}, "
        f"echo-cancel={echo_label}"
    )
