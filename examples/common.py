"""Backward-compatibility shim — these helpers now live in the easycat package."""

from easycat import (  # noqa: F401
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.agents.openai_agents import build_openai_agents_adapter  # noqa: F401

__all__ = [
    "build_openai_agents_adapter",
    "default_event_logging",
    "require_env",
    "wait_for_shutdown_signal",
]
