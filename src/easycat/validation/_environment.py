"""Validation environment metadata and runtime-secret policy."""

from __future__ import annotations

import os

PROVIDER_ENV_VARS = (
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "CARTESIA_API_KEY",
)


def runtime_secret_values() -> tuple[str, ...]:
    """Return configured provider secrets that must be redacted from artifacts."""
    return tuple(value for name in PROVIDER_ENV_VARS if (value := os.environ.get(name)))
