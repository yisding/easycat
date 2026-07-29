"""Leaf networking helpers with zero intra-package imports.

These helpers are shared by the transports, the CLI serve pre-flight, the
server auth guard, and the debugger origin guard. Keeping them here —
depending only on the stdlib — lets every one of those callers import downward
without the import cycles that previously forced lazy imports out of
:mod:`easycat.transports.webrtc`.
"""

from __future__ import annotations

from hmac import compare_digest
from ipaddress import ip_address

__all__ = ["constant_time_strings_equal", "is_loopback_host", "normalize_auth_token"]


def constant_time_strings_equal(candidate: str, expected: str) -> bool:
    """Compare untrusted strings in constant time, denying non-ASCII inputs.

    :func:`hmac.compare_digest` raises :class:`TypeError` for non-ASCII
    strings. Authentication boundaries should treat either a hostile
    credential or a misconfigured expected token as a clean mismatch rather
    than leaking that exception into an HTTP/WebSocket handler.
    """
    return candidate.isascii() and expected.isascii() and compare_digest(candidate, expected)


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
