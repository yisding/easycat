"""Shared setup helpers for EasyCat examples."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from easycat import EventLoggingConfig, OpenAIAgentsAdapter, Session

logger = logging.getLogger(__name__)


def require_env(name: str) -> str:
    """Load a required environment variable or exit with a clear message."""
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def build_openai_agents_adapter(
    *, name: str = "VoiceAssistant", instructions: str
) -> OpenAIAgentsAdapter:
    """Create an OpenAI Agents SDK adapter configured for Responses WebSocket API."""
    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "OpenAI Agents SDK is required. Install with: uv sync --extra openai-agents"
        ) from exc

    run_config = None
    try:
        from agents import ModelSettings, OpenAIProvider, RunConfig  # type: ignore[import-untyped]

        reasoning = None
        try:
            from openai.types.shared import Reasoning  # type: ignore[import-untyped]

            reasoning = Reasoning(effort="none")
        except (ImportError, TypeError):
            reasoning = {"effort": "none"}

        provider = OpenAIProvider(use_responses=True, use_responses_websocket=True)
        run_config = RunConfig(
            model_provider=provider,
            model_settings=ModelSettings(reasoning=reasoning, verbosity="low"),
        )
    except (ImportError, TypeError) as exc:
        logger.debug("RunConfig/OpenAIProvider setup failed, falling back: %s", exc)
        try:
            from agents import set_default_openai_api  # type: ignore[import-untyped]

            set_default_openai_api("responses")
        except (ImportError, AttributeError) as exc:
            logger.debug("set_default_openai_api unavailable: %s", exc)

    voice_agent = Agent(name=name, instructions=instructions)
    return OpenAIAgentsAdapter(voice_agent, run_config=run_config)


def default_event_logging() -> EventLoggingConfig:
    """Useful event trace defaults for examples without overwhelming partials."""
    return EventLoggingConfig(enabled=True, include_partials=False)


async def wait_for_shutdown_signal(session: Session) -> None:
    """Run until SIGINT/SIGTERM, then stop the session cleanly."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await session.stop()
