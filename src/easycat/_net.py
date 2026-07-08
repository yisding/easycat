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
        addr = ip_address(normalized)
    except ValueError:
        return False
    # ``IPv6Address.is_loopback`` only unwraps IPv4-mapped addresses
    # (``::ffff:127.0.0.1``) on Python 3.13+; unwrap explicitly so the
    # answer is the same on every supported interpreter.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return mapped.is_loopback
    return addr.is_loopback
