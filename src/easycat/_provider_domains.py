"""Leaf registry for provider API domains used by redaction checks."""

from __future__ import annotations

import threading
from collections.abc import Iterable

_BUILTIN_SENSITIVE_API_DOMAINS = frozenset(
    {
        "cartesia.ai",
        "deepgram.com",
        "elevenlabs.io",
        "openai.com",
    }
)
_SENSITIVE_API_DOMAINS = set(_BUILTIN_SENSITIVE_API_DOMAINS)
_LOCK = threading.Lock()


def register_sensitive_api_domains(domains: Iterable[str]) -> None:
    """Add provider API domains without importing provider-family modules."""
    normalized = {
        domain.strip().lower() for domain in domains if isinstance(domain, str) and domain.strip()
    }
    if not normalized:
        return
    with _LOCK:
        _SENSITIVE_API_DOMAINS.update(normalized)


def sensitive_api_domains() -> tuple[str, ...]:
    """Return the built-in and dynamically registered API domains."""
    with _LOCK:
        return tuple(sorted(_SENSITIVE_API_DOMAINS))
