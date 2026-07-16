"""Validation environment metadata and runtime-secret policy."""

from __future__ import annotations

import os
import platform
import sys
from typing import Any

PROVIDER_ENV_VARS = (
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "CARTESIA_API_KEY",
)


def runtime_secret_values() -> tuple[str, ...]:
    """Return configured provider secrets that must be redacted from artifacts."""
    return tuple(value for name in PROVIDER_ENV_VARS if (value := os.environ.get(name)))


def is_ci() -> bool:
    """Return whether validation is running in a conventional CI environment."""
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


def validation_environment_metadata() -> dict[str, Any]:
    """Build the shared environment block used by validation artifacts."""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "ci": is_ci(),
        "env_vars": {name: bool(os.environ.get(name)) for name in PROVIDER_ENV_VARS},
    }
