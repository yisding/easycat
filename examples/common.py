"""Backward-compatibility shim — these helpers now live in the easycat package."""

from easycat import (  # noqa: F401
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)

__all__ = [
    "default_event_logging",
    "require_env",
    "wait_for_shutdown_signal",
]
