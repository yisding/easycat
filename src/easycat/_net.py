"""Leaf networking helpers with zero intra-package imports.

Both ``is_loopback_host`` and ``normalize_auth_token`` are shared by the
transports, the CLI serve pre-flight, the server auth guard, and the debugger
origin guard. Keeping them here — depending only on the stdlib — lets every one
of those callers import downward without the import cycles that previously
forced lazy imports out of :mod:`easycat.transports.webrtc`.
"""

from __future__ import annotations

from ipaddress import ip_address

__all__ = ["is_loopback_host", "normalize_auth_token"]


def normalize_auth_token(token: str | None) -> str | None:
    """Treat blank or whitespace-only tokens as no token at all.

    An empty string is not a usable secret: ``Authorization: Bearer `` would
    otherwise pass ``compare_digest("", "")``. Normalizing blank tokens to
    ``None`` keeps the public-bind guard and request authorization in sync.
    """
    if token is None or not token.strip():
        return None
    return token


def is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False
