"""Backward-compatibility shim — this helper now lives in the easycat package."""

from easycat import attach_runtime_feedback  # noqa: F401

__all__ = ["attach_runtime_feedback"]
