"""Provider-neutral signed one-time stream tokens for telephony media streams."""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from easycat._numeric import is_finite_number

STREAM_TOKEN_PARAMETER = "EasyCatStreamToken"
_STREAM_TOKEN_TIME_SCALE = 1_000_000_000


@dataclass(frozen=True)
class _StreamGrant:
    token: str
    expires_at_ns: int
    claims: tuple[tuple[str, str], ...]


class StreamTokenStore:
    """Issue and consume signed one-time telephony stream tokens.

    The store is intentionally in-memory: the process that mints a token also
    consumes it from the subsequent media ``start`` event. Apps running
    multiple replicas can provide their own validator via their transport
    config.
    """

    def __init__(
        self,
        secret: str | bytes | None = None,
        *,
        ttl_s: float = 300.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not is_finite_number(ttl_s) or ttl_s <= 0:
            raise ValueError("ttl_s must be a finite positive number")
        if secret is None:
            secret = secrets.token_urlsafe(32)
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._ttl_s = float(ttl_s)
        self._now = now
        self._pending: dict[str, _StreamGrant] = {}
        self._idempotent: dict[str, _StreamGrant] = {}

    def issue(
        self,
        *,
        idempotency_key: str | None = None,
        claims: Mapping[str, str] | None = None,
    ) -> str:
        """Return a signed token accepted by exactly one future ``consume``.

        Reusing an idempotency key returns the outstanding token while it is
        still unconsumed. Once the grant has been consumed, a retried webhook
        mints a fresh single-use grant bound to the same claims, so the retry's
        media connection is authorized without reviving the spent token:
        previously consumed tokens are never accepted again.
        """
        self._prune_expired()
        normalized_claims = tuple(
            sorted((str(name), str(value)) for name, value in (claims or {}).items() if value)
        )
        if idempotency_key:
            existing = self._idempotent.get(idempotency_key)
            if existing is not None:
                if existing.claims != normalized_claims:
                    raise ValueError("idempotency_key cannot be reused with different claims")
                nonce = existing.token.split(".", 1)[0]
                if nonce in self._pending:
                    # Still outstanding: retries reuse the same token so the
                    # provider receives one stable authorization per webhook.
                    return existing.token

        nonce = secrets.token_urlsafe(24)
        expires_at_ns = math.ceil((self._now() + self._ttl_s) * _STREAM_TOKEN_TIME_SCALE)
        payload = f"{nonce}.{expires_at_ns}"
        signature = self._signature(payload)
        token = f"{payload}.{signature}"
        grant = _StreamGrant(
            token=token,
            expires_at_ns=expires_at_ns,
            claims=normalized_claims,
        )
        self._pending[nonce] = grant
        if idempotency_key:
            self._idempotent[idempotency_key] = grant
        return token

    def issue_parameter(self) -> dict[str, str]:
        """Return the TwiML ``<Parameter>`` mapping for a fresh token."""
        return {STREAM_TOKEN_PARAMETER: self.issue()}

    def consume(self, token: str) -> bool:
        """Validate and consume a token, returning ``False`` on replay/expiry."""
        return self._consume(token, start=None)

    def consume_start(self, context: StreamTokenContext) -> bool:
        """Consume a token only when its bound webhook claims match ``start``."""
        return self._consume(
            context.token,
            start={
                "callSid": context.call_sid,
                "customParameters": context.parameters,
            },
        )

    def _consume(self, token: str, *, start: Mapping[str, Any] | None) -> bool:
        self._prune_expired()
        parts = token.split(".")
        if len(parts) != 3:
            return False
        nonce, expires_text, signature = parts
        try:
            expires_at_ns = int(expires_text)
        except ValueError:
            return False

        payload = f"{nonce}.{expires_at_ns}"
        try:
            matches_signature = hmac.compare_digest(signature, self._signature(payload))
        except TypeError:
            return False
        if not matches_signature:
            return False
        now_ns = math.floor(self._now() * _STREAM_TOKEN_TIME_SCALE)
        if expires_at_ns < now_ns:
            self._pending.pop(nonce, None)
            return False
        grant = self._pending.pop(nonce, None)
        if grant is None or grant.expires_at_ns != expires_at_ns:
            return False
        return _grant_claims_match(grant.claims, start)

    def _signature(self, payload: str) -> str:
        digest = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _prune_expired(self) -> None:
        now_ns = math.floor(self._now() * _STREAM_TOKEN_TIME_SCALE)
        expired = [nonce for nonce, grant in self._pending.items() if grant.expires_at_ns < now_ns]
        for nonce in expired:
            self._pending.pop(nonce, None)
        expired_keys = [
            key for key, grant in self._idempotent.items() if grant.expires_at_ns < now_ns
        ]
        for key in expired_keys:
            self._idempotent.pop(key, None)


def _grant_claims_match(
    claims: tuple[tuple[str, str], ...],
    start: Mapping[str, Any] | None,
) -> bool:
    if not claims:
        return True
    if start is None:
        return False
    custom_parameters = start.get("customParameters")
    params = custom_parameters if isinstance(custom_parameters, Mapping) else {}
    for name, expected in claims:
        actual = start.get("callSid") if name == "CallSid" else params.get(name)
        if not isinstance(actual, (str, int)) or isinstance(actual, bool):
            return False
        if str(actual) != expected:
            return False
    return True


@dataclass(frozen=True, slots=True)
class StreamTokenContext:
    """Stream-token validation context from a provider's ``start`` frame."""

    token: str
    call_sid: str | None
    stream_sid: str | None
    parameters: Mapping[str, str]


__all__ = [
    "STREAM_TOKEN_PARAMETER",
    "StreamTokenContext",
    "StreamTokenStore",
]
