"""Unified auth layer for :class:`VoiceServer` — shared by WebSocket and WebRTC.

This module is the SINGLE owner of the auth policy that both transport paths
reconcile to in M5:

* :class:`AuthPolicy` (Protocol) / :class:`AuthResult` / :class:`NoAuth` /
  :class:`BearerTokenAuth` — the policy types.
* :func:`enforce_bind_guard` — the structured non-loopback-requires-token guard.
  Binding a non-loopback host with no token RAISES :class:`ValueError` unless
  ``unsafe_allow_no_auth=True`` (the ONLY escape hatch). This is the property
  that CLOSES the previously-unauthenticated ``0.0.0.0`` WebSocket voice
  endpoint: the same guard now applies to BOTH WebSocket and WebRTC.

Design notes (deliberate divergences from the plan sketch):

* :meth:`AuthPolicy.authorize` is **synchronous**, NOT ``async``. The auth
  checks it unifies — the WebSocket handshake guard and
  ``WebRTCTransport._request_authorized`` — are both sync and run in sync
  contexts (``process_request`` hooks / sync handler guards). Making
  ``authorize`` async would force an ``await`` into those paths for no benefit
  and would require an event loop where none exists today. The adapters
  (:func:`from_aiohttp_request` / :func:`from_websocket`) likewise do NOT
  require a running loop.
* Tokens are compared with :func:`hmac.compare_digest` (the constant-time
  standard already used across every existing auth surface). Tokens are never
  logged or echoed: the adapters copy ONLY the two credential-bearing fields
  into a small frozen value object and never carry the raw request through.

Import weight: this module imports only ``hmac`` / ``dataclasses`` / ``typing``
/ ``os`` and the leaf ``is_loopback_host`` from :mod:`easycat._net`. It pulls no
aiohttp/websockets/heavy SDK at import time, so ``import easycat.server`` stays
light.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from easycat._net import constant_time_strings_equal, is_loopback_host

# The shipped CLI auth env var. Standardize on ``EASYCAT_SERVE_TOKEN`` (NOT
# ``EASYCAT_SERVER_TOKEN`` — one letter apart, a silent-rename hazard).
EASYCAT_SERVE_TOKEN_ENV = "EASYCAT_SERVE_TOKEN"

AuthReason = Literal["allowed", "missing", "invalid"]


@runtime_checkable
class RequestLike(Protocol):
    """The minimal request shape the auth layer reads.

    It abstracts the two transport request shapes (an aiohttp request with
    ``.headers`` / ``.query`` and a raw-websocket ``Headers`` + ``path``) down
    to the only two credential-bearing fields. Concrete adapters build a frozen
    :class:`_Request` from each shape so nothing carries the raw request object
    into anything that might be echoed.
    """

    @property
    def authorization_header(self) -> str | None:
        """The raw ``Authorization`` header value, or ``None``."""
        ...

    @property
    def query_token(self) -> str | None:
        """The ``?token=`` query value, or ``None``."""
        ...


@dataclass(frozen=True)
class _Request:
    """A frozen credential snapshot — never the raw transport request.

    Only the two credential fields are copied in; the raw object is discarded
    so a token cannot leak through an echoed/dumped request.
    """

    authorization_header: str | None
    query_token: str | None


def from_aiohttp_request(request: object) -> _Request:
    """Adapt an aiohttp-style request (``.headers`` / ``.query``).

    Used by the WebRTC signaling handlers, whose request exposes a mapping-like
    ``.headers`` and ``.query``. Missing attributes degrade to ``None`` so a
    fake/partial request never raises here.
    """
    headers = getattr(request, "headers", None)
    auth = headers.get("Authorization") if headers is not None else None
    query = getattr(request, "query", None)
    token = query.get("token") if query is not None else None
    return _Request(authorization_header=auth, query_token=token)


def from_websocket(headers: Any, path: str) -> _Request:
    """Adapt a raw-websocket request (``Headers`` + handshake ``path``).

    Used by the WebSocket ``process_request`` hook, which receives a
    ``websockets`` ``Headers`` mapping and the handshake ``path`` carrying the
    query string (e.g. ``/voice?token=...``).
    """
    from urllib.parse import parse_qs, urlsplit

    auth = headers.get("Authorization") if headers is not None else None
    token = parse_qs(urlsplit(path).query).get("token", [None])[0]
    return _Request(authorization_header=auth, query_token=token)


@dataclass(frozen=True)
class AuthResult:
    """The outcome of :meth:`AuthPolicy.authorize`.

    ``reason`` distinguishes a missing credential (``"missing"``) from a
    present-but-wrong one (``"invalid"``) so callers can map to the right HTTP
    status, and maps to the future ``easycat.auth_result`` metric label (M8).
    """

    allowed: bool
    reason: AuthReason


_ALLOWED = AuthResult(allowed=True, reason="allowed")
_MISSING = AuthResult(allowed=False, reason="missing")
_INVALID = AuthResult(allowed=False, reason="invalid")


@runtime_checkable
class AuthPolicy(Protocol):
    """A synchronous request authorizer (see module docstring for sync rationale)."""

    def authorize(self, request: RequestLike) -> AuthResult:
        """Return whether ``request`` is authorized."""
        ...


@dataclass
class NoAuth:
    """An open policy that always allows.

    ``unsafe_allow_no_auth`` is the ONLY escape hatch for binding a non-loopback
    host with no token; it defaults ``False`` so the bind guard stays armed even
    with ``NoAuth`` selected.
    """

    unsafe_allow_no_auth: bool = False

    def authorize(self, request: RequestLike) -> AuthResult:
        return _ALLOWED


@dataclass
class BearerTokenAuth:
    """A constant-time bearer-token policy.

    Accepts ``Authorization: Bearer <token>`` always, and a ``?token=`` query
    value ONLY when ``allow_query_token=True`` (default OFF — a deliberate
    breaking change for the browser WS client, which cannot set handshake
    headers; document the loopback/dev opt-in). Comparison is constant-time via
    :func:`hmac.compare_digest`.
    """

    token: str
    allow_query_token: bool = False
    unsafe_allow_no_auth: bool = False

    def authorize(self, request: RequestLike) -> AuthResult:
        # A blank/whitespace token is not a usable secret: never authorize
        # against it. Otherwise ``compare_digest("", "")`` would accept an empty
        # ``Authorization: Bearer `` credential. No-auth is expressed via
        # ``NoAuth`` / ``unsafe_allow_no_auth``, never a blank-token policy.
        if not self.token or not self.token.strip():
            return _MISSING
        header = request.authorization_header
        if header is not None:
            scheme, separator, credential = header.partition(" ")
            if separator == " " and scheme.lower() == "bearer":
                return _ALLOWED if self._token_matches(credential) else _INVALID
        if self.allow_query_token:
            query_token = request.query_token
            if query_token is not None:
                return _ALLOWED if self._token_matches(query_token) else _INVALID
        return _MISSING

    def _token_matches(self, credential: str) -> bool:
        """Constant-time match guarded against non-ASCII on either side.

        ``hmac.compare_digest`` raises ``TypeError`` on a non-ASCII ``str``, so
        guard both inputs: a non-ASCII *credential* (attacker-controlled) can
        never equal an ASCII token, and a non-ASCII *configured token*
        (operator misconfiguration) must deny every request rather than turn
        each authorized-endpoint hit into an HTTP 500 — the auth checks in the
        WebRTC routes are try-less, so an unguarded ``TypeError`` would
        propagate as a 500 DoS / confusing diagnostic instead of a clean 401.
        """
        return constant_time_strings_equal(credential, self.token)


def bearer_auth_from_env(
    env_var: str = EASYCAT_SERVE_TOKEN_ENV,
    *,
    allow_query_token: bool = False,
    unsafe_allow_no_auth: bool = False,
) -> BearerTokenAuth | None:
    """Build a :class:`BearerTokenAuth` from ``EASYCAT_SERVE_TOKEN`` (or another env var).

    Returns ``None`` when the env var is unset/empty so callers can fall back to
    :class:`NoAuth` (subject to the non-loopback bind guard).
    """
    token = os.getenv(env_var)
    if not token:
        return None
    return BearerTokenAuth(
        token=token,
        allow_query_token=allow_query_token,
        unsafe_allow_no_auth=unsafe_allow_no_auth,
    )


def _policy_has_token(auth: AuthPolicy | None) -> bool:
    """Return whether ``auth`` carries a usable token (closes the bind guard)."""
    token = getattr(auth, "token", None)
    return bool(token)


def enforce_bind_guard(
    host: str,
    *,
    auth: AuthPolicy | None,
    unsafe_allow_no_auth: bool = False,
) -> None:
    """Raise unless binding ``host`` is safe — the single structured guard.

    Binding a NON-loopback host with NO token RAISES :class:`ValueError` unless
    ``unsafe_allow_no_auth=True`` (the only escape hatch). A loopback bind, a
    bind with a token-bearing policy, or an explicit ``unsafe_allow_no_auth``
    all pass. Both the WebSocket and WebRTC serve helpers call this so the
    behavior — and the ``0.0.0.0`` gap it closes — is identical across
    transports.

    The escape hatch is honored from BOTH the ``unsafe_allow_no_auth``
    PARAMETER and the same-named field on the policy object (so an
    ``unsafe_allow_no_auth=True`` carried on a :class:`NoAuth` /
    :class:`BearerTokenAuth` is not silently ignored when only the policy is
    passed). The guard stays fail-safe: with neither set it still raises.

    The error wording mirrors the existing WebSocket guard so callers/tests that
    assert on the host + ``unsafe_allow_no_auth`` substrings stay green.
    """
    if is_loopback_host(host):
        return
    if _policy_has_token(auth):
        return
    # Honor the escape hatch from either the parameter OR the policy field; the
    # policy field was previously dead (declared but never consulted).
    if unsafe_allow_no_auth or getattr(auth, "unsafe_allow_no_auth", False):
        return
    raise ValueError(
        f"Refusing to bind {host!r} without a token. Configure an AuthPolicy "
        f"with a token (e.g. {EASYCAT_SERVE_TOKEN_ENV}) when serving beyond "
        "loopback, or pass unsafe_allow_no_auth=True to bind an unauthenticated "
        "endpoint."
    )
